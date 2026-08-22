"""CollectionAgent — queue draining, signal derivation, and failure isolation.

The gate for this phase concerns what ends up on the record. A query that failed,
a query the budget refused, and a query that genuinely found nothing are three
different things, and every one of them must be distinguishable afterwards.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ANCHOR
from surge_iw.agents.collection import SOCIAL_TYPES, TIPPED_TYPES, CollectionAgent
from surge_iw.agents.queueing import (
    EP_CAR_SEARCH,
    EP_FLIGHT_LIVE,
    EP_FLIGHT_SUMMARY,
    EP_LODGING_SEARCH,
    EP_TWITTER,
)
from surge_iw.base.connector import AuthError, SchemaError
from surge_iw.connectors.staying import SearchResult
from surge_iw.services.budget import BudgetGuard

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Fakes. Connectors are already covered in test_connectors.py against respx;
# here the interest is what CollectionAgent does with what they return.
# ---------------------------------------------------------------------------


class FakeSocial:
    provider = "APIDIRECT"

    def __init__(self, posts=None, error=None):
        self.posts = posts if posts is not None else []
        self.error = error
        self.calls = []

    def search(self, endpoint, params, **kwargs):
        self.calls.append((endpoint, params))
        if self.error:
            raise self.error
        return self.posts


class FakeFlights:
    provider = "FR24"

    def __init__(self, live=None, summary=None, error=None):
        self.live = live or []
        self.summary = summary or []
        self.error = error

    def live_positions(self, params, **kwargs):
        if self.error:
            raise self.error
        return self.live

    def flight_summary(self, params, **kwargs):
        if self.error:
            raise self.error
        return self.summary

    def count_live(self, params, **kwargs):
        return len(self.live)


class FakeStaying:
    provider = "STAYING"

    def __init__(self, listings=None, windows=None, error=None, meta=None):
        self.listings = listings or []
        self.windows = windows or []
        self.error = error
        self.meta = meta or {}
        self.search_calls = 0
        self.availability_calls = 0

    def search_listings(self, params, **kwargs):
        self.search_calls += 1
        if self.error:
            raise self.error
        return SearchResult(records=self.listings, meta=self.meta)

    def availability(self, params, **kwargs):
        rows = self.windows[min(self.availability_calls, len(self.windows) - 1)]
        self.availability_calls += 1
        return SearchResult(records=rows, meta=self.meta)


class FakeCars:
    provider = "PRICELINE"

    def __init__(self, near=None, baseline=None, error=None):
        self.near = near
        self.baseline = baseline
        self.error = error
        self.calls = 0

    def search_rental_cars(self, params, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.near if self.calls == 1 else self.baseline


def enqueue(db, session, iteration, *, source_type, endpoint, params,
            city_id=None, location_id=None, key=None):
    return db.enqueue_query(
        session_id=session, iteration_id=iteration, source_type=source_type,
        endpoint=endpoint, params=params, dedup_key=key or f"k-{source_type}-{endpoint}",
        city_id=city_id, location_id=location_id, rule_code="TEST",
    )


@pytest.fixture
def city(db, session):
    return db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")


def make_agent(db, config, **connectors):
    return CollectionAgent(db, config, connectors)


# ===========================================================================


class TestSocialCollection:
    def test_posts_are_stored_but_produce_no_signals(
        self, db, config, session, iteration, city
    ):
        """Relevance is a language judgement, so TriageAgent makes it — not this
        agent. Collection stores the payload and stops."""
        posts = [{"url": "https://x.com/a/1", "snippet": "demonstration team"},
                 {"url": "https://x.com/a/2", "snippet": "static display"}]
        enqueue(db, session, iteration, source_type="SOCIAL",
                endpoint=EP_TWITTER, params={"query": "phoenix"}, city_id=city)
        agent = make_agent(db, config, APIDIRECT=FakeSocial(posts))

        assert agent.run(iteration, source_types=SOCIAL_TYPES) is True
        raw = db.all("SELECT * FROM raw_results WHERE iteration_id = ?", (iteration,))
        assert len(raw) == 1
        assert len(json.loads(raw[0]["payload_json"])) == 2
        assert db.scalar("SELECT COUNT(*) FROM signals") == 0

    def test_query_is_marked_complete_with_a_result_count(
        self, db, config, session, iteration, city
    ):
        query_id = enqueue(db, session, iteration, source_type="SOCIAL",
                           endpoint=EP_TWITTER, params={}, city_id=city)
        agent = make_agent(db, config,
                           APIDIRECT=FakeSocial([{"url": "u1"}, {"url": "u2"}]))
        agent.run(iteration, source_types=SOCIAL_TYPES)
        row = db.one("SELECT * FROM query_queue WHERE query_id = ?", (query_id,))
        assert row["status"] == "COMPLETE"
        assert row["result_count"] == 2

    def test_an_empty_result_is_a_success_not_a_failure(
        self, db, config, session, iteration, city
    ):
        """Zero posts from a working endpoint is real evidence of absence."""
        query_id = enqueue(db, session, iteration, source_type="SOCIAL",
                           endpoint=EP_TWITTER, params={}, city_id=city)
        agent = make_agent(db, config, APIDIRECT=FakeSocial([]))
        agent.run(iteration, source_types=SOCIAL_TYPES)
        row = db.one("SELECT * FROM query_queue WHERE query_id = ?", (query_id,))
        assert row["status"] == "COMPLETE"
        assert db.unreliable_source_types(iteration, city) == []


class TestBudgetRefusalsNameTheirCity:
    """Measured across seven metros: three were collected 24/24 and still
    reported a SOCIAL coverage gap, because the four starved cities' budget
    refusals carried no city name and `refused_source_types` treats a NULL
    city as applying to every city. Correct for a guard that fires before the
    city exists; wrong at collection time, where the queue row names it.

    Over-reporting a gap is the safer direction than under-reporting, but it
    still means `data_completeness` cannot distinguish "this city was not
    collected" from "some other city was not".
    """

    def test_a_starved_city_gets_the_gap_and_a_collected_one_does_not(
        self, db, config, session, iteration
    ):
        from surge_iw.services.budget import BudgetGuard

        rich = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        poor = db.insert_city(session, "Tucson", canonical="tucson", state="AZ")
        enqueue(db, session, iteration, source_type="SOCIAL",
                endpoint=EP_TWITTER, params={"q": 1}, city_id=rich, key="rich")
        enqueue(db, session, iteration, source_type="SOCIAL",
                endpoint=EP_TWITTER, params={"q": 2}, city_id=poor, key="poor")

        class OneThenBroke(BudgetGuard):
            calls = 0

            def can_afford(self, *a, **k):
                OneThenBroke.calls += 1
                return (True, None) if OneThenBroke.calls == 1 else (
                    False, "ITERATION_ALLOCATION_EXHAUSTED")

        agent = CollectionAgent(db, config, {"APIDIRECT": FakeSocial([{"url": "u"}])},
                                budget=OneThenBroke(db, config))
        agent.run(iteration, source_types=SOCIAL_TYPES)

        assert db.refused_source_types(iteration, poor) == ["SOCIAL"]
        assert db.refused_source_types(iteration, rich) == [], (
            "a city collected in full must not inherit another city's refusal")


class TestFailureIsolation:
    def test_a_failed_query_does_not_abort_the_stage(
        self, db, config, session, iteration, city
    ):
        """The others must still run. A social outage cannot be allowed to
        discard flight collection that would otherwise have succeeded."""
        enqueue(db, session, iteration, source_type="SOCIAL",
                endpoint=EP_TWITTER, params={"q": 1}, city_id=city, key="k1")
        enqueue(db, session, iteration, source_type="SOCIAL",
                endpoint=EP_TWITTER, params={"q": 2}, city_id=city, key="k2")

        class FlakySocial(FakeSocial):
            def search(self, endpoint, params, **kwargs):
                self.calls.append(params)
                if len(self.calls) == 1:
                    raise AuthError("401 bad token", provider="APIDIRECT",
                                    status_code=401)
                return [{"url": "https://x/ok"}]

        agent = make_agent(db, config, APIDIRECT=FlakySocial())
        assert agent.run(iteration, source_types=SOCIAL_TYPES) is True

        statuses = sorted(r["status"] for r in db.get_queue(iteration))
        assert statuses == ["COMPLETE", "FAILED"]
        assert db.scalar("SELECT COUNT(*) FROM raw_results") == 1

    def test_a_failure_is_recorded_as_a_coverage_gap(
        self, db, config, session, iteration, city
    ):
        """This is what stops a broken key reading as 'no threat detected'."""
        enqueue(db, session, iteration, source_type="FLIGHT_LIVE",
                endpoint=EP_FLIGHT_LIVE, params={}, city_id=city)
        agent = make_agent(db, config, FR24=FakeFlights(
            error=AuthError("401 expired", provider="FR24", status_code=401)))
        agent.run(iteration, source_types=TIPPED_TYPES)

        row = db.one("SELECT * FROM query_queue")
        assert row["status"] == "FAILED"
        assert "401" in row["error_message"]
        assert db.unreliable_source_types(iteration, city) == ["FLIGHT_LIVE"]

    def test_agent_run_still_reports_success_when_one_query_fails(
        self, db, config, session, iteration, city
    ):
        """A failed query is data, not an agent failure. The agent only fails
        when it could not do its job at all."""
        enqueue(db, session, iteration, source_type="SOCIAL",
                endpoint=EP_TWITTER, params={}, city_id=city)
        agent = make_agent(db, config, APIDIRECT=FakeSocial(
            error=SchemaError("bad shape", provider="APIDIRECT")))
        assert agent.run(iteration, source_types=SOCIAL_TYPES) is True
        runs = db.get_agent_runs(iteration)
        assert runs[0]["status"] == "COMPLETE"

    def test_a_missing_connector_fails_only_that_query(
        self, db, config, session, iteration, city
    ):
        enqueue(db, session, iteration, source_type="SOCIAL",
                endpoint=EP_TWITTER, params={}, city_id=city)
        agent = make_agent(db, config)          # no connectors at all
        agent.run(iteration, source_types=SOCIAL_TYPES)
        assert db.one("SELECT * FROM query_queue")["status"] == "FAILED"


class TestBudgetRefusal:
    def test_exhausted_budget_skips_rather_than_fails(
        self, db, config, session, iteration, city
    ):
        """'We could not afford to look' must stay distinguishable from 'we
        looked and the endpoint was broken'."""
        query_id = enqueue(db, session, iteration, source_type="SOCIAL",
                           endpoint=EP_TWITTER, params={}, city_id=city)
        budget = BudgetGuard(db, config)
        budget.seed_budgets()
        db.set_budget("APIDIRECT", None, "MONTH", 0.0)

        agent = CollectionAgent(db, config, {"APIDIRECT": FakeSocial([{"url": "u"}])},
                                budget=budget)
        assert agent.run(iteration, source_types=SOCIAL_TYPES) is True

        row = db.one("SELECT * FROM query_queue WHERE query_id = ?", (query_id,))
        assert row["status"] == "SKIPPED_BUDGET"
        assert row["skip_reason"] == "MONTHLY_QUOTA_EXHAUSTED"
        assert db.decision_counts(iteration)["BUDGET_EXHAUSTED"] == 1
        # Nothing was collected, and no HTTP call was attempted.
        assert db.scalar("SELECT COUNT(*) FROM raw_results") == 0

    def test_a_budget_skip_is_a_coverage_gap_too(
        self, db, config, session, iteration, city
    ):
        enqueue(db, session, iteration, source_type="CAR",
                endpoint=EP_CAR_SEARCH, params={}, city_id=city)
        budget = BudgetGuard(db, config)
        budget.seed_budgets()
        db.set_budget("PRICELINE", None, "MONTH", 0.0)
        agent = CollectionAgent(db, config, {"PRICELINE": FakeCars()}, budget=budget)
        agent.run(iteration, source_types=TIPPED_TYPES)
        assert db.unreliable_source_types(iteration, city) == ["CAR"]


class TestFlightCollection:
    def test_live_signals_are_ambiguous_on_arrival(
        self, db, config, session, iteration, city
    ):
        from surge_iw.connectors.flightradar import _normalise_live
        live = [_normalise_live(f)
                for f in fixture("fr24_live_positions.json")["data"]]
        enqueue(db, session, iteration, source_type="FLIGHT_LIVE",
                endpoint=EP_FLIGHT_LIVE, params={}, city_id=city)
        agent = make_agent(db, config, FR24=FakeFlights(live=live))
        agent.run(iteration, source_types=TIPPED_TYPES)

        signals = db.signals_by_type(iteration, "FLIGHT")
        assert len(signals) == 3
        assert all(s["category_confidence"] == "AMBIGUOUS" for s in signals)
        assert signals[0]["eta"]

    def test_history_confirms_the_category_of_live_records(
        self, db, config, session, iteration, city
    ):
        """Without this step every flight signal stays AMBIGUOUS and is scored
        at the lowest weight its filter could have earned."""
        from surge_iw.connectors.flightradar import _normalise_live, _normalise_summary
        live = [_normalise_live(f)
                for f in fixture("fr24_live_positions.json")["data"]]
        summary = [_normalise_summary(f)
                   for f in fixture("fr24_flight_summary.json")["data"]]

        enqueue(db, session, iteration, source_type="FLIGHT_LIVE",
                endpoint=EP_FLIGHT_LIVE, params={}, city_id=city, key="k-live")
        agent = make_agent(db, config, FR24=FakeFlights(live=live, summary=summary))
        agent.run(iteration, source_types=("FLIGHT_LIVE",))

        enqueue(db, session, iteration, source_type="FLIGHT_HISTORY",
                endpoint=EP_FLIGHT_SUMMARY, params={}, city_id=city, key="k-hist")
        agent.run(iteration, source_types=("FLIGHT_HISTORY",))

        by_id = {s["fr24_id"]: s for s in db.signals_by_type(iteration, "FLIGHT")}
        # 39bf1c58 appears in both, with category M in the summary.
        assert by_id["39bf1c58"]["flight_category"] == "M"
        assert by_id["39bf1c58"]["category_confidence"] == "CONFIRMED"
        # 39bf2201 is live-only, so it must remain unresolved rather than guessed.
        assert by_id["39bf2201"]["category_confidence"] == "AMBIGUOUS"

    def test_duplicate_airframes_are_deduplicated_not_double_counted(
        self, db, config, session, iteration, city
    ):
        from surge_iw.connectors.flightradar import _normalise_live
        live = [_normalise_live(f)
                for f in fixture("fr24_live_positions.json")["data"]]
        enqueue(db, session, iteration, source_type="FLIGHT_LIVE",
                endpoint=EP_FLIGHT_LIVE, params={"a": 1}, city_id=city, key="k1")
        enqueue(db, session, iteration, source_type="FLIGHT_LIVE",
                endpoint=EP_FLIGHT_LIVE, params={"a": 2}, city_id=city, key="k2")
        agent = make_agent(db, config, FR24=FakeFlights(live=live))
        agent.run(iteration, source_types=TIPPED_TYPES)
        assert len(db.signals_by_type(iteration, "FLIGHT")) == 3


class TestLodgingCollection:
    LISTINGS = [
        {"listing_id": f"L{i}", "platform": "airbnb", "name": f"Loft {i}"}
        for i in range(1, 6)
    ]

    def _windows(self, near_available, base_available):
        near = [{"listing_id": f"L{i}", "platform": "airbnb",
                 "nights_offered": 3, "nights_available": near_available}
                for i in range(1, 6)]
        base = [{"listing_id": f"L{i}", "platform": "airbnb",
                 "nights_offered": 3, "nights_available": base_available}
                for i in range(1, 6)]
        return [near, base]

    def test_two_stage_method_produces_a_drop_signal(
        self, db, config, session, iteration, city
    ):
        location = db.insert_key_location(city, "Riverside Fairground")
        enqueue(db, session, iteration, source_type="LODGING",
                endpoint=EP_LODGING_SEARCH, params={"location": "Phoenix, AZ"},
                city_id=city, location_id=location)
        connector = FakeStaying(self.LISTINGS, self._windows(0, 3))
        agent = make_agent(db, config, STAYING=connector)
        agent.run(iteration, source_types=TIPPED_TYPES)

        signals = db.signals_by_type(iteration, "LODGING")
        assert len(signals) == 5
        assert all(s["drop_pct"] == 100.0 for s in signals)
        assert signals[0]["location_id"] == location

    def test_the_listing_set_is_cached_across_iterations(
        self, db, config, session, iteration, city
    ):
        """/search is asynchronous and took 125 seconds live, and a *fixed* set
        is what makes the two windows comparable at all."""
        connector = FakeStaying(self.LISTINGS, self._windows(1, 3))
        agent = make_agent(db, config, STAYING=connector)
        for seq, key in enumerate(("k1", "k2")):
            it = db.insert_iteration(session, anchor_at=ANCHOR) if seq else iteration
            enqueue(db, session, it, source_type="LODGING",
                    endpoint=EP_LODGING_SEARCH,
                    params={"location": "Phoenix, AZ"}, city_id=city, key=key)
            agent.run(it, source_types=TIPPED_TYPES)
        assert connector.search_calls == 1

    def test_a_thin_sample_is_refused_rather_than_scored(
        self, db, config, session, iteration, city
    ):
        """A drop computed from one listing is arithmetic, not evidence. Live
        coverage is roughly 1 in 15 listings, so this is a common case."""
        one_listing = [{"listing_id": "L1", "platform": "airbnb", "name": "Loft"}]
        windows = [
            [{"listing_id": "L1", "platform": "airbnb",
              "nights_offered": 3, "nights_available": 0}],
            [{"listing_id": "L1", "platform": "airbnb",
              "nights_offered": 3, "nights_available": 3}],
        ]
        query_id = enqueue(db, session, iteration, source_type="LODGING",
                           endpoint=EP_LODGING_SEARCH,
                           params={"location": "Phoenix, AZ"}, city_id=city)
        agent = make_agent(db, config, STAYING=FakeStaying(one_listing, windows))
        agent.run(iteration, source_types=TIPPED_TYPES)

        assert db.signals_by_type(iteration, "LODGING") == []
        row = db.one("SELECT * FROM query_queue WHERE query_id = ?", (query_id,))
        assert row["status"] == "SKIPPED_NO_MAPPING"
        # THIN_PAIRED_SAMPLE, not NO_LISTING_SET: a listing set WAS resolved
        # and the vendor WAS called; only the paired sample was too small.
        assert row["skip_reason"] == "THIN_PAIRED_SAMPLE"
        # `executed_at` is not asserted here: it is read from the `api_calls`
        # ledger, and this fake connector does not go through `_request`, so it
        # writes none. The ledger is the right source even so — line 391 shows
        # that even a genuine NO_LISTING_SET can follow a paid `/search`, so
        # whether money was spent varies per invocation rather than per reason,
        # and only the ledger knows. The contract is covered by
        # `TestASkippedQueryThatSpentMoneySaysSo`.

    def test_an_empty_listing_set_is_recorded_as_no_mapping(
        self, db, config, session, iteration, city
    ):
        query_id = enqueue(db, session, iteration, source_type="LODGING",
                           endpoint=EP_LODGING_SEARCH,
                           params={"location": "Nowhere"}, city_id=city)
        agent = make_agent(db, config, STAYING=FakeStaying([], [[], []]))
        agent.run(iteration, source_types=TIPPED_TYPES)
        row = db.one("SELECT * FROM query_queue WHERE query_id = ?", (query_id,))
        assert row["status"] == "SKIPPED_NO_MAPPING"
        # The genuine case: nothing resolved, so this keeps NO_LISTING_SET.
        assert row["skip_reason"] == "NO_LISTING_SET"


class TestCarCollection:
    def test_two_windows_produce_capacity_bearing_signals(
        self, db, config, session, iteration, city
    ):
        from surge_iw.connectors.priceline import parse_rental_car_response
        near = parse_rental_car_response(fixture("priceline_cars_near.json"))
        base = parse_rental_car_response(fixture("priceline_cars.json"))
        enqueue(db, session, iteration, source_type="CAR",
                endpoint=EP_CAR_SEARCH,
                params={"pickUpLocation": "PHX"}, city_id=city)
        agent = make_agent(db, config, PRICELINE=FakeCars(near, base))
        agent.run(iteration, source_types=TIPPED_TYPES)

        signals = db.signals_by_type(iteration, "CAR")
        assert signals
        vans = [s for s in signals if s["vehicle_class"] == "FVAR"]
        assert vans and vans[0]["people_capacity"] == 12
        # No drop: the baseline fixture's three FVAR rows are three PRICE
        # POINTS for one van, not three vans. Asserting a drop here was
        # asserting the 8.6 defect — a pricing change reading as scarcity.
        # What this test is actually for is that a paired two-window
        # collection produces capacity-bearing signals at all.
        assert vans[0]["drop_pct"] == 0.0
        assert vans[0]["base_available"] == 1
        assert vans[0]["is_on_airport"] == 1

    def test_baseline_window_uses_a_different_date(
        self, db, config, session, iteration, city
    ):
        """The two windows must actually differ.

        This caught a real bug during the 8.5 wrapper swap: the baseline
        override still used the old snake_case key names, so it added two
        ignored parameters and left the near-window dates in place. Both
        windows queried the same dates and the signal reported a perfect 0%
        drop from identical data. Asserting on the CAMEL-CASE key the vendor
        actually reads is the whole point — a test that watches the wrong key
        passes while the collection is broken.
        """
        from surge_iw.connectors.priceline import parse_rental_car_response
        payload = parse_rental_car_response(fixture("priceline_cars.json"))

        captured = []

        class Recording(FakeCars):
            def search_rental_cars(self, params, **kwargs):
                captured.append(params.get("pickUpDate"))
                return payload

        enqueue(db, session, iteration, source_type="CAR",
                endpoint=EP_CAR_SEARCH,
                params={"pickUpLocation": "PHX", "pickUpDate": "2026-07-30",
                        "dropOffDate": "2026-08-01"}, city_id=city)
        agent = make_agent(db, config, PRICELINE=Recording())
        agent.run(iteration, source_types=TIPPED_TYPES)
        assert len(captured) == 2
        assert captured[0] is not None, "the vendor's key name is pickUpDate"
        assert captured[0] != captured[1]


class TestAuditTrail:
    def test_every_signal_traces_to_a_payload_and_a_query(
        self, db, config, session, iteration, city
    ):
        """No analytical record without provenance."""
        from surge_iw.connectors.flightradar import _normalise_live
        live = [_normalise_live(f)
                for f in fixture("fr24_live_positions.json")["data"]]
        enqueue(db, session, iteration, source_type="FLIGHT_LIVE",
                endpoint=EP_FLIGHT_LIVE, params={}, city_id=city)
        make_agent(db, config, FR24=FakeFlights(live=live)).run(
            iteration, source_types=TIPPED_TYPES)

        orphans = db.scalar(
            "SELECT COUNT(*) FROM signals s "
            "LEFT JOIN raw_results r USING (raw_id) "
            "LEFT JOIN query_queue q ON q.query_id = r.query_id "
            "WHERE q.query_id IS NULL"
        )
        assert orphans == 0

    def test_the_agent_run_is_recorded(self, db, config, session, iteration, city):
        enqueue(db, session, iteration, source_type="SOCIAL",
                endpoint=EP_TWITTER, params={}, city_id=city)
        make_agent(db, config, APIDIRECT=FakeSocial([])).run(
            iteration, source_types=SOCIAL_TYPES)
        runs = db.get_agent_runs(iteration)
        assert len(runs) == 1
        assert runs[0]["agent"] == "CollectionAgent"
        assert runs[0]["status"] == "COMPLETE"
        assert runs[0]["finished_at"]

    def test_collection_logs_its_counts(self, db, config, session, iteration, city):
        enqueue(db, session, iteration, source_type="SOCIAL",
                endpoint=EP_TWITTER, params={}, city_id=city)
        make_agent(db, config, APIDIRECT=FakeSocial([{"url": "u"}])).run(
            iteration, source_types=SOCIAL_TYPES)
        logs = db.get_log(iteration, "CollectionAgent")
        assert any("executed" in (l["message"] or "") for l in logs)


class TestASkippedQueryThatSpentMoneySaysSo:
    """A query can be refused AFTER it has called a vendor.

    Found on a live reference run: one Staying `/search` resolved a listing
    set, made 13 calls, spent 100 credits, and was recorded
    `SKIPPED_NO_MAPPING / NO_LISTING_SET` with `executed_at` NULL. Three things
    were wrong with that record, and the third one costs money:

      * the reason said no listing set existed; one did, and is in `geo_cache`
      * the status said the query never ran; `api_calls` showed 13 calls
      * `executed_at` was NULL, so the cooldown never started — the identical
        query would be reissued next iteration and spend the 100 credits again

    The distinction the engine has to keep is "did this call a vendor", and it
    is read from the `api_calls` ledger rather than passed in, because a caller
    that had to remember would eventually forget.
    """

    def _query(self, db, session, iteration, key):
        city = db.find_city(session, "phoenix") or db.insert_city(
            session, "Phoenix", canonical="phoenix")
        city_id = city if isinstance(city, int) else int(city["city_id"])
        return db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="LODGING",
            endpoint="/search", params={}, dedup_key=key, city_id=city_id)

    def test_a_preflight_skip_records_no_execution(self, db, session, iteration):
        """Nothing was called, so there is nothing to sit out a cooldown for."""
        q = self._query(db, session, iteration, "cold")
        db.skip_query(q, "SKIPPED_NO_MAPPING", "NO_LISTING_SET",
                      "the search returned no listings at all")
        assert db.get_query(q)["executed_at"] is None
        assert db.last_execution("cold") is None

    def test_a_skip_after_calling_a_vendor_records_the_execution(
        self, db, session, iteration
    ):
        q = self._query(db, session, iteration, "spent")
        db.record_api_call(iteration_id=iteration, query_id=q,
                           provider="STAYING", endpoint="/search",
                           http_status=202, units=79.0)
        db.skip_query(q, "SKIPPED_NO_MAPPING", "THIN_PAIRED_SAMPLE",
                      "only 2 listing(s) paired, minimum 3")
        assert db.get_query(q)["executed_at"] is not None

    def test_the_cooldown_stops_it_being_bought_twice(
        self, db, session, iteration
    ):
        """The whole point. Without this the same query is reissued next
        iteration and spends again, with nothing bounding the repeat but the
        monthly budget."""
        q = self._query(db, session, iteration, "spent")
        db.record_api_call(iteration_id=iteration, query_id=q,
                           provider="STAYING", endpoint="/search",
                           http_status=202, units=79.0)
        db.skip_query(q, "SKIPPED_NO_MAPPING", "THIN_PAIRED_SAMPLE", "thin")
        assert db.last_execution("spent") is not None

    def test_the_two_causes_are_separable_in_the_record(
        self, db, session, iteration
    ):
        """One spent money and one did not. A reader must not have to consult
        the api_calls ledger to tell them apart."""
        cold = self._query(db, session, iteration, "cold")
        db.skip_query(cold, "SKIPPED_NO_MAPPING", "NO_LISTING_SET", "none")
        spent = self._query(db, session, iteration, "spent")
        db.record_api_call(iteration_id=iteration, query_id=spent,
                           provider="STAYING", endpoint="/search",
                           http_status=202, units=79.0)
        db.skip_query(spent, "SKIPPED_NO_MAPPING", "THIN_PAIRED_SAMPLE", "thin")
        assert db.get_query(cold)["skip_reason"] != \
            db.get_query(spent)["skip_reason"]

    def test_the_detail_is_kept_not_only_logged(self, db, session, iteration):
        """It was computed, logged, and dropped, so the row could not answer a
        question the log could."""
        q = self._query(db, session, iteration, "cold")
        db.skip_query(q, "SKIPPED_NO_MAPPING", "THIN_PAIRED_SAMPLE",
                      "only 2 listing(s) paired, minimum 3")
        assert "paired" in (db.get_query(q)["error_message"] or "")

    def test_a_second_skip_does_not_move_the_first_execution_time(
        self, db, session, iteration
    ):
        """`COALESCE` keeps the original. A retry must not extend a cooldown
        that has already started running down."""
        q = self._query(db, session, iteration, "spent")
        db.record_api_call(iteration_id=iteration, query_id=q,
                           provider="STAYING", endpoint="/search",
                           http_status=202, units=1.0)
        db.skip_query(q, "SKIPPED_NO_MAPPING", "THIN_PAIRED_SAMPLE", "a")
        first = db.get_query(q)["executed_at"]
        db.skip_query(q, "SKIPPED_NO_MAPPING", "THIN_PAIRED_SAMPLE", "b")
        assert db.get_query(q)["executed_at"] == first
