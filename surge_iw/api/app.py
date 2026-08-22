"""FastAPI application factory.

Everything the routes need hangs off `app.state`, built once at startup and torn
down at shutdown: the database, the four connectors, the LLM client, the budget
guard, and the runner that owns the worker pool and the per-session locks.

A factory rather than a module-level `app` so that tests can inject an in-memory
database and stub connectors without touching the environment, and so a second
instance in one process cannot inherit the first one's locks.

**Debug routes are mounted only when `api.debug_endpoints` is true.** They are
not merely hidden — an unmounted route is a 404, and a disabled discard endpoint
that still existed would be one config read away from deleting analytical
records in production.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Callable, Mapping

from fastapi import FastAPI

from ..agents.orchestrator import IterationOrchestrator
from ..config import load_with_mission, mission_overrides
from ..db.database import SurgeDB
from ..services import mission
from ..services.budget import BudgetGuard
from ..services.recovery import RecoveryService
from ..services.redact import install as install_redaction
from ..services.retention import RetentionService
from . import routes
from .runner import IterationRunner
from .security import configured_token

#: Process exit used when shutdown gives up with work still in flight. A
#: module-level name so tests can replace it — calling the real one inside the
#: suite would kill the test runner. 75 is EX_TEMPFAIL, so a supervisor can
#: tell "exited with work stranded" from a clean exit.
_HARD_EXIT = os._exit
STRANDED_EXIT_CODE = 75


def _shutdown(
    app: FastAPI, api_cfg: Mapping[str, Any], *, owns_db: bool
) -> None:
    """Stop, and close resources only if that is provably safe.

    The bounded wait alone is not safety. If it expires, a worker is still
    inside sqlite3 — and closing the connection under it is a known SIGSEGV,
    which is what put the wait here in the first place.

    So on a timeout: record what was stranded, close **nothing**, and exit hard.
    An unclosed connection in a process about to exit costs a file descriptor
    for microseconds, and WAL with `synchronous = NORMAL` is crash-safe for
    committed transactions — every SurgeDB write commits, so nothing is lost.
    Leak versus crash is not a close call.

    The hard exit is not belt-and-braces either. Returning normally lets
    interpreter teardown reach the same segfault by garbage collection, and
    `ThreadPoolExecutor`'s atexit hook would hang forever joining a non-daemon
    worker.

    Deliberately, the iterations are **not** marked interrupted here — the
    worker may still finish, and marking a row that then completes would leave
    two contradictory records. Marking happens in exactly one place: the next
    process's reconcile, by which time the row says truthfully whether it ended.
    """
    runner = app.state.runner
    epoch_id = getattr(app.state, "epoch_id", None)
    stranded = runner.shutdown(
        timeout=float(api_cfg.get("shutdown_timeout_s", 30))
    )

    if stranded:
        if epoch_id is not None:
            app.state.db.close_epoch(epoch_id, "TIMEOUT", stranded=stranded)
        app.state.db.log(
            "api", "ERROR",
            f"Shutdown gave up with {len(stranded)} iteration(s) still "
            f"running: {stranded}. Exiting WITHOUT closing the database or the "
            "connectors — closing them under a live worker crashes the "
            "process. The next startup will reconcile them.",
            stranded=stranded, epoch_id=epoch_id,
        )
        _HARD_EXIT(STRANDED_EXIT_CODE)
        return                                  # pragma: no cover — unless faked

    if epoch_id is not None:
        app.state.db.close_epoch(epoch_id, "CLEAN")
    for connector in app.state.connectors.values():
        close = getattr(connector, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    if owns_db:
        app.state.db.close()

DESCRIPTION = """\
Tactical indications and warning from four independent open sources.

Alerts correlate four independent sources — social chatter, military and charter
flights, short-term-rental availability, and rental-car availability — into a
deterministic confidence score. The score is computed in Python from a weighted
model whose working is recorded per alert; a language model writes the summary
sentence and cannot change the number.

Read `/v1/alerts/{id}/evidence` before acting on a score. It resolves every
contributing signal back to the raw payload and the queue row that fetched it,
and reports the band rule that fired and any collection gap that capped it.

**A failure is never an absence of threat.** A source that could not be
collected lowers `data_completeness`, caps the band below HIGH, and is named in
the alert's caveat. An alert on a PARTIAL iteration is a real finding made from
incomplete evidence, not a quieter one.
"""


def create_app(
    config: Mapping[str, Any] | None = None,
    *,
    db: SurgeDB | None = None,
    connectors: Mapping[str, Any] | None = None,
    llm_client: Any = None,
    config_path: str | None = "config.yaml",
    build_orchestrator: Callable[[], IterationOrchestrator] | None = None,
) -> FastAPI:
    if config is not None:
        # A caller-supplied config is taken as FINAL, not re-layered.
        #
        # Layering it here would rebuild the nested dicts, so the object the
        # caller kept a reference to would stop being the one the application
        # reads — and a later `config["correlation"]["alert_min_score"] = x`
        # would be silently ignored. Callers that want the mission's
        # thresholds underneath their own use `config.load_with_mission()`,
        # which is what `run.py` and the no-config path below both do.
        settings = dict(config)
        loaded_mission = mission.load_configured(settings)
    else:
        settings, loaded_mission = load_with_mission(config_path)
    # 9.1. load_config() installs redaction, but this factory also accepts a
    # config the caller built — a test, an embedder, a future scheduler — and
    # that path would bypass it. Installing here too makes the guarantee a
    # property of the application rather than of how it was constructed.
    # Registration is a set insert, so doing it twice costs nothing, and it
    # must happen before the token is read, the database is opened, or a
    # connector exists.
    redacted_credentials = install_redaction(settings)
    api_cfg = settings.get("api") or {}
    owns_db = db is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # FIRST, before anything else can touch an iteration. start_agent_run
        # erases a stranded RUNNING row, so a stage that re-runs before the
        # reconcile has recorded the interruption destroys its own evidence.
        # FastAPI accepts no request until this returns, which is what makes
        # in-process ordering sufficient.
        report = RecoveryService(app.state.db, settings).open_epoch("serve")
        app.state.epoch_id = report.epoch_id
        app.state.runner.epoch_id = report.epoch_id
        app.state.reconcile = report

        # The count, never the values. Recorded because "redaction was
        # installed" is exactly the kind of claim that was false for months
        # without anything saying so — an operator can now read it back out of
        # the audit trail instead of trusting the code.
        overrides = mission_overrides(settings, app.state.mission)
        if overrides:
            app.state.db.log(
                "api", "WARNING",
                "config.yaml overrides the mission's calibrated value for "
                f"{len(overrides)} setting(s): {'; '.join(overrides)}. "
                "Legitimate, but recorded: a mission's thresholds carry "
                "reasoning, and replacing one silently is how that reasoning "
                "gets lost.",
                overrides=overrides)

        app.state.db.log(
            "api", "INFO" if redacted_credentials else "WARNING",
            f"Credential redaction installed: {redacted_credentials} "
            f"configured value(s) registered")

        # Retention runs on startup as well as after every iteration: a
        # deployment that sat idle past a licence deadline must not serve
        # payloads it is no longer allowed to hold.
        try:
            purged = RetentionService(app.state.db, settings).prune()
            if purged:
                app.state.db.log("api", "INFO",
                                 f"Startup retention prune removed {purged} "
                                 "raw payload(s)")
        except Exception as exc:  # noqa: BLE001 — advisory, never fatal
            app.state.db.log("api", "WARNING", f"Startup prune failed: {exc}")
        try:
            yield
        finally:
            _shutdown(app, api_cfg, owns_db=owns_db)

    app = FastAPI(
        title="Surge I&W",
        description=DESCRIPTION,
        version="0.6.0",
        lifespan=lifespan,
    )

    app.state.config = settings
    # The mission is read once, here, and never re-read. An iteration that
    # could pick up an edited pack halfway through would produce a judgement
    # whose recorded digest described neither half of what it ran on.
    #
    # A pack that fails to load raises out of create_app rather than degrading
    # to a default, because there is no default: the tracks, the lexicon and
    # the weights have no engine-side fallback, and a mission that half-loaded
    # would collect and score against a definition nobody wrote.
    app.state.mission = loaded_mission
    app.state.db = db or SurgeDB(
        (settings.get("database") or {}).get("path", "surge_iw.db"),
        mission=app.state.mission)
    # A caller-supplied database (a test, an embedder) gets the same mission,
    # so there is one loaded pack per application and not two that could
    # disagree about what a track name means.
    if db is not None and getattr(db, "mission", None) is None:
        db.mission = app.state.mission
    app.state.api_token = configured_token(settings)
    app.state.budget = BudgetGuard(app.state.db, settings)

    if connectors is None:
        from ..connectors.registry import build_connectors
        # on_call is not optional wiring. Every guard in services/budget.py —
        # the monthly cap, the per-iteration envelope, the priority reservation
        # near the hard stop — reads `api_calls`, and nothing else writes it.
        # Connectors built without this ledger a spend of zero forever, so the
        # guard would never refuse anything and FR24's per-record billing would
        # run to the plan limit unobserved.
        connectors = build_connectors(settings, on_call=app.state.budget.record)
    app.state.connectors = dict(connectors)
    app.state.llm_client = llm_client

    if build_orchestrator is None:
        def build_orchestrator() -> IterationOrchestrator:
            # Fresh per run: the orchestrator holds a BudgetGuard whose
            # per-iteration envelope is computed at start(), and carrying a
            # stale one into the next iteration would mis-report the allocation.
            return IterationOrchestrator(
                app.state.db, settings, app.state.connectors,
                llm_client=app.state.llm_client, budget=app.state.budget,
            )

    app.state.build_orchestrator = build_orchestrator
    app.state.runner = IterationRunner(
        app.state.db, build_orchestrator,
        max_workers=int(api_cfg.get("max_workers", 4)),
    )

    app.include_router(routes.router)
    if api_cfg.get("debug_endpoints", True):
        app.include_router(routes.debug_router)

    # 9.3. Every protected operation now declares the bearer requirement,
    # because it depends on `security.authenticated` and FastAPI derives the
    # declaration from the dependency. `/v1/healthz` is the one route that
    # cannot: it is anonymous for liveness and authenticated for `?deep=true`,
    # and FastAPI can only say "required" or say nothing.
    #
    # OpenAPI can say it exactly — an empty requirement listed beside the
    # scheme means "either" — so it is written here rather than left as prose.
    # A generated client then knows the token is accepted on this operation,
    # which is what `?deep=true` needs, without being told to send one for a
    # liveness probe, which is the property that keeps an unauthenticated port
    # from being a free way to burn four vendors' rate limits.
    base_openapi = app.openapi

    def openapi_with_optional_liveness_auth() -> dict[str, Any]:
        schema = base_openapi()
        schemes = ((schema.get("components") or {})
                   .get("securitySchemes") or {})
        operation = (schema.get("paths", {}).get("/v1/healthz") or {}).get("get")
        if operation is not None and "BearerToken" in schemes:
            operation.setdefault("security", [{}, {"BearerToken": []}])
        return schema

    app.openapi = openapi_with_optional_liveness_auth

    return app
