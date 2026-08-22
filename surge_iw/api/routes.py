"""The §5 endpoints, plus the three debug endpoints for driving an iteration by
hand.

Two route groups with different characters:

**Operational** — initialise a session, trigger an iteration, poll it, read
alerts and their evidence, inspect the queue. Everything an operations front end
needs, and nothing that can change an analytical record.

**Debug** — step one stage, verify a stage, discard the last stage. These exist
because an eight-stage pipeline that can only be run whole is very hard to
develop against: a bad triage prompt costs a full collection pass to re-test.
They are gated by `api.debug_endpoints` and the discard is gated again by an
explicit confirmation whenever the stage in question spent money.

Routes read and write through `SurgeDB` methods rather than SQL, on the same
argument that keeps SQL out of the agents: the database is the system's contract
and it has exactly one owner.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from fastapi import (APIRouter, Depends, Header, HTTPException, Query,
                     Request, Response)
from fastapi.responses import JSONResponse

from ..agents.orchestrator import (  # noqa: F401
    FINALISE_STAGES,
    PIPELINE_STAGES, SessionHasOpenIteration, StageAlreadyRun,
)
from ..db import enums
from ..db.database import SurgeDB, parse_iso
from ..models import Alert
from ..services import geo, governance, inputs, receipts, tunables
from ..services import mission as mission_service
from . import contract
from ..services.recovery import RecoveryService
from ..services.retention import RetentionService
from ..services.stages import StageInspector, StageReport, StageRollback
from . import schemas
from .runner import IterationRunner, SessionBusy
from .security import authenticated, require_token

router = APIRouter()
debug_router = APIRouter(prefix="/v1/iterations", tags=["debug"])


# ---------------------------------------------------------------------------
# Documented failure modes
# ---------------------------------------------------------------------------

#: Reachable error statuses, with what each actually means here. FastAPI
#: documents only the success model and 422, so without this the generated
#: contract would describe a system that never fails authentication, never
#: refuses a concurrent iteration, and never runs out of an iteration to step.
_ERRORS: dict[int, str] = {
    401: "Missing or invalid bearer token.",
    404: "No such session, iteration or alert.",
    409: "Refused because of current state: an iteration is already running "
         "for this session, the iteration is not at the stage you asked for, "
         "or a paid stage needs an explicit confirm.",
    422: "The request was understood but cannot be acted on — an unknown "
         "stage or band, a malformed timestamp, or a session with no cities.",
    503: "SURGE_API_TOKEN is not configured; the API refuses to serve "
         "without authentication rather than serving without it.",
}


#: Declared on the operations whose 409 can be waited out. OPTIONAL by design,
#: and that IS the contract: the same status also covers the
#: unfinished-iteration case, which deliberately carries no `Retry-After`
#: because waiting never clears it. A client treating the header's presence as
#: the retry decision is reading the rule correctly.
#:
#: Added while checking that Phase 8's hardening survived Phase 9. The runtime
#: has always sent it and the contract did not say so — the same defect as the
#: missing bearer scheme, and it would leave a generated client backing off on
#: a 409 that never clears, or hammering one that would.
_RETRY_AFTER_HEADER: dict[str, Any] = {
    "Retry-After": {
        "description": "Seconds to wait before retrying. Present only when "
                       "waiting helps — a 409 from an iteration that was "
                       "never finished omits it, because that one clears only "
                       "by resuming or abandoning the iteration.",
        "schema": {"type": "integer"},
    }
}

#: On a successful trigger or re-triage, so a caller can tell a replayed
#: response from a fresh run. The body is byte-identical either way, which is
#: the whole point of the replay and the reason the distinction has to live in
#: a header.
_REPLAY_HEADER: dict[str, Any] = {
    "Idempotent-Replay": {
        "description": "`true` when this response is the stored reply to an "
                       "earlier request carrying the same `Idempotency-Key`. "
                       "Absent on a fresh run.",
        "schema": {"type": "string"},
    }
}


def errors(*codes: int,
           retryable_409: bool = False) -> dict[int | str, dict[str, Any]]:
    """Response declarations for the statuses a route can actually return."""
    out: dict[int | str, dict[str, Any]] = {
        code: {"description": _ERRORS[code], "model": schemas.ErrorOut}
        for code in codes
    }
    if retryable_409 and 409 in out:
        out[409] = {**out[409], "headers": _RETRY_AFTER_HEADER}
    return out


# ---------------------------------------------------------------------------
# Shared accessors
# ---------------------------------------------------------------------------


def get_db(request: Request) -> SurgeDB:
    return request.app.state.db


def get_runner(request: Request) -> IterationRunner:
    return request.app.state.runner


def get_config(request: Request) -> dict[str, Any]:
    return request.app.state.config


def get_mission(request: Request) -> "mission_service.Mission | None":
    """The mission pack loaded at startup, or None if none is configured.

    None is legitimate — a deployment that has only initialised its database
    has no mission — so callers that need one must say so themselves rather
    than assuming this returns a value.
    """
    return getattr(request.app.state, "mission", None)


def _session_or_404(db: SurgeDB, session_id: int):
    row = db.get_session(session_id)
    if row is None:
        raise HTTPException(404, f"No session {session_id}")
    return row


def _iteration_or_404(db: SurgeDB, iteration_id: int):
    row = db.get_iteration(iteration_id)
    if row is None:
        raise HTTPException(404, f"No iteration {iteration_id}")
    return row


def _raw_view(raw: Mapping[str, Any] | None, expose: bool) -> dict[str, Any] | None:
    """What the evidence endpoint says about a stored vendor payload.

    Withheld is not the same as absent, and both differ from purged. Each is
    stated: a reader who cannot see the payload still learns that it exists,
    which provider served it, when it was retrieved, when it will be deleted,
    and a hash that identifies it if they obtain it another way.
    """
    if raw is None:
        return None
    payload = _json(raw["payload_json"], None)
    view = {
        "raw_id": int(raw["raw_id"]),
        "provider": raw["provider"],
        "retrieved_at": raw["retrieved_at"],
        "purge_after": raw["purge_after"],
        "payload_hash": receipts.sha256_hex(receipts.canonical_json(payload)),
        # 8.3. Whose data this is and what we may do with it, on the record
        # that redistributes it.
        "governance": governance.evidence_note(raw["provider"]),
    }
    if expose:
        view["payload"] = payload
    else:
        view["payload_withheld"] = (
            "api.expose_raw_payloads is false; the normalised signal above is "
            "the evidence surface")
    return view


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _row(row: Mapping[str, Any], *drop: str) -> dict[str, Any]:
    """A sqlite3.Row as a plain dict, minus columns a client has no use for."""
    return {k: row[k] for k in row.keys() if k not in drop}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@router.post("/v1/sessions", status_code=201,
             response_model=schemas.SessionOut, tags=["sessions"],
             responses=errors(401, 422, 503))
def create_session(
    body: schemas.SessionIn,
    db: SurgeDB = Depends(get_db),
    config: dict = Depends(get_config),
    _: None = Depends(authenticated),
) -> schemas.SessionOut:
    """Initialise a session with its key locations.

    Airport and pickup mappings are resolved here rather than at tip time, so a
    city the geo table cannot place is reported as a warning before anything is
    collected. Discovering it mid-iteration would produce a SKIPPED_NO_MAPPING
    query, which is correct but arrives hours later and reads as a data gap
    rather than a setup mistake.

    **`input_set` loads the geography from a file** (8.7c) instead of inlining
    it — the NAME of a file in `inputs.dir`, never a path. The file is resolved
    all-or-nothing: a city the geo table cannot place is refused by name with a
    422, because a session quietly missing a jurisdiction would report a true
    absence of evidence about a place nobody looked at. The response echoes the
    resolved geography either way, so an operator sees what they created rather
    than trusting that a file still says what they remember.
    """
    # 9.2. Before anything is written. `tunables` was accepted, stored and
    # never read, so a client could ask for narrower criteria, get a 200, and
    # have paid collection run under settings it did not choose. Validating
    # here means an unsupported field is a 422 naming it rather than a silent
    # substitution discovered later — or never.
    try:
        overrides = tunables.validate(body.tunables, config)
    except tunables.TunableError as exc:
        raise HTTPException(422, str(exc)) from exc

    cities = body.cities
    warnings: list[str] = []
    if body.input_set:
        try:
            loaded = inputs.load(
                inputs.input_path(body.input_set, config), config=config,
                mission=db.mission)
        except inputs.InputError as exc:
            # 422, not 400: the request is well-formed and the file is the
            # problem. The message names the file and every city it refused.
            raise HTTPException(422, str(exc)) from exc
        cities = [schemas.CityIn.model_validate(c) for c in loaded.as_payload()]
        warnings.append(
            f"loaded {len(cities)} city/cities from {loaded.path}")
        for label in loaded.without_locations:
            warnings.append(
                f"{label}: no key locations in the input file, so the lodging "
                "family will be absent for it")

    # Every city is resolved by now, whether inline or loaded from a file, and
    # nothing has been written yet. Last point at which a refusal costs the
    # caller nothing.
    _refuse_unknown_location_types(db, cities)

    try:
        session_id = db.insert_session(
            label=body.label,
            expand_cities=body.expand_cities,
            # Omitted means every track the mission defines. The engine has no
            # opinion about which of them matter.
            tracks=body.tracks or None,
            config=overrides,
        )
    except ValueError as exc:
        # A track the loaded mission does not define. 422 rather than 400: the
        # request is well-formed and the VALUE is wrong, and the message names
        # both the value and the mission it was checked against, because a
        # client cannot know the vocabulary without asking /v1/capabilities.
        raise HTTPException(422, str(exc)) from exc
    for line in tunables.describe(overrides):
        db.log("api", "INFO", f"Session {session_id} tunable: {line}",
               session_id=session_id)
    for city in cities:
        _add_city(db, config, session_id, city, warnings)
    return _session_out(db, config, session_id, warnings)


@router.get("/v1/sessions/{session_id}", response_model=schemas.SessionOut,
            tags=["sessions"], responses=errors(401, 404, 503))
def read_session(
    session_id: int,
    db: SurgeDB = Depends(get_db),
    config: dict = Depends(get_config),
    _: None = Depends(authenticated),
) -> schemas.SessionOut:
    """The session as configured, with its resolved geography."""
    _session_or_404(db, session_id)
    return _session_out(db, config, session_id, [])


@router.post("/v1/sessions/{session_id}/cities", status_code=201,
             response_model=schemas.SessionOut, tags=["sessions"],
             responses=errors(401, 404, 409, 422, 503))
def add_cities(
    session_id: int,
    body: schemas.CitiesIn,
    db: SurgeDB = Depends(get_db),
    config: dict = Depends(get_config),
    runner: IterationRunner = Depends(get_runner),
    _: None = Depends(authenticated),
) -> schemas.SessionOut:
    """Add cities between iterations, never during one.

    Refused while an iteration is in flight because stage 1 has already fixed
    the city set it will collect against; a city added mid-run would be seeded
    by the next iteration anyway, but its absence from this one's correlation
    would be invisible.
    """
    _session_or_404(db, session_id)
    running = runner.running_iteration(session_id)
    if running is not None:
        raise HTTPException(
            409, f"Iteration {running} is running; add cities between iterations"
        )
    _refuse_unknown_location_types(db, body.cities)
    warnings: list[str] = []
    for city in body.cities:
        _add_city(db, config, session_id, city, warnings)
    return _session_out(db, config, session_id, warnings)


def _refuse_unknown_location_types(
    db: SurgeDB, cities: Sequence[schemas.CityIn]
) -> None:
    """Check the mission's vocabulary before anything is written.

    `location_type` was a closed enum on `KeyLocationIn` until the mission
    vocabularies left the schema in v12; validation moved to
    `insert_key_location`, which both session routes reach only AFTER the
    session and its cities exist. An unknown value therefore stopped being a
    422 that wrote nothing and became a 500 over a session that exists with
    its lodging anchor silently missing — the shape this project keeps
    finding: a control that reads as enforcement and is not.

    The check runs against the same code path the write does, so the two
    cannot disagree about what the mission permits.
    """
    for city in cities:
        for location in city.key_locations:
            try:
                db.check_location_type(location.location_type)
            except ValueError as exc:
                # 422, matching the track refusal in create_session: the
                # request is well-formed and the VALUE is wrong. The message
                # already lists what was allowed, because a client cannot know
                # the vocabulary without asking /v1/capabilities.
                raise HTTPException(422, str(exc)) from exc


def _add_city(
    db: SurgeDB, config: Mapping[str, Any], session_id: int,
    city: schemas.CityIn, warnings: list[str],
) -> None:
    label = f"{city.name}, {city.state}" if city.state else city.name
    canonical, _method = geo.resolve_city(label, _jurisdictions(db))
    if canonical is None:
        canonical = geo.normalise(label)
    if db.find_city(session_id, canonical) is not None:
        warnings.append(f"{label}: already present; not added again")
        return

    city_id = db.insert_city(
        session_id, city.name, canonical=canonical, state=city.state,
        is_seed=True, admitted_by="USER",
    )
    for location in city.key_locations:
        db.insert_key_location(
            city_id, location.name, address=location.address,
            lat=location.lat, lon=location.lon,
            location_type=location.location_type,
        )
    if not city.key_locations:
        warnings.append(
            f"{label}: no key locations given, so lodging queries have nothing "
            "to anchor on and the lodging family will be absent"
        )
    limit = int((config.get("flightradar") or {}).get("max_airports_per_city", 3))
    if not geo.city_to_airports(canonical, limit=limit):
        warnings.append(
            f"{label}: no airport mapping, so flight queries will be skipped "
            "as NO_AIRPORT_MAPPING rather than reported as no flights"
        )
    if geo.city_to_pickup_location(canonical) is None:
        warnings.append(f"{label}: no rental-car pickup mapping")


def _session_out(
    db: SurgeDB, config: Mapping[str, Any], session_id: int,
    warnings: Sequence[str],
) -> schemas.SessionOut:
    row = db.get_session(session_id)
    limit = int((config.get("flightradar") or {}).get("max_airports_per_city", 3))
    cities = []
    for city in db.get_cities(session_id):
        canonical = city["canonical"]
        cities.append(schemas.CityOut(
            city_id=int(city["city_id"]), name=city["name"],
            state=city["state"], is_seed=bool(city["is_seed"]),
            admitted_by=city["admitted_by"],
            key_locations=[loc["name"] for loc in
                           db.get_key_locations(int(city["city_id"]))],
            airports=geo.city_to_airports(canonical, limit=limit),
            pickup_location=geo.city_to_pickup_location(canonical),
        ))
    # 9.2. Echo what actually governs this session's work, and the fingerprint
    # of the configuration its iterations will run under. The hash is the same
    # one every receipt carries, so a client can confirm after the fact that a
    # judgement was made under the settings it asked for — which is exactly
    # what it could not do while `tunables` was stored and never read.
    stored = json.loads(row["config_json"] or "{}")
    warnings = list(warnings)
    ignored = sorted(set(tunables.unsupported(stored)))
    if ignored:
        warnings.append(
            "stored tunables not applied (predate the allowlist): "
            + ", ".join(ignored))
    return schemas.SessionOut(
        session_id=session_id, label=row["label"], status=row["status"],
        created_at=row["created_at"], expand_cities=bool(row["expand_cities"]),
        tracks=db.session_tracks(session_id), cities=cities,
        warnings=warnings,
        tunables=stored,
        config_hash=receipts.config_fingerprint(
            tunables.effective(config, stored)),
    )


# ---------------------------------------------------------------------------
# Iterations
# ---------------------------------------------------------------------------


@router.post("/v1/sessions/{session_id}/iterations", status_code=202,
             response_model=schemas.IterationAccepted, tags=["iterations"],
             responses={
                 200: {"description": "Finished within ?wait=true's timeout.",
                       "model": schemas.IterationAccepted,
                       "headers": _REPLAY_HEADER},
                 202: {"description": "Accepted; poll `poll_url` for progress.",
                       "headers": _REPLAY_HEADER},
                 **errors(401, 404, 409, 422, 503, retryable_409=True),
             })
def trigger_iteration(
    session_id: int,
    response: Response,
    body: schemas.IterationIn | None = None,
    wait: bool = Query(False, description="Run synchronously up to "
                                          "api.sync_timeout_s"),
    idempotency_key: str | None = Header(
        None, alias=contract.IDEMPOTENCY_HEADER,
        description="Opt-in replay protection. Repeating a POST with the same "
                    "key returns the first response instead of starting a "
                    "second run."),
    db: SurgeDB = Depends(get_db),
    config: dict = Depends(get_config),
    runner: IterationRunner = Depends(get_runner),
    _: None = Depends(authenticated),
) -> Any:
    """Trigger one iteration. The only thing that starts collection.

    Returns 202 and a poll URL; the run continues on a worker. `?wait=true`
    blocks up to the configured timeout for CLI use and still returns 202 if the
    run outlives it — the iteration is not abandoned, because one that has
    already paid for collection should finish and record what it bought.

    **Send an `Idempotency-Key`.** This endpoint spends money. A client that
    loses the response to a network timeout otherwise has no safe move: retry
    and it may buy a second full collection pass, don't and there may have been
    no run at all. With a key the retry replays the first response and starts
    nothing, and the reply carries `Idempotent-Replay: true` so a caller can
    tell. Reusing a key for a *different* body is refused with 422 rather than
    silently answered with the old result.

    A 409 here is retryable and carries `Retry-After`.
    """
    _session_or_404(db, session_id)
    key = contract.validate_key(idempotency_key)
    fingerprint = contract.request_fingerprint(
        session_id, (body.model_dump() if body else None), wait)
    if key:
        prior = db.find_idempotency_key(session_id, key)
        if prior is not None:
            return contract.replay(prior, fingerprint)

    mode = (body or schemas.IterationIn()).mode
    try:
        if mode == "manual":
            iteration_id = runner.create(session_id)
            outcome: str | None = None
        elif wait:
            timeout = float((config.get("api") or {}).get("sync_timeout_s", 600))
            iteration_id, outcome = runner.run_and_wait(session_id, timeout)
        else:
            iteration_id, _future = runner.submit(session_id)
            outcome = None
    except SessionBusy as exc:
        # Retryable, and the wait is worth naming: an iteration runs for
        # minutes, so a client told only "409" either gives up or hammers.
        raise contract.RetryableError(
            409, str(exc), _busy_retry_after(config)) from exc
    except SessionHasOpenIteration as exc:
        # NOT retryable, and deliberately carries no Retry-After: waiting will
        # never clear it. The message names each blocking iteration, which KIND
        # of open it is, and the two URLs that close it — one status code with
        # two different remedies, and the operator should not have to guess
        # which they are in. The machine-readable form is GET /v1/recovery.
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:                     # no cities, closed session
        raise HTTPException(422, str(exc)) from exc

    row = db.get_iteration(iteration_id)
    status_code = 200 if outcome is not None else 202
    response.status_code = status_code
    status = ("FINISHED" if outcome is not None
              else "PENDING" if mode == "manual" else "RUNNING")
    accepted = schemas.IterationAccepted(
        iteration_id=iteration_id, session_id=session_id, status=status,
        stage=row["stage"], poll_url=f"/v1/iterations/{iteration_id}",
        next_stage=row["stage"] if row["stage"] in PIPELINE_STAGES else None,
        budget_plan=_json(row["budget_plan_json"], {}),
    )
    if key:
        # Stored only once the run exists, so a request that failed before
        # starting anything is not replayed as though it had succeeded.
        db.record_idempotency_key(
            session_id=session_id, key=key, request_hash=fingerprint,
            iteration_id=iteration_id, status_code=status_code,
            response=accepted.model_dump(),
            ttl_hours=float((config.get("api") or {}).get(
                "idempotency_ttl_hours", 24.0)),
        )
        response.headers[contract.IDEMPOTENCY_HEADER] = key
    return accepted


@router.get("/v1/sessions/{session_id}/iterations",
            response_model=list[schemas.IterationOut], tags=["iterations"],
            responses=errors(401, 404, 503))
def list_iterations(
    session_id: int,
    limit: int = Query(20, ge=1, le=200),
    db: SurgeDB = Depends(get_db),
    runner: IterationRunner = Depends(get_runner),
    _: None = Depends(authenticated),
) -> list[schemas.IterationOut]:
    """This session's iterations, most recent first."""
    _session_or_404(db, session_id)
    return [_iteration_out(db, runner, row)
            for row in db.get_iterations(session_id, limit=limit)]


@router.get("/v1/iterations/{iteration_id}", response_model=schemas.IterationOut,
            tags=["iterations"], responses=errors(401, 404, 503))
def read_iteration(
    iteration_id: int,
    db: SurgeDB = Depends(get_db),
    runner: IterationRunner = Depends(get_runner),
    _: None = Depends(authenticated),
) -> schemas.IterationOut:
    """The poll target for the 202."""
    return _iteration_out(db, runner, _iteration_or_404(db, iteration_id))


def _busy_retry_after(config: Mapping[str, Any]) -> int:
    """How long to tell a blocked caller to wait.

    Config, not a constant: the honest number is "about as long as an iteration
    takes", and that varies with how many cities and vendors a deployment uses.
    """
    return int((config.get("api") or {}).get("busy_retry_after_s", 60))


def _iteration_status(row, running: bool) -> str:
    """Lifecycle state, in precedence order.

    `is_running` must beat `interrupted_at` so a resume in progress reads
    RUNNING rather than the interruption it is repairing.
    """
    if row["finished_at"]:
        return "FINISHED"
    if running:
        return "RUNNING"
    if row["interrupted_at"]:
        return "INTERRUPTED"
    return "PENDING"


def _iteration_out(db: SurgeDB, runner: IterationRunner, row) -> schemas.IterationOut:
    iteration_id = int(row["iteration_id"])
    running = runner.is_running(iteration_id)
    status = _iteration_status(row, running)
    # Every provider, always, including the ones that cost nothing this run. A
    # key that appears only when there is spend forces a client to distinguish
    # "no spend" from "no such provider", and zero is the more useful answer.
    budget = {
        provider: {
            "used_this_iteration": round(
                db.units_used(provider, iteration_id=iteration_id), 3),
        }
        for provider in sorted(enums.PROVIDERS)
    }
    return schemas.IterationOut(
        iteration_id=iteration_id, session_id=int(row["session_id"]),
        seq=int(row["seq"]), stage=row["stage"], outcome=row["outcome"],
        running=running, status=status,
        interrupted_at=row["interrupted_at"],
        interrupted_stage=row["interrupted_stage"],
        retry_of_iteration_id=row["retry_of_iteration_id"],
        # Any iteration that has not closed and is not on a worker (8.7a).
        # Previously INTERRUPTED only, so a PENDING iteration that was in fact
        # blocking its session advertised no remedy at all.
        resumable=status in ("INTERRUPTED", "PENDING"),
        anchor_at=row["anchor_at"], started_at=row["started_at"],
        finished_at=row["finished_at"],
        next_stage=row["stage"] if row["stage"] in PIPELINE_STAGES else None,
        counts=db.iteration_counts(iteration_id),
        # 9.x / review #8. The database has carried typed outcomes since Phase
        # 7; the operational contract exposed only the total, so a
        # server-to-server client could not tell a rejection from a post that
        # was never judged without parsing degradation prose.
        triage_states=db.triage_state_counts(iteration_id),
        budget=budget,
        degradations=db.degradation_notes(iteration_id),
        error_message=row["error_message"],
    )


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@router.get("/v1/sessions/{session_id}/alerts", tags=["alerts"],
            response_model=list[schemas.AlertOut] | list[schemas.AlertTupleOut],
            responses=errors(401, 404, 422, 503))
def list_alerts(
    session_id: int,
    since: str | None = Query(None, description="ISO-8601 lower bound"),
    min_confidence: str | None = Query(
        None, description="LOW | MEDIUM | HIGH — inclusive floor"),
    city: str | None = None,
    track: str | None = None,
    iteration_id: int | None = None,
    review_state: str | None = Query(
        None, description="UNREVIEWED | RELEASED | WITHHELD. A consumer "
                          "distributing onward should ask for RELEASED."),
    format: str = Query("named", pattern="^(named|tuple)$"),
    db: SurgeDB = Depends(get_db),
    _: None = Depends(authenticated),
) -> Any:
    """The required output: alerts with their evidence, most severe first.

    `format=tuple` returns the four evidence groups as a positional array in the
    order the requirement names them, for clients that want the literal tuple
    shape rather than keys they would have to trust.

    **Unfiltered means unfiltered.** Every alert is returned regardless of
    review state, because an operator cannot review what the API hides. A
    client that distributes onward should pass `review_state=RELEASED`;
    one that shows an analyst a queue should pass `UNREVIEWED`. Defaulting to
    RELEASED here would have made an unreviewed alert invisible rather than
    unreviewed — the same failure as reporting a coverage gap as quiet.
    """
    _session_or_404(db, session_id)
    since_dt = parse_iso(since) if since else None
    if since and since_dt is None:
        raise HTTPException(422, f"since={since!r} is not an ISO-8601 timestamp")
    if min_confidence:
        enums.validate(min_confidence.upper(), enums.ALERT_BANDS, "min_confidence")
    if track:
        if db.mission is None:
            raise HTTPException(
                422, "track= cannot be checked: no mission is loaded, and the "
                     "permitted values are the mission's.")
        db.mission.track(track.upper())
    if review_state:
        enums.validate(review_state.upper(), enums.REVIEW_STATES, "review_state")

    city_id = None
    if city:
        canonical, _m = geo.resolve_city(city, _jurisdictions(db))
        found = db.find_city(session_id, canonical or geo.normalise(city))
        if found is None:
            return []
        city_id = int(found["city_id"])

    rows = db.get_alerts(
        session_id, since=since_dt,
        min_band=min_confidence.upper() if min_confidence else None,
        city_id=city_id,
        track=track.upper() if track else None,
        iteration_id=iteration_id,
    )
    if review_state:
        wanted = review_state.upper()
        rows = [r for r in rows
                if (r["review_state"] or "UNREVIEWED") == wanted]
    alerts = [(row, Alert.from_rows(row, db.correlation_signals(
        int(row["correlation_id"])))) for row in rows]

    if format == "tuple":
        return [
            schemas.AlertTupleOut(
                alert_id=alert.alert_id, city=alert.city,
                track=alert.track, summary=alert.summary,
                confidence=schemas.Confidence(score=alert.confidence_score,
                                              band=alert.confidence_band),
                caveat=alert.caveat, evidence=alert.as_positional(),
                evidence_url=f"/v1/alerts/{alert.alert_id}/evidence",
            )
            for _row, alert in alerts
        ]
    return [_alert_out(row, alert) for row, alert in alerts]


def _alert_out(row, alert: Alert) -> schemas.AlertOut:
    data = alert.as_dict()
    return schemas.AlertOut(
        review_state=row["review_state"] or "UNREVIEWED",
        alert_id=alert.alert_id, iteration_id=int(row["iteration_id"]),
        city=alert.city, track=alert.track,
        confidence=schemas.Confidence(score=alert.confidence_score,
                                      band=alert.confidence_band),
        summary=alert.summary, caveat=alert.caveat,
        earliest_eta=alert.earliest_eta, created_at=alert.created_at,
        evidence_url=f"/v1/alerts/{alert.alert_id}/evidence",
        social_posts=data["social_posts"], flights=data["flights"],
        lodging=data["lodging"], rental_cars=data["rental_cars"],
    )


@router.get("/v1/alerts/{alert_id}/evidence", response_model=schemas.EvidenceOut,
            tags=["alerts"], responses=errors(401, 404, 503))
def read_evidence(
    alert_id: int,
    db: SurgeDB = Depends(get_db),
    config: dict = Depends(get_config),
    _: None = Depends(authenticated),
) -> schemas.EvidenceOut:
    """Every contributing signal back to the raw payload and the queue row.

    This is what makes a score arguable rather than merely reported. The
    arithmetic is included — per-family weight × quality, the band rule that
    fired, and the completeness that capped it — because whoever is asked to
    act on a number is entitled to see how it was reached.

    A signal whose raw payload has passed its retention deadline shows
    `raw: null` with the query row intact. FR24's licence requires deletion
    after 30 days; the analytical record deliberately outlives the licensed data.

    **The evidence surface is the normalised record, not the vendor payload**
    (8.2). By default `raw.payload` and the query parameters are withheld and
    described rather than returned: what may be redistributed from a provider
    response is a rights question per provider (8.3), not something this
    endpoint should assume, and the parameters carry the search lexicon and
    facility coordinates. What is always returned is everything needed to argue
    with the conclusion — the normalised signal, its provenance and receipt, the
    per-family arithmetic, and enough about the payload (provider, timestamps,
    retention deadline, content hash) to demand it through another channel.
    Set `api.expose_raw_payloads: true` to include them.
    """
    alert = db.get_alert(alert_id)
    if alert is None:
        raise HTTPException(404, f"No alert {alert_id}")
    return _evidence_for_correlation(
        db, config, int(alert["correlation_id"]), alert=alert)


def _collection_summary(signals: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """9.4. Contributing signals counted by how directly they are known.

    Empty classes are omitted rather than zero-filled: a reader scanning this
    should see what is there, and `DIRECT: 0` on every alert forever would be
    noise carrying one fact that belongs in the documentation.
    """
    counts: dict[str, int] = {}
    for entry in signals:
        row = entry.get("signal") or {}
        name = row.get("collection_class") or "UNRECORDED"
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def _evidence_for_correlation(
    db: SurgeDB, config: Mapping[str, Any], correlation_id: int,
    alert: Mapping[str, Any] | None = None,
) -> schemas.EvidenceOut:
    """The evidence surface, assembled from the CORRELATION (8.7b).

    It always was: every field below comes from the correlation and its signals,
    and the alert contributed only its id, its summary and its caveat. Keying
    the endpoint on the alert therefore made the whole surface unreachable for
    any correlation that did not produce one — which is precisely the set of
    near misses the interim floors in `correlation` and `sensitivity` are
    supposed to be calibrated from.

    `alert` is passed in when there is one, so the alert route stays a
    resolve-and-delegate rather than a second copy of this.
    """
    expose_raw = bool((config.get("api") or {}).get("expose_raw_payloads", False))
    correlation = db.get_correlation(correlation_id)
    if correlation is None:
        raise HTTPException(404, f"No correlation {correlation_id}")
    city = db.one("SELECT * FROM cities WHERE city_id = ?",
                  (correlation["city_id"],))
    signals = []
    for signal in db.correlation_signals(correlation_id):
        raw = (db.get_raw_result(signal["raw_id"])
               if signal["raw_id"] is not None else None)
        query = db.get_query(int(raw["query_id"])) if raw is not None else None
        signals.append({
            "signal": _row(signal),
            "contribution": round(float(signal["contribution"]), 6),
            # How the judgement behind this signal was reached (8.1). Present
            # for SOCIAL only: the other families are derived deterministically
            # from vendor records with no model involved, and `null` says so.
            "receipt": receipts.public_view(
                db.receipt_for_signal(signal["signal_id"])),
            "raw": _raw_view(raw, expose_raw),
            "query": {
                "query_id": int(query["query_id"]),
                "source_type": query["source_type"],
                "endpoint": query["endpoint"],
                # Request parameters are provenance, not evidence, and they
                # carry the search lexicon and facility coordinates. Shown only
                # when a deployment opts in.
                **({"params": _json(query["params_json"], {})}
                   if expose_raw else
                   {"params_withheld": "api.expose_raw_payloads is false"}),
                "rule_code": query["rule_code"],
                "origin": query["origin"],
                "tip_depth": int(query["tip_depth"]),
                "tipped_by_signal_id": query["tipped_by_signal_id"],
                "status": query["status"],
                "executed_at": query["executed_at"],
            } if query is not None else None,
        })

    if alert is not None:
        name = (f"{alert['city_name']}, {alert['city_state']}"
                if alert["city_state"] else alert["city_name"])
    elif city is not None:
        name = f"{city['name']}, {city['state']}" if city["state"] else city["name"]
    else:
        name = ""
    return schemas.EvidenceOut(
        alert_id=None if alert is None else int(alert["alert_id"]),
        correlation_id=correlation_id,
        city=name,
        track=correlation["track"],
        # From the CORRELATION either way. AlertAgent copies these onto the
        # alert and a Phase 5 test asserts the two are identical, so reading the
        # correlation is not a different answer — it is the same answer from the
        # row that computed it, and the only one a sub-threshold correlation has.
        confidence=schemas.CorrelationConfidence(
            score=float(correlation["score"]), band=correlation["band"]),
        summary=None if alert is None else alert["summary"],
        caveat=None if alert is None else alert["caveat"],
        # Why this did or did not become an alert, recorded by ALERTING rather
        # than left for the reader to infer from `score` and a config value.
        # None means ALERTING has not run for this iteration yet.
        alert_decision=correlation["alert_decision"],
        alert_decision_reason=correlation["alert_decision_reason"],
        rule_trace=correlation["rule_trace"],
        contributions=_json(correlation["contributions_json"], {}),
        distinct_types=int(correlation["distinct_types"]),
        data_completeness=float(correlation["data_completeness"]),
        failed_sources=_csv(correlation, "failed_sources"),
        failed_families=_csv(correlation, "failed_families"),
        band_capped=bool(correlation["band_capped"]),
        # 9.6. Read from the row rather than recomputed, so an alert written
        # months ago still shows the alternatives its own rules produced.
        alternatives=_json(correlation["alternatives_json"], None),
        # 9.10. Read back rather than recomputed: an alert must show the
        # baseline ITS scoring used, not what the city looks like today.
        flight_baseline=_json(correlation["flight_baseline_json"], None),
        # 9.13. Read back rather than recomputed: an alert must say what was
        # true when it was written, not what is true now.
        evidence_freshness=_json(correlation["evidence_freshness_json"], None),
        # 9.4. Counted from the signals rather than stored on the correlation:
        # it is a summary of rows that already carry the value, and a second
        # copy would be one more thing that can disagree with the first.
        collection=_collection_summary(signals),
        signals=signals,
        receipt=(None if alert is None
                 else receipts.public_view(db.get_receipt(alert["receipt_id"]))),
    )


@router.get("/v1/correlations/{correlation_id}/evidence",
            response_model=schemas.EvidenceOut, tags=["correlations"],
            responses=errors(401, 404, 503))
def read_correlation_evidence(
    correlation_id: int,
    db: SurgeDB = Depends(get_db),
    config: dict = Depends(get_config),
    _: None = Depends(authenticated),
) -> schemas.EvidenceOut:
    """The same drill-down, for a correlation that produced no alert (8.7b).

    Identical shape to `GET /v1/alerts/{id}/evidence` — that endpoint now
    resolves its correlation and delegates here — with `alert_id`, `summary`,
    `caveat` and `receipt` null when no alert was written, and
    `alert_decision` saying why.

    **This is the calibration surface.** A correlation below
    `correlation.alert_min_score` is a near miss, and the near misses are what
    the interim floors in `correlation` and `sensitivity` are meant to be set
    from. Until now they were reachable only by opening the database, which also
    meant no front end could ever show one.

    Operational, not debug: an analyst tuning floors on a deployment serving
    an operations team should not have to mount the endpoint that deletes
    analytical records in order to do it. Read-only over rows that already
    exist.
    """
    return _evidence_for_correlation(db, config, correlation_id)


@router.get("/v1/iterations/{iteration_id}/correlations",
            response_model=schemas.CorrelationsOut, tags=["correlations"],
            responses=errors(401, 404, 503))
def read_correlations(
    iteration_id: int,
    db: SurgeDB = Depends(get_db),
    _: None = Depends(authenticated),
) -> schemas.CorrelationsOut:
    """Every correlation this iteration scored, alerting or not.

    One row per city and actor track that produced any evidence at all. The
    ones that became alerts are reachable through `/v1/sessions/{id}/alerts`;
    the ones that did not were, before 8.7(b), reachable nowhere.

    `alert_decision` says what ALERTING concluded about each and why.
    `evidence_url` drills into any of them.
    """
    _iteration_or_404(db, iteration_id)
    rows = db.get_correlations(iteration_id)
    alerted = {int(r["correlation_id"]) for r in db.all(
        "SELECT correlation_id FROM alerts WHERE iteration_id = ?",
        (iteration_id,))}
    return schemas.CorrelationsOut(
        iteration_id=iteration_id,
        counts=_counter(r["alert_decision"] or "NOT_DECIDED" for r in rows),
        correlations=[_correlation_out(db, r, r["correlation_id"] in alerted)
                      for r in rows],
    )


def _csv(row: Mapping[str, Any], column: str) -> list[str]:
    """A stored comma-separated column as a list, tolerant of an old row.

    `failed_families` post-dates some correlations, so a row without the column
    reads as an empty list rather than raising — the same tolerance the caveat
    builder needs.
    """
    keys = row.keys() if hasattr(row, "keys") else row
    if column not in keys:
        return []
    return [value for value in (row[column] or "").split(",") if value]


def _counter(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _correlation_out(
    db: SurgeDB, row: Mapping[str, Any], alerted: bool
) -> schemas.CorrelationOut:
    city = db.one("SELECT * FROM cities WHERE city_id = ?", (row["city_id"],))
    correlation_id = int(row["correlation_id"])
    return schemas.CorrelationOut(
        correlation_id=correlation_id,
        city=(f"{city['name']}, {city['state']}"
              if city is not None and city["state"]
              else (city["name"] if city is not None else "")),
        track=row["track"],
        confidence=schemas.CorrelationConfidence(
            score=float(row["score"]), band=row["band"]),
        distinct_types=int(row["distinct_types"]),
        data_completeness=float(row["data_completeness"]),
        band_capped=bool(row["band_capped"]),
        failed_sources=_csv(row, "failed_sources"),
        failed_families=_csv(row, "failed_families"),
        rule_trace=row["rule_trace"],
        contributions=_json(row["contributions_json"], {}),
        alert_decision=row["alert_decision"],
        alert_decision_reason=row["alert_decision_reason"],
        alerted=alerted,
        computed_at=row["computed_at"],
        evidence_url=f"/v1/correlations/{correlation_id}/evidence",
    )




@router.post("/v1/iterations/{iteration_id}/retry-triage", status_code=202,
             response_model=schemas.IterationAccepted, tags=["iterations"],
             responses={
                 200: {"description": "Finished within ?wait=true's timeout.",
                       "model": schemas.IterationAccepted,
                       "headers": _REPLAY_HEADER},
                 202: {"description": "Accepted; poll `poll_url` for progress.",
                       "headers": _REPLAY_HEADER},
                 **errors(401, 404, 409, 422, 503, retryable_409=True),
             })
def retry_triage(
    iteration_id: int,
    response: Response,
    body: schemas.RetryTriageIn | None = None,
    wait: bool = Query(False, description="Run synchronously up to "
                                          "api.sync_timeout_s"),
    idempotency_key: str | None = Header(
        None, alias=contract.IDEMPOTENCY_HEADER,
        description="Opt-in replay protection. Repeating a POST with the same "
                    "key returns the first response instead of starting a "
                    "second retry."),
    db: SurgeDB = Depends(get_db),
    config: dict = Depends(get_config),
    runner: IterationRunner = Depends(get_runner),
    _: None = Depends(authenticated),
) -> Any:
    """Re-judge the posts this iteration's model calls never answered (8.8).

    A batch that overruns `llm.max_tokens` records **every** post in it as
    `MODEL_ERROR`: the evidence was collected and paid for, only the judgement
    is missing. This recovers those judgements and lets them tip collection.

    **It creates a NEW iteration**, a child of this one, and never edits the
    parent. The parent stays as it was — partial, degraded, its gap named — and
    the child carries `retry_of_iteration_id`. Two records in the order they
    happened rather than one rewritten after the fact.

    The child inherits the parent's `anchor_at`, because the correlation window
    is measured from it and a fresh anchor would slide the window off the
    evidence the retry exists to complete. It runs TRIAGING through ALERTING:
    SEEDING and COLLECTING_SOCIAL are inherited rather than re-run, and
    SCHEDULING is skipped because the parent already queued follow-ons for this
    same evidence. Correlation reads signals across the session by observation
    time, so the child scores the union of both runs' evidence without anything
    being copied.

    **Only `UNDECIDED`, `INVALID_OUTPUT` and `MODEL_ERROR` are retried.**
    ACCEPTED and REJECTED are completed judgements and a rejection is a
    conclusion, not a failure. A post dropped for being older than
    `triage.max_post_age_hours` has no decision row at all, so it cannot be
    reached — that requirement holds by construction rather than by a second
    filter that could drift from the first.

    **Send an `Idempotency-Key`.** This endpoint spends model tokens and tips
    paid collection, and it is *more* dangerous to retry blindly than the
    trigger is: the parent's `MODEL_ERROR` rows are deliberately never edited,
    so the candidate set is unchanged by a successful retry and a repeated call
    would create another child and spend again, indefinitely. With a key the
    retry replays the first response and starts nothing.

    409 if the parent has not closed or the session has an iteration open;
    422 if there is nothing to retry.
    """
    parent = _iteration_or_404(db, iteration_id)
    session_id = int(parent["session_id"])
    body = body or schemas.RetryTriageIn()
    key = contract.validate_key(idempotency_key)
    fingerprint = contract.request_fingerprint(
        iteration_id, body.model_dump(), wait)
    if key:
        prior = db.find_idempotency_key(session_id, key)
        if prior is not None:
            return contract.replay(prior, fingerprint)
    try:
        child_id, future = runner.submit_retry_triage(
            session_id, iteration_id, body.batch_size)
        outcome: str | None = None
        if wait:
            from concurrent.futures import TimeoutError as FTimeout
            try:
                outcome = future.result(
                    timeout=float((config.get("api") or {}).get(
                        "sync_timeout_s", 600)))
            except FTimeout:
                # The run is NOT abandoned. Same rule as the trigger: an
                # iteration that has paid for collection finishes.
                outcome = None
    except SessionBusy as exc:
        raise contract.RetryableError(
            409, str(exc), _busy_retry_after(config)) from exc
    except SessionHasOpenIteration as exc:
        # Not retryable and no Retry-After, for the same reason as the trigger's
        # 409: waiting never clears an outstanding iteration.
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        # Parent not closed, or nothing to retry. Both are 422-shaped: the
        # request is understood and the state cannot satisfy it.
        message = str(exc)
        raise HTTPException(
            409 if "has not closed" in message else 422, message) from exc

    row = db.get_iteration(child_id)
    status_code = 200 if outcome is not None else 202
    response.status_code = status_code
    accepted = schemas.IterationAccepted(
        iteration_id=child_id, session_id=session_id, seq=int(row["seq"]),
        status="FINISHED" if outcome is not None else "RUNNING",
        stage=row["stage"],
        poll_url=f"/v1/iterations/{child_id}",
        retry_of_iteration_id=iteration_id,
        budget_plan=_json(row["budget_plan_json"], {}),
    )
    if key:
        # Stored only once the child exists, so a request refused before
        # anything was created is not replayed as though it had succeeded.
        db.record_idempotency_key(
            session_id=session_id, key=key, request_hash=fingerprint,
            iteration_id=child_id, status_code=status_code,
            response=accepted.model_dump(),
            ttl_hours=float((config.get("api") or {}).get(
                "idempotency_ttl_hours", 24.0)),
        )
        response.headers[contract.IDEMPOTENCY_HEADER] = key
    return accepted


@router.post("/v1/iterations/{iteration_id}/cancel",
             response_model=schemas.CancelOut, tags=["iterations"],
             responses=errors(401, 404, 409, 503))
def cancel_iteration(
    iteration_id: int,
    body: schemas.CancelIn | None = None,
    db: SurgeDB = Depends(get_db),
    runner: IterationRunner = Depends(get_runner),
    _: None = Depends(authenticated),
) -> schemas.CancelOut:
    """Ask a running iteration to stop early.

    **Cooperative, not a kill.** The request is recorded and honoured at the
    next stage boundary; the iteration then runs CORRELATING and ALERTING and
    closes PARTIAL. A hard stop would spend money on collection and throw away
    the evidence it bought, and would produce no alert at all for a city whose
    coverage may be nearly complete — a real cluster reading as silence, which
    is the one outcome this system exists to prevent.

    The skipped stages are recorded as degradations and the uncollected work
    flows into `data_completeness`, so the band is capped and the caveat names
    the gap. Cancelling cannot launder a partial run into full confidence.

    409 if the iteration has already finished — there is nothing to stop, and
    reporting success would imply the run was shortened when it was not.
    """
    row = _iteration_or_404(db, iteration_id)
    if row["finished_at"]:
        raise HTTPException(
            409, f"Iteration {iteration_id} already finished "
                 f"({row['outcome']}); nothing to cancel")
    body = body or schemas.CancelIn()
    db.request_cancel(iteration_id, requested_by=body.requested_by,
                      reason=body.reason)
    db.log("API", "WARNING", f"Cancellation requested for iteration "
                             f"{iteration_id}", iteration_id=iteration_id,
           requested_by=body.requested_by, reason=body.reason)
    fresh = db.get_iteration(iteration_id)
    running = runner.is_running(iteration_id)
    return schemas.CancelOut(
        iteration_id=iteration_id,
        status=_iteration_status(fresh, running),
        cancel_requested_at=fresh["cancel_requested_at"],
        will_still_run=sorted(FINALISE_STAGES),
        note=("Honoured at the next stage boundary. The iteration will score "
              "and alert on what it has already collected, then close PARTIAL."
              if running else
              "Recorded. The iteration is not currently on a worker, so this "
              "takes effect if and when it is resumed."),
    )


@router.post("/v1/alerts/{alert_id}/review", response_model=schemas.ReviewOut,
             tags=["alerts"], responses=errors(401, 404, 422, 503))
def review_alert(
    alert_id: int,
    body: schemas.ReviewIn,
    db: SurgeDB = Depends(get_db),
    _: None = Depends(authenticated),
) -> schemas.ReviewOut:
    """Record a human decision about distributing this alert.

    Distribution only. The score, the band, the evidence and the receipts are
    untouched — a reviewer governs what leaves the system, not what it
    concluded. That separation is deliberate: an alert withheld for being
    operationally unhelpful must remain in the record at its computed
    confidence, or the audit trail becomes a record of what was published
    rather than of what was found.

    Setting UNREVIEWED again clears the reviewer and note rather than leaving
    someone's name attached to a state they no longer hold.
    """
    if db.get_alert(alert_id) is None:
        raise HTTPException(404, f"No alert {alert_id}")
    db.set_review_state(alert_id, body.review_state,
                        reviewed_by=body.reviewed_by, note=body.note)
    row = db.get_alert(alert_id)
    return schemas.ReviewOut(
        alert_id=alert_id, review_state=row["review_state"],
        reviewed_at=row["reviewed_at"], reviewed_by=row["reviewed_by"],
        note=row["review_note"],
    )


@router.get("/v1/capabilities", response_model=schemas.CapabilitiesOut,
            tags=["ops"], responses=errors(401, 503))
def read_capabilities(
    city: list[str] | None = Query(
        None, description="Repeatable. Ask whether a jurisdiction is "
                          "collectable BEFORE creating a session for it."),
    db: SurgeDB = Depends(get_db),
    config: dict = Depends(get_config),
    loaded_mission=Depends(get_mission),
    _: None = Depends(authenticated),
) -> schemas.CapabilitiesOut:
    """What this deployment can and cannot collect, stated up front.

    This endpoint exists because "no alerts for this county" and "this county
    was never collectable" are identical on the wire, and the second silently
    reads as reassurance. An unsupported jurisdiction is now reported as
    unsupported.

    With `?city=` it answers per jurisdiction: whether the name resolves, which
    airports and pickup point it maps to, and which source families would
    therefore produce nothing. Cheap and offline — it consults the geo tables
    and the configuration, and calls no vendor.
    """
    staying = config.get("staying") or {}
    fr24 = config.get("flightradar") or {}
    configured = {
        "APIDIRECT": bool((config.get("apidirect") or {}).get("api_key_env")),
        "FR24": bool(fr24.get("api_key_env")),
        "STAYING": bool(staying.get("api_key_env")),
        "PRICELINE": bool((config.get("priceline") or {}).get("api_key_env")),
    }
    # Straight from the governance record (8.3) rather than restated here,
    # so the endpoint cannot drift from what the system actually enforces.
    providers = {}
    for name, policy in sorted(governance.POLICIES.items()):
        providers[name] = {
            "families": list(policy.families),
            "configured": configured.get(name, False),
            "unit": policy.unit,
            "unit_basis": policy.unit_basis,
            "retention_days": governance.retention_days(name),
            "retention_basis": policy.retention_basis,
            "identifiers": list(policy.identifiers),
            "provenance": policy.provenance,
            "rate_limits": policy.rate_limits,
            "failure_modes": policy.failure_modes,
            "measured": policy.measured,
            # Never True. A 200 means the vendor served the bytes, not that
            # we may redistribute them.
            "downstream_rights_verified": policy.rights_verified,
        }
    providers["STAYING"]["platforms"] = list(staying.get("platforms") or [])
    providers["FR24"]["sandbox"] = bool(fr24.get("sandbox"))
    return schemas.CapabilitiesOut(
        mission=_mission_block(loaded_mission),
        tracks=list(loaded_mission.tracks) if loaded_mission else [],
        signal_families=sorted(enums.SIGNAL_TYPES),
        providers=providers,
        geography={
            "country": "US",
            "resolution": "city or county name, resolved to airport codes",
            "known_cities": len(geo.CITY_AIRPORTS),
            "known_aliases": len(geo.CITY_ALIASES),
            # Stated because it is the single biggest limit on coverage: a city
            # with no airport mapping loses the flight and car families
            # entirely, and there is no fallback that would be honest.
            "unmapped_city_effect":
                "FLIGHT and CAR report SKIPPED_NO_MAPPING — an absent source, "
                "never an absent signal",
        },
        retention={
            "fr24_days": governance.retention_days("FR24"),
            "basis": "FlightRadar24 licence requires deletion within 30 days; "
                     "the analytical record outlives the payload with a nulled "
                     "pointer",
            "governance_version": governance.RULES_VERSION,
        },
        # 8.3. Stated, not buried. Vendor intermediation is not proof of
        # downstream rights, and an operator deciding what to forward is
        # entitled to know which questions are still open.
        open_rights_questions=governance.open_rights_questions(),
        cities=[_capability_for(name, _jurisdictions(db))
                for name in (city or [])],
        caveats=[
            "Confidence scores are a starting hypothesis, not a calibrated "
            "probability. Do not present them to a decision-maker as one.",
            "Sensitivity floors are interim and set from a single model's "
            "measurements.",
            "Coverage is US-only and keyed on airport mappings.",
        ],
    )


def _mission_block(loaded) -> dict[str, Any]:
    """What the loaded mission is, for a client that has to build a request.

    `digest` is the same value stamped on every receipt, so a client holding an
    alert can tell whether the definition has changed since it was written.
    """
    if loaded is None:
        return {
            "configured": False,
            "effect": "No mission is loaded. The database and the contract are "
                      "available; an iteration cannot run, because the tracks, "
                      "the lexicon, the prompts and the weights all come from "
                      "a mission pack and have no engine-side default.",
        }
    return {
        "configured": True,
        "id": loaded.identifier,
        "version": loaded.version,
        "digest": loaded.digest,
        "description": loaded.description,
        "tracks": list(loaded.tracks),
        "location_types": list(loaded.location_types),
    }


def _jurisdictions(db: SurgeDB) -> "geo.Equivalents":
    """The loaded mission's equivalence table, or none at all.

    None is the safe default: without a mission the engine makes no claim that
    two place names mean one place, so an apparent ambiguity is REFUSED rather
    than resolved — a recorded UNRESOLVED instead of a confident answer for the
    wrong place.
    """
    return getattr(getattr(db, "mission", None), "jurisdictions",
                   geo.NO_EQUIVALENTS)


def _capability_for(
    name: str, jurisdictions: "geo.Equivalents" = geo.NO_EQUIVALENTS
) -> schemas.CapabilityCity:
    """Whether one named jurisdiction is collectable, and what is missing."""
    bare, state = geo.split_state(name)
    canonical, method = geo.resolve_city(bare, jurisdictions)
    airports = geo.city_to_airports(bare) if canonical else []
    pickup = geo.city_to_pickup_location(bare) if canonical else None

    supported, unsupported, limits = ["SOCIAL"], [], []
    if not canonical:
        unsupported += ["FLIGHT", "CAR"]
        limits.append(
            f"{name!r} does not resolve to a known city ({method}); flight and "
            "car collection have no airport to key on. Social still works, so "
            "the jurisdiction is partially covered rather than unusable.")
    else:
        if airports:
            supported.append("FLIGHT")
        else:
            unsupported.append("FLIGHT")
            limits.append(f"No airport mapping for {canonical!r}.")
        if airports or pickup:
            supported.append("CAR")
        else:
            unsupported.append("CAR")
            limits.append(f"No rental pickup point for {canonical!r}.")
    # Lodging keys on a named facility, not on the city, so it depends on what
    # the session registers rather than on the geo tables.
    limits.append("LODGING depends on key_locations registered for the "
                  "session, and on Staying returning calendar data — measured "
                  "at roughly 1 listing in 40.")
    supported.append("LODGING")
    return schemas.CapabilityCity(
        name=name, state=state, resolved=bool(canonical),
        resolved_by=method, airports=airports, pickup_location=pickup,
        supported_sources=supported, unsupported_sources=unsupported,
        limitations=limits,
    )



# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


@router.get("/v1/sessions/{session_id}/queue", response_model=schemas.QueueOut,
            tags=["queue"], responses=errors(401, 404, 503))
def read_queue(
    session_id: int,
    iteration_id: int | None = None,
    limit: int = Query(200, ge=1, le=1000),
    db: SurgeDB = Depends(get_db),
    _: None = Depends(authenticated),
) -> schemas.QueueOut:
    """Queue state and every refusal, by outcome.

    The refusals are the point. A query that was deduped, cooled down, capped or
    priced out is a decision the system made, and an operator wondering why a
    city was not searched needs to see it rather than infer it from silence.
    """
    _session_or_404(db, session_id)
    decisions = (db.get_queue_decisions(iteration_id)
                 if iteration_id is not None else [])
    return schemas.QueueOut(
        session_id=session_id, iteration_id=iteration_id,
        status_counts=db.queue_status_counts(session_id,
                                             iteration_id=iteration_id),
        decision_counts=db.decision_counts(iteration_id)
        if iteration_id is not None else {},
        scheduled_ahead=len(db.pending_scheduled(session_id)),
        queries=[_row(row, "params_json") | {"params": _json(row["params_json"], {})}
                 for row in db.session_queue(session_id,
                                             iteration_id=iteration_id,
                                             limit=limit)],
        decisions=[_row(row) for row in decisions],
    )


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------
#
# On the operational router, deliberately. `api.debug_endpoints` is false for a
# deployment serving an operations team, and that is the deployment that has to
# be able to recover from a crash. Recovery is not a debugging aid; contrast
# discard-last-stage, which deletes analytical records and is rightly gated.


def _epoch_out(row) -> schemas.EpochOut | None:
    if row is None:
        return None
    return schemas.EpochOut(
        epoch_id=int(row["epoch_id"]), started_at=row["started_at"],
        host=row["host"], pid=int(row["pid"]),
        entry_point=row["entry_point"], ended_at=row["ended_at"],
        shutdown_kind=row["shutdown_kind"],
        stranded=_json(row["stranded_json"], []),
    )


def _interrupted_out(row) -> schemas.InterruptedOut:
    iteration_id = int(row["iteration_id"])
    return schemas.InterruptedOut(
        iteration_id=iteration_id, session_id=int(row["session_id"]),
        seq=int(row["seq"]),
        kind="INTERRUPTED" if row["interrupted_at"] else "OPEN",
        interrupted_at=row["interrupted_at"],
        interrupted_stage=row["interrupted_stage"],
        stage_pointer=row["stage"], started_at=row["started_at"],
        resume_url=f"/v1/iterations/{iteration_id}/resume",
        abandon_url=f"/v1/iterations/{iteration_id}/abandon",
        plan_url=f"/v1/iterations/{iteration_id}/recovery-plan",
    )


@router.get("/v1/recovery", response_model=schemas.RecoveryOut,
            tags=["recovery"], responses=errors(401, 503))
def read_recovery(
    request: Request,
    db: SurgeDB = Depends(get_db),
    _: None = Depends(authenticated),
) -> schemas.RecoveryOut:
    """What this process found when it started, and what still needs a decision.

    Every interrupted iteration is listed with the stage it died in and the two
    URLs that can close it. There is no third option and no force-unstick: both
    exits write a record.
    """
    epoch_id = getattr(request.app.state, "epoch_id", None)
    epoch = db.get_epoch(epoch_id) if epoch_id is not None else None
    if epoch is None:
        raise HTTPException(503, "No process epoch is open; the application "
                                 "did not complete startup.")
    report = getattr(request.app.state, "reconcile", None)
    return schemas.RecoveryOut(
        epoch=_epoch_out(epoch),
        previous_epoch=_epoch_out(db.previous_epoch(int(epoch_id))),
        interrupted=[_interrupted_out(r) for r in db.interrupted_iterations()],
        # Everything a new iteration would be refused for, crashed or not.
        # `interrupted` answers a different question — what this process found
        # dead at startup — and a merely-open iteration appears in neither the
        # reconcile report nor that list, which is how one could block a session
        # while being invisible to the endpoint that exists to unblock it.
        blocking=[_interrupted_out(r) for r in db.open_iterations()],
        refused_epochs=list(getattr(report, "refused_epochs", []) or []),
    )


def _plan_or_409(db: SurgeDB, config: Mapping[str, Any], iteration_id: int):
    """Shared precondition: the iteration exists and has not closed.

    Unfinished, not crash-stamped (8.7a). The narrower check refused a manual
    walk left half-done — which is now exactly the state that blocks a new
    iteration, so refusing to recover it would leave a session with no exit. A
    refusal with no remedy is worse than the hole it closes.
    """
    row = _iteration_or_404(db, iteration_id)
    service = RecoveryService(db, config)
    if not service.is_unfinished(iteration_id):
        raise HTTPException(
            409,
            f"Iteration {iteration_id} has already closed ({row['outcome']}). "
            "Recovery applies only to a run that has not finished."
        )
    return service


@router.get("/v1/iterations/{iteration_id}/recovery-plan",
            response_model=schemas.RecoveryPlanOut, tags=["recovery"],
            responses=errors(401, 404, 409, 503))
def read_recovery_plan(
    iteration_id: int,
    db: SurgeDB = Depends(get_db),
    config: dict = Depends(get_config),
    _: None = Depends(authenticated),
) -> schemas.RecoveryPlanOut:
    """What a resume would re-collect, and what it would leave alone.

    Read-only, and 409 rather than 200 when the iteration is not interrupted —
    a client that got a 200 here might act on it.
    """
    plan = _plan_or_409(db, config, iteration_id).plan(iteration_id)
    return schemas.RecoveryPlanOut(
        iteration_id=plan.iteration_id, session_id=plan.session_id,
        resume_from=plan.resume_from, derived_by=plan.derived_by,
        stage_pointer=plan.stage_pointer,
        interrupted_stage=plan.interrupted_stage, paid=plan.paid,
        queries_to_recollect=plan.queries_to_recollect,
        already_banked=plan.already_banked,
        already_spent=plan.already_spent,
        estimated_units_upper_bound=plan.estimated_units_upper_bound,
    )


@router.post("/v1/iterations/{iteration_id}/resume", status_code=202,
             response_model=schemas.IterationAccepted, tags=["recovery"],
             responses={
                 200: {"description": "Finished within ?wait=true's timeout.",
                       "model": schemas.IterationAccepted},
                 202: {"description": "Accepted; poll `poll_url`."},
                 # No Retry-After here: resume's 409s are "not interrupted"
                 # and "this would re-collect, confirm it" — neither clears by
                 # waiting, and declaring the header would tell a generated
                 # client to back off forever.
                 **errors(401, 404, 409, 422, 503),
             })
def resume_iteration(
    iteration_id: int,
    response: Response,
    body: schemas.ResumeIn | None = None,
    wait: bool = Query(False),
    db: SurgeDB = Depends(get_db),
    config: dict = Depends(get_config),
    runner: IterationRunner = Depends(get_runner),
    _: None = Depends(authenticated),
) -> schemas.IterationAccepted:
    """Continue an interrupted iteration from where it stopped.

    The interrupted stage is re-run rather than skipped — it was incomplete —
    and that is safe because every agent is re-entrant: collection claims only
    PENDING rows, triage skips URLs it already ruled on, alerting skips
    correlations that already have an alert, and correlation upserts.

    Refuses without `confirm_respend` when the plan would collect again. FR24
    bills per record returned and one historical query was measured at 60
    credits, so this is a purchase and the caller has to say so.
    """
    service = _plan_or_409(db, config, iteration_id)
    payload = body or schemas.ResumeIn()
    plan = service.plan(iteration_id)

    from_stage = payload.from_stage or plan.resume_from
    if from_stage not in PIPELINE_STAGES:
        raise HTTPException(422, f"{from_stage!r} is not a pipeline stage")
    if PIPELINE_STAGES.index(from_stage) > PIPELINE_STAGES.index(
            plan.resume_from):
        raise HTTPException(
            409,
            f"from_stage={from_stage} is later than the derived resume point "
            f"{plan.resume_from}. Skipping a stage that never ran would leave "
            "its output permanently missing; use discard-last-stage to go "
            "further back instead."
        )
    if plan.paid and not payload.confirm_respend:
        raise HTTPException(
            409,
            f"Resuming would collect {len(plan.queries_to_recollect)} "
            f"query(ies) again at the vendors. "
            f"{sum(plan.already_spent.values()):.0f} unit(s) already spent are "
            "not reclaimed. Pass confirm_respend=true to proceed."
        )

    session_id = plan.session_id
    try:
        service.prepare_resume(iteration_id, runner.epoch_id or 0)
        _iteration_id, future = runner.submit_resume(
            session_id, iteration_id, from_stage
        )
    except SessionBusy as exc:
        raise HTTPException(409, str(exc)) from exc

    outcome = None
    if wait:
        from concurrent.futures import TimeoutError as FTimeout
        try:
            outcome = future.result(
                timeout=float((config.get("api") or {}).get(
                    "sync_timeout_s", 600)))
        except FTimeout:
            outcome = None
    if outcome is not None:
        response.status_code = 200

    row = db.get_iteration(iteration_id)
    return schemas.IterationAccepted(
        iteration_id=iteration_id, session_id=session_id,
        status="FINISHED" if outcome is not None else "RUNNING",
        stage=row["stage"], poll_url=f"/v1/iterations/{iteration_id}",
        next_stage=row["stage"] if row["stage"] in PIPELINE_STAGES else None,
        budget_plan=_json(row["budget_plan_json"], {}),
    )


@router.post("/v1/iterations/{iteration_id}/abandon",
             response_model=schemas.AbandonOut, tags=["recovery"],
             responses=errors(401, 404, 409, 503))
def abandon_iteration(
    iteration_id: int,
    body: schemas.AbandonIn,
    db: SurgeDB = Depends(get_db),
    config: dict = Depends(get_config),
    runner: IterationRunner = Depends(get_runner),
    _: None = Depends(authenticated),
) -> schemas.AbandonOut:
    """Close an interrupted iteration, counting the lost collection honestly.

    The stranded queries become `INTERRUPTED`, which is in
    `UNRELIABLE_QUERY_STATUSES` — so they lower `data_completeness`, cap the
    band below HIGH, and get named in the alert caveat, with no change to the
    scoring code.

    Then it correlates and alerts on what *was* collected. Skipping that would
    close the iteration with no alert at all for a city whose evidence may be
    nearly complete, which is a real cluster reading as silence. It never
    schedules follow-ons: work queued by an iteration nobody finished would
    arrive as a surprise next run.
    """
    service = _plan_or_409(db, config, iteration_id)
    if not body.confirm:
        raise HTTPException(
            409,
            f"Abandoning iteration {iteration_id} marks its outstanding "
            "queries as permanent coverage gaps and closes it. Pass "
            "confirm=true to proceed."
        )
    session_id = int(db.get_iteration(iteration_id)["session_id"])
    before = db.iteration_counts(iteration_id)

    result = service.abandon(iteration_id, body.reason,
                             epoch_id=runner.epoch_id)
    outcome = None
    if body.finalise:
        try:
            _id, future = runner.submit_finalise(session_id, iteration_id)
            outcome = future.result()
        except SessionBusy as exc:
            raise HTTPException(409, str(exc)) from exc
    else:
        db.append_degradation(
            iteration_id, f"abandoned without scoring: {body.reason}",
            source=SurgeDB.DEGRADATION_RECOVERY)
        db.finish_iteration(iteration_id, outcome="PARTIAL")
        outcome = "PARTIAL"

    after = db.iteration_counts(iteration_id)
    return schemas.AbandonOut(
        iteration_id=iteration_id, outcome=outcome,
        queries_marked_interrupted=result["queries_marked_interrupted"],
        coverage_gaps=result["coverage_gaps"],
        correlations_written=after["correlations"] - before["correlations"],
        alerts_written=after["alerts"] - before["alerts"],
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/v1/healthz", response_model=schemas.HealthOut, tags=["ops"],
            responses=errors(401))
def healthz(
    request: Request,
    deep: bool = Query(False, description="Also probe the four vendors; "
                                          "requires authentication"),
    db: SurgeDB = Depends(get_db),
    config: dict = Depends(get_config),
    runner: IterationRunner = Depends(get_runner),
) -> schemas.HealthOut:
    """Liveness. Unauthenticated by design; vendor probing is not.

    `deep=true` calls every connector's health check, which costs rate-limit
    budget at four providers. Leaving that reachable without a token would make
    an unauthenticated port a free way to exhaust the collection capacity this
    system depends on, so the deep check requires the same bearer token as
    everything else.
    """
    from ..db.database import SCHEMA_VERSION

    status = "ok"
    try:
        db.scalar("SELECT 1")
        database = "ok"
    except Exception as exc:  # noqa: BLE001 — the whole point of a health check
        database, status = f"error: {exc}", "degraded"

    connectors = None
    if deep:
        require_token(request)
        from ..connectors.registry import health_report
        connectors = health_report(request.app.state.connectors)
        # `healthy` is tri-state on purpose: True, False, or None for a
        # connector with no free endpoint to prove a credential with. Only an
        # explicit False is a degradation — treating "unknown" as unhealthy
        # would make the check cry wolf on a system that is working.
        if any(report.get("healthy") is False for report in connectors.values()):
            status = "degraded"

    budget: dict[str, dict[str, float]] = {}
    guard = getattr(request.app.state, "budget", None)
    if guard is not None:
        for provider in sorted(enums.PROVIDERS):
            budget[provider] = guard.remaining(provider)

    retention = {}
    try:
        retention = RetentionService(db, config).pending_report()
    except Exception:  # noqa: BLE001 — advisory
        retention = {}

    # Opinionated, and defensible under "a failure must never look like an
    # absence of threat": a half-collected city sitting unrecovered for a week
    # should not report ok.
    interrupted = [int(r["iteration_id"]) for r in db.interrupted_iterations()]
    if interrupted:
        status = "degraded"

    return schemas.HealthOut(
        status=status, database=database, schema_version=SCHEMA_VERSION,
        epoch_id=getattr(request.app.state, "epoch_id", None),
        interrupted_iterations=interrupted,
        dry_run=bool(config.get("dry_run")),
        debug_endpoints=bool((config.get("api") or {}).get("debug_endpoints",
                                                           True)),
        active_sessions=int(db.scalar(
            "SELECT COUNT(*) FROM sessions WHERE status = 'ACTIVE'")),
        running_iterations=runner.running(),
        budget=budget, connectors=connectors, retention=retention,
    )


# ===========================================================================
# Debug: step, verify, discard
# ===========================================================================


def _stage_out(report: StageReport) -> schemas.StageOut:
    return schemas.StageOut(
        stage=report.stage, status=report.status,
        started_at=report.started_at, finished_at=report.finished_at,
        error_message=report.error_message, wrote=report.wrote,
        decisions=report.decisions, agents=report.agents,
        api_calls=report.api_calls, log=report.log,
        correlations=report.correlations, skips=report.skips,
    )


@debug_router.post("/{iteration_id}/step", response_model=schemas.StepOut,
                   responses=errors(401, 404, 409, 422, 503))
def step_iteration(
    iteration_id: int,
    request: Request,
    body: schemas.StepIn | None = None,
    db: SurgeDB = Depends(get_db),
    runner: IterationRunner = Depends(get_runner),
    _: None = Depends(authenticated),
) -> schemas.StepOut:
    """Run exactly one stage and return what it did.

    The step response carries the stage's own report, so stepping and verifying
    are one round trip rather than two — when a stage does something surprising
    you want the counts in front of you, not a second URL to visit.
    """
    row = _iteration_or_404(db, iteration_id)
    session_id = int(row["session_id"])
    expect = (body or schemas.StepIn()).expect
    if expect is not None and expect not in PIPELINE_STAGES:
        raise HTTPException(422, f"{expect!r} is not a pipeline stage")

    orchestrator = request.app.state.build_orchestrator()
    try:
        with runner.claim(session_id):
            result = orchestrator.step(iteration_id, expect=expect)
    except SessionBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except StageAlreadyRun as exc:
        raise HTTPException(409, str(exc)) from exc

    report = StageInspector(db).report(iteration_id, result.stage)
    return schemas.StepOut(
        iteration_id=iteration_id, stage=result.stage, ok=result.ok,
        next_stage=result.next_stage, outcome=result.outcome,
        report=_stage_out(report),
    )


@debug_router.get("/{iteration_id}/stages", response_model=schemas.StagesOut,
                  responses=errors(401, 404, 503))
def read_stages(
    iteration_id: int,
    db: SurgeDB = Depends(get_db),
    _: None = Depends(authenticated),
) -> schemas.StagesOut:
    """What happened in every stage, including the ones that never ran.

    A stage the iteration never reached is as much a part of what happened as
    one that did, so all eight are always present with an explicit status.
    """
    row = _iteration_or_404(db, iteration_id)
    return schemas.StagesOut(
        iteration_id=iteration_id, stage=row["stage"], outcome=row["outcome"],
        next_stage=row["stage"] if row["stage"] in PIPELINE_STAGES else None,
        stages=[_stage_out(r) for r in StageInspector(db).report_all(iteration_id)],
    )


@debug_router.get("/{iteration_id}/stages/{stage}",
                  response_model=schemas.StageOut,
                  responses=errors(401, 404, 422, 503))
def read_stage(
    iteration_id: int,
    stage: str,
    db: SurgeDB = Depends(get_db),
    _: None = Depends(authenticated),
) -> schemas.StageOut:
    """One stage in detail: what it wrote, who ran, what it spent, its log."""
    _iteration_or_404(db, iteration_id)
    if stage not in PIPELINE_STAGES:
        raise HTTPException(422, f"{stage!r} is not a pipeline stage")
    return _stage_out(StageInspector(db).report(iteration_id, stage))


@debug_router.post("/{iteration_id}/discard-last-stage",
                   response_model=schemas.DiscardOut,
                   responses=errors(401, 404, 409, 422, 503))
def discard_last_stage(
    iteration_id: int,
    body: schemas.DiscardIn | None = None,
    db: SurgeDB = Depends(get_db),
    runner: IterationRunner = Depends(get_runner),
    _: None = Depends(authenticated),
) -> schemas.DiscardOut:
    """Remove the last stage's output and point the iteration back at it.

    Only the last stage, and one at a time — discarding TRIAGING while TIPPING's
    queries still reference its signals would break the guarantee that every
    query traces to the post that caused it. Repeated calls walk backwards, so
    any point is still reachable.

    `api_calls` and `agent_log` are never deleted. The money is a fact about the
    world rather than an output of the stage, and the audit trail has to record
    the one destructive operation in the system rather than be erased by it.
    """
    row = _iteration_or_404(db, iteration_id)
    session_id = int(row["session_id"])
    payload = body or schemas.DiscardIn()
    try:
        with runner.claim(session_id):
            report = StageRollback(db).discard_last(
                iteration_id, expect=payload.expect, confirm=payload.confirm,
            )
    except SessionBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        # 409 rather than 403: the request is permitted, the state is not ready
        # for it. The caller re-sends with confirm=true.
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    return schemas.DiscardOut(
        iteration_id=iteration_id, stage=report.stage, deleted=report.deleted,
        queries_reset=report.queries_reset, units_spent=report.units_spent,
        not_reverted=report.not_reverted,
        degradations_retracted=report.degradations_retracted,
        next_stage=report.stage_now,
    )


def debug_disabled_response() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"detail": "Debug endpoints are disabled "
                           "(api.debug_endpoints = false)."},
    )
