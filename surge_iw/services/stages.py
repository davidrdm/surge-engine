"""Per-stage inspection and rollback — the debugging surface.

Two operations sit on one declaration. `STAGE_EFFECTS` says, for each of the
eight stages, exactly which rows that stage writes. Inspection SELECTs them;
rollback DELETEs them. Deriving both from the same statement is the point: a
stage whose effects are described wrongly is a stage that inspects wrongly *and*
rolls back wrongly, and one test catches both.

**Attribution is declared, not inferred.** The obvious alternative — attribute
rows by the time window in the stage's `agent_runs` record — looks equivalent
and is not. Stage timings shift under a resume, a re-run rewrites the window,
and rows written by the orchestrator between stages fall inside somebody's
bracket. The stages write disjoint row-sets by construction, so naming those
sets is exact and stays exact.

**What rollback will not delete.**

  * `api_calls` — the money is a fact about the world, not an output of the
    stage. Discard a collection stage and re-run it and the ledger correctly
    shows you paid twice; that is what stops rollback from being a way to make
    spending disappear from the budget guard's view.
  * `agent_log` — the audit trail records that a discard happened. Erasing it
    would make the one destructive operation in the system the only one with no
    record.
  * Cities admitted by a tip. They are referenced by signals and by later
    queries, and re-running TIPPING against an already-admitted city produces
    the identical query set, so removing them would buy nothing and risk
    orphans. Reported as `not_reverted` rather than silently left behind.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..agents.orchestrator import ORCHESTRATOR_AGENT, PIPELINE_STAGES
from ..db.database import SurgeDB

#: Source types collected by each of the two collection stages. Kept here rather
#: than imported from the orchestrator because rollback needs the split by
#: *stage*, and the orchestrator's tuples overlap: LODGING appears in both of
#: its tipped passes.
SOCIAL_SOURCE_TYPES: tuple[str, ...] = ("SOCIAL",)
TIPPED_SOURCE_TYPES: tuple[str, ...] = (
    "FLIGHT_COUNT", "FLIGHT_LIVE", "FLIGHT_HISTORY",
    "LODGING", "LODGING_PRICE", "CAR",
)

#: Stages that record queueing decisions. Keyed on `queue_decisions.stage`, not
#: on rule_code: the flight escalation inside COLLECTING_TIPPED re-uses
#: R4_LODGING and R5_CAR, so the code alone cannot say which stage decided.
DECIDING_STAGES: frozenset[str] = frozenset(
    {"SEEDING", "TRIAGING", "TIPPING", "COLLECTING_TIPPED", "SCHEDULING"}
)


@dataclass(frozen=True)
class StageEffect:
    """One table a stage writes, as a predicate over the iteration."""

    table: str
    #: SQL fragment after `WHERE`, with `?` bound to the parameters below.
    where: str
    params: Sequence[Any] = ()
    #: Human label used in both the inspection counts and the rollback report.
    label: str = ""

    def count(self, db: SurgeDB, iteration_id: int) -> int:
        return int(db.scalar(
            f"SELECT COUNT(*) FROM {self.table} WHERE {self.where}",
            (iteration_id, *self.params),
        ))

    def delete(self, db: SurgeDB, iteration_id: int) -> int:
        return db._exec(
            f"DELETE FROM {self.table} WHERE {self.where}",
            (iteration_id, *self.params),
        )


def _decisions(stage: str) -> StageEffect:
    return StageEffect(
        "queue_decisions", "iteration_id = ? AND stage = ?", (stage,),
        "queue_decisions",
    )


def _queries(stage: str, origins: Sequence[str], *, scheduled: bool) -> StageEffect:
    placeholders = ",".join("?" * len(origins))
    # created_iteration_id, not iteration_id: scheduled rows deliberately own no
    # iteration until a later stage 1 claims them, and two iterations can each
    # write an identical follow-on because SQLite treats NULLs as distinct in
    # idx_qq_dedup. Keying on the owner would delete the other one's work.
    owner = "iteration_id IS NULL" if scheduled else "iteration_id IS NOT NULL"
    return StageEffect(
        "query_queue",
        f"created_iteration_id = ? AND {owner} AND origin IN ({placeholders})",
        origins, "query_queue",
    )


#: What each stage writes. Order within a stage is delete order, so a child
#: table always precedes its parent.
STAGE_EFFECTS: dict[str, tuple[StageEffect, ...]] = {
    "SEEDING": (
        _queries("SEEDING", ("SEED",), scheduled=False),
        _decisions("SEEDING"),
    ),
    "COLLECTING_SOCIAL": (
        StageEffect(
            "raw_results",
            "iteration_id = ? AND source_type IN "
            f"({','.join('?' * len(SOCIAL_SOURCE_TYPES))})",
            SOCIAL_SOURCE_TYPES, "raw_results",
        ),
    ),
    "TRIAGING": (
        StageEffect("triage_decisions", "iteration_id = ?", (),
                    "triage_decisions"),
        StageEffect("signals", "iteration_id = ? AND signal_type = 'SOCIAL'",
                    (), "signals"),
        # City admission is decided during triage, not tipping: the signal needs
        # a city_id before any rule can tip on it.
        _decisions("TRIAGING"),
    ),
    "TIPPING": (
        _queries("TIPPING", ("TIP",), scheduled=False),
        _decisions("TIPPING"),
    ),
    "COLLECTING_TIPPED": (
        StageEffect(
            "raw_results",
            "iteration_id = ? AND source_type IN "
            f"({','.join('?' * len(TIPPED_SOURCE_TYPES))})",
            TIPPED_SOURCE_TYPES, "raw_results",
        ),
        StageEffect(
            "signals",
            "iteration_id = ? AND signal_type IN ('FLIGHT','LODGING','CAR')",
            (), "signals",
        ),
        _decisions("COLLECTING_TIPPED"),
    ),
    "CORRELATING": (
        StageEffect(
            "correlation_signals",
            "correlation_id IN (SELECT correlation_id FROM correlations "
            "WHERE iteration_id = ?)", (), "correlation_signals",
        ),
        StageEffect("correlations", "iteration_id = ?", (), "correlations"),
    ),
    "ALERTING": (
        StageEffect("alerts", "iteration_id = ?", (), "alerts"),
    ),
    "SCHEDULING": (
        _queries("SCHEDULING", ("SCHEDULED", "CARRIED_FORWARD"), scheduled=True),
        _decisions("SCHEDULING"),
    ),
}

#: Stages whose rollback means the next run re-spends real money at a vendor.
#: Everything downstream of collection is arithmetic and prose over rows that
#: are already paid for.
PAID_STAGES: frozenset[str] = frozenset(
    {"COLLECTING_SOCIAL", "COLLECTING_TIPPED"}
)

#: Statuses a rollback resets to PENDING so the stage can collect again.
#: INTERRUPTED is included: a query stranded by a crash and then abandoned is
#: still a query this stage owns, and discarding the stage must not leave it
#: behind as a permanent coverage gap on a stage that no longer exists.
_EXECUTED = ("IN_PROGRESS", "COMPLETE", "FAILED", "INTERRUPTED",
             "SKIPPED_BUDGET", "SKIPPED_NO_MAPPING")


@dataclass
class StageReport:
    """What one stage did, as far as the database can say."""

    stage: str
    status: str                      # PENDING | RUNNING | COMPLETE | FAILED
    started_at: str | None = None
    finished_at: str | None = None
    error_message: str | None = None
    #: Rows this stage wrote that are still present, by table.
    wrote: dict[str, int] = field(default_factory=dict)
    #: Refusals recorded by this stage, by outcome. Empty for stages that make
    #: no queueing decisions.
    decisions: dict[str, int] = field(default_factory=dict)
    #: Which agents ran inside the stage, and how each fared.
    agents: list[dict[str, Any]] = field(default_factory=list)
    #: Paid calls attributed to the stage, by provider.
    api_calls: dict[str, dict[str, float]] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)
    #: CORRELATING only: what it actually scored, not just how many rows it
    #: wrote (8.7b). `wrote: {correlations: 2}` was the entire account of a
    #: stage whose output is the analytical conclusion — and for a correlation
    #: that produced no alert, the only account anywhere outside SQLite.
    correlations: list[dict[str, Any]] = field(default_factory=list)
    #: TRIAGING only: collected posts that never reached the model, by reason
    #: (8.9). Recording them without a route would answer the operator's
    #: question — "this post is in the payload and not in the evidence, why?" —
    #: only for someone willing to open the database.
    skips: dict[str, int] = field(default_factory=dict)


class StageInspector:
    """Read-only: what happened in a stage, and what it left behind."""

    def __init__(self, db: SurgeDB) -> None:
        self.db = db

    def stage_records(self, iteration_id: int) -> dict[str, Any]:
        """The orchestrator's own run row per stage, keyed by stage."""
        return {
            row["stage"]: row
            for row in self.db.all(
                "SELECT * FROM agent_runs WHERE iteration_id = ? AND agent = ? "
                "ORDER BY run_id",
                (iteration_id, ORCHESTRATOR_AGENT),
            )
        }

    def last_completed_stage(self, iteration_id: int) -> str | None:
        """The furthest stage that actually ran, successfully or not.

        Runs, not the pointer: `iterations.stage` says where the iteration will
        go next, which after a failure is not where it has been. A stage that
        FAILED still wrote rows, so it is still the thing to discard — and so
        does a stage that was INTERRUPTED, which is why only RUNNING is skipped.
        """
        records = self.stage_records(iteration_id)
        ran = [s for s in PIPELINE_STAGES
               if s in records and records[s]["status"] != "RUNNING"]
        return ran[-1] if ran else None

    def in_flight_stage(self, iteration_id: int) -> str | None:
        """The furthest stage whose orchestrator run row is still RUNNING.

        The exact complement of `last_completed_stage`, and the reason both
        exist: that one answers "what should be discarded", this one answers
        "where were we". A crash leaves the answer in the one state the other
        deliberately skips.

        Read once, by the reconcile, *before* it rewrites the status — and its
        answer is persisted to `iterations.interrupted_stage`, because
        `start_agent_run` destroys the row the moment the stage runs again.
        """
        records = self.stage_records(iteration_id)
        live = [s for s in PIPELINE_STAGES
                if s in records and records[s]["status"] == "RUNNING"]
        return live[-1] if live else None

    def report(self, iteration_id: int, stage: str) -> StageReport:
        if stage not in PIPELINE_STAGES:
            raise ValueError(
                f"{stage!r} is not a pipeline stage; expected one of "
                f"{list(PIPELINE_STAGES)}"
            )
        record = self.stage_records(iteration_id).get(stage)
        report = StageReport(
            stage=stage,
            status=record["status"] if record else "PENDING",
            started_at=record["started_at"] if record else None,
            finished_at=record["finished_at"] if record else None,
            error_message=record["error_message"] if record else None,
        )
        if record is None:
            return report

        for effect in STAGE_EFFECTS.get(stage, ()):
            count = effect.count(self.db, iteration_id)
            report.wrote[effect.label] = report.wrote.get(effect.label, 0) + count

        if stage in DECIDING_STAGES:
            report.decisions = {
                row["outcome"]: int(row["n"])
                for row in self.db.all(
                    "SELECT outcome, COUNT(*) AS n FROM queue_decisions "
                    "WHERE iteration_id = ? AND stage = ? GROUP BY outcome",
                    (iteration_id, stage),
                )
            }

        report.agents = [
            {"agent": row["agent"], "status": row["status"],
             "started_at": row["started_at"], "finished_at": row["finished_at"],
             "error_message": row["error_message"]}
            for row in self.db.all(
                "SELECT * FROM agent_runs WHERE iteration_id = ? AND stage = ? "
                "AND agent != ? ORDER BY run_id",
                (iteration_id, stage, ORCHESTRATOR_AGENT),
            )
        ]
        if stage == "CORRELATING":
            report.correlations = self._correlations(iteration_id)
        if stage == "TRIAGING":
            report.skips = self.db.triage_skip_counts(iteration_id)

        report.api_calls = self._api_calls(iteration_id, record)
        report.log = self._log(iteration_id, record)
        return report

    def _correlations(self, iteration_id: int) -> list[dict[str, Any]]:
        """What CORRELATING concluded, per city and track.

        Deliberately includes the ones that scored below the alerting floor:
        those are the near misses, they are what the interim floors are supposed
        to be calibrated against, and they produce no alert to reach them by.
        `alert_decision` is null here until ALERTING runs, which is honest —
        this stage does not decide it.
        """
        return [
            {"correlation_id": int(row["correlation_id"]),
             "city": (self.db.one("SELECT name FROM cities WHERE city_id = ?",
                                  (row["city_id"],)) or {"name": ""})["name"],
             "track": row["track"],
             "score": round(float(row["score"]), 4),
             "band": row["band"],
             "distinct_types": int(row["distinct_types"]),
             "data_completeness": round(float(row["data_completeness"]), 4),
             "band_capped": bool(row["band_capped"]),
             "rule_trace": row["rule_trace"],
             "alert_decision": row["alert_decision"],
             "alert_decision_reason": row["alert_decision_reason"],
             "evidence_url":
                 f"/v1/correlations/{int(row['correlation_id'])}/evidence"}
            for row in self.db.get_correlations(iteration_id)
        ]

    def report_all(self, iteration_id: int) -> list[StageReport]:
        """Every stage including the ones that have not run.

        A stage the iteration never reached is as much a part of "what
        happened" as one that did.
        """
        return [self.report(iteration_id, stage) for stage in PIPELINE_STAGES]

    # ------------------------------------------------------------------
    # Time-scoped detail
    # ------------------------------------------------------------------

    def _window(self, record: Mapping[str, Any]) -> tuple[str, str]:
        """The stage's wall-clock bracket, open-ended while it is running."""
        return (record["started_at"], record["finished_at"] or "9999")

    def _api_calls(
        self, iteration_id: int, record: Mapping[str, Any]
    ) -> dict[str, dict[str, float]]:
        """Spend inside the stage window.

        The one place a time window is the right tool: `api_calls` has no
        stage-shaped predicate, and unlike the analytical tables it is never
        deleted, so the window cannot drift out from under it.
        """
        start, end = self._window(record)
        rows = self.db.all(
            "SELECT provider, COUNT(*) AS calls, SUM(units) AS units, "
            "       SUM(COALESCE(records_returned, 0)) AS records "
            "FROM api_calls WHERE iteration_id = ? AND called_at >= ? "
            "  AND called_at <= ? GROUP BY provider",
            (iteration_id, start, end),
        )
        return {
            row["provider"]: {
                "calls": int(row["calls"]),
                "units": round(float(row["units"] or 0.0), 3),
                "records": int(row["records"] or 0),
            }
            for row in rows
        }

    def _log(
        self, iteration_id: int, record: Mapping[str, Any], limit: int = 200
    ) -> list[dict[str, Any]]:
        start, end = self._window(record)
        return [
            {"agent": row["agent"], "level": row["level"],
             "message": row["message"], "logged_at": row["logged_at"]}
            for row in self.db.all(
                "SELECT * FROM agent_log WHERE iteration_id = ? "
                "AND logged_at >= ? AND logged_at <= ? ORDER BY log_id LIMIT ?",
                (iteration_id, start, end, limit),
            )
        ]


@dataclass
class RollbackReport:
    """What a discard removed, and what it deliberately did not."""

    iteration_id: int
    stage: str
    deleted: dict[str, int] = field(default_factory=dict)
    queries_reset: int = 0
    #: Spend already incurred by this stage. Not reclaimed — re-running it
    #: spends again, and the ledger will show both.
    units_spent: dict[str, float] = field(default_factory=dict)
    not_reverted: list[str] = field(default_factory=list)
    #: Degradation notes this stage had recorded, retracted with its rows.
    degradations_retracted: int = 0
    #: Where the iteration now points.
    stage_now: str = ""


class StageRollback:
    """Discard the output of the most recent stage so it can be re-run.

    Only the most recent stage, and one at a time. Discarding TRIAGING while
    TIPPING's queries still exist would leave queue rows tipped by signals that
    no longer exist — the guarantee that every query traces back to the post
    that caused it is exactly what this must not break. Repeated calls walk
    backwards, so any point in the pipeline is still reachable.
    """

    agent_name = "StageRollback"

    def __init__(self, db: SurgeDB) -> None:
        self.db = db
        self.inspector = StageInspector(db)

    def target(self, iteration_id: int) -> str | None:
        return self.inspector.last_completed_stage(iteration_id)

    def spend(self, iteration_id: int, stage: str) -> dict[str, float]:
        """Units this stage has already billed, by provider."""
        record = self.inspector.stage_records(iteration_id).get(stage)
        if record is None:
            return {}
        return {provider: detail["units"]
                for provider, detail in
                self.inspector._api_calls(iteration_id, record).items()}

    def discard_last(
        self, iteration_id: int, *, expect: str | None = None,
        confirm: bool = False,
    ) -> RollbackReport:
        """Remove the last stage's output and point the iteration back at it.

        `confirm` is required for a stage that made paid calls. It is not
        ceremony: FR24 bills per record returned and one historical query was
        measured at 60 credits, so re-running a collection stage is a real
        purchase and the caller should have to say so.
        """
        iteration = self.db.get_iteration(iteration_id)
        if iteration is None:
            raise ValueError(f"No iteration {iteration_id}")
        stage = self.target(iteration_id)
        if stage is None:
            raise ValueError(
                f"Iteration {iteration_id} has no completed stage to discard."
            )
        if expect is not None and expect != stage:
            raise ValueError(
                f"Last stage of iteration {iteration_id} is {stage}, not {expect}."
            )

        spent = self.spend(iteration_id, stage)
        if stage in PAID_STAGES and not confirm:
            total = sum(spent.values())
            raise PermissionError(
                f"Discarding {stage} means collecting again at the vendors. "
                f"{total:.0f} unit(s) already spent here will not be reclaimed "
                f"and re-running will spend more. Pass confirm=true to proceed."
            )

        report = RollbackReport(iteration_id, stage, units_spent=spent)
        for effect in STAGE_EFFECTS.get(stage, ()):
            removed = effect.delete(self.db, iteration_id)
            if removed:
                report.deleted[effect.label] = (
                    report.deleted.get(effect.label, 0) + removed
                )

        if stage == "COLLECTING_SOCIAL":
            report.queries_reset = self._reset(iteration_id, SOCIAL_SOURCE_TYPES)
        elif stage == "COLLECTING_TIPPED":
            report.queries_reset = self._reset(iteration_id, TIPPED_SOURCE_TYPES)
        elif stage == "SEEDING":
            # Adopted follow-ons were not created by this iteration, so the
            # delete above left them alone; hand them back to the future.
            report.queries_reset = self.db._exec(
                "UPDATE query_queue SET iteration_id = NULL, status = 'PENDING' "
                "WHERE iteration_id = ? AND created_iteration_id IS NOT ? "
                "  AND origin IN ('SCHEDULED','CARRIED_FORWARD')",
                (iteration_id, iteration_id),
            )
        elif stage == "TIPPING":
            report.not_reverted.append(
                "cities admitted by tip in this iteration remain admitted; "
                "re-running TIPPING against them enqueues the same queries"
            )

        self.db._exec(
            "UPDATE iterations SET stage = ?, outcome = NULL, "
            "finished_at = NULL, error_message = NULL WHERE iteration_id = ?",
            (stage, iteration_id),
        )
        self.db._exec(
            "DELETE FROM agent_runs WHERE iteration_id = ? AND stage = ?",
            (iteration_id, stage),
        )
        # The rows a stage wrote and the notes it wrote about what it could NOT
        # write are one record. Deleting the first while keeping the second
        # leaves the iteration asserting a gap that no longer exists — and
        # `_finish` reads degradations to decide PARTIAL, so it stays degraded
        # by it. Observed in a real database: TRIAGING truncated at a low token
        # ceiling, was discarded, re-ran at a higher one and judged all twenty
        # posts, and the iteration still carried "10 of 20 post(s) were not
        # judged".
        report.degradations_retracted = self.db.discard_degradations(
            iteration_id, stage)
        report.stage_now = stage

        # Loud on purpose. This is the one operation that removes analytical
        # records, so the record that it happened has to survive it.
        self.db.log(
            self.agent_name, "WARNING",
            f"Discarded stage {stage} of iteration {iteration_id}",
            iteration_id=iteration_id, stage=stage,
            deleted=report.deleted or None,
            queries_reset=report.queries_reset or None,
            units_already_spent=spent or None,
        )
        return report

    def _reset(self, iteration_id: int, source_types: Sequence[str]) -> int:
        """Return executed queries to PENDING so collection can claim them."""
        types = ",".join("?" * len(source_types))
        statuses = ",".join("?" * len(_EXECUTED))
        return self.db._exec(
            f"UPDATE query_queue SET status = 'PENDING', executed_at = NULL, "
            f"  result_count = NULL, error_message = NULL, skip_reason = NULL "
            f"WHERE iteration_id = ? AND source_type IN ({types}) "
            f"  AND status IN ({statuses})",
            (iteration_id, *source_types, *_EXECUTED),
        )
