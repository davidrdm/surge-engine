"""IterationOrchestrator — the stage machine. No LLM.

The only component that instantiates agents, and the only one that knows the
order they run in. It passes an `iteration_id` and nothing else: every agent
reads its own inputs from the database and writes its outputs there, which is
what keeps "no agent calls another directly" true even though one API-triggered
iteration drives five agents in sequence.

Deliberately not a `BaseAgent` subclass. It has no analytical work of its own,
so giving it the agent lifecycle would imply a role it does not have.

**Failure is isolated per agent, not per iteration.** An agent that raises marks
its own `agent_runs` row FAILED, appends to `iterations.degradations_json`, and
returns False; the driver continues to the next stage. Only stages the rest
genuinely depends on can fail the whole run. A social-connector outage must not
discard a military-flight cluster another stage already collected — which is
exactly what an all-or-nothing driver would do.

The iteration therefore ends in one of three states:

    COMPLETE   every stage ran and nothing degraded
    PARTIAL    the sequence finished, but something was lost along the way
    FAILED     a prerequisite stage could not run at all
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ..base.connector import BaseConnector
from ..db import enums
from ..db.database import SurgeDB, parse_iso
from ..services import tunables
from ..services.budget import BudgetGuard
from ..services.retention import RetentionService
from .alerting import AlertAgent
from .collection import SOCIAL_TYPES, CollectionAgent
from .correlation import CorrelationAgent
from .queueing import QueueAgent
from .triage import TriageAgent

# Source types drained by the first tipped-collection pass. FLIGHT_HISTORY is
# excluded because it is only enqueued by the escalation that runs between the
# two passes — buying a 48-hour window before knowing whether there is anything
# to resolve is the most expensive mistake available here.
TIPPED_FIRST_PASS: tuple[str, ...] = (
    "FLIGHT_COUNT", "FLIGHT_LIVE", "LODGING", "LODGING_PRICE", "CAR",
)
TIPPED_SECOND_PASS: tuple[str, ...] = (
    "FLIGHT_HISTORY", "LODGING", "LODGING_PRICE", "CAR",
)

#: Stages whose failure makes the remainder of the iteration meaningless.
#: Kept deliberately short: almost nothing is genuinely load-bearing, because
#: each source contributes independently to the correlation.
CRITICAL_STAGES: frozenset[str] = frozenset({"SEEDING"})

#: The eight working stages, in order. COMPLETE and FAILED are terminal states
#: rather than stages and are deliberately absent.
PIPELINE_STAGES: tuple[str, ...] = (
    "SEEDING", "COLLECTING_SOCIAL", "TRIAGING", "TIPPING",
    "COLLECTING_TIPPED", "CORRELATING", "ALERTING", "SCHEDULING",
)

#: The two stages that turn already-collected evidence into an alert, and the
#: only ones a cancelled iteration still runs (8.2).
#:
#: The same set `finalise()` uses for abandon, for the same reason: an
#: iteration stopped after collection but before scoring produces NO alert at
#: all for a city whose evidence may be nearly complete, which is this system's
#: worst available failure — a real cluster reading as silence. SCHEDULING is
#: excluded because follow-ons for a run nobody finished would arrive as a
#: surprise next iteration.
FINALISE_STAGES: frozenset[str] = frozenset({"CORRELATING", "ALERTING"})

#: Stages a re-triage (8.8) does not run.
#:
#: SEEDING and COLLECTING_SOCIAL are INHERITED, not skipped: the child re-judges
#: posts the parent already collected and paid for, and re-seeding would enqueue
#: the whole social set again. They are deliberately NOT written to
#: `skipped_stages_json` — COLLECTING_SOCIAL is in `STAGE_SOURCE_TYPES`, so
#: recording it would tell CORRELATING that SOCIAL is uncollected on the one run
#: whose purpose is to improve social coverage.
#:
#: SCHEDULING IS recorded as skipped, because there the choice is real: the
#: parent has already queued follow-ons for this evidence.
_RETRY_SKIPPED: frozenset[str] = frozenset(
    {"SEEDING", "COLLECTING_SOCIAL", "SCHEDULING"}
)

#: Agent name under which the orchestrator records one `agent_runs` row per
#: stage it drives. Those rows are the per-stage audit trail: three of the eight
#: stages are QueueAgent, which is not a BaseAgent and writes no run record of
#: its own, so without these the status of SEEDING, TIPPING and SCHEDULING could
#: only be inferred.
ORCHESTRATOR_AGENT = "IterationOrchestrator"


class StageAlreadyRun(RuntimeError):
    """Raised when a step is asked for a stage the iteration is not at."""


class SessionHasOpenIteration(RuntimeError):
    """Raised when a session's unfinished iteration blocks a new one.

    Named for the predicate it actually enforces. The previous name was
    `IterationInterrupted`, and it was wrong in the way that mattered: the guard
    only ever saw crash-stamped iterations, while the API reported a merely-open
    one as PENDING and let the next trigger through. Two different things were
    called "interrupted" and only one of them blocked.

    `blocking` carries a row per offending iteration with the *kind* of open it
    is, because the two have different remedies and a 409 that does not say
    which leaves the operator to guess.
    """

    def __init__(self, session_id: int, blocking: Sequence[Mapping[str, Any]]):
        self.session_id = session_id
        self.blocking = list(blocking)
        super().__init__(self._message())

    def _message(self) -> str:
        parts = []
        for row in self.blocking:
            iteration_id = row["iteration_id"]
            if row["kind"] == "INTERRUPTED":
                parts.append(
                    f"iteration {iteration_id} was interrupted at "
                    f"{row['stage']} when its process died — resume it "
                    f"(POST /v1/iterations/{iteration_id}/resume) or abandon it "
                    f"(POST /v1/iterations/{iteration_id}/abandon)")
            else:
                parts.append(
                    f"iteration {iteration_id} is open at {row['stage']} and "
                    f"was never finished — resume it "
                    f"(POST /v1/iterations/{iteration_id}/resume) or abandon it "
                    f"(POST /v1/iterations/{iteration_id}/abandon)")
        return (
            f"Session {self.session_id} already has an unfinished iteration: "
            + "; ".join(parts)
            + ". Starting a new one now would be silently under-collected: the "
            "cooldown guard is keyed on the query hash across ALL iterations, "
            "so the open run's recent executions would suppress this one's "
            "queries with no visible cause."
        )


@dataclass(frozen=True)
class StepResult:
    """What one stage did. Returned by `step()`, surfaced by the debug API."""

    iteration_id: int
    stage: str
    ok: bool
    #: The stage a further step would run, or None once the pipeline is done.
    next_stage: str | None
    #: Set only on the step that closed the iteration.
    outcome: str | None = None


class IterationOrchestrator:
    """Drives one iteration through its stages."""

    agent_name = "IterationOrchestrator"

    def __init__(
        self,
        db: SurgeDB,
        config: Mapping[str, Any],
        connectors: Mapping[str, BaseConnector],
        llm_client: Any = None,
        budget: BudgetGuard | None = None,
    ) -> None:
        self.db = db
        #: The server's own configuration, kept separately from the one in
        #: force (9.2). Binding to a second session must start from this, not
        #: from the first session's merged result — otherwise one session's
        #: overrides would leak into the next run through the same object.
        self.base_config = config
        self.config = config
        self.connectors = connectors
        self.llm_client = llm_client
        self.budget = budget or BudgetGuard(db, config)
        self._bound_session: int | None = None
        #: True only while RecoveryService is driving. Permits replacing the
        #: stranded `agent_runs` row that would otherwise refuse the re-run.
        self._recovering = False

    # ------------------------------------------------------------------
    # Session configuration (9.2)
    # ------------------------------------------------------------------

    def _bind_session(self, session_id: int | None) -> None:
        """Build the configuration this session's work runs under, once.

        Every agent is constructed with `self.config` and every one of them
        reads its thresholds from it, so re-pointing it here is what makes a
        session's overrides reach the whole pipeline rather than a chosen
        subset. `receipts.config_fingerprint` is computed from the same object,
        which is the part that matters most: a receipt stamped from the
        process-wide configuration while the run used another one records the
        wrong answer to the only question a receipt exists to answer.

        The budget guard is rebuilt rather than reused because a session may
        lower a spending cap, and a guard still holding the server's numbers
        would admit spend the session had asked not to make. It is safe to
        replace: the guard keeps no state of its own — the ledger is
        `api_calls` and the envelope is `iterations.budget_plan_json` — so the
        connectors' `on_call` binding to the process-wide instance continues to
        write the same rows this one reads.
        """
        if session_id is None or session_id == self._bound_session:
            return
        row = self.db.get_session(session_id)
        stored: dict[str, Any] = {}
        if row is not None:
            try:
                stored = json.loads(row["config_json"] or "{}") or {}
            except (TypeError, ValueError):
                stored = {}
        self.config = tunables.effective(self.base_config, stored)
        self.budget = BudgetGuard(self.db, self.config)
        self._bound_session = session_id
        lines = tunables.describe(
            {k: v for k, v in stored.items() if k in tunables.ALLOWED})
        if lines:
            # One row with the detail in `extra_json`, not one row per setting:
            # the audit trail needs to answer "what governed this run" in a
            # single place, and a run under thirty overrides should not be
            # thirty log lines to read past.
            self._log("INFO",
                      f"Session {session_id}: {len(lines)} tunable(s) in force",
                      session_id=session_id, tunables=lines)

    def _bind_iteration(self, iteration_id: int) -> None:
        """Bind from an iteration, for the entry points that take one."""
        row = self.db.get_iteration(iteration_id)
        if row is not None:
            self._bind_session(int(row["session_id"]))

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def start(self, session_id: int, *, epoch_id: int | None = None) -> int:
        """Create an iteration and prepare its spend envelope.

        `epoch_id` records which process instance owns the run. Without it the
        iteration is unowned and a later reconcile cannot tell an interruption
        from a run that never started — which is why every caller that has an
        epoch passes it.
        """
        self._bind_session(session_id)
        session = self.db.get_session(session_id)
        if session is None:
            raise ValueError(f"No session {session_id}")
        if session["status"] != "ACTIVE":
            raise ValueError(f"Session {session_id} is {session['status']}")
        if not self.db.get_cities(session_id):
            raise ValueError(
                f"Session {session_id} has no cities; nothing to collect against"
            )

        # `finished_at IS NULL`, not `interrupted_at IS NOT NULL` (8.7a). The
        # narrower predicate only ever saw a crash the reconcile had stamped,
        # so a manual walk left half-done — the ordinary case when the API is
        # driven by hand — reported PENDING and blocked nothing. The hazard is
        # the same either way: the cooldown guard is keyed on
        # (dedup_key, executed_at) across ALL iterations, so an open run's
        # recent executions silently suppress a new one's queries.
        open_rows = self.db.open_iterations(session_id)
        if open_rows:
            raise SessionHasOpenIteration(session_id, [
                {"iteration_id": int(row["iteration_id"]),
                 "kind": "INTERRUPTED" if row["interrupted_at"] else "OPEN",
                 "stage": row["interrupted_stage"] or row["stage"]}
                for row in open_rows
            ])

        iteration_id = self.db.insert_iteration(
            session_id, owner_epoch_id=epoch_id
        )
        self._warn_if_mission_moved(session, iteration_id)
        self.budget.seed_budgets()
        self._reconcile_remote_budgets(iteration_id)
        self.budget.plan_iteration(iteration_id)
        self._log("INFO", f"Iteration {iteration_id} created",
                  iteration_id=iteration_id, session_id=session_id)
        return iteration_id

    def _warn_if_mission_moved(self, session: Any, iteration_id: int) -> None:
        """Say so when this session's definition is not the one loaded now.

        A session records the mission label it was created under, because a
        track name is meaningless without the definition that gave it meaning.
        The pack can legitimately move underneath it — a mission is versioned
        precisely so it can — and refusing here would strand a running session
        on a pack bump. But a session created under one definition and scored
        under another has had an analytical decision made for it, and this
        system does not make those in silence.

        Found live: a session recorded `reference/1` ran under `reference/2`,
        which had split the social weight row into two streams. Every receipt
        named the pack it actually used, so the judgements were auditable —
        but nothing anywhere said the definition had changed mid-session.
        """
        recorded = session["mission"] if "mission" in session.keys() else None
        mission = getattr(self.db, "mission", None)
        current = getattr(mission, "label", None)
        if not recorded or not current or recorded == current:
            return
        self._log(
            "WARNING",
            f"Session {int(session['session_id'])} was created under mission "
            f"{recorded} and is running under {current}. Weights, lexicon and "
            f"prompts may have moved; scores from this iteration are not "
            f"comparable to earlier ones without reading both packs",
            iteration_id=iteration_id,
            session_id=int(session["session_id"]),
            session_mission=recorded, loaded_mission=current,
        )

    def run(
        self, iteration_id: int, from_stage: str | None = None,
        *, recovering: bool = False,
    ) -> str:
        """Run the stage sequence. Returns the iteration outcome.

        Two different skip rules, and they do not compose:

          * `from_stage` given — every stage from that point runs, and the
            database pointer is NOT consulted. Nothing is re-collected anyway,
            because the agents are re-entrant: `claim_next_query` takes only
            PENDING rows, triage seeds its seen-set from `triaged_urls()`,
            alerting skips correlations that already have an alert, and
            `upsert_correlation` upserts.
          * `from_stage` omitted — stages strictly before the pointer are
            skipped, and the stage it names is re-run.

        `recovering` is set only by RecoveryService, and is what allows a
        stranded run row to be replaced.
        """
        iteration = self.db.get_iteration(iteration_id)
        if iteration is None:
            raise ValueError(f"No iteration {iteration_id}")
        self._bind_session(int(iteration["session_id"]))
        if from_stage is not None:
            enums.validate(from_stage, enums.STAGES, "from_stage")
            if from_stage not in PIPELINE_STAGES:
                # 'COMPLETE' would give start_index 8, skip all eight stages,
                # and fall straight into _finish() — marking an iteration
                # COMPLETE having done nothing at all.
                raise ValueError(
                    f"from_stage={from_stage!r} is a terminal state, not a "
                    f"stage to run. Expected one of {list(PIPELINE_STAGES)}."
                )
        if not recovering:
            self._assert_not_interrupted(iteration)
        self._recovering = recovering
        session_id = int(iteration["session_id"])

        start_index = enums.stage_index(from_stage) if from_stage else 0
        current_index = enums.stage_index(iteration["stage"])
        degraded: list[str] = []
        failed_critical: str | None = None

        cancelled = False
        skipped: list[str] = []
        for stage, handler in self._pipeline():
            index = enums.stage_index(stage)
            if index < start_index:
                continue
            if from_stage is None and index < current_index:
                # Already past this stage. Only reachable on a bare re-run.
                self._log("INFO", f"Skipping {stage}; already past it",
                          iteration_id=iteration_id)
                continue

            # 8.2. Cooperative cancellation, checked at the boundary rather
            # than mid-stage: a stage that has issued a paid request must be
            # allowed to record what it bought.
            if not cancelled and self.db.cancel_requested(iteration_id):
                cancelled = True
                self._log("WARNING", "Cancellation requested; running only "
                                     "CORRELATING and ALERTING from here",
                          iteration_id=iteration_id, at_stage=stage)
            if cancelled and stage not in FINALISE_STAGES:
                # Skipped, not failed — and recorded, so the gap is visible.
                # Unjudged posts and uncollected queries already flow into
                # data_completeness, which caps the band and names the gap in
                # the caveat. Cancelling cannot launder a partial run into
                # full confidence.
                degraded.append((stage, f"{stage} skipped: cancelled"))
                skipped.append(stage)
                # Recorded BEFORE CORRELATING runs, because that is the reader.
                self.db.record_skipped_stages(iteration_id, skipped)
                continue

            ok = self._run_stage(iteration_id, session_id, stage, handler)
            if not ok:
                degraded.append((stage, stage))
                if stage in CRITICAL_STAGES:
                    failed_critical = stage
                    break

        outcome = self._finish(iteration_id, degraded, failed_critical)
        self._prune(iteration_id)
        self._recovering = False
        return outcome

    def retry_triage(
        self, parent_id: int, *, epoch_id: int | None = None,
        batch_size: int | None = None,
    ) -> tuple[int, str]:
        """Prepare and run a re-triage. Returns (child_iteration_id, outcome).

        Split in two so the API can refuse synchronously — before any row
        exists — and run the stages on a worker. A refusal that had already
        created the child would leave a hole in `seq` for every attempt that
        was never allowed to start.
        """
        child_id = self.prepare_retry_triage(parent_id, epoch_id=epoch_id)
        return child_id, self.run_retry_triage(
            child_id, parent_id, batch_size=batch_size)

    def prepare_retry_triage(
        self, parent_id: int, *, epoch_id: int | None = None
    ) -> int:
        """Validate, then create the child iteration. Runs no stage.

        Returns the child's id.

        **The parent is never edited.** It stays as it was — partial, degraded,
        its gap named — and the child says what it is. Two records in the order
        they happened, neither rewritten after the fact, which is what a
        reviewer six months later can reconstruct without being told.

        Three properties carry the design:

        * **The child inherits `anchor_at`.** The correlation window is
          `anchor - window_hours`, so a fresh anchor would slide it forward and
          drop the oldest of the parent's evidence — the very evidence the
          child exists to complete. `started_at` still records when it actually
          ran, so the two facts stay separable.
        * **Nothing is copied.** `CorrelationAgent` reads signals across the
          session by observation time, not by iteration, so the child scores the
          union of both runs' evidence for free. That is why a new iteration
          costs so little here.
        * **SEEDING and COLLECTING_SOCIAL do not run**, because the child would
          otherwise enqueue the whole social set again and re-buy evidence
          already held. They are not recorded as skipped either: that would tell
          CORRELATING that SOCIAL is uncollected, on a run whose entire purpose
          is to improve social coverage.
        """
        parent = self.db.get_iteration(parent_id)
        if parent is None:
            raise ValueError(f"No iteration {parent_id}")
        if not parent["finished_at"]:
            raise ValueError(
                f"Iteration {parent_id} has not closed "
                f"({parent['stage']}). Finish, resume or abandon it before "
                "retrying its triage — a retry is a new iteration, and a "
                "session runs one at a time.")

        session_id = int(parent["session_id"])
        self._bind_session(session_id)
        open_rows = self.db.open_iterations(session_id)
        if open_rows:
            raise SessionHasOpenIteration(session_id, [
                {"iteration_id": int(row["iteration_id"]),
                 "kind": "INTERRUPTED" if row["interrupted_at"] else "OPEN",
                 "stage": row["interrupted_stage"] or row["stage"]}
                for row in open_rows
            ])

        candidates = self.db.uncovered_triage_decisions(parent_id)
        if not candidates:
            raise ValueError(
                f"Iteration {parent_id} has no unjudged posts to retry. Only "
                f"{', '.join(sorted(enums.TRIAGE_UNCOVERED))} decisions are "
                "retryable; ACCEPTED and REJECTED are completed judgements and "
                "a rejection is a conclusion, not a failure.")

        child_id = self.db.insert_iteration(
            session_id,
            # The parent's moment, not this one. See the docstring.
            anchor_at=parse_iso(parent["anchor_at"]),
            owner_epoch_id=epoch_id,
            retry_of_iteration_id=parent_id,
        )
        self.budget.seed_budgets()
        self.budget.plan_iteration(child_id)
        # Owner decision: the child skips SCHEDULING and records the skip. The
        # parent already enqueued follow-ons for the same evidence, and those
        # rows carry `iteration_id IS NULL`, so a duplicate set would all be
        # adopted by the next ordinary iteration. It contributes no source
        # types to `source_types_for_skipped`, and should not: nothing about
        # this iteration's coverage is worse for not having re-queued work that
        # is already queued.
        self.db.record_skipped_stages(child_id, ["SCHEDULING"])
        self._log(
            "INFO",
            f"Iteration {child_id} is a re-triage of {parent_id}: "
            f"{len(candidates)} unjudged post(s), anchored at "
            f"{parent['anchor_at']}. SEEDING and COLLECTING_SOCIAL are not run "
            f"— their evidence is the parent's and is already held.",
            iteration_id=child_id, retry_of=parent_id,
            candidates=len(candidates),
        )
        self.db.append_degradation(
            child_id,
            f"Re-triage of iteration {parent_id}: SEEDING and COLLECTING_SOCIAL "
            f"inherited rather than re-run; SCHEDULING skipped because the "
            f"parent already queued follow-ons for this evidence")
        return child_id

    def run_retry_triage(
        self, child_id: int, parent_id: int, *, batch_size: int | None = None
    ) -> str:
        """Run the child's stages: TRIAGING through ALERTING."""
        child = self.db.get_iteration(child_id)
        if child is None:
            raise ValueError(f"No iteration {child_id}")
        session_id = int(child["session_id"])
        self._bind_session(session_id)

        degraded: list[str] = []
        failed_critical: str | None = None
        cancelled = False
        skipped = ["SCHEDULING"]
        for stage, handler in self._pipeline():
            if stage in _RETRY_SKIPPED:
                continue

            # 8.2's cooperative cancellation applies here too. A retry issues
            # paid tipped collection, so it is exactly as cancellable as an
            # ordinary run — and a cancel that a retry silently ignored would
            # be the contract holding for one path and not the other.
            if not cancelled and self.db.cancel_requested(child_id):
                cancelled = True
                self._log("WARNING", "Cancellation requested; running only "
                                     "CORRELATING and ALERTING from here",
                          iteration_id=child_id, at_stage=stage)
            if cancelled and stage not in FINALISE_STAGES:
                degraded.append((stage, f"{stage} skipped: cancelled"))
                skipped.append(stage)
                # Recorded BEFORE CORRELATING runs, because that is the reader.
                self.db.record_skipped_stages(child_id, skipped)
                continue

            if stage == "TRIAGING":
                ok = self._run_stage(
                    child_id, session_id, stage, handler,
                    retry_of=parent_id, batch_size=batch_size)
            else:
                ok = self._run_stage(child_id, session_id, stage, handler)
            if not ok:
                degraded.append((stage, stage))
                if stage in CRITICAL_STAGES:
                    failed_critical = stage
                    break

        outcome = self._finish(child_id, degraded, failed_critical)
        self._prune(child_id)
        return outcome

    def finalise(self, iteration_id: int) -> str:
        """Score and write alerts for an iteration that will collect no more.

        The abandon path. It runs CORRELATING and ALERTING and nothing else —
        never a collection stage, and not SCHEDULING, because follow-ons for an
        iteration nobody finished would arrive as a surprise next run.

        Skipping the scoring entirely would be the tempting shortcut and the
        wrong one: `unreliable_source_types` has exactly one reader, so an
        iteration abandoned before CORRELATING would close with no correlation
        and no alert at all — for a city whose evidence may be almost complete.
        A real cluster reading as silence is the worst outcome this system has.
        """
        iteration = self.db.get_iteration(iteration_id)
        if iteration is None:
            raise ValueError(f"No iteration {iteration_id}")
        session_id = int(iteration["session_id"])
        self._bind_session(session_id)
        self._recovering = True
        degraded: list[str] = []
        try:
            for stage, handler in self._pipeline():
                if stage not in ("CORRELATING", "ALERTING"):
                    continue
                if not self._run_stage(iteration_id, session_id, stage, handler):
                    degraded.append((stage, stage))
            interrupted_at = iteration["interrupted_stage"]
            critical = (interrupted_at
                        if interrupted_at in CRITICAL_STAGES else None)
            outcome = self._finish(iteration_id, degraded, critical)
        finally:
            self._recovering = False
        self._prune(iteration_id)
        return outcome

    def _assert_not_interrupted(self, iteration: Mapping[str, Any]) -> None:
        """Refuse to re-run an iteration whose interruption is unresolved.

        The second of three layers. `start_agent_run` erases a stranded RUNNING
        row, so a stage that runs before the interruption has been recorded
        destroys its own evidence — and a future entry point that forgets to
        open an epoch would do exactly that, silently.
        """
        if iteration["interrupted_at"] and not iteration["finished_at"]:
            from ..services.recovery import RecoveryRequired

            raise RecoveryRequired(
                f"Iteration {iteration['iteration_id']} was interrupted at "
                f"{iteration['interrupted_stage'] or 'an unrecorded stage'} "
                "and has not been resumed or abandoned. Running it again would "
                "overwrite the record of what was lost."
            )

    def step(
        self, iteration_id: int, *, expect: str | None = None
    ) -> StepResult:
        """Run exactly one stage — the next one — and stop.

        The debugging counterpart to `run()`. It advances the same pointer
        `iterations.stage` and writes the same records, so an iteration can be
        stepped, then finished with `run()`, or the reverse, without either
        entry point knowing which produced the state it found.

        `expect` is a guard, not a selector: it names the stage the caller
        believes is next and raises if the iteration is somewhere else. Without
        it a client that lost track of the pointer silently runs the wrong
        stage and spends real money doing it.
        """
        iteration = self.db.get_iteration(iteration_id)
        if iteration is None:
            raise ValueError(f"No iteration {iteration_id}")
        self._assert_not_interrupted(iteration)
        session_id = int(iteration["session_id"])
        self._bind_session(session_id)
        stage = iteration["stage"]

        if stage not in PIPELINE_STAGES:
            raise StageAlreadyRun(
                f"Iteration {iteration_id} is {stage}; there is no next stage. "
                "Discard a stage first to re-run it."
            )
        if expect is not None and expect != stage:
            raise StageAlreadyRun(
                f"Iteration {iteration_id} is at {stage}, not {expect}."
            )

        handler = dict(self._pipeline())[stage]
        ok = self._run_stage(iteration_id, session_id, stage, handler)
        if not ok:
            # Persisted, because unlike run() the caller does not survive
            # between stages and cannot carry the list in memory.
            self._add_degradation(iteration_id, f"stage {stage} degraded",
                                  source=stage)

        failed_critical = stage if (not ok and stage in CRITICAL_STAGES) else None
        index = PIPELINE_STAGES.index(stage)
        is_last = index == len(PIPELINE_STAGES) - 1

        if failed_critical or is_last:
            outcome = self._finish(iteration_id, [], failed_critical)
            self._prune(iteration_id)
            return StepResult(iteration_id, stage, ok, None, outcome)

        next_stage = PIPELINE_STAGES[index + 1]
        self.db.set_stage(iteration_id, next_stage)
        return StepResult(iteration_id, stage, ok, next_stage)

    def next_stage(self, iteration_id: int) -> str | None:
        """The stage a `step()` would run, or None if the iteration is closed."""
        iteration = self.db.get_iteration(iteration_id)
        if iteration is None:
            raise ValueError(f"No iteration {iteration_id}")
        stage = iteration["stage"]
        return stage if stage in PIPELINE_STAGES else None

    def resume(self, iteration_id: int, from_stage: str) -> str:
        """Restart a stalled iteration at a named stage.

        Nothing special is needed to avoid re-collecting: `claim_next_query`
        only returns PENDING rows, so queries already COMPLETE, FAILED or
        SKIPPED are left alone, and IN_PROGRESS rows left behind by a killed
        process are reset first.

        The budget envelope is *not* recomputed. `plan_iteration` ran at
        `start()` and its result is on the row; `can_afford` compares against
        the units already billed to this iteration, so a resumed run inherits
        the original allowance minus what the crashed run spent — and N crash
        cycles cannot exceed one envelope. The remote reconcile is redone,
        because it is free and the local Staying ledger may be stale.
        """
        enums.validate(from_stage, enums.STAGES, "from_stage")
        self._bind_iteration(iteration_id)
        reset = self._reset_stranded_queries(iteration_id)
        self._reconcile_remote_budgets(iteration_id)
        self._log("INFO",
                  f"Resuming iteration {iteration_id} at {from_stage}"
                  + (f"; reset {reset} stranded quer(ies)" if reset else ""),
                  iteration_id=iteration_id, from_stage=from_stage,
                  reset=reset or None)
        return self.run(iteration_id, from_stage=from_stage, recovering=True)

    # ------------------------------------------------------------------
    # The pipeline
    # ------------------------------------------------------------------

    def _pipeline(self) -> Sequence[tuple[str, Callable[[int, int], bool]]]:
        """Ordered stages. Every entry names a real handler.

        `iw` used a `None` AgentClass to mean "run something in-process", which
        is the kind of implicit branch that makes a state machine hard to resume
        correctly. Here each stage is an explicit callable.
        """
        return (
            ("SEEDING", self._stage_seed),
            ("COLLECTING_SOCIAL", self._stage_collect_social),
            ("TRIAGING", self._stage_triage),
            ("TIPPING", self._stage_tip),
            ("COLLECTING_TIPPED", self._stage_collect_tipped),
            ("CORRELATING", self._stage_correlate),
            ("ALERTING", self._stage_alert),
            ("SCHEDULING", self._stage_schedule),
        )

    def _run_stage(
        self,
        iteration_id: int,
        session_id: int,
        stage: str,
        handler: Callable[..., bool],
        **kwargs: Any,
    ) -> bool:
        """Execute one stage and record that it ran.

        The `agent_runs` row is written under the orchestrator's own name, one
        per stage, and spans the whole stage rather than any single agent inside
        it. COLLECTING_TIPPED runs a collection pass, an escalation and a second
        collection pass; this is what gives that stage a start, an end and a
        status, and it is the record both stage inspection and rollback key on.
        """
        run_id = self.db.start_agent_run(
            iteration_id, ORCHESTRATOR_AGENT, stage,
            replace_running=self._recovering,
        )
        self.db.set_stage(iteration_id, stage)
        error: str | None = None
        try:
            ok = handler(iteration_id, session_id, **kwargs)
        except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            self._log("ERROR", f"Stage {stage} raised: {exc}",
                      iteration_id=iteration_id, exc_type=type(exc).__name__)
        self.db.finish_agent_run(
            run_id, "COMPLETE" if ok else "FAILED",
            error or (None if ok else f"stage {stage} reported failure"),
        )
        return ok

    def _stage_seed(self, iteration_id: int, session_id: int) -> bool:
        agent = QueueAgent(self.db, self.config, budget=self.budget,
                           stage="SEEDING")
        counts = agent.run_seed(iteration_id, session_id)
        if counts["seeded"] == 0 and counts["adopted"] == 0:
            # Nothing to collect. Treated as a critical failure because every
            # later stage reads from a queue that would be empty, and an
            # iteration that quietly does nothing is worse than one that says so.
            self._log("ERROR", "Seeding produced no queries",
                      iteration_id=iteration_id)
            return False
        self._warn_if_envelope_cannot_cover(iteration_id)
        return True

    def _warn_if_envelope_cannot_cover(self, iteration_id: int) -> None:
        """Say so when the spend envelope is smaller than what was just queued.

        Budget starvation is refused per query, with a reason, so nothing is
        lost silently at that level — but WHICH queries get refused is decided
        by queue order, and queue order groups by city. Measured across seven
        metros: the fair share came out at 73 against 168 seeded queries, so
        three cities collected in full, one collected a single query and three
        collected nothing at all. Every city still got a correlation row.

        Nothing here changes the outcome; it makes the shape of the shortfall
        visible at the moment it becomes inevitable, rather than leaving an
        operator to infer it from a skip count afterwards.
        """
        plan = self.db.get_iteration(iteration_id)
        queued = self.db.count_queued(iteration_id)
        try:
            envelope = json.loads(plan["budget_plan_json"] or "{}")
        except (TypeError, ValueError):
            return
        social = envelope.get("APIDIRECT")
        if social is None or queued <= social:
            return
        cities = len(self.db.get_cities(int(plan["session_id"])))
        self._log(
            "WARNING",
            f"Spend envelope covers {social:.0f} of {queued} queued queries. "
            f"About {queued - social:.0f} will be refused for budget, and "
            f"which ones follows QUEUE ORDER — across {cities} cities that "
            f"means whole cities may collect nothing while others collect in "
            f"full. Raise budget.per_iteration_cap, or lower "
            f"budget.iterations_per_month_planned so the fair share stops "
            f"binding below the fan-out.",
            iteration_id=iteration_id, envelope=social, queued=queued,
            cities=cities,
        )

    def _stage_collect_social(self, iteration_id: int, session_id: int) -> bool:
        return self._collect(iteration_id, SOCIAL_TYPES, "COLLECTING_SOCIAL")

    def _stage_triage(
        self, iteration_id: int, session_id: int, *,
        retry_of: int | None = None, batch_size: int | None = None,
    ) -> bool:
        if self.llm_client is None:
            self._log("WARNING",
                      "No LLM client configured; skipping triage. Social posts "
                      "stay collected but unjudged, so no social signals exist "
                      "for tipping.",
                      iteration_id=iteration_id)
            return False
        return TriageAgent(self.db, self.config, self.llm_client).run(
            iteration_id, retry_of=retry_of, batch_size=batch_size)

    def _stage_tip(self, iteration_id: int, session_id: int) -> bool:
        agent = QueueAgent(self.db, self.config, budget=self.budget,
                           stage="TIPPING")
        agent.run_tip(iteration_id, session_id)
        return True

    def _stage_collect_tipped(self, iteration_id: int, session_id: int) -> bool:
        """Two collection passes with an escalation between them.

        The first pass returns live flights, lodging and cars. The escalation
        then decides, from what actually arrived, whether to buy the expensive
        historical window and whether a flight signal has tipped a city whose
        bookings nobody queried. The second pass collects whatever that added.
        """
        first = self._collect(
            iteration_id, TIPPED_FIRST_PASS, "COLLECTING_TIPPED"
        )

        agent = QueueAgent(self.db, self.config, budget=self.budget,
                           stage="COLLECTING_TIPPED")
        escalated = agent.run_escalate(iteration_id, session_id)

        if not escalated:
            return first
        # Both passes share one agent_runs key, so the second replaces the
        # first. The stage-level record above still reports FAILED, because
        # `first and second` carries the first pass's failure forward, and the
        # agent's own degradation note is already on the iteration.
        second = self._collect(
            iteration_id, TIPPED_SECOND_PASS, "COLLECTING_TIPPED"
        )
        return first and second

    def _stage_correlate(self, iteration_id: int, session_id: int) -> bool:
        return CorrelationAgent(self.db, self.config).run(iteration_id)

    def _stage_alert(self, iteration_id: int, session_id: int) -> bool:
        """Write alerts. Degrades to nothing rather than blocking the run.

        Correlations are already durable at this point, so a missing model
        costs the prose but not the finding — `GET /alerts` would be empty while
        the evidence remains fully queryable, and the next iteration can write
        them once a client is configured.
        """
        if self.llm_client is None:
            self._log("WARNING",
                      "No LLM client configured; correlations are recorded but "
                      "no alerts were written.",
                      iteration_id=iteration_id)
            return False
        return AlertAgent(self.db, self.config, self.llm_client).run(iteration_id)

    def _stage_schedule(self, iteration_id: int, session_id: int) -> bool:
        agent = QueueAgent(self.db, self.config, budget=self.budget,
                           stage="SCHEDULING")
        agent.run_schedule(iteration_id, session_id,
                           self.db.session_tracks(session_id))
        return True

    def _collect(
        self, iteration_id: int, source_types: Sequence[str], stage: str
    ) -> bool:
        agent = CollectionAgent(
            self.db, self.config, self.connectors, budget=self.budget
        )
        return agent.run(iteration_id, stage=stage, source_types=source_types)

    # ------------------------------------------------------------------
    # Outcome
    # ------------------------------------------------------------------

    def _finish(
        self, iteration_id: int, degraded: list[Any], failed_critical: str | None,
    ) -> str:
        """Decide COMPLETE, PARTIAL or FAILED and close the iteration.

        The distinction that matters: a stage that ran but lost data is PARTIAL,
        and correlation already knows how to represent that as reduced coverage.
        FAILED is reserved for the case where a prerequisite never ran, because
        the alerts of a FAILED iteration should not be read at all.

        `degraded` holds `(source, note)` pairs so each note is attributed to
        the stage that produced it and can be retracted if that stage is
        discarded. A bare string is accepted and attributed to itself, which is
        what a stage name already was.
        """
        for item in degraded:
            source, note = item if isinstance(item, tuple) else (item, item)
            self.db.append_degradation(iteration_id, note, source=source)

        # Derived, so REPLACED rather than appended: recomputed on every
        # _finish, and a resume that closes some gaps must not leave the older
        # and now wrong summary standing beside the new one.
        gaps = self._collection_gaps(iteration_id)
        if gaps:
            self.db.replace_degradation(
                iteration_id, SurgeDB.DEGRADATION_GAPS,
                "collection gaps: "
                + ", ".join(f"{k}×{v}" for k, v in gaps.items()))
        else:
            self.db.discard_degradations(
                iteration_id, SurgeDB.DEGRADATION_GAPS)

        # A stepped iteration carries no in-memory list across HTTP calls, so
        # its degradations are only on the row. Reading them back is what makes
        # a stepped run and a driven run reach the same outcome.
        notes = self.db.degradation_notes(iteration_id)

        if failed_critical:
            outcome = "FAILED"
            error = f"critical stage {failed_critical} failed"
        elif notes:
            outcome = "PARTIAL"
            error = None
        else:
            outcome = "COMPLETE"
            error = None

        self.db.finish_iteration(
            iteration_id, outcome=outcome, error_message=error)
        self._log("INFO", f"Iteration {iteration_id} finished: {outcome}",
                  iteration_id=iteration_id, outcome=outcome,
                  degraded_stages=[str(d) for d in degraded] or None,
                  gaps=gaps or None)
        return outcome

    def _collection_gaps(self, iteration_id: int) -> dict[str, int]:
        """Queries that did not produce data, grouped by why.

        Reads `enums.UNRELIABLE_QUERY_STATUSES` rather than an inline list.
        The inline one omitted INTERRUPTED — the status `RecoveryService.abandon`
        writes — so an abandoned iteration whose twelve stranded queries were
        already counted as a SOCIAL coverage gap by `unreliable_source_types`
        still closed **COMPLETE** with an empty degradations list. Found live,
        on the merely-open abandon path that 8.7(a) made reachable; the crashed
        path passed its test only because the reconcile's own degradation note
        happened to force PARTIAL first.

        The enum's comment already stated the rule this violated. Two
        definitions of "a query that produced nothing", and the second drifted.
        """
        placeholders = ",".join("?" * len(enums.UNRELIABLE_QUERY_STATUSES))
        rows = self.db.all(
            f"SELECT status, COUNT(*) AS n FROM query_queue "
            f"WHERE iteration_id = ? AND status IN ({placeholders}) "
            f"GROUP BY status",
            (iteration_id, *sorted(enums.UNRELIABLE_QUERY_STATUSES)),
        )
        return {row["status"]: int(row["n"]) for row in rows}

    def _add_degradation(
        self, iteration_id: int, note: str, source: str | None = None
    ) -> None:
        """Record one thing this iteration could not do. One writer, in SurgeDB."""
        self.db.append_degradation(iteration_id, note, source=source)

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def _reset_stranded_queries(self, iteration_id: int) -> int:
        """Return IN_PROGRESS rows to PENDING so a resume can claim them.

        A process killed mid-collection leaves rows claimed but never executed.
        Without this they are invisible to `claim_next_query` forever, and the
        resume would silently collect less than the original run.
        """
        return self.db._exec(
            "UPDATE query_queue SET status = 'PENDING' "
            "WHERE iteration_id = ? AND status = 'IN_PROGRESS'",
            (iteration_id,),
        )

    def _reconcile_remote_budgets(self, iteration_id: int) -> None:
        """Trust the provider's own credit balance over the local ledger.

        Staying's GET /account costs nothing and is authoritative. A ledger that
        has drifted optimistic is how a system discovers exhaustion by 402
        instead of by a graceful skip — and the local per-call accounting was
        measured under-counting Staying by an order of magnitude before it read
        the provider's reported credits.
        """
        connector = self.connectors.get("STAYING")
        if connector is None or not hasattr(connector, "credits_available"):
            return
        try:
            available = connector.credits_available()
        except Exception as exc:  # noqa: BLE001 — advisory, never fatal
            self._log("WARNING", f"Could not read Staying credit balance: {exc}",
                      iteration_id=iteration_id)
            return
        if available is not None:
            self.budget.reconcile_staying(available)

    def _prune(self, iteration_id: int) -> None:
        """Enforce provider retention at the end of every iteration.

        FR24's licence requires deletion 30 days after receipt. Running it here
        rather than on a timer means it cannot be forgotten by a deployment that
        never sets one up.
        """
        try:
            RetentionService(self.db, self.config).prune()
        except Exception as exc:  # noqa: BLE001 — advisory, never fatal
            self._log("WARNING", f"Retention prune failed: {exc}",
                      iteration_id=iteration_id)

    def _log(self, level: str, message: str, **extra: Any) -> None:
        iteration_id = extra.pop("iteration_id", None)
        self.db.log(self.agent_name, level, message,
                    iteration_id=iteration_id, **extra)
