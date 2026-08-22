"""Background iteration execution and the per-session lock.

An iteration takes minutes — Staying's `/search` alone was measured at 125
seconds — so the trigger cannot be synchronous by default. It returns 202 with
an iteration_id and a poll URL, and the run continues on a worker thread.
`SurgeDB` is opened with `check_same_thread=False` behind an RLock precisely so
this is safe.

**One iteration per session at a time.** Two concurrent runs would both seed the
same queries, race the dedup index, and split one city's evidence across two
correlation windows. The lock is held from before `start()` — so the second
caller cannot even create an iteration — until the run finishes, and a second
trigger gets 409 rather than a queue position. Refusing is right here: a caller
who wanted the earlier run's results should poll for them, not start a second.

**The lock is per process.** Running uvicorn with more than one worker would
give each its own registry and silently permit concurrent iterations, which is
why `run.py serve` pins workers to 1.
"""
from __future__ import annotations

import threading
from concurrent.futures import (
    Future, ThreadPoolExecutor, TimeoutError as FTimeout, wait as await_futures,
)
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from ..agents.orchestrator import IterationOrchestrator
from ..db.database import SurgeDB


class SessionBusy(RuntimeError):
    """Raised when a session already has an iteration in flight."""

    def __init__(self, session_id: int, iteration_id: int | None) -> None:
        self.session_id = session_id
        self.iteration_id = iteration_id
        detail = (
            f"Session {session_id} already has iteration {iteration_id} running"
            if iteration_id is not None
            else f"Session {session_id} is busy"
        )
        super().__init__(detail)


class IterationRunner:
    """Owns the worker pool, the session locks, and what is currently running."""

    def __init__(
        self,
        db: SurgeDB,
        build_orchestrator: Callable[[], IterationOrchestrator],
        *,
        max_workers: int = 4,
        epoch_id: int | None = None,
    ) -> None:
        self.db = db
        self._build = build_orchestrator
        #: This process's epoch, stamped onto every iteration it starts. It is
        #: what a later process reads to tell an interruption from a run in
        #: flight.
        self.epoch_id = epoch_id
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="surge-iteration"
        )
        self._registry = threading.Lock()
        self._locks: dict[int, threading.Lock] = {}
        self._active: dict[int, int] = {}          # session_id -> iteration_id
        self._futures: set[Future] = set()
        self._closed = False

    # ------------------------------------------------------------------
    # Locking
    # ------------------------------------------------------------------

    def _lock_for(self, session_id: int) -> threading.Lock:
        with self._registry:
            return self._locks.setdefault(session_id, threading.Lock())

    @contextmanager
    def claim(self, session_id: int) -> Iterator[None]:
        """Hold the session lock for the duration of a short operation.

        Used by the step and discard endpoints so a manual walk cannot interleave
        with an automatic run of the same session.
        """
        lock = self._lock_for(session_id)
        if not lock.acquire(blocking=False):
            raise SessionBusy(session_id, self._active.get(session_id))
        try:
            yield
        finally:
            lock.release()

    def running_iteration(self, session_id: int) -> int | None:
        return self._active.get(session_id)

    def running(self) -> list[int]:
        return sorted(self._active.values())

    def is_running(self, iteration_id: int) -> bool:
        return iteration_id in self._active.values()

    # ------------------------------------------------------------------
    # Starting work
    # ------------------------------------------------------------------

    def create(self, session_id: int) -> int:
        """Create an iteration without running it, for manual stepping."""
        with self.claim(session_id):
            return self._build().start(session_id, epoch_id=self.epoch_id)

    def submit_resume(
        self, session_id: int, iteration_id: int, from_stage: str
    ) -> tuple[int, Future]:
        """Run an EXISTING iteration on a worker.

        `submit()` cannot do this: it calls `orchestrator.start()`, which
        creates a new iteration. Resume needs the same lock, the same `_active`
        bookkeeping — so `is_running` reports it and a concurrent trigger gets
        409 — and the same worker pool, against a row that already exists.
        """
        lock = self._lock_for(session_id)
        if not lock.acquire(blocking=False):
            raise SessionBusy(session_id, self._active.get(session_id))

        if not self.db.try_claim_session(session_id, iteration_id, self.epoch_id):
            held, _epoch = self.db.session_lock(session_id)
            lock.release()
            raise SessionBusy(session_id, held)

        orchestrator = self._build()
        self._active[session_id] = iteration_id

        def work() -> str:
            try:
                return orchestrator.resume(iteration_id, from_stage)
            except BaseException as exc:  # noqa: BLE001 — must not vanish
                self._fail(iteration_id, exc)
                raise
            finally:
                self._active.pop(session_id, None)
                self.db.release_session(session_id, iteration_id)
                lock.release()

        return iteration_id, self._track(self._pool.submit(work))

    def submit_finalise(
        self, session_id: int, iteration_id: int
    ) -> tuple[int, Future]:
        """Score and alert an abandoned iteration on a worker."""
        lock = self._lock_for(session_id)
        if not lock.acquire(blocking=False):
            raise SessionBusy(session_id, self._active.get(session_id))

        if not self.db.try_claim_session(session_id, iteration_id, self.epoch_id):
            held, _epoch = self.db.session_lock(session_id)
            lock.release()
            raise SessionBusy(session_id, held)

        orchestrator = self._build()
        self._active[session_id] = iteration_id

        def work() -> str:
            try:
                return orchestrator.finalise(iteration_id)
            except BaseException as exc:  # noqa: BLE001
                self._fail(iteration_id, exc)
                raise
            finally:
                self._active.pop(session_id, None)
                self.db.release_session(session_id, iteration_id)
                lock.release()

        return iteration_id, self._track(self._pool.submit(work))

    def submit_retry_triage(
        self, session_id: int, parent_id: int, batch_size: int | None = None
    ) -> tuple[int, Future]:
        """Re-judge a finished iteration's unjudged posts in a new one (8.8).

        Validation and the child's creation happen HERE, in the request thread,
        so every refusal — parent still open, session busy, nothing to retry —
        is synchronous and creates no row. A refusal that had already inserted
        the child would leave a hole in `seq` for each attempt that was never
        allowed to start, and an empty iteration for an operator to clean up.

        The lock is taken here for the same reason `submit` takes it here: two
        near-simultaneous retries would otherwise both return 202 and only then
        discover that one has to wait.
        """
        lock = self._lock_for(session_id)
        if not lock.acquire(blocking=False):
            raise SessionBusy(session_id, self._active.get(session_id))

        try:
            held, _epoch = self.db.session_lock(session_id)
            if held is not None:
                raise SessionBusy(session_id, held)
            orchestrator = self._build()
            child_id = orchestrator.prepare_retry_triage(
                parent_id, epoch_id=self.epoch_id)
        except BaseException:
            lock.release()
            raise

        if not self.db.try_claim_session(session_id, child_id, self.epoch_id):
            held, _epoch = self.db.session_lock(session_id)
            self.db.finish_iteration(
                child_id, outcome="FAILED",
                error_message=f"session {session_id} is already running "
                              f"iteration {held}; this retry lost the race")
            lock.release()
            raise SessionBusy(session_id, held)

        self._active[session_id] = child_id

        def work() -> str:
            try:
                return orchestrator.run_retry_triage(
                    child_id, parent_id, batch_size=batch_size)
            except BaseException as exc:  # noqa: BLE001 — must not vanish
                self._fail(child_id, exc)
                raise
            finally:
                self._active.pop(session_id, None)
                self.db.release_session(session_id, child_id)
                lock.release()

        return child_id, self._track(self._pool.submit(work))

    def _track(self, future: Future) -> Future:
        with self._registry:
            self._futures.add(future)
        future.add_done_callback(self._futures.discard)
        return future

    def submit(self, session_id: int) -> tuple[int, Future]:
        """Create an iteration and run it on a worker. Returns immediately.

        The lock is acquired here, in the request thread, and released by the
        worker's `finally`. Acquiring it inside the worker instead would let two
        near-simultaneous triggers both return 202 and only then discover that
        one of them has to wait — a 409 the caller never saw.
        """
        lock = self._lock_for(session_id)
        if not lock.acquire(blocking=False):
            raise SessionBusy(session_id, self._active.get(session_id))

        try:
            # Cheap read first, so ordinary cross-process contention is refused
            # before an iteration row exists. The atomic claim below is what
            # actually closes the race; this only keeps the common case tidy.
            held, _epoch = self.db.session_lock(session_id)
            if held is not None:
                raise SessionBusy(session_id, held)
            orchestrator = self._build()
            iteration_id = orchestrator.start(session_id, epoch_id=self.epoch_id)
        except BaseException:
            lock.release()
            raise

        # The authority. Two processes that both passed the read above reach
        # here; exactly one wins the conditional UPDATE.
        if not self.db.try_claim_session(session_id, iteration_id, self.epoch_id):
            held, _epoch = self.db.session_lock(session_id)
            # The losing iteration is closed rather than left dangling — a row
            # stuck at SEEDING with no outcome reads as "still running" forever,
            # and this one never ran at all.
            self.db.finish_iteration(
                iteration_id, outcome="FAILED",
                error_message=f"session {session_id} is already running "
                              f"iteration {held}; this trigger lost the race")
            lock.release()
            raise SessionBusy(session_id, held)

        self._active[session_id] = iteration_id

        def work() -> str:
            try:
                return orchestrator.run(iteration_id)
            except BaseException as exc:      # noqa: BLE001 — must not vanish
                # run() isolates per-stage failure itself, so reaching here means
                # the driver broke. Close the iteration explicitly: a row stuck
                # mid-stage with no outcome reads as "still running" forever.
                self._fail(iteration_id, exc)
                raise
            finally:
                self._active.pop(session_id, None)
                self.db.release_session(session_id, iteration_id)
                lock.release()

        return iteration_id, self._track(self._pool.submit(work))

    def run_and_wait(
        self, session_id: int, timeout: float
    ) -> tuple[int, str | None]:
        """Trigger and wait up to `timeout`. Returns (iteration_id, outcome).

        A None outcome means the run outlived the wait and is still going — the
        caller falls back to the poll URL. The work is never cancelled: an
        iteration
        that has already paid for collection should finish and record it.
        """
        iteration_id, future = self.submit(session_id)
        try:
            return iteration_id, future.result(timeout=timeout)
        except FTimeout:
            return iteration_id, None

    def _fail(self, iteration_id: int, exc: BaseException) -> None:
        try:
            self.db.log(
                "IterationRunner", "ERROR",
                f"Iteration {iteration_id} aborted: {type(exc).__name__}: {exc}",
                iteration_id=iteration_id,
            )
            self.db.finish_iteration(
                iteration_id, outcome="FAILED",
                error_message=f"{type(exc).__name__}: {exc}"[:2000],
            )
        except Exception:  # noqa: BLE001 — best effort; the raise still stands
            pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self, timeout: float = 30.0) -> list[int]:
        """Stop accepting work and wait, bounded, for what is in flight.

        Waiting is not politeness. The caller closes the database next, and
        sqlite3 **segfaults** if a connection is closed while another thread is
        mid-statement on it — so an unwaited shutdown during collection is a
        crash, not a fast exit. It is also the same principle `?wait=true`
        follows: an iteration that has already paid for collection should finish
        and record what it bought.

        Bounded, because a stuck iteration must not hold a shutdown open
        forever. Returns the iterations still running when the wait expired;
        the caller decides whether closing over them is worth the risk.
        """
        if self._closed:
            return []
        self._closed = True
        # cancel_futures drops work that has not started; running work is not
        # interrupted, which is the point.
        self._pool.shutdown(wait=False, cancel_futures=True)
        pending = list(self._futures)
        if pending:
            _done, unfinished = await_futures(pending, timeout=timeout)
            if unfinished:
                stranded = self.running()
                self.db.log(
                    "IterationRunner", "WARNING",
                    f"Shutdown timed out after {timeout:g}s with "
                    f"{len(unfinished)} iteration(s) still running: {stranded}. "
                    "They will resume as stranded IN_PROGRESS queries.",
                )
                return stranded
        return []


def build_orchestrator_factory(
    db: SurgeDB,
    config: dict[str, Any],
    connectors: dict[str, Any],
    llm_client: Any,
) -> Callable[[], IterationOrchestrator]:
    """A fresh orchestrator per run, sharing the database and connectors.

    Fresh because the orchestrator holds a BudgetGuard whose iteration
    allocation is computed at `start()`; sharing one across runs would carry a
    stale envelope into the next iteration.
    """
    def factory() -> IterationOrchestrator:
        return IterationOrchestrator(
            db, config, connectors, llm_client=llm_client
        )
    return factory
