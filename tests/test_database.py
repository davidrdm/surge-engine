"""Database bus, retention, budget accounting, and schema/enum consistency."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import ANCHOR, REFERENCE_MISSION
from surge_iw.db import enums
from surge_iw.db.database import (
    StrandedRunError, SurgeDB, iso, parse_iso, utcnow,
)
from surge_iw.models import Alert
from surge_iw.services import budget as budget_mod
from surge_iw.services.retention import RetentionService, retention_days

SCHEMA_SQL = (
    Path(__file__).resolve().parents[1] / "surge_iw" / "db" / "schema.sql"
).read_text(encoding="utf-8")


def table_sql(table: str) -> str:
    """The CREATE TABLE body for one table."""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);",
        SCHEMA_SQL, re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"No CREATE TABLE found for {table}")
    return match.group(1)


def check_values(table: str, column: str) -> set[str]:
    """Permitted values from a CHECK (col IN (...)) clause on one table.

    Scoped by table because column names repeat with different vocabularies:
    `outcome` means COMPLETE/PARTIAL/FAILED on iterations and
    ENQUEUED/DEDUPED/... on queue_decisions, and `status` differs on four tables.
    An unscoped regex silently matches whichever appears first in the file.
    """
    body = table_sql(table)
    match = re.search(
        rf"\b{column}\s+IN\s*\(([^)]*)\)", body, re.IGNORECASE | re.DOTALL
    )
    if not match:
        raise AssertionError(f"No CHECK ... IN clause for {table}.{column}")
    return set(re.findall(r"'([^']+)'", match.group(1)))


def has_check(table: str, column: str) -> bool:
    """Whether one column carries a CHECK ... IN clause at all."""
    return re.search(rf"\b{column}\s+IN\s*\(", table_sql(table),
                     re.IGNORECASE | re.DOTALL) is not None


class TestSchemaEnumConsistency:
    """enums.py mirrors the CHECK constraints; drift must fail the suite.

    Both layers validate, so a mismatch means Python accepts a value SQLite will
    reject at write time — or worse, rejects one the schema permits, silently
    dropping a legitimate signal.

    The MISSION-owned columns are deliberately absent from the list below and
    are covered by `TestMissionOwnedColumns` instead. Their permitted values
    come from a pack read at startup, which SQLite cannot know.
    """

    @pytest.mark.parametrize(
        "table,column,expected",
        [
            ("query_queue", "source_type", enums.SOURCE_TYPES),
            ("query_queue", "skip_reason", enums.SKIP_REASONS),
            ("query_queue", "origin", enums.QUERY_ORIGINS),
            ("query_queue", "status", enums.QUERY_STATUSES),
            ("queue_decisions", "outcome", enums.QUEUE_DECISION_OUTCOMES),
            ("iterations", "outcome", enums.ITERATION_OUTCOMES),
            ("iterations", "stage", enums.STAGES),
            ("agent_runs", "status", enums.AGENT_RUN_STATUSES),
            ("process_epochs", "shutdown_kind", enums.SHUTDOWN_KINDS),
            ("raw_results", "provider", enums.PROVIDERS),
            ("signals", "signal_type", enums.SIGNAL_TYPES),
            ("signals", "signal_state", enums.SIGNAL_STATES),
            ("triage_decisions", "state", enums.TRIAGE_STATES),
            ("signals", "flight_category", enums.FLIGHT_CATEGORIES),
            ("signals", "category_confidence", enums.CATEGORY_CONFIDENCE),
            ("signals", "flight_status", enums.FLIGHT_STATUSES),
            ("correlations", "band", enums.BANDS),
            ("alerts", "confidence_band", enums.ALERT_BANDS),
            ("geo_cache", "kind", enums.GEO_CACHE_KINDS),
            ("geo_cache", "resolved_by", enums.GEO_RESOLVED_BY),
            ("agent_log", "level", enums.LOG_LEVELS),
            ("api_budgets", "period", enums.BUDGET_PERIODS),
            ("sessions", "status", enums.SESSION_STATUSES),
        ],
    )
    def test_enum_matches_schema_check(self, table, column, expected):
        assert check_values(table, column) == set(expected)

    def test_stage_order_contains_no_terminal_failure(self):
        """FAILED must stay outside the ordering so resume comparisons work."""
        assert "FAILED" not in enums.STAGE_ORDER
        assert "FAILED" in enums.STAGES
        assert enums.stage_index("FAILED") == -1

    def test_unreliable_statuses_are_real_query_statuses(self):
        assert enums.UNRELIABLE_QUERY_STATUSES <= enums.QUERY_STATUSES

    def test_uncovered_triage_states_are_real_states(self):
        assert enums.TRIAGE_UNCOVERED <= enums.TRIAGE_STATES
        assert "REJECTED" not in enums.TRIAGE_UNCOVERED, (
            "a rejection is a conclusion, not a coverage gap")
        assert "ACCEPTED" not in enums.TRIAGE_UNCOVERED

    def test_alert_bands_exclude_none(self):
        """NONE is a real computed outcome but never an alert."""
        assert "NONE" not in enums.ALERT_BANDS
        assert "NONE" in enums.BANDS


class TestMissionOwnedColumns:
    """The columns whose vocabulary left the schema in version 12.

    Removing a CHECK removes a guarantee, and the point of these tests is that
    the guarantee moved rather than evaporated. Before v12 SQLite refused a bad
    track even if Python forgot to; now Python is the only thing between an
    LLM's output and the analytical record, so "is it validated" has to be
    asserted rather than assumed.
    """

    #: Every write path that stores a mission-owned value, and a call that
    #: exercises it. Kept as data so `test_no_write_path_escapes_validation`
    #: can check this list against the SQL in database.py.
    WRITE_PATHS = ("insert_session", "insert_key_location", "insert_signal",
                   "insert_triage_decision", "upsert_correlation",
                   "insert_alert")

    @pytest.mark.parametrize("table,column", [
        ("sessions", "tracks"),
        ("key_locations", "location_type"),
        ("signals", "track"),
        ("triage_decisions", "track"),
        ("correlations", "track"),
        ("alerts", "track"),
    ])
    def test_the_schema_no_longer_constrains_it(self, table, column):
        """A CHECK re-added "to be safe" would break every mission but one, at
        the storage layer, where the error names neither column nor value."""
        assert not has_check(table, column), (
            f"{table}.{column} has a CHECK again. Its permitted values come "
            f"from the loaded mission, which SQLite cannot know.")

    def test_the_engine_no_longer_defines_the_vocabulary(self):
        """Deleted, not left as unused constants: a leftover frozenset is the
        thing a future validator would reach for by mistake."""
        for name in ("ACTOR_TRACKS", "ACTOR_TYPES", "LOCATION_TYPES",
                     "TRACK_FLIGHT_CATEGORIES"):
            assert not hasattr(enums, name), f"enums.{name} still exists"

    def test_a_value_from_another_mission_is_refused(self, db):
        """The half that a leftover hardcoded frozenset could not satisfy: the
        SAME value must be accepted under one mission and refused under
        another, which only genuine mission-driven validation achieves."""
        import dataclasses
        from conftest import REFERENCE_MISSION
        # A second mission, built rather than loaded: an engine whose tests
        # need a particular pack on disk is not an engine. `SUMMIT` is a
        # perfectly good track name — just not the loaded mission's.
        other = dataclasses.replace(
            REFERENCE_MISSION, identifier="second", tracks=("SUMMIT",))

        assert db.insert_session(label="ok", tracks=["AIRSHOW"])
        with pytest.raises(ValueError) as exc:
            db.insert_session(label="no", tracks=["SUMMIT"])
        assert "SUMMIT" in str(exc.value)

        # Same database, same column, other mission: the verdicts swap. A
        # leftover hardcoded frozenset could not produce this.
        db.mission = other
        assert db.insert_session(label="ok", tracks=["SUMMIT"])
        with pytest.raises(ValueError) as exc:
            db.insert_session(label="no", tracks=["AIRSHOW"])
        assert "AIRSHOW" in str(exc.value)

    def test_no_mission_means_refusal_not_silent_acceptance(self):
        """An unvalidated write is worse than a refused one: it puts a value
        nothing recognises into the analytical record."""
        from surge_iw.db.database import SurgeDB
        with SurgeDB(":memory:") as bare:
            with pytest.raises(enums.EnumViolation) as exc:
                bare.insert_session(label="x", tracks=["ANYTHING"])
            assert "no mission" in str(exc.value)

    def test_no_write_path_escapes_validation(self):
        """The structural heir to the old lockstep.

        That one bound schema.sql to enums.py. This binds the SQL in
        database.py to the list of write paths asserted above, so a NEW insert
        added later with no validation fails here rather than silently writing
        an unrecognised value.
        """
        source = (Path(__file__).resolve().parents[1] / "surge_iw" / "db"
                  / "database.py").read_text(encoding="utf-8")
        tables = "sessions|key_locations|signals|triage_decisions|correlations|alerts"
        found = set()
        current = None
        for line in source.splitlines():
            match = re.match(r"    def (\w+)\(", line)
            if match:
                current = match.group(1)
            if re.search(rf"INSERT INTO ({tables})\b", line) and current:
                found.add(current)
        assert found == set(self.WRITE_PATHS), (
            f"write paths changed: {sorted(found)} vs "
            f"{sorted(self.WRITE_PATHS)}. A new INSERT into a table with a "
            f"mission-owned column needs validation and a case above.")


class TestEnumHelpers:
    def test_validate_rejects_and_names_the_allowed_set(self):
        with pytest.raises(enums.EnumViolation) as exc:
            enums.validate("NOPE", enums.PROVIDERS, "provider")
        assert "provider" in str(exc.value)
        assert "FR24" in str(exc.value)

    def test_validate_optional_passes_none_through(self):
        assert enums.validate_optional(None, enums.PROVIDERS, "x") is None

    def test_cap_band_limits_but_does_not_raise(self):
        assert enums.cap_band("HIGH", "MEDIUM") == "MEDIUM"
        assert enums.cap_band("LOW", "MEDIUM") == "LOW"
        assert enums.cap_band("NONE", "HIGH") == "NONE"


class TestTimestamps:
    def test_iso_round_trip_is_utc_aware(self):
        now = utcnow()
        assert parse_iso(iso(now)) == now
        assert parse_iso(iso(now)).tzinfo is not None

    def test_trailing_z_is_parsed(self):
        """External APIs emit 'Z', which fromisoformat cannot parse on 3.10."""
        parsed = parse_iso("2026-07-27T19:40:00Z")
        assert parsed == datetime(2026, 7, 27, 19, 40, tzinfo=timezone.utc)

    def test_naive_input_is_treated_as_utc(self):
        assert parse_iso("2026-07-27T19:40:00").tzinfo == timezone.utc

    def test_unparseable_input_returns_none_rather_than_raising(self):
        assert parse_iso("not a date") is None
        assert parse_iso(None) is None
        assert parse_iso("") is None


class TestSignalWrites:
    def test_unknown_field_is_rejected(self, db, iteration):
        """A typo in a field name must fail loudly, not vanish."""
        with pytest.raises(ValueError, match="Unknown signal field"):
            db.insert_signal(
                iteration_id=iteration, signal_type="SOCIAL", not_a_column=1
            )

    def test_invalid_flight_category_is_rejected(self, db, iteration):
        with pytest.raises(enums.EnumViolation):
            db.insert_signal(
                iteration_id=iteration, signal_type="FLIGHT",
                flight_category="ZZZ",
            )

    def test_quality_is_clamped(self, db, iteration):
        signal_id = db.insert_signal(
            iteration_id=iteration, signal_type="SOCIAL", quality=5.0
        )
        row = db.one("SELECT quality FROM signals WHERE signal_id = ?", (signal_id,))
        assert row["quality"] == 1.0

    def test_duplicate_signal_in_one_iteration_is_rejected(self, db, iteration):
        db.insert_signal(
            iteration_id=iteration, signal_type="SOCIAL", url="https://a/1"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_signal(
                iteration_id=iteration, signal_type="SOCIAL", url="https://a/1"
            )

    def test_same_url_in_a_later_iteration_is_allowed(self, db, session):
        """Dedup is per iteration; a post reappearing next run is new evidence."""
        first = db.insert_iteration(session, anchor_at=ANCHOR)
        second = db.insert_iteration(session, anchor_at=ANCHOR)
        db.insert_signal(iteration_id=first, signal_type="SOCIAL", url="https://a/1")
        db.insert_signal(iteration_id=second, signal_type="SOCIAL", url="https://a/1")

    def test_boolean_fields_are_coerced_to_integers(self, db, iteration):
        signal_id = db.insert_signal(
            iteration_id=iteration, signal_type="CAR",
            provider_ref="PHX", is_on_airport=True, is_peer_to_peer=False,
        )
        row = db.one("SELECT * FROM signals WHERE signal_id = ?", (signal_id,))
        assert row["is_on_airport"] == 1
        assert row["is_peer_to_peer"] == 0


class TestQueueClaiming:
    def test_claim_returns_highest_priority_first(self, db, session, iteration):
        for priority, name in ((50, "low"), (10, "urgent"), (30, "mid")):
            db.enqueue_query(
                session_id=session, iteration_id=iteration, source_type="SOCIAL",
                endpoint="/v1/twitter/posts", params={"q": name},
                dedup_key=f"k-{name}", priority=priority,
            )
        first = db.claim_next_query(iteration, ["SOCIAL"])
        assert first["priority"] == 10

    def test_claimed_query_is_not_claimed_twice(self, db, session, iteration):
        db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={"q": "a"}, dedup_key="k1",
        )
        assert db.claim_next_query(iteration, ["SOCIAL"]) is not None
        assert db.claim_next_query(iteration, ["SOCIAL"]) is None

    def test_claim_filters_by_source_type(self, db, session, iteration):
        """Collection runs twice per iteration under different filters."""
        db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="CAR",
            endpoint="/search-rental-car", params={"q": "a"}, dedup_key="k1",
        )
        assert db.claim_next_query(iteration, ["SOCIAL"]) is None
        assert db.claim_next_query(iteration, ["CAR"]) is not None

    def test_empty_source_type_list_claims_nothing(self, db, iteration):
        assert db.claim_next_query(iteration, []) is None


class TestFailureIsRecordedDistinctly:
    """A failed query, a skipped query and an empty result are three things."""

    def test_failed_query_is_reported_as_unreliable(
        self, db, session, iteration,
    ):
        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="FLIGHT_LIVE",
            endpoint="/api/live/flight-positions/full", params={}, dedup_key="k1",
            city_id=city,
        )
        db.fail_query(query_id, "401 Unauthorized")
        assert db.unreliable_source_types(iteration, city) == ["FLIGHT_LIVE"]

    def test_completed_empty_query_is_not_unreliable(self, db, session, iteration):
        """Zero results from a working endpoint is real evidence of absence."""
        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="FLIGHT_LIVE",
            endpoint="/api/live/flight-positions/full", params={}, dedup_key="k1",
            city_id=city,
        )
        db.complete_query(query_id, result_count=0)
        assert db.unreliable_source_types(iteration, city) == []

    def test_budget_skip_is_reported_as_unreliable(self, db, session, iteration):
        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="CAR",
            endpoint="/search-rental-car", params={}, dedup_key="k1", city_id=city,
        )
        db.skip_query(query_id, "SKIPPED_BUDGET", "MONTHLY_QUOTA_EXHAUSTED")
        assert db.unreliable_source_types(iteration, city) == ["CAR"]

    def test_failure_still_starts_the_cooldown(self, db, session, iteration):
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="k1",
        )
        db.fail_query(query_id, "500")
        assert db.last_execution("k1") is not None


class TestAgentFailureIsolation:
    """One agent's failure must not fail the iteration."""

    def test_agent_run_failure_leaves_the_iteration_runnable(self, db, iteration):
        run_id = db.start_agent_run(iteration, "CollectionAgent", "COLLECTING_SOCIAL")
        db.finish_agent_run(run_id, "FAILED", "connector exploded")
        db.append_degradation(iteration, "social", source="COLLECTING_SOCIAL")
        db.finish_iteration(iteration, outcome="PARTIAL")
        row = db.get_iteration(iteration)
        assert row["outcome"] == "PARTIAL"
        assert row["stage"] == "COMPLETE"
        assert db.degradation_notes(iteration) == ["social"]

    def test_finishing_never_erases_what_agents_recorded(self, db, iteration):
        """`finish_iteration` used to overwrite `degradations_json` with
        whatever the caller passed, so the three failure paths that pass none
        silently erased every note the agents had written."""
        db.append_degradation(iteration, "connector exploded",
                              source="COLLECTING_SOCIAL")
        db.finish_iteration(iteration, outcome="FAILED",
                            error_message="stage failed")
        assert db.degradation_notes(iteration) == ["connector exploded"]

    def test_rerunning_a_closed_agent_replaces_its_previous_run(self, db,
                                                                iteration):
        """resume() must not accumulate duplicate run rows."""
        run_id = db.start_agent_run(iteration, "TriageAgent", "TRIAGING")
        db.finish_agent_run(run_id, "FAILED", "model outage")
        db.start_agent_run(iteration, "TriageAgent", "TRIAGING")
        runs = [r for r in db.get_agent_runs(iteration)
                if r["agent"] == "TriageAgent"]
        assert len(runs) == 1
        assert runs[0]["status"] == "RUNNING"

    def test_rerunning_a_stranded_agent_refuses(self, db, iteration):
        """The replacement is a DELETE, so re-running a stage whose row is
        still RUNNING erases the only durable trace that a process died inside
        it — before anything could reconcile it."""
        db.start_agent_run(iteration, "TriageAgent", "TRIAGING")
        with pytest.raises(StrandedRunError, match="still RUNNING"):
            db.start_agent_run(iteration, "TriageAgent", "TRIAGING")
        assert db.get_agent_runs(iteration)[0]["status"] == "RUNNING"

    def test_a_stranded_run_can_be_replaced_deliberately(self, db, iteration):
        """Recovery says so explicitly; nothing else may."""
        db.start_agent_run(iteration, "TriageAgent", "TRIAGING")
        db.start_agent_run(iteration, "TriageAgent", "TRIAGING",
                           replace_running=True)
        assert len(db.get_agent_runs(iteration)) == 1

    def test_two_collection_passes_in_one_stage_are_unaffected(self, db,
                                                               iteration):
        """COLLECTING_TIPPED legitimately runs CollectionAgent twice under one
        key. The first row is closed by then, so the guard never sees it."""
        first = db.start_agent_run(iteration, "CollectionAgent",
                                   "COLLECTING_TIPPED")
        db.finish_agent_run(first, "COMPLETE")
        second = db.start_agent_run(iteration, "CollectionAgent",
                                    "COLLECTING_TIPPED")
        db.finish_agent_run(second, "COMPLETE")
        assert len(db.get_agent_runs(iteration)) == 1


class TestCoverageFromRefusals:
    """A query a guard refused to enqueue leaves no query_queue row, so the
    status-based check cannot see it. Measured live: the per-city cap refused
    every CONCERT_TOUR seed query, that track was never collected, and its
    correlation still reported completeness as though only triage had degraded.
    """

    def test_a_capped_query_is_a_coverage_gap(self, db, session, iteration):
        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        db.record_queue_decision(
            iteration, "R0_SEED", "CAP_CITY", source_type="SOCIAL",
            city_name="Phoenix", detail="city already holds 12 queries",
            stage="SEEDING")
        assert db.refused_source_types(iteration, city) == ["SOCIAL"]

    def test_a_budget_refusal_is_a_coverage_gap(self, db, session, iteration):
        db.record_queue_decision(
            iteration, "R4_LODGING", "BUDGET_EXHAUSTED", source_type="LODGING",
            detail="STAYING: month exhausted", stage="TIPPING")
        assert "LODGING" in db.refused_source_types(iteration)

    def test_an_ordinary_refusal_is_not_a_gap(self, db, session, iteration):
        """A deduped or cooled-down query is collection we already have or
        deliberately declined to repeat — not collection that is missing."""
        for outcome in ("DEDUPED", "COOLDOWN", "ENQUEUED"):
            db.record_queue_decision(
                iteration, "R0_SEED", outcome, source_type="SOCIAL",
                stage="SEEDING")
        assert db.refused_source_types(iteration) == []


class TestFreeEndpoints:
    def test_a_documented_free_call_is_not_billed(self):
        """Staying's /account is free and runs on every iteration start, so
        billing it makes the local ledger disagree with the vendor by one per
        run — and it is the call the remote reconciliation trusts."""
        assert budget_mod.units_for("STAYING", "/account", 0) == 0.0
        assert budget_mod.units_for("STAYING", "/search", 0) == 1.0


class TestRetention:
    def test_fr24_retention_is_capped_at_thirty_days(self, config):
        """A config file must not be able to extend a contractual limit."""
        config["flightradar"]["retention_days"] = 365
        assert retention_days(config, "FR24") == 30

    def test_other_providers_honour_their_configured_window(self, config):
        config["staying"]["retention_days"] = 120
        assert retention_days(config, "STAYING") == 120

    def test_unknown_provider_falls_back_to_the_strictest_window(self, config):
        assert retention_days(config, "NOT_A_PROVIDER") == 30

    def test_prune_deletes_only_expired_rows(self, db, config, session, iteration):
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="FLIGHT_LIVE",
            endpoint="/api/live/flight-positions/full", params={}, dedup_key="k1",
        )
        fresh = db.insert_raw_result(
            query_id=query_id, iteration_id=iteration, source_type="FLIGHT_LIVE",
            provider="FR24", payload=[{"a": 1}], retention_days=30,
        )
        stale = db.insert_raw_result(
            query_id=query_id, iteration_id=iteration, source_type="SOCIAL",
            provider="APIDIRECT", payload=[{"b": 2}], retention_days=30,
        )
        db._exec(
            "UPDATE raw_results SET purge_after = ? WHERE raw_id = ?",
            (iso(utcnow() - timedelta(days=1)), stale),
        )
        assert RetentionService(db, config).prune() == 1
        assert db.get_raw_result(fresh) is not None
        assert db.get_raw_result(stale) is None

    def test_prune_does_not_delete_derived_signals(
        self, db, config, session, iteration
    ):
        """Retention covers the licensed raw payload. The analytical record —
        that an aircraft was inbound and contributed to an alert — is this
        system's own product and must outlive it."""
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="FLIGHT_LIVE",
            endpoint="/api/live/flight-positions/full", params={}, dedup_key="k1",
        )
        raw_id = db.insert_raw_result(
            query_id=query_id, iteration_id=iteration, source_type="FLIGHT_LIVE",
            provider="FR24", payload=[{"a": 1}], retention_days=30,
        )
        signal_id = db.insert_signal(
            iteration_id=iteration, raw_id=raw_id, signal_type="FLIGHT",
            fr24_id="abc", flight_category="M", category_confidence="CONFIRMED",
        )
        db._exec(
            "UPDATE raw_results SET purge_after = ? WHERE raw_id = ?",
            (iso(utcnow() - timedelta(days=1)), raw_id),
        )
        RetentionService(db, config).prune()
        row = db.one("SELECT * FROM signals WHERE signal_id = ?", (signal_id,))
        assert row is not None
        assert row["flight_category"] == "M"
        # The pointer is nulled by ON DELETE SET NULL rather than the delete
        # being refused, which is what the foreign key would otherwise do.
        assert row["raw_id"] is None

    def test_prune_preserves_triage_decisions(
        self, db, config, session, iteration
    ):
        """Whether a post was accepted or rejected is an audit record."""
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="k1",
        )
        raw_id = db.insert_raw_result(
            query_id=query_id, iteration_id=iteration, source_type="SOCIAL",
            provider="APIDIRECT", payload=[{"a": 1}], retention_days=30,
        )
        db.insert_triage_decision(
            iteration_id=iteration, raw_id=raw_id, state="REJECTED",
            rationale="off topic", model="test", url="https://x/1",
        )
        db._exec(
            "UPDATE raw_results SET purge_after = ? WHERE raw_id = ?",
            (iso(utcnow() - timedelta(days=1)), raw_id),
        )
        RetentionService(db, config).prune()
        row = db.one("SELECT * FROM triage_decisions")
        assert row is not None
        assert row["rationale"] == "off topic"
        assert row["raw_id"] is None

    def test_prune_on_an_empty_database_is_a_no_op(self, db, config):
        assert RetentionService(db, config).prune() == 0


class TestBudgetAccounting:
    def test_fr24_bills_per_record_returned(self):
        """`limit` is not a cost control: the bill follows what came back."""
        endpoint = "/api/live/flight-positions/full"
        assert budget_mod.fr24_units(endpoint, 1) == 8.0
        assert budget_mod.fr24_units(endpoint, 10) == 80.0

    def test_empty_fr24_response_still_costs_one_credit(self):
        assert budget_mod.fr24_units("/api/live/flight-positions/full", 0) == 1.0

    def test_count_endpoint_is_far_cheaper_than_full(self):
        """The entire justification for the FLIGHT_COUNT tripwire."""
        count = budget_mod.fr24_units("/api/live/flight-positions/count", 0)
        full = budget_mod.fr24_units("/api/live/flight-positions/full", 5)
        assert count < full
        assert count == pytest.approx(1.2)

    def test_per_call_providers_bill_one_unit(self):
        for provider in ("APIDIRECT", "STAYING", "PRICELINE"):
            assert budget_mod.units_for(provider, "/anything", 50) == 1.0

    @pytest.mark.parametrize(
        "endpoint,provider",
        [
            ("/api/live/flight-positions/full", "FR24"),
            ("/v1/twitter/posts", "APIDIRECT"),
            ("/v1/news/articles", "APIDIRECT"),
            ("/search-rental-car", "PRICELINE"),
            ("/auto-complete-location", "PRICELINE"),
            ("/search", "STAYING"),
            ("/availability", "STAYING"),
            ("/account", "STAYING"),
        ],
    )
    def test_endpoint_maps_to_the_right_provider(self, endpoint, provider):
        assert budget_mod.provider_for_endpoint(endpoint) == provider

    def test_longest_prefix_wins(self):
        """/search-rental-car must not match STAYING's /search."""
        assert budget_mod.provider_for_endpoint("/search-rental-car") == "PRICELINE"
        assert budget_mod.provider_for_endpoint("/search") == "STAYING"

    def test_unmapped_endpoint_raises(self):
        with pytest.raises(ValueError):
            budget_mod.provider_for_endpoint("/who-knows")

    def test_recording_a_call_advances_the_ledger(self, db, budget):
        budget.record(
            provider="FR24", endpoint="/api/live/flight-positions/full",
            records_returned=3, http_status=200,
        )
        assert db.units_used("FR24") == 24.0

    def test_failed_calls_are_still_charged(self, db, budget):
        """A 402 or 429 consumed a request; an optimistic ledger discovers
        exhaustion by outage rather than by a graceful skip."""
        budget.record(
            provider="PRICELINE", endpoint="/search-rental-car",
            http_status=429, error_message="rate limited",
        )
        assert db.units_used("PRICELINE") == 1.0

    def test_dry_run_charges_nothing(self, db, config):
        config["dry_run"] = True
        guard = budget_mod.BudgetGuard(db, config)
        guard.record(
            provider="FR24", endpoint="/api/live/flight-positions/full",
            records_returned=100,
        )
        assert db.units_used("FR24") == 0.0

    def test_remote_balance_below_the_ledger_wins(self, db, config):
        guard = budget_mod.BudgetGuard(db, config)
        guard.seed_budgets()
        guard.record(provider="STAYING", endpoint="/search", records_returned=1)
        guard.reconcile_staying(remote_available=5.0)
        assert guard.remaining("STAYING")["remaining"] == pytest.approx(5.0)
        assert db.all(
            "SELECT * FROM agent_log WHERE agent = 'BudgetGuard' "
            "AND level = 'WARNING'"
        )

    def test_iteration_plan_is_persisted_and_returned(self, db, budget, iteration):
        plan = budget.plan_iteration(iteration)
        assert set(plan) >= {"APIDIRECT", "FR24", "STAYING", "PRICELINE"}
        row = db.get_iteration(iteration)
        assert row["budget_plan_json"]


class TestAlertRoundTrip:
    def test_alert_as_tuple_has_the_four_specified_parts(
        self, db, session, iteration
    ):
        city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        signals = [
            db.insert_signal(
                iteration_id=iteration, signal_type="SOCIAL", city_id=city,
                url="https://x/1", author="a", platform="twitter", salience=0.9,
            ),
            db.insert_signal(
                iteration_id=iteration, signal_type="FLIGHT", city_id=city,
                fr24_id="f1", flight_category="M", category_confidence="CONFIRMED",
                flight_status="airborne_inbound", eta="2026-07-27T19:40:00Z",
            ),
            db.insert_signal(
                iteration_id=iteration, signal_type="LODGING", city_id=city,
                provider_ref="L1", near_available=2, base_available=30,
                drop_pct=93.3,
            ),
            db.insert_signal(
                iteration_id=iteration, signal_type="CAR", city_id=city,
                provider_ref="PHX", vehicle_class="ECAR", people_capacity=5,
                near_available=1, base_available=20, drop_pct=95.0,
                is_on_airport=True,
            ),
        ]
        correlation_id = db.upsert_correlation(
            iteration_id=iteration, city_id=city, track="AIRSHOW",
            score=0.82, band="HIGH", distinct_types=4,
            contributions={"social": 0.3}, data_completeness=1.0,
            rule_trace="test",
        )
        for signal_id in signals:
            db.link_correlation_signal(correlation_id, signal_id, 0.1)
        alert_id = db.insert_alert(
            correlation_id=correlation_id, session_id=session,
            iteration_id=iteration, city_id=city, track="AIRSHOW",
            confidence_score=0.82, confidence_band="HIGH",
            summary="Two military aircraft inbound.", model="test-model",
            earliest_eta="2026-07-27T19:40:00Z",
        )

        rows = db.get_alerts(session)
        assert len(rows) == 1
        alert = Alert.from_rows(rows[0], db.correlation_signals(correlation_id))

        social, flights, lodging, cars = alert.as_tuple()
        assert len(social) == 1 and len(flights) == 1
        assert len(lodging) == 1 and len(cars) == 1
        assert alert.city == "Phoenix, AZ"
        assert alert.alert_id == alert_id

        # The fields the old pipeline collected and then dropped at parse time.
        assert flights[0].eta == "2026-07-27T19:40:00Z"
        assert flights[0].status == "airborne_inbound"
        assert flights[0].category == "M"
        assert flights[0].category_confidence == "CONFIRMED"
        assert cars[0].people_capacity == 5
        assert cars[0].is_on_airport is True

        positional = alert.as_positional()
        assert len(positional) == 4
        assert all(isinstance(group, list) for group in positional)

    def test_alerts_filter_by_minimum_band(self, db, session, iteration):
        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        for band, score in (("LOW", 0.2), ("HIGH", 0.9)):
            correlation_id = db.upsert_correlation(
                iteration_id=iteration, city_id=city,
                track="AIRSHOW" if band == "LOW" else "CONCERT_TOUR",
                score=score, band=band, distinct_types=2, contributions={},
                data_completeness=1.0, rule_trace="t",
            )
            db.insert_alert(
                correlation_id=correlation_id, session_id=session,
                iteration_id=iteration, city_id=city,
                track="AIRSHOW" if band == "LOW" else "CONCERT_TOUR",
                confidence_score=score, confidence_band=band, summary="s",
                model="m",
            )
        assert len(db.get_alerts(session)) == 2
        assert len(db.get_alerts(session, min_band="MEDIUM")) == 1

    def test_correlation_upsert_is_idempotent(self, db, session, iteration):
        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        kwargs = dict(
            iteration_id=iteration, city_id=city, track="AIRSHOW",
            distinct_types=1, contributions={}, data_completeness=1.0,
            rule_trace="t",
        )
        first = db.upsert_correlation(score=0.2, band="LOW", **kwargs)
        second = db.upsert_correlation(score=0.9, band="HIGH", **kwargs)
        assert first == second
        assert db.get_correlation(first)["band"] == "HIGH"


class TestGeoCache:
    def test_round_trip(self, db):
        db.put_geo_cache("AIRPORT", "phoenix", ["PHX"], resolved_by="TABLE")
        assert db.get_geo_cache("AIRPORT", "phoenix") == ["PHX"]

    def test_unresolved_entry_reads_as_a_miss_but_records_the_attempt(self, db):
        """So callers stop retrying, while the audit trail keeps the attempt."""
        db.put_geo_cache("AIRPORT", "nowhere", [], resolved_by="UNRESOLVED")
        assert db.get_geo_cache("AIRPORT", "nowhere") is None
        assert db.geo_cache_attempted("AIRPORT", "nowhere") is True

    def test_expired_entry_reads_as_a_miss(self, db):
        db.put_geo_cache("LISTING_SET", "k", [1], ttl_days=1)
        db._exec(
            "UPDATE geo_cache SET expires_at = ? WHERE lookup_key = 'k'",
            (iso(utcnow() - timedelta(days=1)),),
        )
        assert db.get_geo_cache("LISTING_SET", "k") is None

    def test_put_is_an_upsert(self, db):
        db.put_geo_cache("AIRPORT", "phoenix", ["PHX"])
        db.put_geo_cache("AIRPORT", "phoenix", ["PHX", "AZA"])
        assert db.get_geo_cache("AIRPORT", "phoenix") == ["PHX", "AZA"]
        assert db.scalar("SELECT COUNT(*) FROM geo_cache") == 1


class TestSessionsAndIterations:
    def test_iteration_sequence_increments_per_session(self, db):
        first_session = db.insert_session(label="a")
        second_session = db.insert_session(label="b")
        db.insert_iteration(first_session)
        db.insert_iteration(first_session)
        db.insert_iteration(second_session)
        assert db.get_iteration(2)["seq"] == 2
        assert db.get_iteration(3)["seq"] == 1

    def test_session_requires_at_least_one_track(self, db):
        with pytest.raises(ValueError):
            db.insert_session(tracks=[])

    def test_invalid_track_is_rejected(self, db):
        # MissionError now, not EnumViolation: the permitted set is the
        # mission's, so the refusal names the mission it was checked against.
        # Both are ValueError, which is what the API layer catches.
        with pytest.raises(ValueError) as exc:
            db.insert_session(tracks=["NOPE"])
        assert "NOPE" in str(exc.value)
        assert "reference" in str(exc.value)

    def test_foreign_keys_are_enforced(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_city(9999, "Ghost", canonical="ghost")

    def test_duplicate_city_in_one_session_is_rejected(self, db, session):
        db.insert_city(session, "Phoenix", canonical="phoenix")
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_city(session, "Phoenix", canonical="phoenix")


class TestFileBackedDatabase:
    def test_wal_is_enabled_for_file_databases(self, tmp_path):
        path = tmp_path / "surge.db"
        with SurgeDB(path, mission=REFERENCE_MISSION) as db:
            mode = db.scalar("PRAGMA journal_mode")
            assert mode.lower() == "wal"

    def test_data_survives_reopening(self, tmp_path, mission):
        """The whole reason for file-backed storage: follow-on queries and the
        audit trail must outlive the process."""
        path = tmp_path / "surge.db"
        with SurgeDB(path, mission=mission) as db:
            session = db.insert_session(label="persist")
        with SurgeDB(path, mission=mission) as db:
            assert db.get_session(session)["label"] == "persist"

    def test_missing_file_can_be_required_to_exist(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            SurgeDB(tmp_path / "absent.db", create_if_missing=False, mission=REFERENCE_MISSION)
