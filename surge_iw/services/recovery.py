"""Crash recovery: telling an interrupted iteration from a running one.

The database is file-backed *specifically* so an interrupted run survives, and
`IterationOrchestrator` has been able to resume one since Phase 4. What was
missing was the ability to know that a run *needs* resuming: an iteration killed
mid-stage is byte-for-byte identical in the database to one still executing —
`outcome IS NULL`, `finished_at IS NULL`, an `agent_runs` row RUNNING, some
queries IN_PROGRESS. Nothing distinguished them, so nothing could act.

**A process epoch is what distinguishes them.** Each process records one
`process_epochs` row and stamps `iterations.owner_epoch_id` on the runs it
starts. Interruption is then structural — owned by an epoch that is not this one
and not finished — with no clock in the predicate.

A heartbeat would have been the obvious alternative and is wrong here.
Legitimate silences in this system are minutes long: Staying's `/search` was
measured at 125 seconds and polls to 420, and FR24 is paced at one request every
six seconds. Any threshold that cleared those would be too coarse to catch a
real crash promptly, and a false positive costs a duplicate collection pass in
real money. The epoch also makes a crash *simulable in-process* — open a second
epoch against the same database — which is why this phase needs no subprocess,
no signals and no clock control to test.

**Reconcile marks; it never decides.** Startup records the interruption and
stops. Resuming automatically would let a crash loop re-buy FR24 records on
every restart, and the choice between re-collecting and counting the loss as a
coverage gap belongs to an operator. There are exactly two exits, and both write
a record:

    resume    IN_PROGRESS -> PENDING, then run from the interrupted stage
    abandon   IN_PROGRESS -> INTERRUPTED, then correlate, alert, and close

Abandon still correlates and alerts. An iteration that crashed before
`CORRELATING` and is merely closed produces no alert at all for a city whose
evidence was 80% collected — a real cluster reading as silence, which is the
worst failure this system has available to it.
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..agents.orchestrator import PIPELINE_STAGES
from ..db.database import SurgeDB
from ..services.budget import provider_for_endpoint
from ..services.stages import StageInspector

AGENT_NAME = "RecoveryService"

#: Source types whose collection legitimately produces no signal, so a banked
#: payload with no signal is complete rather than half-done. SOCIAL signals are
#: written by triage, a stage later; FLIGHT_COUNT returns only a number.
NO_SIGNAL_EXPECTED: frozenset[str] = frozenset({"SOCIAL", "FLIGHT_COUNT"})


class RecoveryRequired(RuntimeError):
    """Raised when an iteration must be reconciled before it can run again."""


@dataclass
class ReconcileReport:
    """What opening a process epoch found and did."""

    epoch_id: int
    host: str
    pid: int
    #: Epochs found open and closed as UNKNOWN — i.e. processes that died.
    closed_epochs: list[int] = field(default_factory=list)
    #: Epochs left alone because their process is demonstrably alive.
    refused_epochs: list[int] = field(default_factory=list)
    #: Iterations newly marked interrupted by this reconcile.
    interrupted: list[int] = field(default_factory=list)
    #: Already marked by an earlier reconcile and still awaiting a decision.
    outstanding: list[int] = field(default_factory=list)
    #: Sessions whose run slot was held by a process that has since died (8.6).
    #: Reported rather than silently swept: a freed lock means somebody's
    #: iteration stopped without releasing it, which is worth an operator
    #: knowing even though the fix is automatic.
    freed_session_locks: list[int] = field(default_factory=list)

    @property
    def needs_attention(self) -> list[int]:
        return sorted(set(self.interrupted) | set(self.outstanding))


@dataclass
class ResumePlan:
    """What resuming an iteration would do, before it does any of it."""

    iteration_id: int
    session_id: int
    resume_from: str
    derived_by: str
    stage_pointer: str
    interrupted_stage: str | None = None
    #: Queries that would be executed again. Each entry names why.
    queries_to_recollect: list[dict[str, Any]] = field(default_factory=list)
    #: Paid for, payload on disk, not re-bought. Settled by the reconcile.
    already_banked: list[dict[str, Any]] = field(default_factory=list)
    #: Units already billed against this iteration, per provider.
    already_spent: dict[str, float] = field(default_factory=dict)
    #: An UPPER BOUND, not a price. FR24 bills per record *returned*, so no
    #: pre-flight number can be exact; `queries_to_recollect` is the honest
    #: artefact and the operator judges from it.
    estimated_units_upper_bound: dict[str, float] = field(default_factory=dict)

    @property
    def paid(self) -> bool:
        return bool(self.queries_to_recollect)


class RecoveryService:
    """Reconcile, plan, resume, abandon."""

    def __init__(self, db: SurgeDB, config: Mapping[str, Any] | None = None):
        self.db = db
        self.config = config or {}
        self.inspector = StageInspector(db)

    # ------------------------------------------------------------------
    # Opening a process
    # ------------------------------------------------------------------

    def open_epoch(self, entry_point: str = "cli") -> ReconcileReport:
        """Record this process and reconcile whatever the last one left.

        Called as the very first thing every entry point does, because
        `SurgeDB.start_agent_run` erases a stranded RUNNING row and a stage that
        re-runs before this has happened destroys its own evidence.

        Deliberately NOT done in `SurgeDB.__init__`: hundreds of tests and two
        read-only CLI commands construct one, and a constructor that closes
        other processes' iterations as a side effect is a landmine.
        """
        host = socket.gethostname()
        epoch = self.db.open_epoch(host=host, pid=os.getpid(),
                                   entry_point=entry_point)
        epoch_id = int(epoch["epoch_id"])
        report = ReconcileReport(epoch_id=epoch_id, host=host, pid=os.getpid())

        alive: set[int] = set()
        for stale in self.db.open_epochs(before=epoch_id):
            stale_id = int(stale["epoch_id"])
            if self._still_alive(stale):
                alive.add(stale_id)
                report.refused_epochs.append(stale_id)
                self.db.log(
                    AGENT_NAME, "ERROR",
                    f"Epoch {stale_id} ({stale['host']}:{stale['pid']}) is "
                    "still alive; refusing to reconcile its iterations. Since "
                    "8.6 the session lock lives in the database, so two "
                    "processes sharing it is no longer a correctness problem "
                    "— but this process cannot tell a healthy sibling worker "
                    "from a hung one, and reconciling a LIVE peer's iterations "
                    "would mark work interrupted while it is still running.",
                    epoch_id=stale_id,
                )
                continue
            self.db.close_epoch(stale_id, "UNKNOWN", closed_by=epoch_id)
            report.closed_epochs.append(stale_id)

        for row in self.db.unfinished_iterations(not_owned_by=epoch_id):
            if int(row["owner_epoch_id"] or 0) in alive:
                continue
            iteration_id = int(row["iteration_id"])
            if self._mark_interrupted(iteration_id, epoch):
                report.interrupted.append(iteration_id)
            else:
                report.outstanding.append(iteration_id)

        # Free run slots held by processes that are gone (8.6). Ordered after
        # the epoch sweep above, which is what turns a dead predecessor's epoch
        # from "open" into "ended" — the predicate this depends on. Without
        # this a crash would wedge the session permanently, because the lock now
        # outlives the process that took it.
        report.freed_session_locks = self.db.clear_stale_session_locks(epoch_id)
        if report.freed_session_locks:
            self.db.log(
                AGENT_NAME, "WARNING",
                f"Released the run slot on session(s) "
                f"{report.freed_session_locks}: held by a process that is no "
                "longer running.",
                epoch_id=epoch_id,
            )

        # Already marked by an earlier process and still undecided.
        for row in self.db.interrupted_iterations():
            iteration_id = int(row["iteration_id"])
            if iteration_id not in report.interrupted:
                report.outstanding.append(iteration_id)
        report.outstanding = sorted(set(report.outstanding))

        self.db.log(
            AGENT_NAME, "WARNING" if report.needs_attention else "INFO",
            f"Epoch {epoch_id} opened ({entry_point})"
            + (f"; {len(report.interrupted)} iteration(s) newly interrupted"
               if report.interrupted else "")
            + (f"; {len(report.outstanding)} awaiting a decision"
               if report.outstanding else ""),
            epoch_id=epoch_id, entry_point=entry_point,
            interrupted=report.interrupted or None,
            outstanding=report.outstanding or None,
            refused_epochs=report.refused_epochs or None,
        )
        return report

    @staticmethod
    def _still_alive(epoch: Mapping[str, Any]) -> bool:
        """Whether the process behind an open epoch is demonstrably running.

        `workers=1` makes "any prior epoch is dead" true by construction — so
        assert it rather than assume it. Three lines, and it turns a silent
        money bug (two processes reconciling each other's live work) into a
        loud refusal the day someone drops that setting.

        Only a same-host check is possible, and a recycled pid can produce a
        false positive. Refusing to reconcile is the safe direction: the
        operator sees the ERROR and the iteration stays visible.
        """
        if epoch["host"] != socket.gethostname():
            return False
        if int(epoch["pid"]) == os.getpid():
            # An earlier epoch of *this* process, not a live peer. A concurrent
            # process cannot share my pid, and after a reboot a recycled pid
            # names a process that is genuinely gone. Either way it is stale.
            return False
        try:
            os.kill(int(epoch["pid"]), 0)
        except (OSError, ValueError):
            return False
        return True

    def _mark_interrupted(
        self, iteration_id: int, epoch: Mapping[str, Any]
    ) -> bool:
        """Record the interruption. Returns False if already recorded.

        Order matters: the in-flight stage is read *first*, before the run rows
        that carry it are rewritten.
        """
        stage = self.inspector.in_flight_stage(iteration_id)
        if stage is None:
            stage = self.db.get_iteration(iteration_id)["stage"]
            if stage not in PIPELINE_STAGES:
                stage = None

        detail = (f"interrupted: owning process ended without finishing "
                  f"(epoch {epoch['epoch_id']})")
        closed = self.db.interrupt_agent_runs(iteration_id, detail)
        settled = self._settle_banked_work(iteration_id)

        if not self.db.mark_interrupted(iteration_id, stage):
            return False

        note = (f"interrupted during {stage or 'an unrecorded stage'}; "
                f"{len(closed)} agent run(s) closed, "
                f"{settled} banked query(ies) settled")
        self._add_degradation(iteration_id, note)
        self.db.log(
            AGENT_NAME, "WARNING",
            f"Iteration {iteration_id} marked interrupted at "
            f"{stage or 'unknown stage'}",
            iteration_id=iteration_id, stage=stage,
            agent_runs_closed=len(closed) or None,
            banked_settled=settled or None,
        )
        return True

    def _settle_banked_work(self, iteration_id: int) -> int:
        """Close out queries whose data was already paid for and stored.

        `CollectionAgent` writes `raw_results` per vendor call but marks the
        query COMPLETE only after its handler returns, so a crash can leave the
        money spent and the payload on disk with the query still IN_PROGRESS.
        Resuming naively would buy it a second time.

        Three outcomes, and the middle one is the careful part:

          raw + a signal        -> COMPLETE. Paid and derived; nothing to redo.
          raw, no signal        -> INTERRUPTED. Do not re-buy — the money is
                                   gone and the payload is on disk — but do not
                                   claim coverage either, because a COMPLETE row
                                   with no signal reads as "we looked and found
                                   nothing", the exact confusion this system
                                   exists to prevent.
          no raw                -> left IN_PROGRESS for the operator to decide.

        SOCIAL and FLIGHT_COUNT legitimately produce no signal at collection
        time, so for them "raw exists" is the whole test.
        """
        settled = 0
        for row in self.db.all(
            "SELECT * FROM query_queue WHERE iteration_id = ? "
            "AND status = 'IN_PROGRESS' ORDER BY query_id",
            (iteration_id,),
        ):
            query_id = int(row["query_id"])
            raw = self.db.one(
                "SELECT raw_id FROM raw_results WHERE query_id = ? LIMIT 1",
                (query_id,),
            )
            if raw is None:
                continue
            has_signal = self.db.one(
                "SELECT 1 FROM signals s JOIN raw_results r USING (raw_id) "
                "WHERE r.query_id = ? LIMIT 1",
                (query_id,),
            ) is not None
            if has_signal or row["source_type"] in NO_SIGNAL_EXPECTED:
                self.db.complete_query(query_id, self._raw_count(query_id))
            else:
                self.db.interrupt_query(
                    query_id,
                    "payload stored but no signal derived before the process "
                    "ended; not re-collected and not counted as coverage",
                )
            settled += 1
        return settled

    def _raw_count(self, query_id: int) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM raw_results WHERE query_id = ?", (query_id,)
        ))

    def _add_degradation(self, iteration_id: int, note: str) -> None:
        """Recorded under "recovery", never a stage: an operator's decision to
        stop is not something a stage rollback may retract."""
        self.db.append_degradation(
            iteration_id, note, source=SurgeDB.DEGRADATION_RECOVERY)

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def is_interrupted(self, iteration_id: int) -> bool:
        """Crash-stamped and unresolved. A strict subset of `is_unfinished`."""
        row = self.db.get_iteration(iteration_id)
        return bool(row and row["interrupted_at"] and not row["finished_at"])

    def is_unfinished(self, iteration_id: int) -> bool:
        """Open by any route, which is what recovery must be able to act on.

        `plan()`, `resume()` and `abandon()` never needed `interrupted_at`:
        `resume_point()` falls back to the in-flight stage, then to the last
        completed one, then to NOTHING_RAN, and both exits operate on whatever
        queries and stages it finds. Only the API's precondition was narrower —
        which meant that once 8.7(a) blocked a merely-open iteration, the
        operator would have had a blocked session and no way out.
        """
        row = self.db.get_iteration(iteration_id)
        return bool(row and not row["finished_at"])

    def open_kind(self, iteration_id: int) -> str | None:
        """INTERRUPTED, OPEN, or None if the iteration has closed."""
        row = self.db.get_iteration(iteration_id)
        if row is None or row["finished_at"]:
            return None
        return "INTERRUPTED" if row["interrupted_at"] else "OPEN"

    def resume_point(self, iteration_id: int) -> tuple[str, str]:
        """Where to restart, and how that was decided.

        Priority order, most durable first. `iterations.stage` is deliberately
        not a source: `_run_stage` sets it at the top of a stage, `step()`
        advances it past one, and `StageRollback` rewrites it — three different
        meanings for one column.
        """
        row = self.db.get_iteration(iteration_id)
        if row is None:
            raise ValueError(f"No iteration {iteration_id}")

        stage = row["interrupted_stage"]
        if stage in PIPELINE_STAGES:
            return stage, "RECONCILED"

        stage = self.inspector.in_flight_stage(iteration_id)
        if stage is not None:
            return stage, "IN_FLIGHT"

        last = self.inspector.last_completed_stage(iteration_id)
        if last is None:
            return PIPELINE_STAGES[0], "NOTHING_RAN"
        index = PIPELINE_STAGES.index(last)
        if index + 1 < len(PIPELINE_STAGES):
            # Died in the gap between stages: the last one completed cleanly,
            # so the next one is the point.
            return PIPELINE_STAGES[index + 1], "BETWEEN_STAGES"
        return last, "BETWEEN_STAGES"

    def plan(self, iteration_id: int) -> ResumePlan:
        """What a resume would re-collect and what it would leave alone."""
        row = self.db.get_iteration(iteration_id)
        if row is None:
            raise ValueError(f"No iteration {iteration_id}")
        resume_from, derived_by = self.resume_point(iteration_id)
        plan = ResumePlan(
            iteration_id=iteration_id,
            session_id=int(row["session_id"]),
            resume_from=resume_from,
            derived_by=derived_by,
            stage_pointer=row["stage"],
            interrupted_stage=row["interrupted_stage"],
        )
        if plan.stage_pointer != resume_from:
            self.db.log(
                AGENT_NAME, "INFO",
                f"Iteration {iteration_id}: stage pointer says "
                f"{plan.stage_pointer}, resume point is {resume_from} "
                f"({derived_by})",
                iteration_id=iteration_id,
            )

        for query in self.db.all(
            "SELECT * FROM query_queue WHERE iteration_id = ? "
            "AND status IN ('PENDING','IN_PROGRESS') ORDER BY query_id",
            (iteration_id,),
        ):
            entry = {
                "query_id": int(query["query_id"]),
                "source_type": query["source_type"],
                "endpoint": query["endpoint"],
                "provider": provider_for_endpoint(query["endpoint"]),
                "city_id": query["city_id"],
                "status": query["status"],
                "reason": ("claimed but never recorded"
                           if query["status"] == "IN_PROGRESS"
                           else "never claimed"),
            }
            plan.queries_to_recollect.append(entry)
            provider = entry["provider"]
            plan.estimated_units_upper_bound[provider] = round(
                plan.estimated_units_upper_bound.get(provider, 0.0)
                + self._upper_bound(provider, query["endpoint"]), 3
            )

        for query in self.db.all(
            "SELECT * FROM query_queue WHERE iteration_id = ? "
            "AND status = 'INTERRUPTED' ORDER BY query_id",
            (iteration_id,),
        ):
            plan.already_banked.append({
                "query_id": int(query["query_id"]),
                "source_type": query["source_type"],
                "provider": provider_for_endpoint(query["endpoint"]),
                "detail": query["error_message"],
            })

        from ..db import enums
        for provider in sorted(enums.PROVIDERS):
            spent = self.db.units_used(provider, iteration_id=iteration_id)
            if spent:
                plan.already_spent[provider] = round(spent, 3)
        return plan

    def _upper_bound(self, provider: str, endpoint: str) -> float:
        """A generous ceiling per query, for the plan's warning only.

        FR24 bills per record returned, so this cannot be a price. It is sized
        from the configured record cap so the number errs high rather than
        reassuring an operator with an under-estimate.
        """
        if provider == "FR24":
            limit = float((self.config.get("flightradar") or {}).get(
                "live_limit", 20))
            return limit * (3.0 if "flight-summary" in endpoint else 8.0)
        if provider == "STAYING":
            return 20.0
        return 1.0

    # ------------------------------------------------------------------
    # The two exits
    # ------------------------------------------------------------------

    def prepare_resume(self, iteration_id: int, epoch_id: int) -> int:
        """Hand stranded queries back to the collector. Returns how many.

        Not done by the reconcile: resetting to PENDING there would silently
        pre-authorise a re-spend that nobody has agreed to.
        """
        reset = self.db._exec(
            "UPDATE query_queue SET status = 'PENDING' "
            "WHERE iteration_id = ? AND status = 'IN_PROGRESS'",
            (iteration_id,),
        )
        self.db.set_owner_epoch(iteration_id, epoch_id)
        self.db.log(
            AGENT_NAME, "WARNING",
            f"Resuming iteration {iteration_id}; {reset} stranded query(ies) "
            "returned to PENDING and will be collected again",
            iteration_id=iteration_id, reset=reset or None, epoch_id=epoch_id,
        )
        return reset

    def abandon(
        self, iteration_id: int, reason: str, *, epoch_id: int | None = None
    ) -> dict[str, Any]:
        """Convert stranded collection into a recorded coverage gap.

        Marking the queries is only half of it. `unreliable_source_types` has
        exactly one reader — `CorrelationAgent` — so an iteration abandoned
        before CORRELATING would close with no correlation and no alert at all,
        for a city whose evidence may be almost complete. The caller runs
        `finalise()` next; this prepares the ground for it.
        """
        marked = self.db._exec(
            "UPDATE query_queue SET status = 'INTERRUPTED', error_message = ? "
            "WHERE iteration_id = ? AND status IN ('PENDING','IN_PROGRESS')",
            (f"abandoned: {reason}"[:2000], iteration_id),
        )
        if epoch_id is not None:
            self.db.set_owner_epoch(iteration_id, epoch_id)
        gaps = self._coverage_gaps(iteration_id)
        # On the ITERATION, not only in the log. `_finish` decides COMPLETE vs
        # PARTIAL from the degradation list plus its own gap count, and a log
        # line reaches neither. Abandoning is an operator's decision to stop
        # collecting, which is a fact about this run that no gap count can
        # express — and an abandoned iteration that closes COMPLETE is the exact
        # failure this system exists to prevent.
        if marked or gaps:
            self.db.append_degradation(
                iteration_id,
                f"RecoveryService: iteration abandoned ({reason}); {marked} "
                f"query(ies) marked INTERRUPTED and counted as coverage gaps")
        self.db.log(
            AGENT_NAME, "WARNING",
            f"Abandoning iteration {iteration_id}: {reason}. "
            f"{marked} query(ies) marked INTERRUPTED and counted as coverage "
            "gaps.",
            iteration_id=iteration_id, marked=marked or None,
            coverage_gaps=gaps or None,
        )
        return {"queries_marked_interrupted": marked, "coverage_gaps": gaps}

    def _coverage_gaps(self, iteration_id: int) -> dict[str, int]:
        from ..db import enums

        placeholders = ",".join("?" * len(enums.UNRELIABLE_QUERY_STATUSES))
        rows = self.db.all(
            f"SELECT source_type, COUNT(*) AS n FROM query_queue "
            f"WHERE iteration_id = ? AND status IN ({placeholders}) "
            f"GROUP BY source_type",
            (iteration_id, *sorted(enums.UNRELIABLE_QUERY_STATUSES)),
        )
        return {row["source_type"]: int(row["n"]) for row in rows}

    # ------------------------------------------------------------------
    # Guard
    # ------------------------------------------------------------------

    def assert_recoverable(self, iteration_id: int) -> None:
        """Refuse to re-run an iteration whose interruption is unresolved."""
        if self.is_interrupted(iteration_id):
            raise RecoveryRequired(
                f"Iteration {iteration_id} was interrupted and has not been "
                "resumed or abandoned. Running it again would overwrite the "
                "record of what was lost."
            )


def open_process_epoch(
    db: SurgeDB, config: Mapping[str, Any] | None = None,
    entry_point: str = "cli",
) -> ReconcileReport:
    """Convenience wrapper — every entry point's first statement."""
    return RecoveryService(db, config).open_epoch(entry_point)


def describe(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Interrupted iterations, shaped for an API response or a CLI table."""
    return [{
        "iteration_id": int(row["iteration_id"]),
        "session_id": int(row["session_id"]),
        "seq": int(row["seq"]),
        "interrupted_at": row["interrupted_at"],
        "interrupted_stage": row["interrupted_stage"],
        "stage_pointer": row["stage"],
        "started_at": row["started_at"],
    } for row in rows]
