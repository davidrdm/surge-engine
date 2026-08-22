"""Live API smoke tests. Opt-in, bounded, and skipped by default.

    pytest tests/test_live_smoke.py -m live -v

These are the only tests that touch the network, and they exist to answer
questions the fixtures cannot:

  * Are the query PARAMETERS right? FR24's sandbox ignores every query
    parameter, so a malformed `airports` or `categories` value returns the same
    canned payload as a correct one. Only a live call distinguishes them.
  * Does Priceline's `vehicles[]` really have the shape we captured? The OpenAPI
    spec declares the array untyped, so the fixture is evidence of one response,
    not a contract.
  * Do the five documented unknowns resolve? (§14.5 of the plan.)

Cost is a few tens of API calls, well under a dollar in total. Each response is
written to tests/fixtures/live/ with credentials scrubbed, so the fixtures can
be promoted and the cost is not repeated.

Nothing here prints a response body wholesale — a live payload could contain an
echoed credential, and test output is read by both humans and assistants.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from surge_iw.connectors import priceline as pl
from surge_iw.connectors.apidirect import EP_NEWS, EP_TWITTER, APIDirectConnector
from surge_iw.connectors.flightradar import FlightRadarConnector
from surge_iw.connectors.priceline import PricelineConnector
from surge_iw.connectors.staying import StayingConnector
from surge_iw.services.ratelimit import RateLimiter
from surge_iw.services.redact import default_redactor, redact_payload

pytestmark = pytest.mark.live

CAPTURE_DIR = Path(__file__).parent / "fixtures" / "live"


def capture(name: str, payload: Any) -> None:
    """Write a scrubbed response for promotion into the fixture set.

    The sink is mission-neutral; what lands in it is not. A flight or lodging
    payload is a fact about a vendor's API, but a social capture is a fact
    about the loaded mission's LEXICON — it is whatever those search terms
    returned. Keep the first here; file the second with the pack whose terms
    produced it, or the engine's fixtures accumulate one mission's subject
    matter without anybody deciding that they should.

    The social captures are gitignored for exactly that reason: a live run
    re-creates them locally and cannot commit them. Measured — a sweep for a
    benign term still came back carrying real articles about whatever the
    world was arguing about that day.
    """
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    (CAPTURE_DIR / f"{name}.json").write_text(
        json.dumps(redact_payload(payload), indent=1, default=str)
    )


def env_or_skip(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} not set; run: source scripts/load-secrets.sh")
    default_redactor().register(value)
    return value


@pytest.fixture(scope="module")
def anchor() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def fr24_limiter() -> RateLimiter:
    """One limiter shared by every FR24 connector in this module.

    Per-test connectors each got their own bucket, so ten tests could each
    burst independently and collectively breach the account limit. That
    produced a real 429 on the first live run — a useful reminder that a
    limiter is only a limiter if it is shared.
    """
    return RateLimiter(per_minute=10)


# ===========================================================================
# API Direct — no sandbox exists, so these are live and cost a few cents
# ===========================================================================


class TestAPIDirectLive:
    @pytest.fixture
    def connector(self):
        with APIDirectConnector(env_or_skip("APIDIRECT_API_KEY")) as c:
            yield c

    def test_news_path_and_recency_filter(self, connector):
        """Confirms /v1/news/articles exists and time_published is honoured.

        The old connector used /v1/news, which does not exist, and swallowed the
        404 into an empty list — so news collection silently never worked.
        """
        articles = connector.search(
            EP_NEWS,
            {"query": "air show", "limit": 10, "time_published": "1d"},
        )
        capture("apidirect_news_live", articles)
        assert isinstance(articles, list)
        for article in articles:
            assert article["url"]
            # The field the 48-hour correlation window depends on.
            assert article["observed_at"], "news article has no timestamp"

    def test_twitter_shape(self, connector):
        posts = connector.search(
            EP_TWITTER, {"query": "air show", "pages": 1,
                         "sort_by": "most_recent"},
        )
        capture("apidirect_twitter_live", posts)
        for post in posts:
            assert post["source_domain"], "post has no domain for corroboration"


# ===========================================================================
# FlightRadar24 — sandbox proves parsing; live proves the parameters
# ===========================================================================


class TestFlightRadarSandbox:
    """Free and unlimited, but IGNORES ALL QUERY PARAMETERS."""

    @pytest.fixture
    def connector(self, fr24_limiter):
        with FlightRadarConnector(env_or_skip("FR24_SANDBOX_KEY"),
                                  limiter=fr24_limiter) as c:
            yield c

    def test_sandbox_parses_identically_to_production(self, connector):
        flights = connector.live_positions({"airports": "inbound:PHX",
                                            "categories": "M,J"})
        capture("fr24_live_sandbox", flights)
        for flight in flights:
            # The invariant that must hold regardless of environment.
            assert flight["flight_category"] == "AMBIGUOUS"
            assert flight["category_confidence"] == "AMBIGUOUS"

    def test_sandbox_key_is_rejected_by_production_semantics(self, connector):
        """Documented FR24 behaviour worth pinning: a sandbox token against a
        real endpoint is refused rather than silently serving canned data."""
        # Recorded rather than asserted, because which endpoints are sandboxed
        # is a provider decision that may change.
        try:
            usage = connector.health_check()
            capture("fr24_usage_sandbox", usage)
        except Exception as exc:  # noqa: BLE001
            capture("fr24_usage_sandbox_error", {"error": str(exc)})


class TestFlightRadarLive:
    """Costs credits. Only these calls can prove a filter is correct."""

    @pytest.fixture
    def connector(self, fr24_limiter):
        with FlightRadarConnector(env_or_skip("FR24_API_KEY"),
                                  limiter=fr24_limiter) as c:
            yield c

    def test_count_endpoints_are_not_available_on_this_tier(self, connector):
        """The finding that killed the tripwire design.

        /count would have cost ~15% of /full and answered "is there any
        qualifying traffic at all" for about one credit. Both /count
        endpoints return 403 on this subscription, so collection now goes
        straight to /full with a `limit` cap instead. Asserted rather than
        skipped so that a tier upgrade shows up as a failing test.
        """
        from surge_iw.base.connector import AuthError
        with pytest.raises(AuthError) as exc:
            connector.count_live(
                {"airports": "inbound:PHX", "categories": "M,J"}
            )
        capture("fr24_count_denied", {"detail": str(exc.value)})
        assert exc.value.status_code == 403

    def test_airports_filter_actually_filters(self, connector):
        """The parameter the sandbox cannot verify.

        Every returned flight must be bound for the requested airport. If the
        `inbound:` syntax were wrong, FR24 would return unrelated traffic and the
        whole flight signal would be attributed to the wrong city.
        """
        flights = connector.live_positions(
            {"airports": "inbound:LAX", "categories": "M,J", "limit": 20}
        )
        capture("fr24_live_lax", flights)
        for flight in flights:
            assert flight["dest_iata"] in ("LAX", "KLAX"), (
                f"expected LAX-bound traffic, got dest {flight['dest_iata']!r} "
                "— the airports filter syntax is wrong"
            )

    def test_summary_returns_a_category_and_it_normalises(self, connector):
        """flight-summary is the ONLY endpoint that returns a category, which is
        what makes a live record's category resolvable at all."""
        now = datetime.now(timezone.utc)
        flights = connector.flight_summary({
            "airports": "inbound:LAX",
            "categories": "M,J",
            "flight_datetime_from": (now - timedelta(hours=24)).strftime(
                "%Y-%m-%dT%H:%M:%S"),
            "flight_datetime_to": now.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        capture("fr24_summary_live", flights)
        confirmed = [f for f in flights if f["category_confidence"] == "CONFIRMED"]
        if flights:
            assert confirmed, (
                "no flight-summary record produced a recognised category; "
                "normalise_category may need another spelling"
            )

    def test_usage_endpoint_reports_the_tier(self, connector):
        """Determines the real rate limit: 30 q/min on Essential, 90 on
        Advanced. Currently assumed rather than read."""
        health = connector.health_check()
        capture("fr24_usage_live", health)
        assert health["healthy"] is True


# ===========================================================================
# Staying — /account is free even in production
# ===========================================================================


class TestStayingLive:
    @pytest.fixture
    def connector(self):
        with StayingConnector(env_or_skip("STAYING_API_KEY")) as c:
            yield c

    def test_account_reports_environment_and_credits(self, connector):
        account = connector.account()
        capture("staying_account_live", account)
        assert isinstance(account, dict)
        credits = connector.credits_available()
        # Logged as a boolean, never as the balance itself in the assertion text.
        assert credits is None or credits >= 0

    def test_search_then_availability_round_trip(self, connector):
        """The two-stage method end to end, on a real location.

        Skipped rather than failed when the account cannot afford it: a credit
        balance is an environmental precondition, like an unset key, and the 402
        path itself is covered by mocks in test_connectors.py. A /search was
        measured at 36 credits.
        """
        from surge_iw.base.connector import PaymentRequiredError

        account = connector.account()
        plan = (account.get("plan") or {}).get("code", "")
        credits = connector.credits_available()
        try:
            # Constrained to airbnb, exactly as CollectionAgent does. An
            # unconstrained search now raises when any leg is skipped, and a
            # date-less Google leg is always skipped.
            search = connector.search_listings({
                "location": "Phoenix, AZ", "limit": 5, "platforms": "airbnb",
            })
        except PaymentRequiredError as exc:
            # Reported as an account state, not a code defect.
            pytest.skip(
                f"Staying plan {plan!r} refused the request "
                f"(balance reads {credits}). The two-stage lodging method needs "
                f"a paid plan. Provider said: {exc}"
            )
        capture("staying_search_live", search.records)
        capture("staying_search_meta_live", search.meta)

        # The metadata the connector now reads rather than discards.
        assert search.credits_charged is not None, (
            "meta.creditsCharged absent — the ledger would fall back to the "
            "flat one-unit charge and under-count Staying badly"
        )
        for platform_name, reason in search.not_searched().items():
            print(f"  leg not searched: {platform_name} ({reason})")

        if not search.records:
            pytest.skip("no listings returned for the test location")

        platform = search.records[0]["platform"]
        ids = ",".join(
            l["listing_id"] for l in search.records if l["platform"] == platform
        )
        start = datetime.now(timezone.utc).date() + timedelta(days=2)
        avail = connector.availability({
            "platform": platform,
            "listingIds": ids,
            "startDate": start.isoformat(),
            "endDate": (start + timedelta(days=2)).isoformat(),
        })
        capture("staying_availability_live", avail.records)
        for row in avail.records:
            # The per-date booleans are the whole reason for choosing this over
            # counting search results.
            assert row["nights_offered"] >= 0
            assert row["nights_available"] <= row["nights_offered"]

    def test_a_dateless_multiplatform_search_reports_the_skipped_google_leg(
        self, connector
    ):
        """The vendor notice, asserted directly.

        Their API used to normalise the Google validation failure into an
        "ok / 0 results" leg, which is indistinguishable from a real search that
        found nothing. Their fix reports it as `skipped` with a `requires_dates`
        reason. This asserts the fix is live AND that we now refuse to treat the
        shortfall as an absence of listings.
        """
        from surge_iw.base.connector import PlatformUnavailableError

        with pytest.raises(PlatformUnavailableError, match="requires_dates"):
            connector.search_listings({"location": "Phoenix, AZ", "limit": 3})


# ===========================================================================
# Priceline — mandatory, because vehicles[] has no published schema
# ===========================================================================


class TestPricelineLive:
    @pytest.fixture
    def connector(self):
        with PricelineConnector(env_or_skip("PRICELINE_RAPIDAPI_KEY")) as c:
            yield c

    @pytest.fixture
    def search_params(self) -> dict[str, Any]:
        """The six parameters priceline-com2 requires, and no others (8.5).

        camelCase, and no `currency` — the previous wrapper accepted one and
        this endpoint declares six required parameters with no optional ones.
        The response carries `rate[].currencyCode` instead.
        """
        pickup = datetime.now(timezone.utc).date() + timedelta(days=2)
        dropoff = pickup + timedelta(days=2)
        return {
            "pickUpLocation": "PHX",
            "dropOffLocation": "PHX",
            "pickUpDate": pickup.isoformat(),
            "pickUpTime": "12:00",
            "dropOffDate": dropoff.isoformat(),
            "dropOffTime": "12:00",
        }

    def test_rapidapi_key_header_name_is_correct(self, connector, search_params):
        """Unknown #1: x-rapidapi-key is the platform standard but was not
        literally present in the fetched listing HTML. A 401 here means the
        header name is wrong."""
        result = connector.search_rental_cars(search_params)
        assert result["total_results_available"] >= 0

    def test_total_results_available_is_populated(self, connector, search_params):
        """Unknown #2: the availability count.

        The entire car signal rests on it, and this is the check that would
        have caught the 8.5 outage — the previous wrapper answered
        `success: true` with a count of zero for every airport and window.
        On this wrapper the count is the number of DISTINCT offers parsed, so a
        zero here means the vendor genuinely returned nothing.
        """
        result = connector.search_rental_cars(search_params)
        capture("priceline_cars_live_summary", {
            "total_results_available": result["total_results_available"],
            "results_count": result["results_count"],
            "truncated": result["truncated"],
            "skipped": result["skipped"],
            "offer_count": len(result["offers"]),
        })
        assert result["total_results_available"] > 0, (
            "totalResultsAvailable is zero or absent on a major airport — "
            "the car availability signal needs rethinking"
        )

    def test_captured_vehicle_shape_still_holds(self, connector, search_params):
        """Unknown #3: the 40-field shape came from one captured response, and
        the spec declares the array untyped. Zero skipped offers means the
        mapping still matches."""
        result = connector.search_rental_cars(search_params)
        capture("priceline_cars_live", result["offers"][:5])
        assert result["skipped"] == 0, (
            f"{result['skipped']} offers failed to normalise — the vendor "
            f"schema has drifted from FIELD_MAP_VERSION {pl.FIELD_MAP_VERSION}"
        )

    def test_people_capacity_is_present(self, connector, search_params):
        """Unknown #4: capacity weighting is the main analytic gain from this
        provider. Without it the car signal is just a count."""
        result = connector.search_rental_cars(search_params)
        with_capacity = [o for o in result["offers"]
                         if o["people_capacity"] is not None]
        assert with_capacity, "no offer reported peopleCapacity"

    def test_whether_an_undocumented_limit_parameter_exists(
        self, connector, search_params
    ):
        """Unknown #5: whether the page size is controllable.

        priceline-com2 declares six required parameters and no optional ones,
        and paginates through `meta`. Recorded rather than asserted — the
        answer decides whether truncation is something we can steer or only
        detect.
        """
        baseline = connector.search_rental_cars(search_params)
        limited = connector.search_rental_cars({**search_params, "limit": 3})
        capture("priceline_limit_probe", {
            "without_limit_results_count": baseline["results_count"],
            "with_limit_3_results_count": limited["results_count"],
            "limit_appears_honoured":
                limited["results_count"] < baseline["results_count"],
        })

    def test_one_car_at_several_prices_counts_once(self, connector,
                                                   search_params):
        """Unknown #6, now ANSWERED and therefore asserted.

        The question was whether counting offers should count vehicles or
        rates. It is vehicles: the vendor lists the same car once per rate
        plan, with an identical `id` and a differing `rate`. Counting rows
        made a supplier's extra price point look like extra inventory, and a
        trimmed price list look like scarcity — on the one family whose whole
        job is measuring scarcity.

        Asserted live because it is a property of the vendor's data, not of
        our parsing: if rows ever stop sharing an id per rate plan, the
        de-duplication silently starts discarding real offers instead.
        """
        result = connector.search_rental_cars(search_params)
        assert result["offers"], "no offers returned"
        keys = [o["offer_key"] for o in result["offers"]]
        capture("priceline_rate_cardinality", {
            "distinct_offers": len(result["offers"]),
            "distinct_offer_keys": len(set(keys)),
            "total_reported": result["total_results_available"],
        })
        # Every parsed offer must be a distinct vendor id — that is what the
        # de-duplication guarantees, and what the counts then mean.
        assert len(result["offers"]) == result["results_count"]

    @pytest.mark.xfail(
        reason="Autocomplete was a vendor-side 500 on the previous wrapper and "
               "is not on the critical path on this one either: "
               "city_to_pickup_location() supplies an airport IATA code, which "
               "the car search endpoint accepts directly and echoes back as "
               "pickupLocation.airportCode. Kept as a probe, not a gate.",
        strict=False,
    )
    def test_autocomplete_resolves_a_city_to_an_airport(self, connector):
        results = connector.autocomplete_location("Phoenix")
        capture("priceline_autocomplete_live", results)
        airports = [r for r in results if r["type"] == "AIRPORT"]
        assert airports, "autocomplete returned no AIRPORT entries"
