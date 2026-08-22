#!/usr/bin/env python3
"""Surge I&W — command line entry point.

Four commands, deliberately:

    init-db     create or migrate the database and exit
    serve       run the REST API (the normal way to use the system)
    iterate     run one iteration against a session, for a cron job or a shell
    alerts      print a session's alerts

The old entry point ran a whole detection pass from the command line with cities
and locations passed as files. That is gone: an iteration is now triggered by an
API call against a session that already holds its geography, and `iterate` is a
thin client of the same orchestrator the API drives rather than a second way to
do the work.

Credentials come from the environment, via the variable NAMES in config.yaml.
Nothing here reads or prints a key.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from surge_iw.config import (ConfigError, build_llm_client, load_config,
                             load_with_mission)
from surge_iw.db.database import SCHEMA_VERSION, SurgeDB
# Only the constant, at module level: the CLI's default input-set name and the
# name the API's refusal message suggests are the same fact, and a second copy
# of it here is how `--from` came to default to a file the engine no longer
# ships.
from surge_iw.services.inputs import DEFAULT_INPUT_NAME


def _db(args: argparse.Namespace) -> tuple[SurgeDB, dict]:
    config, loaded = load_with_mission(args.config)
    if args.database:
        config.setdefault("database", {})["path"] = args.database
    return SurgeDB(config["database"]["path"], mission=loaded), config


def _db_recovered(
    args: argparse.Namespace, entry_point: str
) -> tuple[SurgeDB, dict, Any]:
    """Open the database AND this process's epoch, in that order.

    Every command that can touch an iteration goes through here. The reconcile
    has to happen before any stage can run again, because `start_agent_run`
    erases a stranded run row — the only durable trace that a process died
    inside that stage.
    """
    from surge_iw.services.recovery import RecoveryService

    db, config = _db(args)
    report = RecoveryService(db, config).open_epoch(entry_point)
    if report.needs_attention:
        print(f"note: {len(report.needs_attention)} interrupted iteration(s) "
              f"awaiting a decision: "
              f"{', '.join(str(i) for i in report.needs_attention)}",
              file=sys.stderr)
        print("      run `python run.py recover` to inspect them.",
              file=sys.stderr)
    return db, config, report


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init_db(args: argparse.Namespace) -> int:
    db, config = _db(args)
    tables = [row["name"] for row in db.all(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    print(f"Database : {db.path}")
    print(f"Schema   : version {SCHEMA_VERSION}, {len(tables)} tables")
    print("Tables   : " + ", ".join(tables))
    db.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the API. One worker, always — see the note below."""
    import uvicorn

    from surge_iw.api.app import create_app
    from surge_iw.api.security import configured_token

    config = load_config(args.config)
    if args.database:
        config.setdefault("database", {})["path"] = args.database
    api = config.get("api", {})
    host, port = args.host or api.get("host", "127.0.0.1"), \
        args.port or int(api.get("port", 8000))

    if not configured_token(config):
        var = api.get("token_env", "SURGE_API_TOKEN")
        print(f"error: {var} is not set. Every route except /v1/healthz "
              f"requires it, and the API will refuse to serve without it.",
              file=sys.stderr)
        return 1
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: binding {host} exposes alerts that name real "
              "facilities and real places. Put TLS and a real identity layer "
              "in front.",
              file=sys.stderr)

    llm = None
    try:
        llm = build_llm_client(config)
    except (ConfigError, ImportError) as exc:
        print(f"warning: no LLM client ({exc}). Triage and alerting will be "
              "skipped; collection and correlation still run.", file=sys.stderr)

    app = create_app(config, llm_client=llm)
    print(f"Surge I&W on http://{host}:{port}  "
          f"(docs at /docs, database {config['database']['path']})")
    # Which definition this instrument is running, said out loud at startup.
    # An operator who cannot see the mission cannot tell a run under the
    # shipped synthetic pack from a run under their own.
    loaded = getattr(app.state, "mission", None)
    if loaded is None:
        print("warning: no mission configured. The API will serve, but an "
              "iteration cannot run — the tracks, lexicon, prompts and weights "
              "all come from a mission pack.", file=sys.stderr)
    else:
        for line in loaded.describe():
            print(line)
    # 8.6: workers=1 is now a DEFAULT, not a correctness requirement.
    #
    # It used to be one. The one-iteration-per-session lock was a
    # threading.Lock, true only inside this interpreter, so a second worker
    # brought its own and two iterations of one session could run at once —
    # spending twice and racing each other's queue rows. The lock now lives in
    # `sessions.running_iteration_id`, taken by a conditional UPDATE, so the
    # guarantee holds across processes and hosts.
    #
    # It stays 1 by default for a different reason: each worker opens its own
    # process epoch, and sibling workers show up in each other's startup
    # reconcile as live epochs it must refuse to touch. That is safe — refusing
    # is the designed behaviour — but it means crash recovery cannot run while
    # siblings are up, so a multi-worker deployment needs an operator who
    # understands that trade. Raise it deliberately, not by default.
    workers = int((config.get("api") or {}).get("uvicorn_workers", 1))
    if workers != 1:
        print(f"note: {workers} uvicorn workers. The session lock is held in "
              "the database so this is safe, but startup recovery will refuse "
              "to reconcile while sibling workers are live.", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, workers=workers, log_level="info")
    return 0


def cmd_iterate(args: argparse.Namespace) -> int:
    from surge_iw.agents.orchestrator import (
        IterationOrchestrator, SessionHasOpenIteration,
    )
    from surge_iw.connectors.registry import build_connectors
    from surge_iw.services.budget import BudgetGuard

    db, config, report = _db_recovered(args, "iterate")
    if db.get_session(args.session) is None:
        print(f"error: no session {args.session}", file=sys.stderr)
        return 1
    # Same predicate as orchestrator.start()'s guard, which remains the
    # authority — this only fails fast, before connectors and an LLM client are
    # built from credentials a blocked session should not have to supply.
    # `unfinished_iterations`, not `interrupted_iterations`: the narrow one was
    # the 8.7(a) defect, and duplicating it here was the second place it lived.
    blocked = db.open_iterations(args.session)
    if blocked:
        ids = ", ".join(str(r["iteration_id"]) for r in blocked)
        print(f"error: session {args.session} has unfinished iteration(s) "
              f"{ids}. Resume or abandon them first — a new iteration now "
              f"would be silently under-collected by the cooldown guard.\n"
              f"       python run.py resume {blocked[0]['iteration_id']} "
              f"--dry-run",
              file=sys.stderr)
        return 3

    llm = None
    try:
        llm = build_llm_client(config)
    except (ConfigError, ImportError) as exc:
        print(f"warning: no LLM client ({exc}); triage and alerting will be "
              "skipped.", file=sys.stderr)

    # The budget guard must be the same object the connectors report to:
    # `api_calls` is the only record of spend, and connectors built without
    # on_call would leave it empty while every cap read it as zero.
    budget = BudgetGuard(db, config)
    orchestrator = IterationOrchestrator(
        db, config, build_connectors(config, on_call=budget.record),
        llm_client=llm, budget=budget,
    )
    try:
        iteration_id = orchestrator.start(
            args.session, epoch_id=report.epoch_id
        )
    except SessionHasOpenIteration as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    print(f"Iteration {iteration_id} started.")

    if args.step:
        while True:
            stage = orchestrator.next_stage(iteration_id)
            if stage is None:
                break
            result = orchestrator.step(iteration_id)
            print(f"  {result.stage:<18} {'ok' if result.ok else 'DEGRADED'}")
        outcome = db.get_iteration(iteration_id)["outcome"]
    else:
        outcome = orchestrator.run(iteration_id)

    counts = db.iteration_counts(iteration_id)
    print(f"Outcome  : {outcome}")
    print("  " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    degradations = json.loads(
        db.get_iteration(iteration_id)["degradations_json"] or "[]")
    for note in degradations:
        print(f"  ! {note}")
    db.close()
    return 0 if outcome != "FAILED" else 2


def cmd_alerts(args: argparse.Namespace) -> int:
    from surge_iw.models import Alert

    db, _config = _db(args)
    rows = db.get_alerts(args.session, min_band=args.min_confidence)
    if not rows:
        print("No alerts.")
        db.close()
        return 0

    for row in rows:
        alert = Alert.from_rows(
            row, db.correlation_signals(int(row["correlation_id"])))
        if args.json:
            print(json.dumps(alert.as_dict(), indent=2))
            continue
        print(f"\n[{alert.confidence_band}] {alert.city} — "
              f"{alert.track}  (score {alert.confidence_score:.3f})")
        print(f"  {alert.summary}")
        if alert.caveat:
            print(f"  ! {alert.caveat}")
        print(f"  evidence: {len(alert.social_posts)} post(s), "
              f"{len(alert.flights)} flight(s), {len(alert.lodging)} lodging, "
              f"{len(alert.rental_cars)} car row(s)")
    db.close()
    return 0


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def cmd_session_create(args: argparse.Namespace) -> int:
    """Create a session from an input file (8.7c).

    Accepts a NAME resolved inside `inputs.dir`, or a path. The API deliberately
    accepts only a name — a path there would be a file-disclosure primitive —
    but an operator at a shell already has the filesystem, so refusing one here
    would be ceremony rather than a control.
    """
    from surge_iw.services import geo
    from surge_iw.services.inputs import InputError, input_path, load

    db, config = _db(args)
    source = args.source
    try:
        path = Path(source) if ("/" in source or Path(source).is_file()) \
            else input_path(source, config)
        loaded = load(path, config=config, mission=db.mission)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        db.close()
        return 2

    tracks = ([t.strip().upper() for t in args.tracks.split(",") if t.strip()]
              if args.tracks else None)
    print(f"Input    : {loaded.path}")
    print(f"Tracks   : {', '.join(tracks) if tracks else 'all (mission default)'}")
    print(f"Expand   : {args.expand}")
    print(f"\n{len(loaded.cities)} city/cities:\n")
    for line in loaded.describe():
        print(f"  {line}")
    for label in loaded.without_locations:
        print(f"\n  warning: {label} has no key locations; the LODGING family "
              f"will be absent for it", file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        db.close()
        return 0

    session_id = db.insert_session(
        label=args.label, expand_cities=args.expand, tracks=tracks,
    )
    limit = int((config.get("flightradar") or {}).get(
        "max_airports_per_city", 3))
    for city in loaded.cities:
        city_id = db.insert_city(
            session_id, city.name, canonical=city.canonical, state=city.state,
            is_seed=True, admitted_by="USER",
        )
        for location in city.key_locations:
            db.insert_key_location(
                city_id, location["name"], address=location.get("address"),
                lat=location.get("lat"), lon=location.get("lon"),
                location_type=location.get("location_type"),
            )
        if not geo.city_to_airports(city.canonical, limit=limit):
            print(f"warning: {city.label} has no airport mapping; flight "
                  f"queries will be SKIPPED_NO_MAPPING", file=sys.stderr)

    print(f"\nSession {session_id} created.")
    print(f"  python run.py --config {args.config} iterate {session_id}")
    db.close()
    return 0


def cmd_retry_triage(args: argparse.Namespace) -> int:
    """Re-judge the posts an iteration's model calls never answered (8.8)."""
    from surge_iw.agents.orchestrator import (
        IterationOrchestrator, SessionHasOpenIteration,
    )
    from surge_iw.connectors.registry import build_connectors
    from surge_iw.services.budget import BudgetGuard

    # A dry run reads and writes nothing, so it must not open an epoch: the
    # reconcile that comes with one closes a predecessor's iterations, which is
    # a real side effect for a command whose whole promise is that it has none.
    if args.dry_run:
        db, config = _db(args)
        report = None
    else:
        db, config, report = _db_recovered(args, "retry-triage")
    parent = db.get_iteration(args.iteration)
    if parent is None:
        print(f"error: no iteration {args.iteration}", file=sys.stderr)
        db.close()
        return 1

    candidates = db.uncovered_triage_decisions(args.iteration)
    print(f"Parent   : iteration {args.iteration} "
          f"(anchor {parent['anchor_at']}, outcome {parent['outcome']})")
    print(f"Unjudged : {len(candidates)} post(s)")
    for row in candidates[:10]:
        detail = (row["fault_detail"] or "")[:70]
        print(f"  {row['state']:14} {(row['url'] or '')[:52]}")
        if detail:
            print(f"      {detail}")
    if len(candidates) > 10:
        print(f"  ... and {len(candidates) - 10} more")
    if not candidates:
        print("\nNothing to retry. Only UNDECIDED, INVALID_OUTPUT and "
              "MODEL_ERROR are retryable;\nACCEPTED and REJECTED are completed "
              "judgements and a rejection is a conclusion.", file=sys.stderr)
        db.close()
        return 2

    if args.dry_run:
        print("\n--dry-run: nothing written. A real run creates a NEW "
              "iteration that\ninherits this one's anchor and does not "
              "re-collect.")
        db.close()
        return 0

    llm = None
    try:
        llm = build_llm_client(config)
    except (ConfigError, ImportError) as exc:
        print(f"error: no LLM client ({exc}); a re-triage is a model call and "
              f"cannot run without one.", file=sys.stderr)
        db.close()
        return 1

    budget = BudgetGuard(db, config)
    orchestrator = IterationOrchestrator(
        db, config, build_connectors(config, on_call=budget.record),
        llm_client=llm, budget=budget,
    )
    try:
        child, outcome = orchestrator.retry_triage(
            args.iteration, epoch_id=report.epoch_id,
            batch_size=args.batch_size)
    except SessionHasOpenIteration as exc:
        print(f"error: {exc}", file=sys.stderr)
        db.close()
        return 3
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        db.close()
        return 2

    counts = db.iteration_counts(child)
    remaining = len(db.uncovered_triage_decisions(child))
    print(f"\nIteration {child} (retry of {args.iteration}): {outcome}")
    print(f"  judged      : {counts['triage_decisions']} "
          f"({remaining} still unjudged)")
    print(f"  signals     : {counts['signals']}")
    print(f"  correlations: {counts['correlations']}  alerts: {counts['alerts']}")
    db.close()
    return 0


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def cmd_recover(args: argparse.Namespace) -> int:
    """List what still blocks a new iteration. Opening the epoch is the
    reconcile.

    Every iteration with `finished_at IS NULL`, not only the crash-stamped ones
    (8.7a): both refuse a new run on their session and both are closed the same
    two ways, so a listing that showed only one kind would send an operator to
    resolve something it had not told them about.
    """
    from surge_iw.services.recovery import RecoveryService, describe

    db, config, report = _db_recovered(args, "recover")
    rows = db.open_iterations()
    service = RecoveryService(db, config)

    if args.json:
        payload = {
            "epoch_id": report.epoch_id,
            "closed_epochs": report.closed_epochs,
            "refused_epochs": report.refused_epochs,
            # Key kept for continuity; contents widened to every open run, each
            # tagged with its kind.
            "interrupted": [
                {**item, "kind": "INTERRUPTED" if row["interrupted_at"]
                                 else "OPEN"}
                for item, row in zip(describe(rows), rows)
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(f"Epoch    : {report.epoch_id} ({report.host}:{report.pid})")
        if report.closed_epochs:
            print(f"Closed   : epoch(s) {report.closed_epochs} found open — "
                  "those processes died without saying so")
        if report.refused_epochs:
            print(f"REFUSED  : epoch(s) {report.refused_epochs} are still "
                  "alive. Two processes are sharing this database.")
        if not rows:
            print("Nothing to recover.")
        else:
            print(f"\n{len(rows)} unfinished iteration(s):\n")
            for row in rows:
                iteration_id = int(row["iteration_id"])
                plan = service.plan(iteration_id)
                kind = "INTERRUPTED" if row["interrupted_at"] else "OPEN"
                print(f"  #{iteration_id}  session {row['session_id']} "
                      f"seq {row['seq']}  [{kind}]")
                print("      "
                      + (f"died in    : {row['interrupted_stage']}"
                         if kind == "INTERRUPTED" else
                         f"stopped at : {row['stage']} (no crash; never "
                         f"finished)"))
                print(f"      resume from: {plan.resume_from} "
                      f"({plan.derived_by})")
                print(f"      re-collect : {len(plan.queries_to_recollect)} "
                      f"query(ies)"
                      + ("  [SPENDS MONEY]" if plan.paid else ""))
                if plan.already_banked:
                    print(f"      banked     : {len(plan.already_banked)} "
                          "payload(s) already paid for, not re-bought")
            print(f"\n  python run.py resume {int(rows[0]['iteration_id'])} --dry-run")
            print(f"  python run.py abandon {int(rows[0]['iteration_id'])} "
                  "--reason '...'")
    db.close()
    return 1 if (args.check and rows) else 0


def cmd_resume(args: argparse.Namespace) -> int:
    from surge_iw.agents.orchestrator import IterationOrchestrator
    from surge_iw.connectors.registry import build_connectors
    from surge_iw.services.budget import BudgetGuard
    from surge_iw.services.recovery import RecoveryService

    db, config, report = _db_recovered(args, "recover")
    service = RecoveryService(db, config)
    if not service.is_interrupted(args.iteration):
        print(f"error: iteration {args.iteration} is not interrupted.",
              file=sys.stderr)
        db.close()
        return 1

    plan = service.plan(args.iteration)
    from_stage = args.from_stage or plan.resume_from
    print(f"Iteration {args.iteration}: resume from {from_stage} "
          f"({plan.derived_by})")
    for query in plan.queries_to_recollect:
        print(f"  re-collect  {query['source_type']:<14} {query['provider']:<10}"
              f" {query['reason']}")
    for banked in plan.already_banked:
        print(f"  banked      {banked['source_type']:<14} "
              f"{banked['provider']:<10} not re-bought")
    if plan.already_spent:
        print("  already spent: " + ", ".join(
            f"{k}={v:g}" for k, v in sorted(plan.already_spent.items())))
    if plan.estimated_units_upper_bound:
        print("  upper bound  : " + ", ".join(
            f"{k}<={v:g}" for k, v in
            sorted(plan.estimated_units_upper_bound.items()))
            + "   (a bound, not a price — FR24 bills per record returned)")

    if args.dry_run:
        db.close()
        return 0
    if plan.paid and not args.confirm:
        print(f"\nRefusing: this would collect "
              f"{len(plan.queries_to_recollect)} query(ies) again at the "
              f"vendors. Re-run with --confirm.", file=sys.stderr)
        db.close()
        return 2

    llm = None
    try:
        llm = build_llm_client(config)
    except (ConfigError, ImportError) as exc:
        print(f"warning: no LLM client ({exc})", file=sys.stderr)
    budget = BudgetGuard(db, config)
    orchestrator = IterationOrchestrator(
        db, config, build_connectors(config, on_call=budget.record),
        llm_client=llm, budget=budget,
    )
    service.prepare_resume(args.iteration, report.epoch_id)
    outcome = orchestrator.resume(args.iteration, from_stage)
    print(f"\nOutcome  : {outcome}")
    print("  " + "  ".join(f"{k}={v}" for k, v in
                           db.iteration_counts(args.iteration).items()))
    db.close()
    return 0 if outcome != "FAILED" else 2


def cmd_abandon(args: argparse.Namespace) -> int:
    from surge_iw.agents.orchestrator import IterationOrchestrator
    from surge_iw.connectors.registry import build_connectors
    from surge_iw.services.budget import BudgetGuard
    from surge_iw.services.recovery import RecoveryService

    db, config, report = _db_recovered(args, "recover")
    service = RecoveryService(db, config)
    if not service.is_interrupted(args.iteration):
        print(f"error: iteration {args.iteration} is not interrupted.",
              file=sys.stderr)
        db.close()
        return 1
    if not args.confirm:
        print(f"Refusing: abandoning iteration {args.iteration} marks its "
              "outstanding queries as permanent coverage gaps and closes it. "
              "Re-run with --confirm.", file=sys.stderr)
        db.close()
        return 2

    result = service.abandon(args.iteration, args.reason,
                             epoch_id=report.epoch_id)
    print(f"Marked {result['queries_marked_interrupted']} query(ies) "
          "INTERRUPTED — these now count as coverage gaps.")
    for source, count in sorted(result["coverage_gaps"].items()):
        print(f"  gap  {source:<16} {count}")

    if args.no_finalise:
        db.finish_iteration(
            args.iteration, outcome="PARTIAL",
            degradations=json.loads(
                db.get_iteration(args.iteration)["degradations_json"] or "[]")
            + [f"abandoned without scoring: {args.reason}"],
        )
        print("Outcome  : PARTIAL (not scored, by request)")
        db.close()
        return 0

    llm = None
    try:
        llm = build_llm_client(config)
    except (ConfigError, ImportError) as exc:
        print(f"warning: no LLM client ({exc}); alerts will be skipped",
              file=sys.stderr)
    budget = BudgetGuard(db, config)
    orchestrator = IterationOrchestrator(
        db, config, build_connectors(config, on_call=budget.record),
        llm_client=llm, budget=budget,
    )
    outcome = orchestrator.finalise(args.iteration)
    counts = db.iteration_counts(args.iteration)
    print(f"Outcome  : {outcome}   "
          f"correlations={counts['correlations']} alerts={counts['alerts']}")
    print("Follow-on scheduling was skipped: work queued by an iteration "
          "nobody finished would arrive as a surprise.")
    db.close()
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="surge",
        description="Tipping-and-queuing engine for tactical indications and "
                    "warning. What it looks for comes from the configured "
                    "mission pack.",
    )
    parser.add_argument("--config", default="config.yaml", metavar="FILE")
    parser.add_argument("--database", default=None, metavar="FILE",
                        help="Override database.path from the config.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create or migrate the database.")

    serve = sub.add_parser("serve", help="Run the REST API.")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    session = sub.add_parser(
        "session", help="Create a session from an input file.")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    create = session_sub.add_parser(
        "create", help="Create a session from inputs/<name>.yaml.")
    create.add_argument(
        "--from", dest="source", default=DEFAULT_INPUT_NAME,
        metavar="NAME|PATH",
        help="Input set name (resolved in inputs.dir) or a path. "
             f"Default: {DEFAULT_INPUT_NAME}")
    create.add_argument("--label", default=None)
    create.add_argument(
        "--tracks", default=None,
        help="Comma-separated tracks. Defaults to every track the loaded "
             "mission defines.")
    create.add_argument(
        "--expand", action="store_true",
        help="Permit admitting cities the input file did not name, when two "
             "independent sources corroborate them.")
    create.add_argument(
        "--dry-run", action="store_true",
        help="Print the resolved geography and write nothing.")

    iterate = sub.add_parser("iterate", help="Run one iteration.")
    iterate.add_argument("session", type=int)
    iterate.add_argument("--step", action="store_true",
                         help="Run one stage at a time, printing each.")

    retry = sub.add_parser(
        "retry-triage",
        help="Re-judge posts an iteration's model calls never answered.")
    retry.add_argument("iteration", type=int)
    retry.add_argument(
        "--batch-size", type=int, default=None,
        help="Posts per model call. Halves on each truncation regardless.")
    retry.add_argument(
        "--dry-run", action="store_true",
        help="List what would be re-judged and write nothing.")

    alerts = sub.add_parser("alerts", help="Print a session's alerts.")
    alerts.add_argument("session", type=int)
    alerts.add_argument("--min-confidence", default=None,
                        choices=["LOW", "MEDIUM", "HIGH"])
    alerts.add_argument("--json", action="store_true")

    recover = sub.add_parser(
        "recover", help="Reconcile and list interrupted iterations.")
    recover.add_argument("--check", action="store_true",
                         help="Exit 1 if anything needs a decision.")
    recover.add_argument("--json", action="store_true")

    resume = sub.add_parser("resume", help="Continue an interrupted iteration.")
    resume.add_argument("iteration", type=int)
    resume.add_argument("--from-stage", default=None)
    resume.add_argument("--dry-run", action="store_true",
                        help="Print the plan and stop.")
    resume.add_argument("--confirm", action="store_true",
                        help="Required when the plan would re-collect.")

    abandon = sub.add_parser(
        "abandon", help="Close an interrupted iteration as a coverage gap.")
    abandon.add_argument("iteration", type=int)
    abandon.add_argument("--reason", required=True)
    abandon.add_argument("--confirm", action="store_true")
    abandon.add_argument("--no-finalise", action="store_true",
                         help="Skip correlation and alerting. Rarely right: "
                              "the alert from what WAS collected is the point.")
    return parser


COMMANDS = {
    "init-db": cmd_init_db,
    "serve": cmd_serve,
    "session": cmd_session_create,     # one subcommand today: `session create`
    "retry-triage": cmd_retry_triage,
    "iterate": cmd_iterate,
    "alerts": cmd_alerts,
    "recover": cmd_recover,
    "resume": cmd_resume,
    "abandon": cmd_abandon,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "init-db" and args.config and \
            not Path(args.config).exists():
        print(f"note: {args.config} not found; using built-in defaults.",
              file=sys.stderr)
    try:
        return COMMANDS[args.command](args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
