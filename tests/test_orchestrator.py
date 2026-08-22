"""The Phase 4 gate: stages 1–5 driven end to end, and resume.

The plan's gate text predates the Phase 2 live findings, which killed the
/count tripwire — both /count endpoints return 403 on the FR24 Explorer tier, so
`R2` now escalates to flight-summary once live records exist rather than firing
on a count threshold. The gate is asserted against the rules as they actually
are, plus the count path behind its config flag so a tier upgrade is covered.
"""
from __future__ import annotations

import json

import pytest

from surge_iw.agents.orchestrator import IterationOrchestrator
from surge_iw.agents.queueing import QueueAgent
from surge_iw.connectors.flightradar import _normalise_live, _normalise_summary
from surge_iw.connectors.priceline import parse_rental_car_response
from surge_iw.connectors.staying import SearchResult
from surge_iw.db.database import iso, utcnow
from surge_iw.services.budget import BudgetGuard
from test_collection import fixture
from test_triage import FakeLLM


# ---------------------------------------------------------------------------
# Stubs. The connectors are covered against respx in test_connectors.py; here
# the interest is the sequencing the orchestrator imposes on them.
# ---------------------------------------------------------------------------


class StubSocial:
    provider = "APIDIRECT"

    def __init__(self, posts_by_city=None, error=None):
        self.posts_by_city = posts_by_city or {}
        self.error = error
        self.calls = 0

    def search(self, endpoint, params, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        query = params.get("query", "")
        for city, posts in self.posts_by_city.items():
            if city.lower() in query.lower():
                return posts
        return []


class StubFlights:
    provider = "FR24"

    def __init__(self, live=None, summary=None):
        self.live = live or []
        self.summary = summary or []
        self.live_calls = 0
        self.summary_calls = 0
        self.count_calls = 0

    def live_positions(self, params, **kwargs):
        self.live_calls += 1
        return self.live

    def flight_summary(self, params, **kwargs):
        self.summary_calls += 1
        return self.summary

    def count_live(self, params, **kwargs):
        self.count_calls += 1
        return len(self.live)


class StubStaying:
    provider = "STAYING"

    def __init__(self, listings=None, windows=None, credits=5000.0):
        self.listings = listings or []
        self.windows = windows or [[], []]
        self._credits = credits
        self.availability_calls = 0

    def credits_available(self):
        return self._credits

    def search_listings(self, params, **kwargs):
        return SearchResult(records=self.listings, meta={"creditsCharged": 10})

    def availability(self, params, **kwargs):
        rows = self.windows[min(self.availability_calls, len(self.windows) - 1)]
        self.availability_calls += 1
        return SearchResult(records=rows, meta={"creditsCharged": 12})


class StubCars:
    provider = "PRICELINE"

    def __init__(self, near=None, baseline=None):
        self.near = near
        self.baseline = baseline
        self.calls = 0

    def search_rental_cars(self, params, **kwargs):
        self.calls += 1
        return self.near if self.calls % 2 == 1 else self.baseline


def post(url, domain,
         snippet="Ground crew staging near the Riverside Fairground"):
    from datetime import timedelta
    return {
        "url": url, "author": "reporter", "platform": "twitter",
        "source_domain": domain, "snippet": snippet,
        "observed_at": iso(utcnow() - timedelta(hours=2)),
    }


def decision(url, city, *, relevant=True, salience=0.9):
    return {
        "url": url, "relevant": relevant, "track": "AIRSHOW",
        "cities": [city] if city else [], "locations": [],
        "activity_type": "static display", "imminence_hours": 6,
        "salience": salience, "rationale": "judged",
    }


LISTINGS = [{"listing_id": f"L{i}", "platform": "airbnb", "name": f"Loft {i}"}
            for i in range(1, 6)]
NEAR = [{"listing_id": f"L{i}", "platform": "airbnb",
         "nights_offered": 3, "nights_available": 0} for i in range(1, 6)]
BASE = [{"listing_id": f"L{i}", "platform": "airbnb",
         "nights_offered": 3, "nights_available": 3} for i in range(1, 6)]


@pytest.fixture
def wiring(db, config):
    """A session with Phoenix, plus stub connectors for all four providers."""
    config["tipping"]["max_queries_per_city"] = 99
    config["triage"] = {"batch_size": 20}
    session = db.insert_session(label="AZ", expand_cities=False,
                                tracks=["AIRSHOW"], config=config)
    city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
    db.insert_key_location(city, "Riverside Fairground",
                           location_type="FAIRGROUND")
    connectors = {
        "APIDIRECT": StubSocial({"Phoenix": [post("https://x.com/1", "x.com"),
                                             post("https://apnews.com/2",
                                                  "apnews.com")]}),
        "FR24": StubFlights(
            live=[_normalise_live(f)
                  for f in fixture("fr24_live_positions.json")["data"]],
            summary=[_normalise_summary(f)
                     for f in fixture("fr24_flight_summary.json")["data"]],
        ),
        "STAYING": StubStaying(LISTINGS, [NEAR, BASE]),
        "PRICELINE": StubCars(
            parse_rental_car_response(fixture("priceline_cars_near.json")),
            parse_rental_car_response(fixture("priceline_cars.json")),
        ),
    }
    llm = FakeLLM([decision("https://x.com/1", "Phoenix"),
                   decision("https://apnews.com/2", "Phoenix")])
    return session, city, connectors, llm


def build(db, config, connectors, llm=None):
    return IterationOrchestrator(db, config, connectors, llm_client=llm,
                                 budget=BudgetGuard(db, config))


# ===========================================================================


class TestStagesRunEndToEnd:
    def test_a_full_iteration_reaches_every_source(self, db, config, wiring):
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        outcome = orch.run(iteration)

        assert outcome in ("COMPLETE", "PARTIAL")
        types = {q["source_type"] for q in db.get_queue(iteration)}
        assert {"SOCIAL", "FLIGHT_LIVE", "LODGING", "CAR"} <= types

        # Signals from all four families.
        families = {s["signal_type"] for s in db.all(
            "SELECT DISTINCT signal_type FROM signals WHERE iteration_id = ?",
            (iteration,))}
        assert families == {"SOCIAL", "FLIGHT", "LODGING", "CAR"}

    def test_stages_are_recorded_in_order(self, db, config, wiring):
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.run(iteration)
        agents = {r["agent"] for r in db.get_agent_runs(iteration)}
        assert {"CollectionAgent", "TriageAgent"} <= agents

    def test_the_iteration_is_closed_with_an_outcome(self, db, config, wiring):
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.run(iteration)
        row = db.get_iteration(iteration)
        assert row["outcome"] in ("COMPLETE", "PARTIAL")
        assert row["finished_at"]
        assert row["stage"] == "COMPLETE"

    def test_the_budget_envelope_is_planned_before_spending(
        self, db, config, wiring
    ):
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        assert db.get_iteration(iteration)["budget_plan_json"]

    def test_remote_credit_balance_overrides_the_local_ledger(
        self, db, config, wiring
    ):
        """Staying's /account is free and authoritative; the local per-call
        accounting was measured under-counting by an order of magnitude."""
        session, city, connectors, llm = wiring
        connectors["STAYING"]._credits = 42.0
        orch = build(db, config, connectors, llm)
        orch.start(session)
        assert orch.budget.remaining("STAYING")["remaining"] == pytest.approx(42.0)


class TestCityAdmissionGate:
    def test_unlisted_city_is_refused_and_generates_no_queries(
        self, db, config, wiring
    ):
        """expand_cities=false: the refusal is recorded, not silent."""
        session, city, connectors, llm = wiring
        connectors["APIDIRECT"] = StubSocial(
            {"Phoenix": [post("https://x.com/t1", "x.com")]}
        )
        orch = build(db, config, connectors,
                     FakeLLM([decision("https://x.com/t1", "Tucson")]))
        iteration = orch.start(session)
        orch.run(iteration)

        assert db.find_city(session, "tucson") is None
        assert db.decision_counts(iteration).get("CITY_NOT_ADMITTED") == 1
        # No signal, therefore no tip, therefore no queries for that city.
        assert db.scalar(
            "SELECT COUNT(*) FROM signals WHERE signal_type = 'SOCIAL'") == 0
        assert not [q for q in db.get_queue(iteration)
                    if q["source_type"] in ("FLIGHT_LIVE", "LODGING", "CAR")]

    def test_corroborated_city_is_admitted_and_tipped(self, db, config, wiring):
        """expand_cities=true with two distinct domains admits, then tips the
        full R1–R5 set for the newly admitted city."""
        session, city, connectors, llm = wiring
        db.close_session(session)
        expand = db.insert_session(label="expand", expand_cities=True,
                                    tracks=["AIRSHOW"], config=config)
        db.insert_city(expand, "Phoenix", canonical="phoenix", state="AZ")
        connectors["APIDIRECT"] = StubSocial({
            "Phoenix": [post("https://x.com/t1", "x.com"),
                        post("https://apnews.com/t2", "apnews.com")]
        })
        orch = build(db, config, connectors, FakeLLM([
            decision("https://x.com/t1", "Tucson"),
            decision("https://apnews.com/t2", "Tucson"),
        ]))
        iteration = orch.start(expand)
        orch.run(iteration)

        admitted = db.find_city(expand, "tucson")
        assert admitted is not None
        assert admitted["admitted_by"] == "TIP"
        tipped = {q["source_type"] for q in db.get_queue(iteration)
                  if q["city_id"] == admitted["city_id"]}
        # Flights and cars key on the airport, which geo.py can resolve. Lodging
        # cannot: a tip-admitted city has no operator-registered facility to
        # anchor to, and an unanchored lodging search would be scored as though
        # it were near one.
        assert {"FLIGHT_LIVE", "CAR"} <= tipped
        assert "LODGING" not in tipped

    def test_a_city_without_key_locations_records_why_lodging_was_skipped(
        self, db, config
    ):
        """Silence would be indistinguishable from a bug in the tipping rules."""
        config["tipping"]["max_queries_per_city"] = 99
        session = db.insert_session(label="AZ", tracks=["AIRSHOW"])
        city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        iteration = db.insert_iteration(session)
        QueueAgent(db, config).tip_from_social(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", state="AZ", signal_id=None, tracks=["AIRSHOW"],
        )
        refusal = [d for d in db.get_queue_decisions(iteration)
                   if d["outcome"] == "NO_MAPPING" and d["source_type"] == "LODGING"]
        assert len(refusal) == 1
        assert "no key locations" in refusal[0]["detail"]


class TestEscalationRules:
    def test_history_is_bought_only_after_live_records_arrive(
        self, db, config, wiring
    ):
        """R2. flight-summary is the most expensive call in the system, so it
        is not purchased speculatively."""
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.run(iteration)

        flights = connectors["FR24"]
        assert flights.live_calls >= 1
        assert flights.summary_calls >= 1
        rows = [q for q in db.get_queue(iteration)
                if q["source_type"] == "FLIGHT_HISTORY"]
        assert rows and rows[0]["rule_code"] == "R2_FLIGHT_HIST"

    def test_no_live_records_means_no_history_purchase(self, db, config, wiring):
        session, city, connectors, llm = wiring
        connectors["FR24"] = StubFlights(live=[], summary=[])
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.run(iteration)
        assert connectors["FR24"].summary_calls == 0
        assert not [q for q in db.get_queue(iteration)
                    if q["source_type"] == "FLIGHT_HISTORY"]

    def test_history_resolves_live_categories(self, db, config, wiring):
        """Without this the flight signal stays AMBIGUOUS and is scored at the
        lowest weight its filter could have earned."""
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.run(iteration)
        confirmed = [s for s in db.signals_by_type(iteration, "FLIGHT")
                     if s["category_confidence"] == "CONFIRMED"]
        assert confirmed
        assert any(s["flight_category"] == "M" for s in confirmed)

    def test_history_is_escalated_per_destination_not_per_mapped_airport(
        self, db, config
    ):
        """A city with three mapped airports must not buy three 48-hour windows
        to resolve records that all came from one of them."""
        config["tipping"]["max_queries_per_city"] = 99
        session = db.insert_session(label="LA", tracks=["AIRSHOW"])
        city = db.insert_city(session, "Los Angeles", canonical="los angeles")
        iteration = db.insert_iteration(session)
        # Three mapped airports, but every record landed at LAX.
        for index in range(3):
            db.insert_signal(
                iteration_id=iteration, signal_type="FLIGHT", city_id=city,
                observed_at=iso(utcnow()), fr24_id=f"f{index}", dest_iata="LAX",
                flight_category="AMBIGUOUS", category_confidence="AMBIGUOUS",
            )
        QueueAgent(db, config).run_escalate(iteration, session)
        history = [q for q in db.get_queue(iteration)
                   if q["source_type"] == "FLIGHT_HISTORY"]
        assert len(history) == 1

    def test_a_flight_signal_tips_bookings_when_none_are_queued(
        self, db, config
    ):
        """R6: the surge signature can enter through the flight door."""
        config["tipping"]["max_queries_per_city"] = 99
        session = db.insert_session(label="AZ", tracks=["AIRSHOW"])
        city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        db.insert_key_location(city, "Riverside Fairground")
        iteration = db.insert_iteration(session)
        db.insert_signal(
            iteration_id=iteration, signal_type="FLIGHT", city_id=city,
            observed_at=iso(utcnow()), fr24_id="f1", dest_iata="PHX",
            flight_category="M", category_confidence="CONFIRMED",
        )
        counts = QueueAgent(db, config).run_escalate(iteration, session)
        assert counts.get("LODGING") == 1
        assert counts.get("CAR") == 1

    def test_the_count_tripwire_stays_off_unless_enabled(self, db, config, wiring):
        """Both /count endpoints return 403 on Explorer."""
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.run(iteration)
        assert connectors["FR24"].count_calls == 0

    def test_the_count_tripwire_can_be_re_enabled_for_a_higher_tier(
        self, db, config, wiring
    ):
        session, city, connectors, llm = wiring
        config["flightradar"]["use_count_tripwire"] = True
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.run(iteration)
        assert connectors["FR24"].count_calls >= 1


class TestFailureIsolation:
    def test_one_source_failing_does_not_discard_the_others(
        self, db, config, wiring
    ):
        """The whole reason failure is per-agent rather than per-iteration."""
        from surge_iw.base.connector import AuthError

        session, city, connectors, llm = wiring
        original = connectors["FR24"]

        class BrokenFlights(StubFlights):
            def live_positions(self, params, **kwargs):
                raise AuthError("401 expired", provider="FR24", status_code=401)

        connectors["FR24"] = BrokenFlights()
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        outcome = orch.run(iteration)

        assert outcome == "PARTIAL"
        # Lodging and cars still collected.
        families = {s["signal_type"] for s in db.all(
            "SELECT DISTINCT signal_type FROM signals WHERE iteration_id = ?",
            (iteration,))}
        assert "LODGING" in families and "CAR" in families
        # And the flight gap is on the record.
        assert "FLIGHT_LIVE" in db.unreliable_source_types(iteration, city)
        assert original is not connectors["FR24"]

    def test_a_missing_llm_degrades_rather_than_crashing(self, db, config, wiring):
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm=None)
        iteration = orch.start(session)
        outcome = orch.run(iteration)
        assert outcome == "PARTIAL"
        assert any("TRIAGING" in n for n in db.degradation_notes(iteration))

    def test_a_critical_stage_failure_stops_the_run(self, db, config):
        """Seeding produces the queue everything else reads from."""
        session = db.insert_session(label="empty", tracks=["AIRSHOW"])
        db.insert_city(session, "Phoenix", canonical="phoenix")
        config["tipping"]["max_queries_per_iteration"] = 0
        orch = build(db, config, {"APIDIRECT": StubSocial()})
        iteration = orch.start(session)
        assert orch.run(iteration) == "FAILED"
        assert db.get_iteration(iteration)["stage"] == "FAILED"

    def test_degradations_from_agents_survive_into_the_outcome(
        self, db, config, wiring
    ):
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm=None)
        iteration = orch.start(session)
        orch.run(iteration)
        notes = db.degradation_notes(iteration)
        assert any("Triage" in n or "TRIAGING" in n for n in notes)

    def test_a_clean_run_reports_complete(self, db, config, wiring):
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        outcome = orch.run(iteration)
        gaps = db.all(
            "SELECT * FROM query_queue WHERE iteration_id = ? AND status IN "
            "('FAILED','SKIPPED_BUDGET','SKIPPED_NO_MAPPING')", (iteration,))
        assert (outcome == "COMPLETE") == (not gaps)


class TestResume:
    def test_resume_reclaims_queries_stranded_in_progress(
        self, db, config, wiring
    ):
        """A killed process leaves rows claimed but never executed. Without a
        reset they are invisible to claim_next_query forever, and the resume
        would silently collect less than the original run."""
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        QueueAgent(db, config).run_seed(iteration, session)

        claimed = db.claim_next_query(iteration, ["SOCIAL"])
        assert claimed["status"] == "IN_PROGRESS"      # claimed, never executed

        orch.resume(iteration, "COLLECTING_SOCIAL")
        row = db.one("SELECT * FROM query_queue WHERE query_id = ?",
                     (claimed["query_id"],))
        assert row["status"] == "COMPLETE"

    def test_resume_does_not_re_execute_completed_queries(
        self, db, config, wiring
    ):
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.run(iteration)
        social_calls = connectors["APIDIRECT"].calls

        orch.resume(iteration, "COLLECTING_SOCIAL")
        assert connectors["APIDIRECT"].calls == social_calls

    def test_resume_does_not_re_triage(self, db, config, wiring):
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.run(iteration)
        before = db.scalar("SELECT COUNT(*) FROM triage_decisions")

        orch.resume(iteration, "TRIAGING")
        assert db.scalar("SELECT COUNT(*) FROM triage_decisions") == before

    def test_resume_rejects_an_unknown_stage(self, db, config, wiring):
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        with pytest.raises(Exception):
            orch.resume(iteration, "NOT_A_STAGE")


class TestStartValidation:
    def test_a_session_without_cities_is_refused(self, db, config):
        session = db.insert_session(label="bare", tracks=["AIRSHOW"])
        orch = build(db, config, {})
        with pytest.raises(ValueError, match="no cities"):
            orch.start(session)

    def test_a_closed_session_is_refused(self, db, config):
        session = db.insert_session(label="done", tracks=["AIRSHOW"])
        db.insert_city(session, "Phoenix", canonical="phoenix")
        db.close_session(session)
        orch = build(db, config, {})
        with pytest.raises(ValueError, match="CLOSED"):
            orch.start(session)

    def test_iterations_are_numbered_per_session(self, db, config, wiring):
        """The first must be CLOSED before the second may start (8.7a) — this
        test is about `seq`, not about whether two runs may overlap."""
        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        first = orch.start(session)
        db.finish_iteration(first, outcome="COMPLETE")
        second = orch.start(session)
        assert db.get_iteration(first)["seq"] == 1
        assert db.get_iteration(second)["seq"] == 2


class TestRetention:
    def test_retention_runs_at_the_end_of_every_iteration(
        self, db, config, wiring
    ):
        """FR24's licence requires deletion 30 days after receipt. Running it
        here means a deployment that never sets up a timer still complies."""
        from datetime import timedelta

        session, city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="FLIGHT_LIVE",
            endpoint="/api/live/flight-positions/full", params={},
            dedup_key="stale-key",
        )
        raw_id = db.insert_raw_result(
            query_id=query_id, iteration_id=iteration, source_type="FLIGHT_LIVE",
            provider="FR24", payload=[{"a": 1}], retention_days=30,
        )
        db._exec("UPDATE raw_results SET purge_after = ? WHERE raw_id = ?",
                 (iso(utcnow() - timedelta(days=1)), raw_id))

        orch.run(iteration)
        assert db.get_raw_result(raw_id) is None
