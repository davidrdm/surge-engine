"""The queue guards, and the four invariants the Phase 1 gate names.

The queue is where spend and fan-out are decided. The previous implementation
handed four tools to an LLM and let the model decide how many paid API calls to
make; these invariants are the replacement for trusting it.

Property tests use Hypothesis to generate arbitrary tipping workloads, because
the failure mode that matters is not a specific input — it is a combination
nobody thought to write down.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conftest import ANCHOR, REFERENCE_MISSION
from surge_iw.agents.queueing import EP_NEWS, EP_TWITTER, QueueAgent, dedup_key
from surge_iw.config import load_config
from surge_iw.db.database import SurgeDB, iso, utcnow
from surge_iw.services.budget import BudgetGuard


def make_agent(db, config, budget=None) -> QueueAgent:
    return QueueAgent(db, config, budget=budget)


@pytest.fixture
def city(db: SurgeDB, session: int) -> int:
    return db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")


@pytest.fixture
def location(db: SurgeDB, city: int) -> int:
    return db.insert_key_location(
        city, "Riverside Fairground", location_type="FAIRGROUND"
    )


class TestDedupKey:
    def test_parameter_order_does_not_change_identity(self):
        a = dedup_key("/x", {"query": "phoenix", "pages": 2})
        b = dedup_key("/x", {"pages": 2, "query": "phoenix"})
        assert a == b

    def test_different_params_give_different_keys(self):
        assert dedup_key("/x", {"q": "a"}) != dedup_key("/x", {"q": "b"})

    def test_different_endpoints_give_different_keys(self):
        assert dedup_key("/x", {"q": "a"}) != dedup_key("/y", {"q": "a"})


class TestInvariantNoDuplicates:
    """Invariant 1: no two queue rows in one iteration share a dedup_key.

    Enforced by the idx_qq_dedup UNIQUE index rather than by a prior SELECT, so
    it holds even if two enqueues race.
    """

    def test_second_identical_enqueue_is_refused(
        self, db, config, session, iteration, city
    ):
        agent = make_agent(db, config)
        params = {"query": "phoenix air show", "pages": 2, "sort_by": "most_recent"}
        first = agent.enqueue(
            iteration_id=iteration, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params=params, rule_code="T", city_id=city,
        )
        second = agent.enqueue(
            iteration_id=iteration, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params=params, rule_code="T", city_id=city,
        )
        assert first is not None
        assert second is None
        assert db.decision_counts(iteration)["DEDUPED"] == 1

    def test_refusal_is_recorded_not_silent(
        self, db, config, session, iteration, city
    ):
        """A query that did not happen must be as auditable as one that did."""
        agent = make_agent(db, config)
        params = {"query": "x"}
        for _ in range(3):
            agent.enqueue(
                iteration_id=iteration, session_id=session, source_type="SOCIAL",
                endpoint=EP_TWITTER, params=params, rule_code="T", city_id=city,
            )
        rows = db.get_queue_decisions(iteration)
        assert len(rows) == 3
        assert [r["outcome"] for r in rows] == ["ENQUEUED", "DEDUPED", "DEDUPED"]
        assert all(r["detail"] for r in rows)


class TestInvariantFanOutIsBounded:
    """Invariants 2: per-iteration and per-city caps hold for any workload."""

    def test_iteration_cap_holds(self, db, config, session, iteration, city):
        config = load_config(None)
        config["tipping"]["max_queries_per_iteration"] = 5
        config["tipping"]["max_queries_per_city"] = 999
        agent = make_agent(db, config)
        for i in range(20):
            agent.enqueue(
                iteration_id=iteration, session_id=session, source_type="SOCIAL",
                endpoint=EP_TWITTER, params={"query": f"q{i}"}, rule_code="T",
                city_id=city,
            )
        assert db.count_queued(iteration) == 5
        assert db.decision_counts(iteration)["CAP_ITERATION"] == 15

    def test_city_cap_holds(self, db, config, session, iteration, city):
        config = load_config(None)
        config["tipping"]["max_queries_per_city"] = 3
        agent = make_agent(db, config)
        for i in range(10):
            agent.enqueue(
                iteration_id=iteration, session_id=session, source_type="SOCIAL",
                endpoint=EP_TWITTER, params={"query": f"q{i}"}, rule_code="T",
                city_id=city,
            )
        assert db.count_queued_for_city(iteration, city) == 3

    @settings(
        max_examples=40, deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        n_attempts=st.integers(min_value=0, max_value=60),
        iteration_cap=st.integers(min_value=1, max_value=15),
        city_cap=st.integers(min_value=1, max_value=15),
    )
    def test_caps_hold_for_arbitrary_workloads(
        self, n_attempts, iteration_cap, city_cap
    ):
        config = load_config(None)
        config["tipping"]["max_queries_per_iteration"] = iteration_cap
        config["tipping"]["max_queries_per_city"] = city_cap
        with SurgeDB(":memory:", mission=REFERENCE_MISSION) as db:
            session = db.insert_session()
            iteration = db.insert_iteration(session, anchor_at=ANCHOR)
            city = db.insert_city(session, "Phoenix", canonical="phoenix")
            agent = make_agent(db, config)
            for i in range(n_attempts):
                agent.enqueue(
                    iteration_id=iteration, session_id=session,
                    source_type="SOCIAL", endpoint=EP_TWITTER,
                    params={"query": f"q{i}"}, rule_code="T", city_id=city,
                )
            queued = db.count_queued(iteration)
            assert queued <= iteration_cap
            assert queued <= city_cap
            assert db.count_queued_for_city(iteration, city) <= city_cap
            # Nothing vanishes: every attempt produced exactly one decision row.
            assert sum(db.decision_counts(iteration).values()) == n_attempts


class TestInvariantTipDepthIsBounded:
    """Invariant 3: no queue row exceeds max_tip_depth.

    Stops social -> flight -> lodging -> ... from chaining without end.
    """

    def test_depth_beyond_the_cap_is_refused(
        self, db, config, session, iteration, city
    ):
        agent = make_agent(db, config)
        max_depth = config["tipping"]["max_tip_depth"]
        ok = agent.enqueue(
            iteration_id=iteration, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params={"query": "a"}, rule_code="T",
            city_id=city, tip_depth=max_depth,
        )
        too_deep = agent.enqueue(
            iteration_id=iteration, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params={"query": "b"}, rule_code="T",
            city_id=city, tip_depth=max_depth + 1,
        )
        assert ok is not None
        assert too_deep is None
        assert db.decision_counts(iteration)["CAP_DEPTH"] == 1

    @settings(
        max_examples=30, deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(depths=st.lists(st.integers(min_value=0, max_value=12), max_size=25))
    def test_no_stored_row_exceeds_the_cap(self, depths):
        config = load_config(None)
        max_depth = config["tipping"]["max_tip_depth"]
        with SurgeDB(":memory:", mission=REFERENCE_MISSION) as db:
            session = db.insert_session()
            iteration = db.insert_iteration(session, anchor_at=ANCHOR)
            city = db.insert_city(session, "Phoenix", canonical="phoenix")
            agent = make_agent(db, config)
            for i, depth in enumerate(depths):
                agent.enqueue(
                    iteration_id=iteration, session_id=session,
                    source_type="SOCIAL", endpoint=EP_TWITTER,
                    params={"query": f"q{i}"}, rule_code="T", city_id=city,
                    tip_depth=depth,
                )
            stored = db.all(
                "SELECT tip_depth FROM query_queue WHERE iteration_id = ?",
                (iteration,),
            )
            assert all(r["tip_depth"] <= max_depth for r in stored)


class TestInvariantCooldown:
    """Invariant 4: an identical query does not re-run inside the cooldown."""

    def test_recently_executed_query_is_refused_in_a_later_iteration(
        self, db, config, session, city
    ):
        agent = make_agent(db, config)
        first_iter = db.insert_iteration(session, anchor_at=ANCHOR)
        params = {"query": "phoenix air show"}
        query_id = agent.enqueue(
            iteration_id=first_iter, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params=params, rule_code="T", city_id=city,
        )
        db.complete_query(query_id, result_count=3)

        second_iter = db.insert_iteration(session, anchor_at=ANCHOR)
        again = agent.enqueue(
            iteration_id=second_iter, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params=params, rule_code="T", city_id=city,
        )
        assert again is None
        assert db.decision_counts(second_iter)["COOLDOWN"] == 1

    def test_cooldown_expires(self, db, config, session, city):
        agent = make_agent(db, config)
        first_iter = db.insert_iteration(session, anchor_at=ANCHOR)
        params = {"query": "phoenix air show"}
        query_id = agent.enqueue(
            iteration_id=first_iter, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params=params, rule_code="T", city_id=city,
        )
        stale = utcnow() - timedelta(
            minutes=config["tipping"]["cooldown_minutes"] + 10
        )
        db._exec(
            "UPDATE query_queue SET status='COMPLETE', executed_at = ? "
            "WHERE query_id = ?",
            (iso(stale), query_id),
        )
        second_iter = db.insert_iteration(session, anchor_at=ANCHOR)
        assert agent.enqueue(
            iteration_id=second_iter, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params=params, rule_code="T", city_id=city,
        ) is not None

    def test_a_failed_query_also_starts_the_cooldown(
        self, db, config, session, city
    ):
        """A broken endpoint should not be retried on a tight loop either."""
        agent = make_agent(db, config)
        first_iter = db.insert_iteration(session, anchor_at=ANCHOR)
        params = {"query": "phoenix air show"}
        query_id = agent.enqueue(
            iteration_id=first_iter, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params=params, rule_code="T", city_id=city,
        )
        db.fail_query(query_id, "401 Unauthorized")

        second_iter = db.insert_iteration(session, anchor_at=ANCHOR)
        assert agent.enqueue(
            iteration_id=second_iter, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params=params, rule_code="T", city_id=city,
        ) is None


class TestBudgetRefusal:
    def test_exhausted_budget_refuses_and_records_the_reason(
        self, db, config, session, iteration, city
    ):
        budget = BudgetGuard(db, config)
        db.set_budget("APIDIRECT", None, "MONTH", 0.0)
        agent = make_agent(db, config, budget=budget)
        assert agent.enqueue(
            iteration_id=iteration, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params={"query": "x"}, rule_code="T",
            city_id=city,
        ) is None
        row = db.one(
            "SELECT * FROM queue_decisions WHERE iteration_id = ? "
            "AND outcome = 'BUDGET_EXHAUSTED'",
            (iteration,),
        )
        assert row is not None
        assert "MONTHLY_QUOTA_EXHAUSTED" in row["detail"]

    def test_hard_stop_reserves_the_tail_for_imminent_work(
        self, db, config, session, iteration, city
    ):
        """Near the cliff, only high-priority tips may spend."""
        budget = BudgetGuard(db, config)
        db.set_budget("APIDIRECT", None, "MONTH", 100.0)
        db.record_api_call(
            provider="APIDIRECT", endpoint=EP_TWITTER, units=95.0
        )
        agent = make_agent(db, config, budget=budget)
        low_priority = agent.enqueue(
            iteration_id=iteration, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params={"query": "routine"}, rule_code="R0_SEED",
            priority=40, city_id=city,
        )
        urgent = agent.enqueue(
            iteration_id=iteration, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params={"query": "urgent"}, rule_code="R1",
            priority=10, city_id=city,
        )
        assert low_priority is None
        assert urgent is not None

    def test_dry_run_never_refuses_on_budget(
        self, db, config, session, iteration, city
    ):
        config["dry_run"] = True
        db.set_budget("APIDIRECT", None, "MONTH", 0.0)
        agent = make_agent(db, config, budget=BudgetGuard(db, config))
        assert agent.enqueue(
            iteration_id=iteration, session_id=session, source_type="SOCIAL",
            endpoint=EP_TWITTER, params={"query": "x"}, rule_code="T",
            city_id=city,
        ) is not None


class TestCityAdmission:
    def test_constrained_mode_refuses_an_unlisted_city(
        self, db, config, session, iteration
    ):
        agent = make_agent(db, config)
        assert agent.admit_city(
            iteration_id=iteration, session_id=session, name="Tucson",
            signals=[{"source_domain": "a.com", "salience": 0.9}],
            expand_cities=False,
        ) is None
        row = db.one(
            "SELECT * FROM queue_decisions WHERE outcome = 'CITY_NOT_ADMITTED'"
        )
        assert row is not None
        assert "expand_cities=false" in row["detail"]

    def test_expansion_needs_two_independent_domains(
        self, db, config, session, iteration
    ):
        """One viral post must not steer collection into a new city."""
        agent = make_agent(db, config)
        assert agent.admit_city(
            iteration_id=iteration, session_id=session, name="Tucson",
            signals=[
                {"source_domain": "a.com", "salience": 0.9},
                {"source_domain": "a.com", "salience": 0.95},
            ],
            expand_cities=True,
        ) is None

    def test_expansion_admits_a_corroborated_city(
        self, db, config, session, iteration
    ):
        agent = make_agent(db, config)
        city_id = agent.admit_city(
            iteration_id=iteration, session_id=session, name="Tucson, AZ",
            signals=[
                {"source_domain": "a.com", "salience": 0.9},
                {"source_domain": "b.org", "salience": 0.8},
            ],
            expand_cities=True,
        )
        assert city_id is not None
        row = db.one("SELECT * FROM cities WHERE city_id = ?", (city_id,))
        assert row["is_seed"] == 0
        assert row["admitted_by"] == "TIP"
        assert row["state"] == "AZ"
        assert row["admitted_iteration"] == iteration

    def test_low_salience_is_refused(self, db, config, session, iteration):
        agent = make_agent(db, config)
        assert agent.admit_city(
            iteration_id=iteration, session_id=session, name="Tucson",
            signals=[
                {"source_domain": "a.com", "salience": 0.1},
                {"source_domain": "b.org", "salience": 0.2},
            ],
            expand_cities=True,
        ) is None

    def test_expansion_cap_holds(self, db, config, session, iteration):
        config["tipping"]["max_expanded_cities"] = 2
        agent = make_agent(db, config)
        signals = [
            {"source_domain": "a.com", "salience": 0.9},
            {"source_domain": "b.org", "salience": 0.9},
        ]
        admitted = [
            agent.admit_city(
                iteration_id=iteration, session_id=session, name=name,
                signals=signals, expand_cities=True,
            )
            for name in ("Tucson", "Mesa", "Denver", "Reno")
        ]
        assert sum(1 for a in admitted if a is not None) == 2
        assert db.count_expanded_cities(session) == 2

    def test_an_already_known_city_is_returned_not_re_admitted(
        self, db, config, session, iteration
    ):
        existing = db.insert_city(session, "Phoenix", canonical="phoenix")
        agent = make_agent(db, config)
        assert agent.admit_city(
            iteration_id=iteration, session_id=session, name="Phoenix, AZ",
            signals=[], expand_cities=False,
        ) == existing


class TestSocialQueryBuilding:
    def test_news_and_twitter_get_different_parameters(self, db, config):
        """API Direct's endpoints do not share a parameter vocabulary; the old
        connector sent one shape to all of them."""
        agent = make_agent(db, config)
        queries = dict(
            (endpoint, params)
            for _stream, endpoint, params in agent.build_social_queries(
                "Phoenix", "AZ", ["AIRSHOW"]
            )
        )
        assert "time_published" in queries[EP_NEWS]
        assert "limit" in queries[EP_NEWS]
        assert "pages" not in queries[EP_NEWS]
        assert "pages" in queries[EP_TWITTER]
        assert "time_published" not in queries[EP_TWITTER]

    def test_query_text_is_truncated_to_the_api_limit(self, db, config):
        agent = make_agent(db, config)
        for _stream, _endpoint, params in agent.build_social_queries(
            "X" * 600, "AZ", ["AIRSHOW", "CONCERT_TOUR"]
        ):
            assert len(params["query"]) <= 500

    def test_sentiment_is_off_by_default(self, db, config):
        """+$0.001/page for an eight-emotion vector that is not an indicator."""
        agent = make_agent(db, config)
        for _stream, _endpoint, params in agent.build_social_queries(
            "Phoenix", "AZ", ["AIRSHOW"]
        ):
            assert "get_sentiment" not in params

    def test_both_tracks_produce_distinct_lexicons(self, db, config):
        agent = make_agent(db, config)
        airshow = {
            p["query"] for _s, _e, p in agent.build_social_queries(
                "Phoenix", "AZ", ["AIRSHOW"])
        }
        concert = {
            p["query"] for _s, _e, p in agent.build_social_queries(
                "Phoenix", "AZ", ["CONCERT_TOUR"])
        }
        assert airshow and concert
        assert not (airshow & concert)


class TestTipping:
    def test_social_signal_tips_all_three_other_sources(
        self, db, config, session, iteration, city, location
    ):
        agent = make_agent(db, config)
        config["tipping"]["max_queries_per_city"] = 99
        enqueued = agent.tip_from_social(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", state="AZ", signal_id=None, tracks=["AIRSHOW"],
        )
        assert enqueued["FLIGHT_LIVE"] == 1         # Phoenix maps to PHX only
        assert enqueued["LODGING"] == 1
        assert enqueued["CAR"] == 1
        # History is deliberately NOT tipped here — see the test below.
        assert "FLIGHT_HISTORY" not in enqueued

    def test_live_positions_are_cost_capped_by_limit(
        self, db, config, session, iteration, city, location
    ):
        """FR24 bills per record RETURNED, and `limit` is the only cost control
        available on flight-positions."""
        agent = make_agent(db, config)
        agent.tip_from_social(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", state="AZ", signal_id=None, tracks=["AIRSHOW"],
        )
        row = db.one(
            "SELECT params_json FROM query_queue WHERE source_type = 'FLIGHT_LIVE'"
        )
        assert json.loads(row["params_json"])["limit"] == 20

    def test_expensive_history_is_not_tipped_speculatively(
        self, db, config, session, iteration, city, location
    ):
        """flight-summary is the only source of a real category but also the
        most expensive call in the system — measured at 60 credits for a single
        24-hour query. It is only worth paying for once a live record exists
        whose category needs resolving."""
        agent = make_agent(db, config)
        agent.tip_from_social(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", state="AZ", signal_id=None, tracks=["AIRSHOW"],
        )
        types = {r["source_type"] for r in db.get_queue(iteration)}
        assert "FLIGHT_LIVE" in types
        assert "FLIGHT_HISTORY" not in types

    def test_history_is_enqueued_once_live_records_exist(
        self, db, config, session, iteration, city
    ):
        agent = make_agent(db, config)
        query_id = agent.escalate_to_history(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", iata="PHX", live_record_count=2,
            signal_id=None, tracks=["AIRSHOW"],
        )
        assert query_id is not None
        row = db.one("SELECT * FROM query_queue WHERE query_id = ?", (query_id,))
        assert row["source_type"] == "FLIGHT_HISTORY"
        assert row["tip_depth"] == 2
        # The window bounds the cost here; `limit` does not apply to
        # flight-summary.
        params = json.loads(row["params_json"])
        assert "flight_datetime_from" in params
        assert "limit" not in params

    def test_no_live_records_means_no_history_purchase(
        self, db, config, session, iteration, city
    ):
        agent = make_agent(db, config)
        assert agent.escalate_to_history(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", iata="PHX", live_record_count=0,
            signal_id=None, tracks=["AIRSHOW"],
        ) is None
        assert not db.all(
            "SELECT 1 FROM query_queue WHERE source_type = 'FLIGHT_HISTORY'"
        )

    def test_count_tripwire_is_off_by_default(self, db, config):
        """Both /count endpoints return 403 on this subscription. The code path
        is retained for a higher tier but must not be the default."""
        assert config["flightradar"]["use_count_tripwire"] is False

    def test_count_tripwire_can_be_re_enabled(
        self, db, config, session, iteration, city, location
    ):
        config["flightradar"]["use_count_tripwire"] = True
        agent = make_agent(db, config)
        enqueued = agent.tip_from_social(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", state="AZ", signal_id=None, tracks=["AIRSHOW"],
        )
        assert enqueued["FLIGHT_COUNT"] == 1
        assert "FLIGHT_LIVE" not in enqueued

    def test_tripwire_below_threshold_does_not_buy_full_records(
        self, db, config, session, iteration, city
    ):
        agent = make_agent(db, config)
        assert agent.escalate_flight_count(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", iata="PHX", record_count=0, signal_id=None,
            tracks=["AIRSHOW"],
        ) is None
        assert not db.all(
            "SELECT 1 FROM query_queue WHERE source_type = 'FLIGHT_LIVE'"
        )

    def test_tripwire_above_threshold_buys_full_records(
        self, db, config, session, iteration, city
    ):
        agent = make_agent(db, config)
        query_id = agent.escalate_flight_count(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", iata="PHX", record_count=4, signal_id=None,
            tracks=["AIRSHOW"],
        )
        assert query_id is not None
        row = db.one("SELECT * FROM query_queue WHERE query_id = ?", (query_id,))
        assert row["source_type"] == "FLIGHT_LIVE"
        assert row["tip_depth"] == 2

    def test_flight_categories_differ_by_track(self, db, config, mission):
        """Each track asks for the categories ITS mission named, and one call
        serves several tracks by asking for the union."""
        agent = make_agent(db, config)
        for track in mission.tracks:
            asked = agent._flight_params("PHX", [track])["categories"]
            assert asked == ",".join(sorted(mission.flight_categories[track]))

        # A track the mission scored zero for military does not ask for it.
        quiet = [t for t in mission.tracks
                 if "M" not in mission.flight_categories[t]]
        assert quiet, "the reference mission needs a track that skips M"
        assert "M" not in agent._flight_params("PHX", [quiet[0]])["categories"]

        # The union across tracks, because one call can serve them all.
        union = agent._flight_params("PHX", list(mission.tracks))["categories"]
        assert set(union.split(",")) == {
            code for t in mission.tracks
            for code in mission.flight_categories[t]}

    def test_airports_use_the_verified_inbound_syntax(self, db, config):
        agent = make_agent(db, config)
        assert agent._flight_params("PHX", ["AIRSHOW"])["airports"] == "inbound:PHX"

    def test_unmapped_city_records_no_mapping_rather_than_failing(
        self, db, config, session, iteration
    ):
        """An unresolvable city must yield an absent source, never a wrong one."""
        unknown = db.insert_city(
            session, "Nowheresville", canonical="nowheresville"
        )
        agent = make_agent(db, config)
        enqueued = agent.tip_from_social(
            iteration_id=iteration, session_id=session, city_id=unknown,
            city_name="Nowheresville", state=None, signal_id=None,
            tracks=["AIRSHOW"],
        )
        assert "FLIGHT_COUNT" not in enqueued
        assert "CAR" not in enqueued
        # Each unavailable source states its own reason rather than being
        # inferable only from a missing row.
        refusals = {
            d["source_type"] for d in db.get_queue_decisions(iteration)
            if d["outcome"] == "NO_MAPPING"
        }
        assert refusals == {"FLIGHT_LIVE", "CAR", "LODGING"}

    def test_flight_escalation_does_not_re_tip_existing_bookings(
        self, db, config, session, iteration, city, location
    ):
        agent = make_agent(db, config)
        config["tipping"]["max_queries_per_city"] = 99
        agent.tip_from_social(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", state="AZ", signal_id=None, tracks=["AIRSHOW"],
        )
        before = db.count_queued(iteration)
        assert agent.tip_from_flight(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", state="AZ", signal_id=None, tracks=["AIRSHOW"],
        ) == {}
        assert db.count_queued(iteration) == before

    def test_flight_escalation_tips_bookings_when_none_exist(
        self, db, config, session, iteration, city, location
    ):
        """A surge can enter through the flight door with no social post at all."""
        agent = make_agent(db, config)
        config["tipping"]["max_queries_per_city"] = 99
        enqueued = agent.tip_from_flight(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", state="AZ", signal_id=None, tracks=["AIRSHOW"],
        )
        assert enqueued.get("LODGING") == 1
        assert enqueued.get("CAR") == 1


class TestCarParams:
    def test_dates_use_the_verified_format(self, db, config):
        """8.5: camelCase names, verified live against priceline-com2. The
        airport IATA code goes in directly — no location-resolution call."""
        agent = make_agent(db, config)
        params = agent._car_params("PHX")
        assert params["pickUpLocation"] == "PHX"
        assert params["dropOffLocation"] == "PHX"
        assert len(params["pickUpDate"]) == 10
        assert params["pickUpDate"][4] == "-"
        assert len(params["pickUpTime"]) == 5
        assert params["pickUpTime"][2] == ":"

    def test_all_six_required_params_are_sent_and_nothing_else(self, db, config):
        """The endpoint declares six required params and no optional ones.
        `currency` was accepted by the old wrapper and is not by this one."""
        params = make_agent(db, config)._car_params("PHX")
        assert set(params) == {"pickUpLocation", "dropOffLocation", "pickUpDate",
                               "pickUpTime", "dropOffDate", "dropOffTime"}


class TestSeeding:
    def test_seeding_covers_every_active_city(
        self, db, config, session, iteration
    ):
        config["tipping"]["max_queries_per_iteration"] = 999
        config["tipping"]["max_queries_per_city"] = 999
        db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        db.insert_city(session, "Tucson", canonical="tucson", state="AZ")
        agent = make_agent(db, config)
        counts = agent.run_seed(iteration, session)
        assert counts["seeded"] > 0
        cities_queried = {
            r["city_id"] for r in db.get_queue(iteration)
        }
        assert len(cities_queried) == 2

    def test_due_follow_ons_are_adopted(self, db, config, session, iteration, city):
        agent = make_agent(db, config)
        past = utcnow() - timedelta(hours=1)
        db.enqueue_query(
            session_id=session, iteration_id=None, source_type="SOCIAL",
            endpoint=EP_TWITTER, params={"query": "scheduled"},
            dedup_key="scheduled-key", origin="SCHEDULED", city_id=city,
            not_before=past,
        )
        counts = agent.run_seed(iteration, session)
        assert counts["adopted"] == 1
        row = db.one(
            "SELECT * FROM query_queue WHERE dedup_key = 'scheduled-key'"
        )
        assert row["iteration_id"] == iteration

    def test_future_follow_ons_are_not_adopted(
        self, db, config, session, iteration, city
    ):
        agent = make_agent(db, config)
        db.enqueue_query(
            session_id=session, iteration_id=None, source_type="SOCIAL",
            endpoint=EP_TWITTER, params={"query": "later"},
            dedup_key="later-key", origin="SCHEDULED", city_id=city,
            not_before=utcnow() + timedelta(hours=6),
        )
        assert agent.run_seed(iteration, session)["adopted"] == 0
