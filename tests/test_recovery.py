"""Phase 6a: telling an interrupted iteration from a running one, and closing it.

A crash is simulated **in-process** — stamp `owner_epoch_id`, then open a second
epoch against the same database. No subprocess, no signals, no clock control, no
sleeping. That testability is a direct consequence of choosing the epoch table
over a heartbeat, and is most of the argument for it.

The gate this file defends: a crash must never be able to launder incomplete
collection into full confidence. An interrupted iteration that is abandoned has
to produce an alert from what *was* collected, with the loss counted, the band
capped and the caveat naming the gap — not silence, and not false confidence.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

import test_orchestrator as T
from conftest import ANCHOR, REFERENCE_MISSION
from surge_iw.agents.orchestrator import (
    PIPELINE_STAGES, IterationOrchestrator, SessionHasOpenIteration,
)
from surge_iw.connectors.flightradar import _normalise_live, _normalise_summary
from surge_iw.connectors.priceline import parse_rental_car_response
from surge_iw.db import enums
from surge_iw.db.database import StrandedRunError, iso
from surge_iw.services.budget import BudgetGuard
from surge_iw.services.recovery import RecoveryRequired, RecoveryService
from surge_iw.services.stages import StageInspector, StageRollback
from test_collection import fixture
from test_triage import FakeLLM


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def service(db, config):
    return RecoveryService(db, config)


@pytest.fixture
def wiring(db, config):
    """A Phoenix session plus stub connectors, as in test_orchestrator."""
    config["tipping"]["max_queries_per_city"] = 99
    config["triage"] = {"batch_size": 20}
    session = db.insert_session(label="AZ", expand_cities=False,
                                tracks=["AIRSHOW"], config=config)
    city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
    db.insert_key_location(city, "Riverside Fairground",
                           location_type="FAIRGROUND")
    connectors = {
        "APIDIRECT": T.StubSocial({"Phoenix": [
            T.post("https://x.com/1", "x.com"),
            T.post("https://apnews.com/2", "apnews.com")]}),
        "FR24": T.StubFlights(
            live=[_normalise_live(f)
                  for f in fixture("fr24_live_positions.json")["data"]],
            summary=[_normalise_summary(f)
                     for f in fixture("fr24_flight_summary.json")["data"]]),
        "STAYING": T.StubStaying(T.LISTINGS, [T.NEAR, T.BASE]),
        "PRICELINE": T.StubCars(
            parse_rental_car_response(fixture("priceline_cars_near.json")),
            parse_rental_car_response(fixture("priceline_cars.json"))),
    }
    # A LIST of responses, each a full batch. `[a, b] * 20` would be ONE
    # response of forty items for two posts — which the pre-Phase-7 boundary
    # silently deduplicated and the strict one correctly rejects as duplicate
    # judgements.
    llm = FakeLLM(*[[T.decision("https://x.com/1", "Phoenix"),
                     T.decision("https://apnews.com/2", "Phoenix")]] * 20)
    return session, city, connectors, llm


def build(db, config, connectors, llm=None):
    return IterationOrchestrator(db, config, connectors, llm_client=llm,
                                 budget=BudgetGuard(db, config))


def crash(db, session, *, stage="COLLECTING_TIPPED", epoch_id, claimed=0,
          city_id=None):
    """An iteration left exactly as a killed process would leave it."""
    iteration = db.insert_iteration(session, anchor_at=ANCHOR,
                                    owner_epoch_id=epoch_id)
    for done in PIPELINE_STAGES[:PIPELINE_STAGES.index(stage)]:
        run_id = db.start_agent_run(iteration, "IterationOrchestrator", done)
        db.finish_agent_run(run_id, "COMPLETE")
    db.start_agent_run(iteration, "IterationOrchestrator", stage)
    db.start_agent_run(iteration, "CollectionAgent", stage)
    db.set_stage(iteration, stage)
    for index in range(claimed):
        db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="CAR",
            endpoint="/search-rental-car", params={"n": index},
            dedup_key=f"crash{iteration}-{index}", city_id=city_id)
    for _ in range(claimed):
        db.claim_next_query(iteration, ["CAR"])
    return iteration


# ===========================================================================
# Detection
# ===========================================================================


class TestDetection:
    def test_a_prior_epochs_unfinished_iteration_is_interrupted(
        self, db, service, session
    ):
        first = service.open_epoch("test")
        iteration = crash(db, session, epoch_id=first.epoch_id)

        second = service.open_epoch("test")
        assert second.interrupted == [iteration]
        row = db.get_iteration(iteration)
        assert row["interrupted_at"]
        assert row["interrupted_stage"] == "COLLECTING_TIPPED"

    def test_a_reconcile_never_touches_its_own_epochs_work(self, db, service,
                                                            session):
        """The money test. Marking a live run interrupted would strand it and,
        if acted on, buy its collection a second time."""
        epoch = service.open_epoch("test")
        live = crash(db, session, epoch_id=epoch.epoch_id)

        assert live not in [int(r["iteration_id"]) for r in
                            db.unfinished_iterations(not_owned_by=epoch.epoch_id)]
        assert db.get_iteration(live)["interrupted_at"] is None

    def test_an_unowned_iteration_is_never_reconciled(self, db, service,
                                                      session):
        """A row with no owner was never claimed by any process, so nothing can
        say it was interrupted. Guessing would be the same false positive."""
        service.open_epoch("test")
        iteration = db.insert_iteration(session)          # no epoch stamped
        second = service.open_epoch("test")
        assert second.interrupted == []
        assert db.get_iteration(iteration)["interrupted_at"] is None

    def test_a_finished_iteration_is_left_alone(self, db, service, session):
        first = service.open_epoch("test")
        iteration = db.insert_iteration(session, owner_epoch_id=first.epoch_id)
        db.finish_iteration(iteration, outcome="COMPLETE")

        second = service.open_epoch("test")
        assert second.interrupted == []
        assert db.get_iteration(iteration)["interrupted_at"] is None

    def test_reconciling_twice_stamps_once(self, db, service, session):
        first = service.open_epoch("test")
        iteration = crash(db, session, epoch_id=first.epoch_id)
        second = service.open_epoch("test")
        stamped = db.get_iteration(iteration)["interrupted_at"]

        third = service.open_epoch("test")
        assert third.interrupted == []
        assert third.outstanding == [iteration]
        assert db.get_iteration(iteration)["interrupted_at"] == stamped
        notes = db.degradation_notes(iteration)
        assert len([n for n in notes if "interrupted during" in n]) == 1
        assert second.interrupted == [iteration]

    def test_an_epoch_that_died_is_closed_unknown_with_no_ended_at(
        self, db, service, session
    ):
        """Nothing knows when a killed process died. Inventing a timestamp
        that later reads as fact is worse than admitting the gap."""
        first = service.open_epoch("test")
        second = service.open_epoch("test")
        assert second.closed_epochs == [first.epoch_id]

        row = db.get_epoch(first.epoch_id)
        assert row["shutdown_kind"] == "UNKNOWN"
        assert row["ended_at"] is None
        assert row["closed_by_epoch"] == second.epoch_id

    def test_a_clean_shutdown_is_recorded_as_such(self, db, service):
        epoch = service.open_epoch("test")
        db.close_epoch(epoch.epoch_id, "CLEAN")
        row = db.get_epoch(epoch.epoch_id)
        assert row["shutdown_kind"] == "CLEAN"
        assert row["ended_at"]
        assert db.open_epochs(before=epoch.epoch_id + 1) == []

    def test_a_timed_out_shutdown_records_what_it_stranded(self, db, service):
        epoch = service.open_epoch("test")
        db.close_epoch(epoch.epoch_id, "TIMEOUT", stranded=[7, 9])
        row = db.get_epoch(epoch.epoch_id)
        assert row["shutdown_kind"] == "TIMEOUT"
        assert json.loads(row["stranded_json"]) == [7, 9]

    def test_a_live_peer_is_refused_not_reconciled(self, db, service, session,
                                                   monkeypatch):
        """`workers=1` makes 'any prior epoch is dead' true by construction.
        Assert it rather than assume it, or the day someone drops that setting
        two processes quietly reconcile each other's live work."""
        first = service.open_epoch("test")
        iteration = crash(db, session, epoch_id=first.epoch_id)
        # Make the prior epoch look like a different, living process.
        db._exec("UPDATE process_epochs SET pid = ? WHERE epoch_id = ?",
                 (os_pid_of_a_living_process(), first.epoch_id))

        second = service.open_epoch("test")
        assert second.refused_epochs == [first.epoch_id]
        assert second.interrupted == []
        assert db.get_iteration(iteration)["interrupted_at"] is None
        assert db.one(
            "SELECT * FROM agent_log WHERE agent = 'RecoveryService' "
            "AND level = 'ERROR' ORDER BY log_id DESC")


def os_pid_of_a_living_process() -> int:
    """This test process's parent — alive, and not us."""
    import os
    return os.getppid()


# ===========================================================================
# Evidence preservation
# ===========================================================================


class TestEvidence:
    def test_a_stranded_run_row_cannot_be_silently_replaced(self, db, service,
                                                            session):
        """start_agent_run replaces by DELETE, so re-running the stage would
        erase the only durable trace that a process died inside it."""
        epoch = service.open_epoch("test")
        iteration = crash(db, session, epoch_id=epoch.epoch_id)
        with pytest.raises(StrandedRunError):
            db.start_agent_run(iteration, "IterationOrchestrator",
                               "COLLECTING_TIPPED")

    def test_the_interrupted_stage_survives_the_stage_re_running(
        self, db, service, session
    ):
        """The row it was derived from is destroyed by the re-run. Persisting
        it is what makes a resume that itself crashes still recoverable."""
        first = service.open_epoch("test")
        iteration = crash(db, session, epoch_id=first.epoch_id)
        service.open_epoch("test")
        assert db.get_iteration(iteration)["interrupted_stage"] == \
            "COLLECTING_TIPPED"

        db.start_agent_run(iteration, "IterationOrchestrator",
                           "COLLECTING_TIPPED", replace_running=True)
        assert db.get_iteration(iteration)["interrupted_stage"] == \
            "COLLECTING_TIPPED"

    def test_running_an_unrecovered_iteration_is_refused(self, db, config,
                                                         service, session,
                                                         wiring):
        _session, _city, connectors, llm = wiring
        first = service.open_epoch("test")
        iteration = crash(db, session, epoch_id=first.epoch_id)
        service.open_epoch("test")

        orchestrator = build(db, config, connectors, llm)
        with pytest.raises(RecoveryRequired, match="interrupted"):
            orchestrator.run(iteration)
        with pytest.raises(RecoveryRequired):
            orchestrator.step(iteration)

    def test_every_agents_run_row_is_closed_not_only_the_orchestrators(
        self, db, service, session
    ):
        """Otherwise a stage report shows a CollectionAgent running forever."""
        first = service.open_epoch("test")
        iteration = crash(db, session, epoch_id=first.epoch_id)
        service.open_epoch("test")
        statuses = {r["agent"]: r["status"] for r in db.get_agent_runs(iteration)}
        assert statuses["IterationOrchestrator"] == "INTERRUPTED"
        assert statuses["CollectionAgent"] == "INTERRUPTED"


# ===========================================================================
# The resume point
# ===========================================================================


class TestResumePoint:
    def test_reconciled_is_preferred(self, db, service, session):
        first = service.open_epoch("test")
        iteration = crash(db, session, stage="TRIAGING", epoch_id=first.epoch_id)
        service.open_epoch("test")
        assert service.resume_point(iteration) == ("TRIAGING", "RECONCILED")

    def test_in_flight_is_the_fallback_before_reconcile(self, db, service,
                                                        session):
        epoch = service.open_epoch("test")
        iteration = crash(db, session, stage="TIPPING", epoch_id=epoch.epoch_id)
        assert service.resume_point(iteration) == ("TIPPING", "IN_FLIGHT")

    def test_dying_between_stages_resumes_at_the_next_one(self, db, service,
                                                          session):
        epoch = service.open_epoch("test")
        iteration = db.insert_iteration(session, owner_epoch_id=epoch.epoch_id)
        for stage in ("SEEDING", "COLLECTING_SOCIAL"):
            run_id = db.start_agent_run(iteration, "IterationOrchestrator",
                                        stage)
            db.finish_agent_run(run_id, "COMPLETE")
        assert service.resume_point(iteration) == ("TRIAGING", "BETWEEN_STAGES")

    def test_nothing_ran_resumes_at_the_beginning(self, db, service, session):
        epoch = service.open_epoch("test")
        iteration = db.insert_iteration(session, owner_epoch_id=epoch.epoch_id)
        assert service.resume_point(iteration) == ("SEEDING", "NOTHING_RAN")

    def test_rollback_now_targets_the_interrupted_stage(self, db, service,
                                                        session):
        """A latent bug before 6a: last_completed_stage skips RUNNING, so
        rollback targeted the stage BEFORE the interrupted one and would have
        orphaned its partial output."""
        first = service.open_epoch("test")
        iteration = crash(db, session, stage="TIPPING", epoch_id=first.epoch_id)
        assert StageInspector(db).last_completed_stage(iteration) == "TRIAGING"

        service.open_epoch("test")
        assert StageInspector(db).last_completed_stage(iteration) == "TIPPING"
        assert StageRollback(db).target(iteration) == "TIPPING"

    def test_a_terminal_from_stage_is_refused(self, db, config, session,
                                              wiring):
        """run(from_stage='COMPLETE') skipped all eight stages and called
        _finish() — marking an iteration COMPLETE having done nothing."""
        _session, _city, connectors, llm = wiring
        iteration = db.insert_iteration(session)
        orchestrator = build(db, config, connectors, llm)
        with pytest.raises(ValueError, match="terminal state"):
            orchestrator.run(iteration, from_stage="COMPLETE")
        assert db.get_iteration(iteration)["outcome"] is None


# ===========================================================================
# Money
# ===========================================================================


class TestMoney:
    def test_the_plan_names_every_query_that_would_be_recollected(
        self, db, service, session
    ):
        city = db.insert_city(session, "Tucson", canonical="tucson")
        first = service.open_epoch("test")
        iteration = crash(db, session, epoch_id=first.epoch_id, claimed=2,
                          city_id=city)
        service.open_epoch("test")

        plan = service.plan(iteration)
        assert plan.paid
        assert len(plan.queries_to_recollect) == 2
        assert {q["reason"] for q in plan.queries_to_recollect} == {
            "claimed but never recorded"}
        assert plan.estimated_units_upper_bound["PRICELINE"] == 2.0

    def test_a_banked_payload_with_a_signal_is_completed_not_rebought(
        self, db, service, session
    ):
        """CollectionAgent stores raw_results per vendor call but marks the
        query COMPLETE only after its handler returns. A crash in that window
        leaves the money spent and the payload on disk."""
        city = db.insert_city(session, "Tucson", canonical="tucson")
        first = service.open_epoch("test")
        iteration = crash(db, session, epoch_id=first.epoch_id, claimed=1,
                          city_id=city)
        query = db.get_queue(iteration)[0]["query_id"]
        raw = db.insert_raw_result(
            query_id=query, iteration_id=iteration, source_type="CAR",
            provider="PRICELINE", payload={"vehicles": []}, retention_days=90)
        db.insert_signal(iteration_id=iteration, signal_type="CAR",
                         raw_id=raw, city_id=city, provider_ref="PHX")

        service.open_epoch("test")
        assert db.get_query(query)["status"] == "COMPLETE"
        plan = service.plan(iteration)
        assert plan.queries_to_recollect == []
        assert not plan.paid

    def test_a_banked_payload_with_no_signal_is_a_gap_not_a_completion(
        self, db, service, session
    ):
        """A COMPLETE row with no signal reads as 'we looked and found
        nothing' — the exact confusion this system exists to prevent."""
        city = db.insert_city(session, "Tucson", canonical="tucson")
        first = service.open_epoch("test")
        iteration = crash(db, session, epoch_id=first.epoch_id, claimed=1,
                          city_id=city)
        query = db.get_queue(iteration)[0]["query_id"]
        db.insert_raw_result(
            query_id=query, iteration_id=iteration, source_type="CAR",
            provider="PRICELINE", payload={"vehicles": []}, retention_days=90)

        service.open_epoch("test")
        assert db.get_query(query)["status"] == "INTERRUPTED"
        assert "CAR" in db.unreliable_source_types(iteration, city)
        plan = service.plan(iteration)
        assert not plan.paid
        assert len(plan.already_banked) == 1

    def test_social_needs_no_signal_to_count_as_collected(self, db, service,
                                                          session):
        """Triage writes social signals, a stage later. Judging SOCIAL on
        'a signal exists' would mark every successful social query a gap."""
        city = db.insert_city(session, "Tucson", canonical="tucson")
        first = service.open_epoch("test")
        iteration = db.insert_iteration(session, owner_epoch_id=first.epoch_id)
        db.start_agent_run(iteration, "IterationOrchestrator",
                           "COLLECTING_SOCIAL")
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="s1",
            city_id=city)
        db.claim_next_query(iteration, ["SOCIAL"])
        db.insert_raw_result(
            query_id=query, iteration_id=iteration, source_type="SOCIAL",
            provider="APIDIRECT", payload=[], retention_days=90)

        service.open_epoch("test")
        assert db.get_query(query)["status"] == "COMPLETE"

    def test_a_resume_inherits_the_original_budget_envelope(self, db, config,
                                                            service, session):
        """N crash-resume cycles cannot exceed one envelope: plan_iteration
        runs at start(), its result is on the row, and can_afford compares
        against units already billed to the iteration."""
        epoch = service.open_epoch("test")
        guard = BudgetGuard(db, config)
        guard.seed_budgets()
        iteration = db.insert_iteration(session, owner_epoch_id=epoch.epoch_id)
        plan = guard.plan_iteration(iteration)
        assert plan

        before = json.loads(db.get_iteration(iteration)["budget_plan_json"])
        db.record_api_call(provider="FR24", endpoint="/api/live/x", units=500.0,
                           iteration_id=iteration)
        service.prepare_resume(iteration, epoch.epoch_id)
        after = json.loads(db.get_iteration(iteration)["budget_plan_json"])
        assert after == before, "resume must not re-plan the envelope"
        assert db.units_used("FR24", iteration_id=iteration) == 500.0


# ===========================================================================
# Abandon — the requirement-4 gate
# ===========================================================================


class TestAbandon:
    @pytest.fixture
    def half_collected(self, db, config, service, session):
        """Social and flight evidence banked; the car query stranded."""
        epoch = service.open_epoch("test")
        city = db.insert_city(session, "Phoenix", canonical="phoenix",
                              state="AZ")
        iteration = db.insert_iteration(session, anchor_at=ANCHOR,
                                        owner_epoch_id=epoch.epoch_id)
        for stage in ("SEEDING", "COLLECTING_SOCIAL", "TRIAGING", "TIPPING"):
            run_id = db.start_agent_run(iteration, "IterationOrchestrator",
                                        stage)
            db.finish_agent_run(run_id, "COMPLETE")
        db.start_agent_run(iteration, "IterationOrchestrator",
                           "COLLECTING_TIPPED")
        db.set_stage(iteration, "COLLECTING_TIPPED")

        for index, domain in enumerate(("a.com", "b.org", "c.net"), start=1):
            db.insert_signal(
                iteration_id=iteration, signal_type="SOCIAL", city_id=city,
                track="AIRSHOW",
                observed_at=iso(ANCHOR - timedelta(hours=index)),
                url=f"https://{domain}/{index}", source_domain=domain,
                salience=0.95, snippet="Crews staging at the fairground",
                quality=0.95)
        for index in range(3):
            db.insert_signal(
                iteration_id=iteration, signal_type="FLIGHT", city_id=city,
                observed_at=iso(ANCHOR - timedelta(minutes=20)),
                fr24_id=f"m{index}", callsign=f"RCH{index}", aircraft_type="C17",
                flight_category="M", category_confidence="CONFIRMED",
                flight_status="airborne_inbound", quality=1.0)
        db.insert_signal(
            iteration_id=iteration, signal_type="LODGING", city_id=city,
            observed_at=iso(ANCHOR), provider_ref="L1", near_available=0,
            base_available=30, drop_pct=100.0, distance_km=3.0, quality=1.0)

        db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="CAR",
            endpoint="/search-rental-car", params={}, dedup_key="car1",
            city_id=city)
        db.claim_next_query(iteration, ["CAR"])
        service.open_epoch("test")
        return iteration, city

    def test_abandon_counts_the_loss_and_still_alerts(
        self, db, config, service, session, half_collected, wiring
    ):
        """The gate. An iteration closed without scoring produces no alert at
        all for a city whose evidence was nearly complete — a real cluster
        reading as silence."""
        iteration, city = half_collected
        _s, _c, connectors, llm = wiring

        result = service.abandon(iteration, "operator abandoned after crash")
        assert result["queries_marked_interrupted"] == 1
        assert result["coverage_gaps"] == {"CAR": 1}

        outcome = build(db, config, connectors, llm).finalise(iteration)
        assert outcome == "PARTIAL"

        correlation = db.one(
            "SELECT * FROM correlations WHERE iteration_id = ? "
            "AND track = 'AIRSHOW'", (iteration,))
        assert correlation is not None, "an abandoned iteration must still score"
        # One family lost of the five the mission defines (the four engine
        # families plus the promoted LOCAL_NEWS stream family).
        assert correlation["data_completeness"] == 0.8
        assert "CAR" in correlation["failed_sources"]
        assert correlation["band"] != "HIGH"

        alert = db.one("SELECT * FROM alerts WHERE iteration_id = ?",
                       (iteration,))
        assert alert is not None
        assert "car" in (alert["caveat"] or "").lower()

    def test_interrupted_is_a_coverage_gap(self, db, service, session,
                                           half_collected):
        iteration, city = half_collected
        service.abandon(iteration, "x")
        assert "CAR" in db.unreliable_source_types(iteration, city)
        assert "INTERRUPTED" in enums.UNRELIABLE_QUERY_STATUSES

    def test_an_abandoned_iteration_never_reads_complete(
        self, db, config, service, half_collected, wiring
    ):
        iteration, _city = half_collected
        _s, _c, connectors, llm = wiring
        service.abandon(iteration, "x")
        assert build(db, config, connectors, llm).finalise(iteration) == \
            "PARTIAL"

    def test_it_reads_partial_without_a_reconcile_note_too(
        self, db, config, wiring
    ):
        """Found live while verifying 8.7(a), and the reason the test above was
        passing for the wrong reason.

        `half_collected` crashes the iteration first, so the reconcile has
        already appended a degradation and `_finish` reaches PARTIAL on that
        alone. Abandon an iteration that was merely LEFT OPEN — no crash, no
        reconcile, no note — and the outcome came out **COMPLETE**, with twelve
        queries marked INTERRUPTED and `unreliable_source_types` already
        reporting them as a SOCIAL coverage gap.

        Two causes, both fixed: `_collection_gaps` hand-rolled a status list
        that omitted INTERRUPTED while `enums.UNRELIABLE_QUERY_STATUSES` —
        whose own comment warns about exactly this — includes it; and
        `abandon()` recorded the operator's decision only in the log, which
        `_finish` never reads.
        """
        session, _city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.step(iteration)                    # SEEDING enqueues real queries
        assert db.get_iteration(iteration)["interrupted_at"] is None
        assert db.degradation_notes(iteration) == []

        from surge_iw.services.recovery import RecoveryService
        result = RecoveryService(db, config).abandon(iteration, "left open")
        assert result["queries_marked_interrupted"] > 0
        assert result["coverage_gaps"], "already counted as a coverage gap"

        assert orch.finalise(iteration) == "PARTIAL", (
            "an abandoned iteration that reads COMPLETE is collection that "
            "never happened reporting as a finished run")
        assert any("abandoned" in note for note in db.degradation_notes(iteration))

    def test_interrupted_queries_count_as_collection_gaps(self, db, config,
                                                          wiring):
        """The narrow half of the fix, pinned on its own so a future edit to
        `_collection_gaps` cannot quietly drop the status again."""
        session, _city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.step(iteration)
        db._exec("UPDATE query_queue SET status = 'INTERRUPTED' "
                 "WHERE iteration_id = ?", (iteration,))
        gaps = orch._collection_gaps(iteration)
        assert gaps.get("INTERRUPTED"), gaps
        assert set(gaps) <= enums.UNRELIABLE_QUERY_STATUSES

    def test_abandon_after_a_seeding_crash_fails_the_iteration(
        self, db, config, service, session, wiring
    ):
        """SEEDING is the one stage the rest depends on. Its alerts should not
        be read at all."""
        _s, _c, connectors, llm = wiring
        first = service.open_epoch("test")
        iteration = crash(db, session, stage="SEEDING",
                          epoch_id=first.epoch_id)
        service.open_epoch("test")
        service.abandon(iteration, "x")
        assert build(db, config, connectors, llm).finalise(iteration) == "FAILED"

    def test_finalise_never_schedules_follow_ons(
        self, db, config, service, half_collected, wiring
    ):
        """Work queued by an iteration nobody finished would arrive as a
        surprise next run."""
        iteration, _city = half_collected
        _s, _c, connectors, llm = wiring
        service.abandon(iteration, "x")
        build(db, config, connectors, llm).finalise(iteration)
        assert db.one(
            "SELECT 1 FROM agent_runs WHERE iteration_id = ? AND stage = ?",
            (iteration, "SCHEDULING")) is None

    def test_a_resumed_iteration_can_never_be_complete(
        self, db, config, service, session, wiring
    ):
        """The reconcile's degradation note makes _finish's existing
        `notes or existing` rule reach PARTIAL for free."""
        _s, _c, connectors, llm = wiring
        first = service.open_epoch("test")
        iteration = crash(db, session, stage="ALERTING",
                          epoch_id=first.epoch_id)
        epoch = service.open_epoch("test")
        service.prepare_resume(iteration, epoch.epoch_id)
        assert build(db, config, connectors, llm).resume(
            iteration, "ALERTING") == "PARTIAL"


# ===========================================================================
# A new iteration must not start alongside an UNFINISHED one — 8.7(a)
#
# The guard used to key on `interrupted_at`, which only the crash reconcile
# stamps. Everything else that leaves a run open — a manual walk stepped partway
# and left, a cancellation recorded against an iteration not on a worker — left
# it NULL, so the API reported PENDING and the next trigger was allowed. The
# word INTERRUPTED named two different things and only one of them blocked.
#
# `test_a_merely_open_iteration_blocks_too` is the load-bearing one: it is the
# case the operator actually hit, and the one the old predicate missed.
# ===========================================================================


class TestBlocksNewWork:
    def test_starting_a_new_iteration_is_refused(self, db, config, service,
                                                 wiring):
        """The cooldown is keyed on (dedup_key, executed_at) across ALL
        iterations, so the interrupted run's recent executions would silently
        suppress the new run's queries and it would under-collect with no
        visible cause."""
        session, _city, connectors, llm = wiring
        first = service.open_epoch("test")
        crash(db, session, epoch_id=first.epoch_id)
        service.open_epoch("test")

        with pytest.raises(SessionHasOpenIteration, match="under-collected"):
            build(db, config, connectors, llm).start(session)

    def test_a_merely_open_iteration_blocks_too(self, db, config, wiring):
        """No crash, no reconcile, nothing stamped — just an iteration created
        and never finished, which is what driving the API by hand produces.

        The rationale is identical: the open run's executed queries suppress the
        new one's through the cooldown. Nothing about that depends on HOW the
        first run came to be open.
        """
        session, _city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        first = orch.start(session)
        assert db.get_iteration(first)["interrupted_at"] is None, (
            "the premise: this iteration is open, not crash-stamped")

        with pytest.raises(SessionHasOpenIteration) as caught:
            orch.start(session)
        assert caught.value.blocking == [
            {"iteration_id": first, "kind": "OPEN", "stage": "SEEDING"}]

    def test_the_refusal_names_which_kind_of_open_and_how_to_close_it(
        self, db, config, service, wiring
    ):
        """One status code, two different remedies. A 409 that says only
        'conflict' leaves the operator to guess which situation they are in."""
        session, _city, connectors, llm = wiring
        first = service.open_epoch("test")
        iteration = crash(db, session, epoch_id=first.epoch_id)
        service.open_epoch("test")

        with pytest.raises(SessionHasOpenIteration) as caught:
            build(db, config, connectors, llm).start(session)
        message = str(caught.value)
        assert "was interrupted" in message
        assert f"/v1/iterations/{iteration}/resume" in message
        assert f"/v1/iterations/{iteration}/abandon" in message
        assert caught.value.blocking[0]["kind"] == "INTERRUPTED"

    def test_it_is_allowed_once_the_iteration_is_closed(
        self, db, config, service, wiring
    ):
        session, _city, connectors, llm = wiring
        first = service.open_epoch("test")
        iteration = crash(db, session, epoch_id=first.epoch_id)
        service.open_epoch("test")
        service.abandon(iteration, "x")
        db.finish_iteration(iteration, outcome="PARTIAL")
        assert build(db, config, connectors, llm).start(session) > iteration

    def test_a_finished_iteration_never_blocks(self, db, config, wiring):
        """`finished_at IS NULL` is the whole rule, and finishing is the only
        thing that has to happen for the session to be usable again."""
        session, _city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        first = orch.start(session)
        db.finish_iteration(first, outcome="COMPLETE")
        assert orch.start(session) > first

    def test_a_debug_discard_reopens_and_therefore_blocks(
        self, db, config, wiring
    ):
        """The one sanctioned way back to open, and it must block like any
        other open iteration.

        Discarding a stage sets `finished_at` back to NULL deliberately — the
        iteration genuinely is open again and its queries are back in play, so
        starting a fresh run alongside it would hit exactly the cooldown
        the thing being watched for, which is what the guard exists to prevent.
        """
        from surge_iw.services.stages import StageRollback

        session, _city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        first = orch.start(session)
        orch.step(first)                       # run SEEDING so there is one to discard
        db.finish_iteration(first, outcome="COMPLETE")
        assert orch.start(session) > first, "closed, so a new run is fine"
        db.finish_iteration(first + 1, outcome="COMPLETE")

        StageRollback(db).discard_last(first, confirm=True)
        assert db.get_iteration(first)["finished_at"] is None
        with pytest.raises(SessionHasOpenIteration):
            orch.start(session)


# ===========================================================================
# Property
# ===========================================================================


class TestProperty:
    @hyp_settings(max_examples=25, deadline=None,
                  suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(stranded=st.lists(
        st.sampled_from(["CAR", "LODGING", "FLIGHT_LIVE", "SOCIAL"]),
        min_size=1, max_size=4, unique=True))
    def test_no_high_band_survives_an_abandon(self, config, stranded):
        """The Phase 1 property extended to interruption: a crash must not be
        able to launder incomplete collection into full confidence."""
        from surge_iw.db.database import SurgeDB

        db = SurgeDB(":memory:", mission=REFERENCE_MISSION)
        try:
            service = RecoveryService(db, config)
            epoch = service.open_epoch("test")
            session = db.insert_session(label="p", tracks=["AIRSHOW"])
            city = db.insert_city(session, "Phoenix", canonical="phoenix")
            iteration = db.insert_iteration(session, anchor_at=ANCHOR,
                                            owner_epoch_id=epoch.epoch_id)
            # Maximal evidence in every family.
            for index, domain in enumerate(("a.com", "b.org", "c.net"), 1):
                db.insert_signal(
                    iteration_id=iteration, signal_type="SOCIAL", city_id=city,
                    track="AIRSHOW",
                    observed_at=iso(ANCHOR - timedelta(hours=index)),
                    url=f"https://{domain}/{index}", source_domain=domain,
                    salience=1.0, quality=1.0)
            for index in range(3):
                db.insert_signal(
                    iteration_id=iteration, signal_type="FLIGHT", city_id=city,
                    observed_at=iso(ANCHOR), fr24_id=f"f{index}",
                    flight_category="M", category_confidence="CONFIRMED",
                    quality=1.0)
            db.insert_signal(
                iteration_id=iteration, signal_type="LODGING", city_id=city,
                observed_at=iso(ANCHOR), provider_ref="L1", near_available=0,
                base_available=30, drop_pct=100.0, distance_km=1.0, quality=1.0)
            db.insert_signal(
                iteration_id=iteration, signal_type="CAR", city_id=city,
                observed_at=iso(ANCHOR), provider_ref="PHX", near_available=0,
                base_available=20, drop_pct=100.0, distance_km=1.0,
                people_capacity=12, is_on_airport=True, quality=1.0)

            for index, source in enumerate(stranded):
                db.enqueue_query(
                    session_id=session, iteration_id=iteration,
                    source_type=source, endpoint="/x", params={"i": index},
                    dedup_key=f"p{index}", city_id=city)
            service.abandon(iteration, "property")

            from surge_iw.agents.correlation import CorrelationAgent
            CorrelationAgent(db, config).run(iteration)
            for row in db.get_correlations(iteration):
                assert row["band"] != "HIGH", (
                    f"stranded {stranded} still produced a HIGH band")
        finally:
            db.close()
