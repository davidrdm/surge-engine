"""Connector behaviour, mocked with respx. No live network.

The gate for this phase is not "does it parse" — it is "does it refuse to lie".
The old connectors caught every exception and returned `[]`, which turned an
expired token into "no military flights inbound". TestFailsLoud is therefore the
most important class here: every connector must raise on every error status, for
every endpoint, with a typed exception the operator can act on.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from surge_iw.base.connector import (
    AuthError,
    ConnectorError,
    PaymentRequiredError,
    RateLimitError,
    SchemaError,
    TransportError,
    UpstreamError,
)
from surge_iw.connectors import flightradar as fr
from surge_iw.connectors import priceline as pl
from surge_iw.connectors import staying as st
from surge_iw.connectors.apidirect import EP_NEWS, EP_TWITTER, APIDirectConnector
from surge_iw.connectors.flightradar import FlightRadarConnector
from surge_iw.connectors.priceline import PricelineConnector
from surge_iw.connectors.staying import StayingConnector

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


APIDIRECT_URL = "https://apidirect.io"
FR24_URL = "https://fr24api.flightradar24.com"
# The /v1 is required; the bare host 404s. Verified live.
STAYING_URL = "https://api.stayingapi.com/v1"
PRICELINE_URL = "https://priceline-com2.p.rapidapi.com"


@pytest.fixture
def calls() -> list[dict]:
    """Captures every on_call invocation, standing in for BudgetGuard.record."""
    return []


@pytest.fixture
def recorder(calls):
    def _record(**kwargs):
        calls.append(kwargs)
    return _record


@pytest.fixture
def apidirect(recorder):
    return APIDirectConnector("test-key", on_call=recorder, sleep=lambda s: None)


@pytest.fixture
def fr24(recorder):
    return FlightRadarConnector("test-key", on_call=recorder, sleep=lambda s: None)


@pytest.fixture
def staying(recorder):
    return StayingConnector("stay_test_key", on_call=recorder, sleep=lambda s: None)


@pytest.fixture
def priceline(recorder):
    return PricelineConnector("test-key", on_call=recorder, sleep=lambda s: None)


# ===========================================================================
# The gate: no connector may ever convert an error into an empty result
# ===========================================================================


class TestFailsLoud:
    """Every error status raises a typed exception on every connector.

    Parameterised across all four providers because the old bug was a per-file
    `except Exception: return []`, and the only way to be sure it has not been
    reintroduced anywhere is to assert it nowhere.
    """

    CASES = [
        (401, AuthError), (403, AuthError), (402, PaymentRequiredError),
        (429, RateLimitError), (500, UpstreamError), (503, UpstreamError),
        (400, ConnectorError),
    ]

    @pytest.mark.parametrize("status,expected", CASES)
    @respx.mock
    def test_apidirect(self, apidirect, status, expected):
        respx.get(f"{APIDIRECT_URL}{EP_TWITTER}").mock(
            return_value=httpx.Response(status, json={"message": "nope"})
        )
        with pytest.raises(expected):
            apidirect.search(EP_TWITTER, {"query": "phoenix"})

    @pytest.mark.parametrize("status,expected", CASES)
    @respx.mock
    def test_flightradar(self, fr24, status, expected):
        respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            return_value=httpx.Response(status, json={"message": "nope"})
        )
        with pytest.raises(expected):
            fr24.live_positions({"airports": "inbound:PHX"})

    @pytest.mark.parametrize("status,expected", CASES)
    @respx.mock
    def test_staying(self, staying, status, expected):
        respx.get(f"{STAYING_URL}{st.EP_SEARCH}").mock(
            return_value=httpx.Response(status, json={"message": "nope"})
        )
        with pytest.raises(expected):
            staying.search_listings({"location": "Phoenix"})

    @pytest.mark.parametrize("status,expected", CASES)
    @respx.mock
    def test_priceline(self, priceline, status, expected):
        respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            return_value=httpx.Response(status, json={"message": "nope"})
        )
        with pytest.raises(expected):
            priceline.search_rental_cars({"pickUpLocation": "PHX"})

    @respx.mock
    def test_network_failure_raises_rather_than_returning_empty(self, fr24):
        respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            side_effect=httpx.ConnectError("dns failure")
        )
        with pytest.raises(TransportError):
            fr24.live_positions({"airports": "inbound:PHX"})

    @respx.mock
    def test_non_json_body_raises_schema_error(self, fr24):
        respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            return_value=httpx.Response(200, text="<html>maintenance</html>")
        )
        with pytest.raises(SchemaError):
            fr24.live_positions({"airports": "inbound:PHX"})

    @respx.mock
    def test_a_genuine_empty_result_is_not_an_error(self, fr24):
        """The distinction the whole design rests on: zero results from a
        working endpoint is real evidence of absence, and must succeed."""
        respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            return_value=httpx.Response(200, json=fixture("fr24_live_positions_empty.json"))
        )
        assert fr24.live_positions({"airports": "inbound:PHX"}) == []

    def test_missing_api_key_fails_at_construction(self):
        """Rather than at request time, where it would look like empty data."""
        with pytest.raises(ConnectorError):
            FlightRadarConnector("")

    @respx.mock
    def test_error_detail_is_scrubbed_of_credentials(self):
        """Providers sometimes echo the submitted key back in an error body."""
        from surge_iw.services.redact import default_redactor
        secret = "sk_live_abcdef1234567890abcdef"
        default_redactor().register(secret)
        connector = APIDirectConnector(secret, sleep=lambda s: None)
        respx.get(f"{APIDIRECT_URL}{EP_TWITTER}").mock(
            return_value=httpx.Response(401, json={"message": f"bad key {secret}"})
        )
        with pytest.raises(AuthError) as exc:
            connector.search(EP_TWITTER, {"query": "x"})
        assert secret not in str(exc.value)


class TestRetryPolicy:
    @respx.mock
    def test_transient_5xx_is_retried_then_succeeds(self, fr24, calls):
        route = respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            side_effect=[
                httpx.Response(503, json={"message": "unavailable"}),
                httpx.Response(200, json=fixture("fr24_live_positions.json")),
            ]
        )
        assert len(fr24.live_positions({"airports": "inbound:PHX"})) == 3
        assert route.call_count == 2
        # Both attempts are recorded: a failed call still consumed a request.
        assert len(calls) == 2

    @respx.mock
    def test_429_is_not_retried(self, fr24):
        """A rate-limit breach means local pacing is misconfigured. Retrying
        would hide that while still losing the collection window."""
        route = respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "30"},
                                        json={"message": "slow down"})
        )
        with pytest.raises(RateLimitError) as exc:
            fr24.live_positions({"airports": "inbound:PHX"})
        assert route.call_count == 1
        assert exc.value.retry_after == 30.0

    @respx.mock
    def test_401_is_not_retried(self, fr24):
        route = respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            return_value=httpx.Response(401, json={"message": "bad token"})
        )
        with pytest.raises(AuthError):
            fr24.live_positions({"airports": "inbound:PHX"})
        assert route.call_count == 1


# ===========================================================================
# API Direct
# ===========================================================================


class TestAPIDirect:
    @respx.mock
    def test_twitter_posts_normalise(self, apidirect):
        respx.get(f"{APIDIRECT_URL}{EP_TWITTER}").mock(
            return_value=httpx.Response(200, json=fixture("apidirect_twitter.json"))
        )
        posts = apidirect.search(EP_TWITTER, {"query": "phoenix", "pages": 2})
        assert len(posts) == 2
        assert posts[0]["source_domain"] == "x.com"
        assert posts[0]["observed_at"] == "2026-07-27T10:12:00Z"
        assert posts[0]["author"] == "azreporter"
        assert posts[0]["engagement"]["likes"] == 840

    @respx.mock
    def test_news_articles_use_different_field_names(self, apidirect):
        """News carries published_datetime_utc and authors[], not date/author.

        The old connector applied one shape to every endpoint, so news articles
        arrived with no usable timestamp — which silently excluded them from the
        48-hour correlation window.
        """
        respx.get(f"{APIDIRECT_URL}{EP_NEWS}").mock(
            return_value=httpx.Response(200, json=fixture("apidirect_news.json"))
        )
        articles = apidirect.search(EP_NEWS, {"query": "phoenix", "limit": 50})
        assert articles[0]["observed_at"] == "2026-07-27T09:05:00Z"
        assert articles[0]["author"] == "Jane Doe"
        assert articles[0]["source_domain"] == "apnews.com"

    @respx.mock
    def test_news_path_is_v1_news_articles(self, apidirect):
        """The old connector used /v1/news, which does not exist."""
        route = respx.get(f"{APIDIRECT_URL}/v1/news/articles").mock(
            return_value=httpx.Response(200, json=fixture("apidirect_news.json"))
        )
        apidirect.search(EP_NEWS, {"query": "x"})
        assert route.called

    @respx.mock
    def test_key_travels_in_a_header_not_a_query_string(self, apidirect):
        route = respx.get(f"{APIDIRECT_URL}{EP_TWITTER}").mock(
            return_value=httpx.Response(200, json=fixture("apidirect_empty.json"))
        )
        apidirect.search(EP_TWITTER, {"query": "x"})
        request = route.calls[0].request
        assert request.headers["X-API-Key"] == "test-key"
        assert "test-key" not in str(request.url)

    @respx.mock
    def test_empty_result_set_is_returned_not_raised(self, apidirect):
        respx.get(f"{APIDIRECT_URL}{EP_TWITTER}").mock(
            return_value=httpx.Response(200, json=fixture("apidirect_empty.json"))
        )
        assert apidirect.search(EP_TWITTER, {"query": "x"}) == []

    @respx.mock
    def test_unexpected_envelope_raises_rather_than_reading_as_empty(self, apidirect):
        respx.get(f"{APIDIRECT_URL}{EP_TWITTER}").mock(
            return_value=httpx.Response(200, json={"items": [{"url": "x"}], "count": 1})
        )
        with pytest.raises(SchemaError):
            apidirect.search(EP_TWITTER, {"query": "x"})

    def test_unknown_endpoint_is_rejected(self, apidirect):
        with pytest.raises(SchemaError):
            apidirect.search("/v1/tiktok/posts", {"query": "x"})


# ===========================================================================
# FlightRadar24
# ===========================================================================


class TestFlightRadar:
    @respx.mock
    def test_required_headers_are_sent(self, fr24):
        route = respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            return_value=httpx.Response(200, json=fixture("fr24_live_positions.json"))
        )
        fr24.live_positions({"airports": "inbound:PHX"})
        headers = route.calls[0].request.headers
        assert headers["Authorization"] == "Bearer test-key"
        # Required by the spec, not optional.
        assert headers["Accept-Version"] == "v1"

    @respx.mock
    def test_live_positions_are_always_ambiguous_on_category(self, fr24):
        """FlightPositionsFull returns 22 fields and category is not one of them.

        The old code labelled these "M/J" and let military-weighted rules act on
        them, which is the difference between reporting a fact and inventing one.
        """
        respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            return_value=httpx.Response(200, json=fixture("fr24_live_positions.json"))
        )
        flights = fr24.live_positions({"airports": "inbound:PHX"})
        assert len(flights) == 3
        assert all(f["flight_category"] == "AMBIGUOUS" for f in flights)
        assert all(f["category_confidence"] == "AMBIGUOUS" for f in flights)

    @respx.mock
    def test_live_positions_preserve_eta_and_status(self, fr24):
        """The fields the old pipeline collected and then dropped at parse time.
        An ETA is what makes a warning tactical rather than descriptive."""
        respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            return_value=httpx.Response(200, json=fixture("fr24_live_positions.json"))
        )
        flights = fr24.live_positions({"airports": "inbound:PHX"})
        assert flights[0]["eta"] == "2026-07-27T11:52:00Z"
        assert flights[0]["flight_status"] == "airborne_inbound"
        assert flights[0]["registration"] == "04-4128"

    @respx.mock
    def test_flight_summary_carries_a_confirmed_category(self, fr24):
        respx.get(f"{FR24_URL}{fr.EP_SUMMARY_FULL}").mock(
            return_value=httpx.Response(200, json=fixture("fr24_flight_summary.json"))
        )
        flights = fr24.flight_summary({"airports": "inbound:PHX"})
        by_id = {f["fr24_id"]: f for f in flights}
        assert by_id["39bf1c58"]["flight_category"] == "M"
        assert by_id["39bf1c58"]["category_confidence"] == "CONFIRMED"
        assert by_id["39bf3390"]["flight_category"] == "J"

    @respx.mock
    def test_null_category_in_summary_stays_ambiguous(self, fr24):
        """A null category is unknown, not benign — and not military either."""
        respx.get(f"{FR24_URL}{fr.EP_SUMMARY_FULL}").mock(
            return_value=httpx.Response(200, json=fixture("fr24_flight_summary.json"))
        )
        flights = fr24.flight_summary({"airports": "inbound:PHX"})
        unknown = next(f for f in flights if f["fr24_id"] == "39bf9999")
        assert unknown["flight_category"] == "AMBIGUOUS"
        assert unknown["category_confidence"] == "AMBIGUOUS"

    @respx.mock
    def test_count_tripwire_returns_an_integer(self, fr24, calls):
        respx.get(f"{FR24_URL}{fr.EP_LIVE_COUNT}").mock(
            return_value=httpx.Response(200, json=fixture("fr24_live_count.json"))
        )
        assert fr24.count_live({"airports": "inbound:PHX"}) == 3
        # Billed as one flat unit, not as three records.
        assert calls[0]["records_returned"] == 1

    @respx.mock
    def test_full_positions_are_billed_per_record(self, fr24, calls):
        """FR24 bills per record returned, so accounting must count records."""
        respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            return_value=httpx.Response(200, json=fixture("fr24_live_positions.json"))
        )
        fr24.live_positions({"airports": "inbound:PHX"})
        assert calls[0]["records_returned"] == 3

    @pytest.mark.parametrize(
        "raw,expected",
        [("M", "M"), ("m", "M"), ("MILITARY_AND_GOVERNMENT", "M"),
         ("Military", "M"), ("Business Jets", "J"), ("J", "J"),
         ("General Aviation", "T"), ("Helicopters", "H"),
         (None, None), ("", None), ("nonsense", None)],
    )
    def test_category_normalisation(self, raw, expected):
        assert fr.normalise_category(raw) == expected

    def test_category_letters_match_the_spec_not_the_mcp_readme(self):
        """The vendor's own MCP server README transposes T/H/B/G. The OpenAPI
        specification is authoritative and says T is general aviation."""
        assert fr.CATEGORY_CODES["T"] == "GENERAL_AVIATION"
        assert fr.CATEGORY_CODES["H"] == "HELICOPTERS"
        assert fr.CATEGORY_CODES["M"] == "MILITARY_AND_GOVERNMENT"
        assert fr.CATEGORY_CODES["J"] == "BUSINESS_JETS"

    def test_resolve_categories_upgrades_matching_live_records(self):
        """The historical query is what makes a live record's category legible."""
        live = [
            {"fr24_id": "39bf1c58", "registration": "04-4128", "callsign": "RCH285",
             "flight_category": "AMBIGUOUS", "category_confidence": "AMBIGUOUS"},
            {"fr24_id": "unmatched", "registration": "N999ZZ", "callsign": "XX1",
             "flight_category": "AMBIGUOUS", "category_confidence": "AMBIGUOUS"},
        ]
        summary = [
            {"fr24_id": "39bf1c58", "registration": "04-4128", "callsign": "RCH285",
             "flight_category": "M", "category_confidence": "CONFIRMED"},
        ]
        resolved = fr.resolve_categories(live, summary)
        assert resolved[0]["flight_category"] == "M"
        assert resolved[0]["category_confidence"] == "CONFIRMED"
        # No match means no guess.
        assert resolved[1]["flight_category"] == "AMBIGUOUS"

    def test_resolve_categories_matches_on_registration(self):
        live = [{"fr24_id": "different", "registration": "04-4128",
                 "callsign": "", "flight_category": "AMBIGUOUS",
                 "category_confidence": "AMBIGUOUS"}]
        summary = [{"fr24_id": "39bf1c58", "registration": "04-4128",
                    "callsign": "", "flight_category": "M",
                    "category_confidence": "CONFIRMED"}]
        assert fr.resolve_categories(live, summary)[0]["flight_category"] == "M"

    def test_resolve_categories_ignores_unconfirmed_summary_rows(self):
        """An AMBIGUOUS summary row cannot confirm anything."""
        live = [{"fr24_id": "a", "registration": "", "callsign": "",
                 "flight_category": "AMBIGUOUS", "category_confidence": "AMBIGUOUS"}]
        summary = [{"fr24_id": "a", "registration": "", "callsign": "",
                    "flight_category": "AMBIGUOUS",
                    "category_confidence": "AMBIGUOUS"}]
        assert fr.resolve_categories(live, summary)[0]["category_confidence"] == "AMBIGUOUS"


# ===========================================================================
# Staying
# ===========================================================================


class TestStaying:
    @respx.mock
    def test_account_costs_nothing_and_reports_environment(self, staying, calls):
        respx.get(f"{STAYING_URL}{st.EP_ACCOUNT}").mock(
            return_value=httpx.Response(200, json=fixture("staying_account.json"))
        )
        account = staying.account()
        assert account["key"]["environment"] == "test"
        assert calls[0]["records_returned"] == 0

    @respx.mock
    def test_credits_available_is_read_from_the_provider(self, staying):
        respx.get(f"{STAYING_URL}{st.EP_ACCOUNT}").mock(
            return_value=httpx.Response(200, json=fixture("staying_account.json"))
        )
        assert staying.credits_available() == 18412.0

    def test_sandbox_keys_are_self_describing(self, recorder):
        assert StayingConnector("stay_test_x", on_call=recorder).is_sandbox_key
        assert not StayingConnector("stay_live_x", on_call=recorder).is_sandbox_key

    @respx.mock
    def test_search_returns_the_listing_set(self, staying):
        respx.get(f"{STAYING_URL}{st.EP_SEARCH}").mock(
            return_value=httpx.Response(200, json=fixture("staying_search.json"))
        )
        result = staying.search_listings({"location": "Phoenix, AZ"})
        assert [l["listing_id"] for l in result.records] == ["bk-88001", "bk-88002"]
        assert result.records[0]["name"] == "Downtown Phoenix Loft"

    @respx.mock
    def test_availability_counts_nights_not_offers(self, staying):
        """Per-date booleans are what make this better than Amadeus offer counts:
        the denominator becomes nights offered rather than results returned."""
        respx.get(f"{STAYING_URL}{st.EP_AVAILABILITY}").mock(
            return_value=httpx.Response(
                200, json=fixture("staying_availability_near.json"))
        )
        result = staying.availability({"platform": "booking", "listingIds": "a,b"})
        assert result.records[0]["nights_offered"] == 2
        assert result.records[0]["nights_available"] == 0
        assert result.records[1]["nights_available"] == 1

    @respx.mock
    def test_202_job_is_polled_to_completion(self, staying):
        """HTTP 202 is a success at the HTTP layer but not yet an answer."""
        respx.get(f"{STAYING_URL}{st.EP_AVAILABILITY}").mock(
            return_value=httpx.Response(
                202, headers={"Retry-After": "1"},
                json=fixture("staying_job_accepted.json"))
        )
        job = respx.get(f"{STAYING_URL}/jobs/job_abc123").mock(
            side_effect=[
                httpx.Response(200, json=fixture("staying_job_running.json")),
                httpx.Response(200, json=fixture("staying_job_complete.json")),
            ]
        )
        result = staying.availability({"platform": "booking", "listingIds": "a"})
        assert job.call_count == 2
        assert result.records[0]["listing_id"] == "bk-88001"

    @respx.mock
    def test_job_failure_raises(self, staying):
        respx.get(f"{STAYING_URL}{st.EP_AVAILABILITY}").mock(
            return_value=httpx.Response(202, json=fixture("staying_job_accepted.json"))
        )
        respx.get(f"{STAYING_URL}/jobs/job_abc123").mock(
            return_value=httpx.Response(200, json={"status": "failed"})
        )
        with pytest.raises(UpstreamError):
            staying.availability({"platform": "booking", "listingIds": "a"})

    @respx.mock
    def test_job_timeout_raises_rather_than_returning_partial_data(self, recorder):
        """An incomplete lodging picture must register as a coverage gap, not as
        availability."""
        connector = StayingConnector(
            "stay_test_k", on_call=recorder, poll_max_s=0.0, sleep=lambda s: None
        )
        respx.get(f"{STAYING_URL}{st.EP_AVAILABILITY}").mock(
            return_value=httpx.Response(202, json=fixture("staying_job_accepted.json"))
        )
        with pytest.raises(UpstreamError, match="did not finish"):
            connector.availability({"platform": "booking", "listingIds": "a"})

    @respx.mock
    def test_202_without_a_job_id_raises(self, staying):
        respx.get(f"{STAYING_URL}{st.EP_AVAILABILITY}").mock(
            return_value=httpx.Response(202, json={"data": {}})
        )
        with pytest.raises(SchemaError):
            staying.availability({"platform": "booking", "listingIds": "a"})

    def test_availability_signal_pairs_the_two_windows(self):
        near = [
            {"listing_id": "a", "nights_available": 0, "nights_offered": 2,
             "platform": "booking"},
            {"listing_id": "b", "nights_available": 1, "nights_offered": 2,
             "platform": "booking"},
        ]
        base = [
            {"listing_id": "a", "nights_available": 2, "nights_offered": 2,
             "platform": "booking"},
            {"listing_id": "b", "nights_available": 2, "nights_offered": 2,
             "platform": "booking"},
        ]
        signals = st.availability_signal(near, base)
        by_ref = {s["provider_ref"]: s for s in signals}
        assert by_ref["a"]["drop_pct"] == 100.0
        assert by_ref["b"]["drop_pct"] == 50.0

    def test_listings_absent_from_one_window_are_excluded(self):
        """A listing measured in only one window says the listing set moved, not
        that availability did — which is the confound the fixed set removes."""
        near = [{"listing_id": "a", "nights_available": 0, "nights_offered": 2}]
        base = [{"listing_id": "a", "nights_available": 2, "nights_offered": 2},
                {"listing_id": "orphan", "nights_available": 2, "nights_offered": 2}]
        signals = st.availability_signal(near, base)
        assert [s["provider_ref"] for s in signals] == ["a"]

    def test_zero_baseline_does_not_divide_by_zero(self):
        near = [{"listing_id": "a", "nights_available": 0, "nights_offered": 2}]
        base = [{"listing_id": "a", "nights_available": 0, "nights_offered": 2}]
        assert st.availability_signal(near, base)[0]["drop_pct"] == 0.0

    def test_window_dates_align_baselines_to_the_same_weekday(self):
        """A Friday near window against a midweek baseline makes ordinary
        weekend demand look like a surge."""
        from datetime import date
        near, base = st.window_dates(date(2026, 7, 28), near_hours=48,
                                     baseline_days=7)
        assert near[0].weekday() == base[0].weekday()
        assert (base[0] - near[0]).days == 7
        assert (near[1] - near[0]) == (base[1] - base[0])


# ===========================================================================
# Priceline
# ===========================================================================


class TestPriceline:
    @respx.mock
    def test_rapidapi_headers_are_sent(self, priceline):
        route = respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            return_value=httpx.Response(200, json=fixture("priceline_cars.json"))
        )
        priceline.search_rental_cars({"pickUpLocation": "PHX"})
        headers = route.calls[0].request.headers
        assert headers["x-rapidapi-key"] == "test-key"
        assert headers["x-rapidapi-host"] == "priceline-com2.p.rapidapi.com"

    @respx.mock
    def test_uses_the_real_endpoint_path(self, priceline):
        """8.5: the wrapper moved from priceline8 to priceline-com2 after the
        former began returning zero inventory for every airport and window.
        The upstream Priceline data is unchanged; only the reseller moved."""
        route = respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            return_value=httpx.Response(200, json=fixture("priceline_cars.json"))
        )
        priceline.search_rental_cars({"pickUpLocation": "PHX"})
        assert route.called

    @respx.mock
    def test_availability_comes_from_a_first_class_count(self, priceline):
        """totalResultsAvailable is a real count, so the car signal does not
        have to be inferred by counting offers."""
        respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            return_value=httpx.Response(200, json=fixture("priceline_cars.json"))
        )
        result = priceline.search_rental_cars({"pickUpLocation": "PHX"})
        assert result["total_results_available"] == 7
        assert result["results_count"] == 7
        assert result["truncated"] is False

    @respx.mock
    def test_truncation_is_detected_not_scored_as_scarcity(self, priceline):
        respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            return_value=httpx.Response(
                200, json=fixture("priceline_cars_truncated.json"))
        )
        result = priceline.search_rental_cars({"pickUpLocation": "PHX"})
        assert result["truncated"] is True

    @respx.mock
    def test_missing_total_raises_rather_than_defaulting_to_zero(self, priceline):
        """Zero reads as total scarcity, and scarcity is what this alerts on."""
        respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            return_value=httpx.Response(
                200, json=fixture("priceline_cars_missing_total.json"))
        )
        with pytest.raises(SchemaError, match="totalResultsAvailable"):
            priceline.search_rental_cars({"pickUpLocation": "PHX"})

    @respx.mock
    def test_systematic_schema_drift_raises(self, priceline):
        """A vendor change must present as a loud FAILED query, never as an
        empty lot."""
        respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            return_value=httpx.Response(
                200, json=fixture("priceline_cars_schema_drift.json"))
        )
        with pytest.raises(SchemaError, match="failed to normalise"):
            priceline.search_rental_cars({"pickUpLocation": "PHX"})

    @respx.mock
    def test_one_malformed_offer_is_tolerated_and_counted(self, priceline):
        """One bad row is noise; the count makes it visible rather than silent."""
        respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            return_value=httpx.Response(
                200, json=fixture("priceline_cars_one_bad_vehicle.json"))
        )
        result = priceline.search_rental_cars({"pickUpLocation": "PHX"})
        assert result["skipped"] == 1
        # Distinct offers, not rows: the fixture's seven rows carry four
        # vendor ids, and the malformed one is among them.
        assert len(result["offers"]) == 3

    @respx.mock
    def test_offers_carry_capacity_and_counter_metadata(self, priceline):
        respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            return_value=httpx.Response(200, json=fixture("priceline_cars.json"))
        )
        result = priceline.search_rental_cars({"pickUpLocation": "PHX"})
        van = next(o for o in result["offers"] if o["vehicle_class"] == "FVAR")
        assert van["people_capacity"] == 12
        assert van["is_on_airport"] is True
        assert van["partner_name"] == "Hertz"
        # Pre-computed by the provider; no haversine needed for cars.
        # 2.9 MILES as reported -> 4.667 km. Measured live 2026-08-10: every
        # PHX counter's distanceFromSearchLocation matched the great-circle
        # distance in miles, not km, so it is converted before it reaches a
        # column named distance_km and a 15 KM spatial gate.
        assert van["distance_km"] == 4.667

    @respx.mock
    def test_peer_to_peer_offers_are_flagged(self, priceline):
        respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            return_value=httpx.Response(200, json=fixture("priceline_cars.json"))
        )
        result = priceline.search_rental_cars({"pickUpLocation": "PHX"})
        p2p = next(o for o in result["offers"] if o["vehicle_class"] == "PEER")
        assert p2p["is_peer_to_peer"] is True

    @respx.mock
    def test_session_bearing_fields_are_never_carried_into_offers(self, priceline):
        """checkoutUrl and detailsKey embed a booking refCode and session tokens.
        The fixture retains them precisely so this assertion is meaningful."""
        respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            return_value=httpx.Response(200, json=fixture("priceline_cars.json"))
        )
        result = priceline.search_rental_cars({"pickUpLocation": "PHX"})
        blob = json.dumps(result)
        assert "checkoutUrl" not in blob
        assert "detailsKey" not in blob
        assert "refCode" not in blob

    @respx.mock
    def test_daily_price_is_derived_not_trusted(self, priceline):
        """dailyPrice came back as 0 in the captured live response while
        totalPrice was populated."""
        respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            return_value=httpx.Response(200, json=fixture("priceline_cars.json"))
        )
        result = priceline.search_rental_cars({"pickUpLocation": "PHX"})
        offer = result["offers"][0]
        assert offer["total_price"] == 157.0
        assert offer["daily_price"] == pytest.approx(78.5)

    def test_offer_key_excludes_the_date_window(self):
        """itemKey encodes pickup/dropoff datetimes, so the same car in two
        windows would have two different itemKeys and never pair up."""
        vehicle = fixture("priceline_cars.json")["data"]["vehicles"][0]
        offer = pl.normalise_car_offer(vehicle)
        assert "280726" not in offer["offer_key"]
        assert offer["offer_key"] == "AC|ECAR|AC-PHX01"

    def test_missing_pickup_location_raises(self):
        vehicle = dict(fixture("priceline_cars.json")["data"]["vehicles"][0])
        del vehicle["pickupLocation"]
        with pytest.raises(SchemaError):
            pl.normalise_car_offer(vehicle)

    def test_missing_capacity_degrades_conservatively(self):
        """Under-weighting a real signal is safe; fabricating one is not."""
        vehicle = json.loads(
            json.dumps(fixture("priceline_cars.json")["data"]["vehicles"][0]))
        del vehicle["vehicleFeatures"]["peopleCapacity"]
        offer = pl.normalise_car_offer(vehicle)
        assert offer["people_capacity"] is None

    @respx.mock
    def test_car_signal_rows_pair_classes_across_windows(self, priceline):
        respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            side_effect=[
                httpx.Response(200, json=fixture("priceline_cars_near.json")),
                httpx.Response(200, json=fixture("priceline_cars.json")),
            ]
        )
        near = priceline.search_rental_cars({"pickUpLocation": "PHX"})
        base = priceline.search_rental_cars({"pickUpLocation": "PHX"})
        rows = pl.car_signal_rows(near, base, pickup="PHX")

        by_class = {r["vehicle_class"]: r for r in rows}
        # THE van, not three vans. The baseline fixture carries three FVAR rows
        # sharing one vendor `id` and differing only in `rate.totalPrice` —
        # three price points for one vehicle. Counting them as three available
        # vans (which this did until 8.6) meant that a supplier trimming to a
        # single price point read as a 66% availability collapse: scarcity
        # manufactured out of a pricing change, on the one family whose entire
        # job is measuring scarcity.
        assert by_class["FVAR"]["base_available"] == 1
        assert by_class["FVAR"]["near_available"] == 1
        assert by_class["FVAR"]["drop_pct"] == 0.0
        assert by_class["FVAR"]["people_capacity"] == 12
        # Peer-to-peer inventory is carried but flagged for scoring to exclude.
        assert by_class["PEER"]["is_peer_to_peer"] is True

    def test_price_points_for_one_car_are_not_availability(self):
        """The regression guard for the defect above, stated directly.

        Rental inventory is not enumerated per vehicle: a response lists what
        is bookable, and the same car appears once per rate plan. The vendor's
        own `id` is the offer identity, so rows sharing one are one offer.
        """
        one_car_three_prices = {
            "success": True,
            "data": {
                "totalResultsAvailable": 3, "resultsCount": 3,
                "vehicles": [
                    {"id": "FVAR-ZE-PHX02", "code": "FVAR",
                     "partner": {"code": "ZE", "name": "Enterprise"},
                     "pickupLocation": {"locationId": "ZE-PHX02",
                                        "counterType": "ON_AIRPORT"},
                     "vehicleFeatures": {"peopleCapacity": 12},
                     "rate": [{"totalPrice": price}]}
                    for price in (399, 349, 429)
                ],
            },
        }
        result = pl.parse_rental_car_response(one_car_three_prices)
        assert len(result["offers"]) == 1, (
            "three prices for one van is one van")

    def test_field_map_version_is_stamped_on_every_row(self):
        """So rows produced by a pre-verification mapping stay identifiable
        after a vendor schema change."""
        vehicle = fixture("priceline_cars.json")["data"]["vehicles"][0]
        assert pl.normalise_car_offer(vehicle)["field_map_ver"] == pl.FIELD_MAP_VERSION

    @respx.mock
    def test_autocomplete_returns_airport_codes(self, priceline):
        respx.get(f"{PRICELINE_URL}{pl.EP_AUTOCOMPLETE}").mock(
            return_value=httpx.Response(
                200, json=fixture("priceline_autocomplete.json"))
        )
        results = priceline.autocomplete_location("Phoenix")
        assert results[0]["airport_code"] == "PHX"
        assert results[0]["type"] == "AIRPORT"

    @respx.mock
    def test_provider_reported_failure_raises(self, priceline):
        respx.get(f"{PRICELINE_URL}{pl.EP_CARS}").mock(
            return_value=httpx.Response(
                200, json={"success": False, "message": "invalid date"})
        )
        with pytest.raises(SchemaError, match="invalid date"):
            priceline.search_rental_cars({"pickUpLocation": "PHX"})


# ===========================================================================
# Accounting
# ===========================================================================


class TestCallAccounting:
    @respx.mock
    def test_every_attempt_is_recorded_including_failures(self, fr24, calls):
        respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            return_value=httpx.Response(401, json={"message": "bad"})
        )
        with pytest.raises(AuthError):
            fr24.live_positions({"airports": "inbound:PHX"})
        assert len(calls) == 1
        assert calls[0]["http_status"] == 401
        assert calls[0]["provider"] == "FR24"
        assert calls[0]["records_returned"] == 0

    @respx.mock
    def test_iteration_and_query_ids_are_threaded_through(self, fr24, calls):
        respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            return_value=httpx.Response(200, json=fixture("fr24_live_positions.json"))
        )
        fr24.live_positions({"airports": "inbound:PHX"}, iteration_id=7, query_id=42)
        assert calls[0]["iteration_id"] == 7
        assert calls[0]["query_id"] == 42

    @respx.mock
    def test_latency_is_measured(self, fr24, calls):
        respx.get(f"{FR24_URL}{fr.EP_LIVE_FULL}").mock(
            return_value=httpx.Response(200, json=fixture("fr24_live_positions.json"))
        )
        fr24.live_positions({"airports": "inbound:PHX"})
        assert calls[0]["latency_ms"] >= 0


class TestStayingResponseMetadata:
    """The provider reports HOW it obtained a result, not just the result.

    Prompted by a vendor notice: their API had been normalising a Google Hotels
    validation failure into an "ok / 0 results" leg. A date-less multi-platform
    search therefore looked like a complete search that found fewer listings,
    when one vendor had never been queried at all. Their fix reports the leg as
    `skipped` with a `requires_dates` reason — but only helps if we read it.
    """

    LIVE_META = {
        "requestId": "req_01TEST",
        "platforms": ["vrbo", "booking", "airbnb"],
        "cached": False,
        "partial": False,
        "creditsCharged": 20,
        "platformResults": [
            {"platform": "vrbo", "status": "ok", "count": 5, "creditsCharged": 5},
            {"platform": "booking", "status": "ok", "count": 5, "creditsCharged": 5},
            {"platform": "airbnb", "status": "ok", "count": 5, "creditsCharged": 10},
            {"platform": "google", "status": "skipped", "count": 0,
             "reason": "requires_dates", "creditsCharged": 0,
             "message": "google requires checkIn/checkOut for search; leg skipped"},
        ],
        "warnings": [
            {"code": "platform_requires_dates", "param": "checkIn/checkOut",
             "platform": "google",
             "message": "google requires checkIn/checkOut for search"},
        ],
    }

    def _body(self, meta=None):
        payload = fixture("staying_search.json")
        payload["meta"] = meta if meta is not None else self.LIVE_META
        return payload

    @respx.mock
    def test_metadata_is_surfaced_rather_than_discarded(self, staying):
        respx.get(f"{STAYING_URL}{st.EP_SEARCH}").mock(
            return_value=httpx.Response(200, json=self._body())
        )
        # Constrained to airbnb, so the skipped Google leg does not raise and
        # the metadata can be inspected on a successful call.
        result = staying.search_listings(
            {"location": "Phoenix, AZ", "platforms": "airbnb"}
        )
        assert result.warnings[0]["code"] == "platform_requires_dates"
        assert result.platform_results["google"]["status"] == "skipped"
        assert result.credits_charged == 20.0
        assert result.partial is False

    @respx.mock
    def test_a_skipped_leg_we_did_not_request_is_informational(self, staying):
        """We ask for airbnb only, so a skipped Google leg is not our gap."""
        respx.get(f"{STAYING_URL}{st.EP_SEARCH}").mock(
            return_value=httpx.Response(200, json=self._body())
        )
        result = staying.search_listings(
            {"location": "Phoenix, AZ", "platforms": "airbnb"}
        )
        assert result.not_searched() == {"google": "requires_dates"}
        assert len(result.records) == 2      # the call still succeeded

    @respx.mock
    def test_a_skipped_leg_we_did_request_raises(self, staying):
        """"We did not search" must never present as "we searched and found
        fewer" — that is the whole failure mode the vendor notice described."""
        meta = dict(self.LIVE_META)
        meta["platformResults"] = [
            {"platform": "airbnb", "status": "skipped", "count": 0,
             "reason": "requires_dates"},
        ]
        respx.get(f"{STAYING_URL}{st.EP_SEARCH}").mock(
            return_value=httpx.Response(200, json=self._body(meta))
        )
        with pytest.raises(st.PlatformUnavailableError, match="requires_dates"):
            staying.search_listings(
                {"location": "Phoenix, AZ", "platforms": "airbnb"}
            )

    @respx.mock
    def test_an_unconstrained_search_treats_every_leg_as_requested(self, staying):
        """With no `platforms` parameter the caller asked for all of them, so a
        skipped leg is a genuine gap in what was requested."""
        respx.get(f"{STAYING_URL}{st.EP_SEARCH}").mock(
            return_value=httpx.Response(200, json=self._body())
        )
        with pytest.raises(st.PlatformUnavailableError):
            staying.search_listings({"location": "Phoenix, AZ"})

    @respx.mock
    def test_actual_credits_correct_the_ledger(self, staying, calls):
        """The generic accounting charges one unit per call. A multi-platform
        search really cost 20 credits and an airbnb-only one 10, so an
        uncorrected ledger under-counts Staying by an order of magnitude."""
        respx.get(f"{STAYING_URL}{st.EP_SEARCH}").mock(
            return_value=httpx.Response(200, json=self._body())
        )
        staying.search_listings({"location": "Phoenix, AZ", "platforms": "airbnb"})
        total = sum(c.get("units", 1.0) if c.get("units") is not None else 1.0
                    for c in calls)
        assert total == pytest.approx(20.0)

    @respx.mock
    def test_a_response_without_meta_still_works(self, staying):
        """Older responses, and the /jobs poll, carry no meta block."""
        respx.get(f"{STAYING_URL}{st.EP_SEARCH}").mock(
            return_value=httpx.Response(200, json=fixture("staying_search.json"))
        )
        result = staying.search_listings({"location": "Phoenix, AZ"})
        assert len(result.records) == 2
        assert result.warnings == []
        assert result.credits_charged is None

    @respx.mock
    def test_a_partial_result_set_is_visible(self, staying):
        meta = dict(self.LIVE_META)
        meta["partial"] = True
        meta["platformResults"] = []
        respx.get(f"{STAYING_URL}{st.EP_SEARCH}").mock(
            return_value=httpx.Response(200, json=self._body(meta))
        )
        assert staying.search_listings({"location": "Phoenix, AZ"}).partial is True

    @pytest.mark.parametrize(
        "params,expected",
        [({"platforms": "airbnb"}, {"airbnb"}),
         ({"platforms": "airbnb,vrbo"}, {"airbnb", "vrbo"}),
         ({"platforms": ["airbnb", "VRBO"]}, {"airbnb", "vrbo"}),
         ({"platform": "booking"}, {"booking"}),
         ({}, set())],
    )
    def test_requested_platforms_parsing(self, params, expected):
        assert st._requested_platforms(params) == expected
