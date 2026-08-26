#!/usr/bin/env python3
"""Generate the API contract artifacts from the running application.

    python scripts/build_api_contract.py            # write docs/api/
    python scripts/build_api_contract.py --check    # fail if they are stale

Everything under `docs/api/` is derived, never hand-edited. The specification
comes from the FastAPI app itself, and the examples are real HTTP exchanges
captured by driving that app through a full eight-stage iteration. Nothing here
is written by hand, so nothing here can quietly disagree with the code.

**The run is fully offline.** Connectors come from `dry_run` mode, which serves
the recorded fixtures under `tests/fixtures/` through the real connector classes
and parsers, and the model client is a fixed stub (see `StubModel`). No vendor is
contacted and no credential is read.

**The output is byte-stable.** The clock is frozen for the capture, so
re-running this produces an identical tree unless the contract actually changed —
which is what makes `--check` a usable gate and what keeps a diff meaningful.
Two things a frozen clock cannot fix are normalised afterwards: measured
latencies, and the credit balance the Staying fixture reports.

Artifacts:

    openapi.json / openapi.yaml   The full surface, debug endpoints included.
    openapi-operational.json      What a deployment with api.debug_endpoints
                                  false actually serves. The difference is part
                                  of the contract: a client generated from the
                                  full spec would call three paths that 404.
    API.md                        Human reference, generated from the spec.
    examples/*.json               Real captured request/response exchanges.
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import yaml                                                       # noqa: E402
from fastapi.testclient import TestClient                         # noqa: E402

from surge_iw.api.app import create_app                           # noqa: E402
from surge_iw.config import load_config                           # noqa: E402
from surge_iw.db.database import SurgeDB                          # noqa: E402

DEFAULT_OUT = REPO / "docs" / "api"
TOKEN = "example-token"                     # not a credential; a literal
AUTH = {"Authorization": f"Bearer {TOKEN}"}

#: The instant the capture pretends it is. Fixed so every generated timestamp —
#: and every booking window derived from one — is reproducible.
FROZEN = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)

#: Values that vary even under a frozen clock, and what to replace them with.
#: `pid` and `host` are runtime facts of whatever machine generated the
#: capture; leaving them in would make the committed artifacts fail `--check`
#: on every other machine and on every re-run, which is how a drift gate gets
#: switched off.
VOLATILE: dict[str, Any] = {
    "latency_ms": 0,
    "elapsed_ms": 0,
    "pid": 10000,
    "host": "example-host",
    # 9.3 / issue #12. A receipt stamps the git HEAD it was written under, so
    # a captured receipt went stale on EVERY commit rather than on an API
    # change. The gate then failed for a reason that had nothing to do with
    # the contract, which trains an operator to regenerate reflexively — and
    # that is how a real drift gets waved through.
    #
    # The all-zero sha is git's own idiom for "no revision", so the example
    # keeps the field's shape while stating plainly that it is not a snapshot
    # of one machine. The receipt schema still documents the field, and a live
    # receipt still carries the real value; what is excluded is only the
    # example, which is a shape rather than a measurement.
    "code_revision": "0000000",
}


# ---------------------------------------------------------------------------
# A model client that is not a model
# ---------------------------------------------------------------------------


class _TruncatingClient:
    """A model whose every reply overruns `llm.max_tokens` (8.8).

    Used for one iteration so example 40 documents a real recovery rather than
    a manufactured one: the parent genuinely loses its whole batch to
    truncation, exactly as the broad-leg run did at batch_size 10.
    """

    def __init__(self) -> None:
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        class _Msg:
            content = '[{"item_id": "trun'          # cut off mid-answer

        class _Choice:
            message = _Msg()
            finish_reason = "length"

        class _Resp:
            choices = [_Choice()]
            usage = None
        return _Resp()


class StubModel:
    """Deterministic stand-in for the OpenAI-compatible client.

    It reads the posts it is actually given and returns a fixed verdict for each,
    rather than matching invented URLs — otherwise the example iteration judges
    nothing, writes no signal, and the captured alert list is empty, which
    documents the contract's least interesting path.

    **It is not a model and its output is not a sample of one.** Every post is
    accepted at the same salience and the summary is one fixed sentence. Read
    the captured examples as structure; the prose a real model writes will
    differ and is meant to.

    It lives here rather than in the connector registry because `dry_run`
    currently swaps only the four HTTP connectors; the model client is still
    live under it. See the README's carried-forward list.
    """

    _SUMMARY = (
        "Multiple sources report crews and equipment staging at the Riverside "
        "Fairground in Phoenix, and short-term-rental availability within "
        "15 km has fallen sharply against the two-week baseline."
    )

    def __init__(self) -> None:
        self.chat = self                       # client.chat.completions.create
        self.completions = self

    def create(self, *, messages: list[dict[str, str]], **_: Any) -> Any:
        system, prompt = messages[0]["content"], messages[-1]["content"]
        if "summary" in system:
            return _Completion(json.dumps({"summary": self._SUMMARY}))
        return _Completion(json.dumps(
            [self._verdict(item) for item in _posts_in(prompt)]))

    @staticmethod
    def _verdict(post: Mapping[str, Any]) -> dict[str, Any]:
        """A compliant answer: the item_id echoed, and nothing extra.

        Echoing the id is the whole contract. A stub that returned the URL
        instead — as this one did before Phase 7 — is now correctly recorded as
        UNDECIDED for every post, which is honest but documents an empty system.
        """
        return {
            "item_id": post.get("item_id", ""),
            "relevant": True,
            "track": "AIRSHOW",
            "cities": ["Phoenix"],
            "locations": ["Riverside Fairground"],
            "activity_type": "static display",
            "imminence_hours": 6.0,
            "salience": 0.9,
            "rationale": "Stub verdict: every post is accepted for Phoenix.",
        }


def _posts_in(prompt: str) -> list[dict[str, Any]]:
    """Recover the JSON array TriageAgent embedded in its prompt."""
    start = prompt.find("[")
    if start < 0:
        return []
    try:
        # raw_decode: the array may be followed by the operator-calendar
        # context block, which is prompt text rather than payload.
        items = json.JSONDecoder().raw_decode(prompt, start)[0]
    except ValueError:
        return []
    return [item for item in items if isinstance(item, dict)]


class _Completion:
    def __init__(self, content: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {
            "content": content})()})()]
        self.usage = type("U", (), {"prompt_tokens": 0, "completion_tokens": 0})()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class Clock:
    """A settable stand-in for `utcnow`, so the capture can also move time.

    Movable rather than merely fixed because the cooldown guard is keyed on the
    dedup hash across *all* iterations: a second iteration captured at the same
    instant as the first refuses every query it would otherwise enqueue, fails
    seeding, and documents a path no operator will ever take deliberately.
    """

    def __init__(self, instant: datetime) -> None:
        self.now = instant

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


def freeze_clock(clock: Clock) -> list[tuple[Any, str, Any]]:
    """Pin every `utcnow` in the package. Returns undo entries.

    Five modules do `from ..db.database import utcnow`, which binds the function
    at import time — patching the definition alone would miss all of them, and
    the booking windows they compute are exactly the values that would otherwise
    churn on every run.
    """
    import surge_iw.db.database as database

    # Held before the loop. Patching `database` first and then comparing later
    # modules against `database.utcnow` compares them against the replacement,
    # so every module after the first fails the test and keeps the real clock —
    # which is exactly the bug --check caught the first time this ran.
    original = database.utcnow

    undone: list[tuple[Any, str, Any]] = []
    for module in list(sys.modules.values()):
        if not getattr(module, "__name__", "").startswith("surge_iw"):
            continue
        if getattr(module, "utcnow", None) is original:
            undone.append((module, "utcnow", original))
            module.utcnow = clock
    if not any(module is database for module, _, _ in undone):
        undone.append((database, "utcnow", original))
        database.utcnow = clock
    return undone


def normalise(value: Any) -> Any:
    """Replace what a frozen clock cannot make reproducible."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "code_revision":
                # Unconditionally, INCLUDING when it is None. `code_revision()`
                # reads git HEAD, so it is None in any checkout that is not a
                # repository — a tarball, a Docker build context, a vendored
                # copy. Leaving the null through made the drift gate fail on
                # exactly those installs, for a reason with nothing to do with
                # the contract, which is how an operator learns to ignore it.
                out[key] = VOLATILE[key]
            elif key in VOLATILE:
                out[key] = VOLATILE[key] if item is not None else None
            elif key == "credits":
                out[key] = "<varies with the account>"
            else:
                out[key] = normalise(item)
        return out
    if isinstance(value, list):
        return [normalise(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


class Capture:
    """Records real exchanges against the app, in the order they were made."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.exchanges: list[dict[str, Any]] = []

    def call(
        self, name: str, method: str, path: str, *,
        body: Any = None, headers: Mapping[str, str] | None = None,
        note: str = "", keep: bool = True,
    ) -> Any:
        response = self.client.request(
            method, path, json=body,
            headers=dict(headers if headers is not None else AUTH),
        )
        try:
            payload = response.json()
        except ValueError:
            payload = None
        contract_headers = {
            name_: response.headers[name_]
            for name_ in ("Retry-After", "Idempotent-Replay", "Idempotency-Key")
            if name_ in response.headers
        }
        if keep:
            self.exchanges.append({
                "name": name,
                "note": note,
                "request": {
                    "method": method.upper(),
                    "path": path,
                    "headers": {"Authorization": "Bearer <SURGE_API_TOKEN>"}
                    if headers is None else dict(headers),
                    "body": body,
                },
                "response": {
                    "status": response.status_code,
                    # Only the headers that carry contract meaning. Capturing
                    # all of them would add Date and Content-Length, which
                    # differ every run and would make the artifact churn — and
                    # a --check gate that churns is a gate people switch off.
                    **({"headers": contract_headers}
                       if contract_headers else {}),
                    "body": normalise(payload),
                },
            })
        return payload


SESSION_BODY = {
    "label": "AZ display season",
    "expand_cities": True,
    "tracks": ["AIRSHOW", "CONCERT_TOUR"],
    "cities": [{
        "name": "Phoenix", "state": "AZ",
        "key_locations": [{
            "name": "Riverside Fairground",
            "address": "510 S 3rd Ave, Phoenix, AZ",
            "location_type": "FAIRGROUND",
        }],
    }],
}


def scenario(capture: Capture, clock: Clock) -> None:
    """One session, one automatic iteration, one stepped-and-discarded one.

    Ordered to exercise every endpoint at least once against real state, so no
    captured example is a hypothetical.
    """
    session = capture.call(
        "01-create-session", "POST", "/v1/sessions", body=SESSION_BODY,
        note="Airports and pickup points resolve here, so a mapping gap is a "
             "warning at init rather than a skipped query hours later.",
    )
    sid = session["session_id"]

    capture.call("01b-session-from-input-set", "POST", "/v1/sessions",
                 body={"label": "loaded from a file",
                       "input_set": "example",
                       "tracks": ["AIRSHOW"]},
                 note="The geography from `inputs/<name>.yaml` instead of "
                      "inline. `input_set` is a NAME resolved inside "
                      "`inputs.dir`, never a path. All-or-nothing: a city the "
                      "geo table cannot place is refused by name with a 422, "
                      "because a session quietly missing a jurisdiction would "
                      "report a true absence of evidence about a place nobody "
                      "looked at. The response echoes what actually resolved.")
    capture.call("01c-input-set-path-refused", "POST", "/v1/sessions",
                 body={"input_set": "../../etc/passwd"},
                 note="Authenticated is not the same as trusted with the "
                      "filesystem. A path here would be a file-disclosure "
                      "primitive whatever the intent behind it.")

    capture.call("01d-session-with-tunables", "POST", "/v1/sessions",
                 body={"label": "narrower window, tighter cap",
                       "cities": SESSION_BODY["cities"],
                       "tracks": ["AIRSHOW"],
                       "tunables": {
                           "correlation": {"window_hours": 48},
                           "triage": {"require_nexus": True},
                           "budget": {"per_iteration_cap": {"FR24": 500.0}}}},
                 note="Per-session overrides, nested as in config.yaml. The "
                      "response echoes them and returns `config_hash` — the "
                      "same value every receipt this session produces carries, "
                      "so a client can confirm after the fact that a judgement "
                      "was made under the settings it asked for.")
    capture.call("01e-tunable-refused", "POST", "/v1/sessions",
                 body={"cities": SESSION_BODY["cities"],
                       "tunables": {"staying": {"retention_days": 3650}}},
                 note="Refused by name. Credentials, provider endpoints, "
                      "retention ceilings and deployment controls are "
                      "server-owned; so is an unknown field. A tunable that "
                      "was accepted and then ignored is the defect this "
                      "replaced — the client would have been told its request "
                      "succeeded while collection ran on other settings.")
    capture.call("01f-tunable-cap-cannot-be-raised", "POST", "/v1/sessions",
                 body={"cities": SESSION_BODY["cities"],
                       "tunables": {"budget":
                                    {"per_iteration_cap": {"FR24": 99999.0}}}},
                 note="A session may lower a spending cap and never raise "
                      "one. The budget being protected is the operator's.")

    capture.call("01g-session-with-calendar", "POST", "/v1/sessions",
                 body={"label": "with a calendar of scheduled events",
                       "cities": SESSION_BODY["cities"],
                       "tracks": ["AIRSHOW"],
                       "calendar_set": "example-calendar"},
                 note="`calendar_set` names a calendar of scheduled events in "
                      "the same `inputs.dir` — context, never input: triage "
                      "shows the events to the model as background and "
                      "correlations record the ones overlapping their window, "
                      "but no score or band ever moves. Same NAME-not-path "
                      "rule and the same all-or-nothing loading as "
                      "`input_set`.")

    capture.call("02-unauthenticated", "GET", f"/v1/sessions/{sid}",
                 headers={}, note="Every route except /v1/healthz needs the "
                                  "bearer token.")
    capture.call("03-read-session", "GET", f"/v1/sessions/{sid}")

    capture.call("03b-append-calendar", "POST", f"/v1/sessions/{sid}/calendar",
                 body={"calendar_set": "example-calendar"},
                 note="Between iterations, never during one — each "
                      "iteration's triage context is fixed at its start "
                      "(`added_at <= started_at`), so events appended now "
                      "join the NEXT run. Append-only: re-loading a grown "
                      "file is the normal way to add, and events already "
                      "present come back as warnings rather than errors.")
    capture.call("03c-read-calendar", "GET", f"/v1/sessions/{sid}/calendar",
                 note="Oldest addition first. `added_at` is what decides "
                      "which iterations saw an event.")

    iteration = capture.call(
        "04-trigger-iteration", "POST",
        f"/v1/sessions/{sid}/iterations?wait=true",
        note="Without ?wait=true this returns 202 immediately and the run "
             "continues on a worker; poll_url is the same either way.",
    )
    iid = iteration["iteration_id"]

    # The guard reads the runner's active-iteration bookkeeping, which only a
    # worker mid-run holds. A capture cannot deterministically pause a real
    # worker thread here, so it stages exactly the entry the worker keeps for
    # the duration of one call — the same in-process arranging the crash
    # examples below use.
    runner = capture.client.app.state.runner
    runner._active[sid] = iid
    try:
        capture.call("04b-append-calendar-blocked", "POST",
                     f"/v1/sessions/{sid}/calendar",
                     body={"calendar_set": "example-calendar"},
                     note="409 while an iteration runs, for the same reason "
                          "as add-cities: each iteration's triage context is "
                          "fixed at its start, and a mid-run append would "
                          "make two batches of one run see different "
                          "calendars. No Retry-After — appending is not "
                          "urgent, and the remedy is simply 'between "
                          "iterations'.")
    finally:
        runner._active.pop(sid, None)

    capture.call("05-poll-iteration", "GET", f"/v1/iterations/{iid}",
                 note="The poll target. `degradations` is what the run could "
                      "not do; an empty list and a missing field differ.")
    capture.call("06-alerts", "GET", f"/v1/sessions/{sid}/alerts",
                 note="Most severe first, then most recent.")
    capture.call("07-alerts-tuple", "GET",
                 f"/v1/sessions/{sid}/alerts?format=tuple",
                 note="`evidence` is a fixed four-element array: social posts, "
                      "flights, lodging, rental cars.")

    alerts = capture.call("_", "GET", f"/v1/sessions/{sid}/alerts", keep=False)
    if alerts:
        capture.call("08-evidence", "GET",
                     f"/v1/alerts/{alerts[0]['alert_id']}/evidence",
                     note="Every contributing signal back to its raw payload "
                          "and the queue row that fetched it, with the "
                          "arithmetic that produced the score. Assembled from "
                          "the CORRELATION — this route resolves it and "
                          "delegates — so 40 returns the identical shape for a "
                          "correlation that produced no alert.")

    capture.call("08b-correlations", "GET",
                 f"/v1/iterations/{iid}/correlations",
                 note="Everything the iteration scored, alerting or not. "
                      "`alert_decision` says what ALERTING concluded about "
                      "each and why, recorded by the agent that decided rather "
                      "than left for a reader to infer from `score` and a "
                      "config value.")

    capture.call("09-queue", "GET",
                 f"/v1/sessions/{sid}/queue?iteration_id={iid}&limit=5",
                 note="Refusals are the point: a query that was deduped, "
                      "cooled down, capped or priced out is a decision.")
    # Past the 180-minute cooldown, so the second iteration has work to do.
    # Without this it enqueues nothing, fails seeding, and every debug example
    # below documents an empty iteration.
    clock.advance(hours=4)

    manual = capture.call(
        "10-create-manual-iteration", "POST", f"/v1/sessions/{sid}/iterations",
        body={"mode": "manual"},
        note="mode=manual creates the iteration without running it, for "
             "stepping. Note the four-hour gap from the first iteration: the "
             "cooldown guard is keyed on the query hash across all iterations.",
    )
    mid = manual["iteration_id"]
    capture.call("11-step", "POST", f"/v1/iterations/{mid}/step",
                 body={"expect": "SEEDING"},
                 note="One stage, with the report of what it did. `expect` is "
                      "a guard, not a selector.")
    capture.call("12-step-wrong-stage", "POST", f"/v1/iterations/{mid}/step",
                 body={"expect": "ALERTING"},
                 note="The guard refusing: a client that lost track of the "
                      "pointer must not run the wrong stage and spend money "
                      "doing it.")
    for _ in range(6):                      # through ALERTING
        capture.call("_", "POST", f"/v1/iterations/{mid}/step", keep=False)

    capture.call("13-stages", "GET", f"/v1/iterations/{mid}/stages",
                 note="All eight, including SCHEDULING which has not run.")
    capture.call("14-stage", "GET", f"/v1/iterations/{mid}/stages/TRIAGING",
                 note="One stage in detail: what it wrote, which agents ran, "
                      "what it spent, and its log.")
    capture.call("15-discard-last-stage", "POST",
                 f"/v1/iterations/{mid}/discard-last-stage",
                 note="Discards ALERTING. Only the last stage, one at a time; "
                      "repeated calls walk backwards.")
    capture.call("_", "POST", f"/v1/iterations/{mid}/discard-last-stage",
                 keep=False)                # CORRELATING
    capture.call("16-discard-needs-confirm", "POST",
                 f"/v1/iterations/{mid}/discard-last-stage",
                 note="Refused: discarding a collection stage means buying the "
                      "same data again at the vendor, so it takes an explicit "
                      "acknowledgement and reports what was already spent.")
    capture.call("17-discard-confirmed", "POST",
                 f"/v1/iterations/{mid}/discard-last-stage",
                 body={"confirm": True},
                 note="The same call, acknowledged. `queries_reset` are back to "
                      "PENDING and will be collected again; `units_spent` is "
                      "not reclaimed.")

    capture.call("18-add-cities", "POST", f"/v1/sessions/{sid}/cities",
                 body={"cities": [{"name": "Tucson", "state": "AZ",
                                   "key_locations": [
                                       {"name": "Lakeside Arena"}]}]},
                 note="Between iterations, never during one.")
    capture.call("19-healthz", "GET", "/v1/healthz", headers={},
                 note="Unauthenticated liveness. ?deep=true probes the four "
                      "vendors and does require the token.")
    capture.call("20-not-found", "GET", "/v1/sessions/999",
                 note="The application's error shape.")
    capture.call("21-validation-error", "POST", "/v1/sessions",
                 body={"cities": []},
                 note="Pydantic's own 422 shape, which is richer than the "
                      "application's {\"detail\": \"...\"} and distinct from it.")

    # ------------------------------------------------------------------
    # A real crash, and the recovery of it
    # ------------------------------------------------------------------
    # Simulated in-process: leave an iteration exactly as a killed process
    # would, then open a new epoch. That is the whole crash — no subprocess and
    # no signal — which is what lets these stay REAL captures rather than
    # hand-written hypotheticals.
    clock.advance(hours=4)
    crashed = _strand_an_iteration(capture.client, clock)

    capture.call("22-recovery", "GET", "/v1/recovery",
                 note="What this process found when it started. Each entry "
                      "carries the two URLs that can close it; there is no "
                      "third option and no force-unstick.")
    capture.call("23-interrupted-iteration", "GET", f"/v1/iterations/{crashed}",
                 note="status INTERRUPTED is what lets a watching client stop. "
                      "Before it, an interrupted run returned running:false, "
                      "outcome:null forever.")
    capture.call("24-recovery-plan", "GET",
                 f"/v1/iterations/{crashed}/recovery-plan",
                 note="Read-only. `estimated_units_upper_bound` is a bound, "
                      "not a price — FR24 bills per record returned, so judge "
                      "from queries_to_recollect.")
    capture.call("25-resume-needs-confirm", "POST",
                 f"/v1/iterations/{crashed}/resume", body={},
                 note="Refused: resuming would collect again at the vendors "
                      "and nothing already spent is reclaimed.")
    capture.call("26-trigger-blocked", "POST",
                 f"/v1/sessions/{session_of(capture.client, crashed)}/iterations",
                 note="409 while an interrupted iteration is outstanding. The "
                      "cooldown is keyed across ALL iterations, so a new run "
                      "would be silently under-collected. The message names "
                      "which iteration blocks, which KIND of open it is, and "
                      "the two URLs that close it — see 37 for the other kind.")
    capture.call("27-abandon", "POST", f"/v1/iterations/{crashed}/abandon",
                 body={"reason": "operator closed it after a crash",
                       "confirm": True},
                 note="The lost collection becomes a counted coverage gap, and "
                      "the iteration is still scored and alerted from what WAS "
                      "collected — closing it unscored would be a real cluster "
                      "reading as silence.")


    # ------------------------------------------------------------------
    # Contract hardening (8.2)
    # ------------------------------------------------------------------
    capture.call("28-capabilities", "GET",
                 "/v1/capabilities?city=Phoenix%2C+AZ&city=Nowheresville%2C+ZZ",
                 note="What the deployment can and cannot collect, BEFORE a "
                      "session exists. Nowheresville reports as unsupported "
                      "rather than quietly returning nothing, which is the "
                      "distinction the endpoint exists to make.")

    # A fresh session, because the one above deliberately still carries an
    # abandoned crash. These legs are about the healthy path.
    clock.advance(hours=1)
    healthy = capture.call("29-second-session", "POST", "/v1/sessions",
                           body=SESSION_BODY, keep=False)
    sid2 = healthy["session_id"]

    capture.call("30-idempotent-trigger", "POST",
                 f"/v1/sessions/{sid2}/iterations", body={"mode": "manual"},
                 headers={**AUTH, "Idempotency-Key": "contract-demo-key-01"},
                 note="This endpoint spends money, so a lost response must be "
                      "safe to retry. The key opts in.")
    capture.call("31-idempotent-replay", "POST",
                 f"/v1/sessions/{sid2}/iterations", body={"mode": "manual"},
                 headers={**AUTH, "Idempotency-Key": "contract-demo-key-01"},
                 note="The same key returns the FIRST response and starts "
                      "nothing. Identical body, plus Idempotent-Replay: true "
                      "so a caller can tell the difference.")
    capture.call("32-idempotency-key-reused", "POST",
                 f"/v1/sessions/{sid2}/iterations", body={"mode": "auto"},
                 headers={**AUTH, "Idempotency-Key": "contract-demo-key-01"},
                 note="Same key, different body: refused. Replaying the old "
                      "response would let the caller believe it started a run "
                      "with its new parameters.")

    # A retryable 409, so the contract shows one. Contrast 26-trigger-blocked,
    # which is also 409 and deliberately carries NO Retry-After: waiting will
    # never clear an unrecovered interruption, and telling a client otherwise
    # would buy it a retry loop that can only fail.
    with capture.client.app.state.runner.claim(sid2):
        capture.call("33-busy-retry-after", "POST",
                     f"/v1/sessions/{sid2}/iterations", body={"mode": "manual"},
                     note="An iteration runs for minutes, so a bare 409 leaves "
                          "a client to guess between giving up and hammering. "
                          "Retry-After answers it.")

    latest = capture.client.app.state.db.one(
        "SELECT iteration_id FROM iterations WHERE session_id = ? "
        "AND finished_at IS NULL ORDER BY iteration_id DESC", (sid2,))
    if latest is not None:
        capture.call("34-cancel", "POST",
                     f"/v1/iterations/{latest['iteration_id']}/cancel",
                     body={"requested_by": "duty analyst",
                           "reason": "wrong city"},
                     note="Cooperative, not a kill. Honoured at the next stage "
                          "boundary; CORRELATING and ALERTING still run, "
                          "because stopping dead would spend the collection "
                          "budget and then discard the evidence it bought.")

    first_alert = capture.client.app.state.db.one(
        "SELECT alert_id FROM alerts ORDER BY alert_id")
    if first_alert is not None:
        aid = int(first_alert["alert_id"])
        capture.call("35-review-alert", "POST", f"/v1/alerts/{aid}/review",
                     body={"review_state": "RELEASED",
                           "reviewed_by": "duty analyst",
                           "note": "corroborated against the organiser's "
                                   "published schedule"},
                     note="A human gate on DISTRIBUTION only. The score, band "
                          "and evidence are untouched — an alert withheld for "
                          "being unhelpful must stay in the record at its "
                          "computed confidence.")
        capture.call("36-alerts-released-only", "GET",
                     f"/v1/sessions/{sid}/alerts?review_state=RELEASED",
                     note="What a client distributing onward should ask "
                          "for. The unfiltered listing deliberately returns "
                          "every alert, because an operator cannot review what "
                          "the API hides.")

    # ------------------------------------------------------------------
    # The other 409 (8.7a)
    # ------------------------------------------------------------------
    # Session `sid2` carries the manual iteration created at 30 and never
    # finished. It is genuinely OPEN rather than interrupted: it was created
    # after the last reconcile, so nothing stamped `interrupted_at` and nothing
    # in `/v1/recovery`'s `interrupted` list knows about it. That is exactly why
    # the pre-8.7 guard let a second iteration start here, and why
    # `GET /v1/iterations/{id}` reported it as PENDING.
    #
    # `sid` would NOT do: its manual iteration was owned by the epoch that
    # _strand_an_iteration closed, so the reconcile marked it INTERRUPTED —
    # correctly, and that is example 26's case rather than this one's.
    capture.call("37-trigger-blocked-open", "POST",
                 f"/v1/sessions/{sid2}/iterations",
                 note="409 for an iteration that is merely OPEN rather than "
                      "crash-interrupted. Same status, same two remedies, and "
                      "no Retry-After for the same reason as 26: waiting never "
                      "clears it. The kind is what tells an operator what "
                      "happened; it does not change what to do about it.")
    capture.call("38-recovery-blocking", "GET", "/v1/recovery",
                 note="`interrupted` is what THIS process found dead at "
                      "startup. `blocking` is everything a new iteration would "
                      "be refused for — a superset, and the list to act on. An "
                      "iteration left open without a crash appears only here.")

    # ------------------------------------------------------------------
    # A correlation that produced no alert (8.7b)
    # ------------------------------------------------------------------
    # The shipped `alert_min_score` of 0.15 is low enough that this scenario's
    # correlations all alert, so the case is produced the way an operator would
    # produce it: by raising the floor. The config the app is holding IS the
    # config the run reads, so this is a real iteration under a real setting,
    # not a hand-built row. Restored afterwards so nothing downstream inherits
    # a floor the rest of the contract was not captured under.
    config = capture.client.app.state.config
    floor_was = config["correlation"]["alert_min_score"]
    config["correlation"]["alert_min_score"] = 0.99
    clock.advance(hours=4)
    try:
        strict = capture.call("_", "POST", "/v1/sessions", body=SESSION_BODY,
                              keep=False)
        sid3 = strict["session_id"]
        capture.call("_", "POST", f"/v1/sessions/{sid3}/iterations?wait=true",
                     keep=False)
        unalerted = capture.client.app.state.db.one(
            "SELECT correlation_id FROM correlations "
            "WHERE alert_decision = 'BELOW_FLOOR' "
            "ORDER BY score DESC, correlation_id LIMIT 1")
    finally:
        config["correlation"]["alert_min_score"] = floor_was

    # ------------------------------------------------------------------
    # Re-triage (8.8)
    # ------------------------------------------------------------------
    # Produced the way it happens in production: a model whose replies overrun
    # `llm.max_tokens`, so a whole batch is recorded MODEL_ERROR. The client is
    # swapped for one iteration and put back, so the rest of the contract is
    # captured against the healthy model.
    clock.advance(hours=4)
    healthy_client = capture.client.app.state.llm_client
    capture.client.app.state.llm_client = _TruncatingClient()
    try:
        lossy = capture.call("_", "POST", "/v1/sessions", body=SESSION_BODY,
                             keep=False)
        sid4 = lossy["session_id"]
        capture.call("_", "POST", f"/v1/sessions/{sid4}/iterations?wait=true",
                     keep=False)
    finally:
        capture.client.app.state.llm_client = healthy_client

    lost = capture.client.app.state.db.one(
        "SELECT iteration_id FROM iterations WHERE session_id = ? "
        "ORDER BY iteration_id DESC LIMIT 1", (sid4,))
    if lost is not None:
        parent = int(lost["iteration_id"])
        capture.call("40-retry-triage", "POST",
                     f"/v1/iterations/{parent}/retry-triage?wait=true",
                     note="A batch that overran `llm.max_tokens` recorded every "
                          "post in it as MODEL_ERROR: the evidence was "
                          "collected and paid for, only the judgement is "
                          "missing. This re-judges exactly those posts in a "
                          "NEW iteration that carries `retry_of_iteration_id`, "
                          "inherits the parent's `anchor_at` so the correlation "
                          "window does not slide off the evidence it exists to "
                          "complete, and does not re-collect. The parent is "
                          "never edited — both records stand as what each run "
                          "did.")

    if unalerted is not None:
        cid = int(unalerted["correlation_id"])
        capture.call("39-correlation-evidence", "GET",
                     f"/v1/correlations/{cid}/evidence",
                     note="The same drill-down for a correlation that became "
                          "no alert — here because the deployment's "
                          "`correlation.alert_min_score` was raised to 0.99. "
                          "`alert_id`, `summary`, `caveat` and `receipt` are "
                          "null because no alert and no model call exist; the "
                          "signals, the arithmetic and the provenance are all "
                          "present. `alert_decision_reason` states the "
                          "comparison that produced the outcome, so a reader "
                          "does not need the configuration to understand it. "
                          "These near misses are what the interim floors are "
                          "meant to be calibrated from, and before 8.7(b) they "
                          "were reachable only by opening the database.")


def session_of(client: TestClient, iteration_id: int) -> int:
    return int(client.app.state.db.get_iteration(iteration_id)["session_id"])


def _strand_an_iteration(client: TestClient, clock: Clock) -> int:
    """Run an iteration part-way, then simulate the process dying inside it.

    On its OWN session. The first session's manual iteration is deliberately
    left mid-discard so the debug examples can document that state, and since
    8.7(a) an unfinished iteration blocks a new one on the same session — which
    is the point of the guard, not an obstacle to route around. Stranding here
    would otherwise be refused, and the recovery examples below need a crash
    that is the session's only outstanding work.
    """
    from surge_iw.services.recovery import RecoveryService

    db = client.app.state.db
    session_id = db.insert_session(label="recovery scenario",
                                   tracks=["AIRSHOW"])
    db.insert_city(session_id, "Phoenix", canonical="phoenix", state="AZ")
    orchestrator = client.app.state.build_orchestrator()
    iteration_id = orchestrator.start(session_id,
                                      epoch_id=client.app.state.epoch_id)
    for _ in range(4):                     # through TIPPING
        orchestrator.step(iteration_id)

    # The kill: a stage opened, nothing closed it.
    db.start_agent_run(iteration_id, "IterationOrchestrator",
                       "COLLECTING_TIPPED")
    db.start_agent_run(iteration_id, "CollectionAgent", "COLLECTING_TIPPED")
    db.set_stage(iteration_id, "COLLECTING_TIPPED")
    db.claim_next_query(iteration_id, ("CAR", "LODGING", "FLIGHT_LIVE"))

    report = RecoveryService(db, client.app.state.config).open_epoch("serve")
    client.app.state.epoch_id = report.epoch_id
    client.app.state.runner.epoch_id = report.epoch_id
    client.app.state.reconcile = report
    return iteration_id


# ---------------------------------------------------------------------------
# Markdown reference
# ---------------------------------------------------------------------------


def _type_of(schema: Mapping[str, Any]) -> str:
    """A readable type for a JSON-Schema node."""
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[1]
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            parts = [_type_of(item) for item in schema[key]]
            parts = [p for p in parts if p != "null"]
            return " | ".join(dict.fromkeys(parts)) or "null"
    if schema.get("type") == "array":
        return f"{_type_of(schema.get('items', {}))}[]"
    if "enum" in schema:
        return " | ".join(f"`{v}`" for v in schema["enum"])
    if "const" in schema:
        return f"`{schema['const']}`"
    return schema.get("type", "object")


def _escape(text: str | None) -> str:
    return (text or "").replace("\n", " ").replace("|", "\\|").strip()


def _anchor(heading: str) -> str:
    """GitHub's heading slug: lowercase, punctuation dropped, spaces hyphened.

    Worth getting exactly right rather than approximately — a table of contents
    whose links all 404 is worse than no table of contents, because it looks
    navigable.
    """
    slug = heading.strip().lower()
    slug = "".join(c for c in slug if c.isalnum() or c in " -_")
    return slug.replace(" ", "-")


def _purpose(operation: Mapping[str, Any]) -> str:
    """The first sentence of the handler's docstring.

    FastAPI's `summary` is derived from the function name — "Read Iteration",
    "Healthz" — which restates the path and tells a reader nothing. The
    docstring is where the intent actually is.
    """
    description = (operation.get("description") or "").strip()
    if description:
        first = description.split("\n\n")[0].replace("\n", " ").strip()
        return first if len(first) < 160 else first.split(". ")[0] + "."
    return _escape(operation.get("summary"))


#: Reading order for the reference: what a client does first, first.
TAG_ORDER = ("sessions", "iterations", "alerts", "queue", "ops", "debug")


def markdown(spec: Mapping[str, Any], operational: Mapping[str, Any],
             exchanges: list[dict[str, Any]]) -> str:
    """The human reference. Generated, so it cannot drift from the spec."""
    info = spec["info"]
    debug_only = sorted(set(spec["paths"]) - set(operational["paths"]))
    lines: list[str] = [
        f"# {info['title']} — API reference",
        "",
        "<!-- Generated by scripts/build_api_contract.py. Do not edit. -->",
        "",
        f"Version `{info['version']}`. OpenAPI `{spec['openapi']}`.",
        "",
        info.get("description", "").strip(),
        "",
        "## Authentication",
        "",
        "Every route except `GET /v1/healthz` requires a bearer token, read "
        "from the environment variable named by `api.token_env` "
        "(`SURGE_API_TOKEN` by default).",
        "",
        "```",
        "Authorization: Bearer <SURGE_API_TOKEN>",
        "```",
        "",
        "An unset token makes the API return **503** rather than serving "
        "unauthenticated: a check that switches itself off when unconfigured "
        "is worse than none, because it looks like one.",
        "",
        "## Endpoints",
        "",
        "| Method | Path | Purpose |",
        "|---|---|---|",
    ]

    operations: list[tuple[str, str, dict[str, Any]]] = []
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            operations.append((path, method.upper(), operation))

    def order(row: tuple[str, str, dict[str, Any]]) -> tuple[int, str, str]:
        tags = row[2].get("tags") or [""]
        rank = TAG_ORDER.index(tags[0]) if tags[0] in TAG_ORDER else len(TAG_ORDER)
        return (rank, row[0], row[1])

    operations.sort(key=order)

    for path, method, operation in operations:
        flag = " *(debug)*" if path in debug_only else ""
        anchor = _anchor(f"{method} {path}")
        lines.append(f"| `{method}` | [`{path}`](#{anchor}){flag} | "
                     f"{_escape(_purpose(operation))} |")

    if debug_only:
        lines += [
            "",
            f"The {len(debug_only)} paths marked *(debug)* are mounted only "
            "when `api.debug_endpoints` is true. `openapi-operational.json` is "
            "the surface without them — generate a client from that one for a "
            f"deployment, or it will call {len(debug_only)} paths that "
            "return 404.",
        ]

    lines += [
        "",
        "## Errors",
        "",
        "Non-2xx responses carry `{\"detail\": \"...\"}` — the `ErrorOut` "
        "schema — with one exception noted below.",
        "",
        "| Status | Meaning |",
        "|---|---|",
    ]
    from surge_iw.api.routes import _ERRORS
    for status, meaning in sorted(_ERRORS.items()):
        lines.append(f"| `{status}` | {_escape(meaning)} |")
    lines += [
        "",
        "**`422` has two shapes.** A request that fails schema validation "
        "returns FastAPI's `HTTPValidationError` — a `detail` *array* naming "
        "the offending field. A request that is well-formed but cannot be "
        "acted on (an unknown stage, a malformed timestamp, a session with no "
        "cities) returns `ErrorOut`, whose `detail` is a string. Handle both: "
        "the responses table on each endpoint below says which one that "
        "endpoint declares, and both are reachable wherever path or query "
        "parameters exist.",
        "",
        "---",
        "",
        "## Reference",
        "",
    ]

    by_name = {item["name"]: item for item in exchanges}
    example_for = _example_index(by_name, spec)

    for path, method, operation in operations:
        lines += [f"### `{method} {path}`", ""]
        if path in debug_only:
            lines += ["> Mounted only when `api.debug_endpoints` is true.", ""]
        description = (operation.get("description") or "").strip()
        if description:
            lines += [description, ""]

        params = operation.get("parameters", [])
        if params:
            lines += ["**Parameters**", "",
                      "| Name | In | Required | Type | Description |",
                      "|---|---|---|---|---|"]
            for param in params:
                lines.append(
                    f"| `{param['name']}` | {param['in']} | "
                    f"{'yes' if param.get('required') else 'no'} | "
                    f"{_type_of(param.get('schema', {}))} | "
                    f"{_escape(param.get('description'))} |")
            lines.append("")

        body = operation.get("requestBody")
        if body:
            schema = body["content"]["application/json"]["schema"]
            lines += [f"**Request body** — `{_type_of(schema)}`"
                      f"{'' if body.get('required') else ' (optional)'}", ""]

        lines += ["**Responses**", "", "| Status | Body | Meaning |",
                  "|---|---|---|"]
        for status, response in sorted(operation.get("responses", {}).items()):
            content = response.get("content", {}).get("application/json", {})
            model = _type_of(content["schema"]) if content else "—"
            lines.append(f"| `{status}` | {model} | "
                         f"{_escape(response.get('description'))} |")
        lines.append("")

        example = example_for.get((path, method))
        if example:
            lines += [f"**Example** — [`examples/{example}.json`]"
                      f"(examples/{example}.json)", ""]
        lines.append("")

    lines += ["---", "", "## Schemas", ""]
    for name, schema in sorted(
            spec.get("components", {}).get("schemas", {}).items()):
        lines += [f"### `{name}`", ""]
        if schema.get("description"):
            lines += [schema["description"].strip(), ""]
        properties = schema.get("properties")
        if not properties:
            lines += ["*(no properties)*", ""]
            continue
        required = set(schema.get("required", []))
        lines += ["| Field | Type | Required | Description |", "|---|---|---|---|"]
        for field, definition in properties.items():
            lines.append(
                f"| `{field}` | {_type_of(definition)} | "
                f"{'yes' if field in required else 'no'} | "
                f"{_escape(definition.get('description'))} |")
        lines.append("")

    lines += [
        "---",
        "",
        "## Captured examples",
        "",
        "Real exchanges, recorded by driving the application through a full "
        "iteration with fixture-backed connectors and a frozen clock. Two "
        "things to read past:",
        "",
        "* The alert summaries come from a stub model. They are placeholders "
        "for the shape, not a sample of the system's prose.",
        "* Every `units_spent` and `budget` figure is **zero**, because "
        "`dry_run` records zero units by design. On a real run these carry the "
        "credits actually billed.",
        "",
        "| # | Exchange | Status | Note |",
        "|---|---|---|---|",
    ]
    for item in exchanges:
        lines.append(
            f"| [`{item['name']}`](examples/{item['name']}.json) | "
            f"`{item['request']['method']} {item['request']['path']}` | "
            f"`{item['response']['status']}` | {_escape(item['note'])} |")
    lines.append("")
    return "\n".join(lines)


def _example_index(
    by_name: Mapping[str, Any], spec: Mapping[str, Any]
) -> dict[tuple[str, str], str]:
    """Map each operation to the first captured exchange that exercised it."""
    index: dict[tuple[str, str], str] = {}
    for name, item in by_name.items():
        actual = item["request"]["path"].split("?")[0]
        method = item["request"]["method"]
        for template in spec["paths"]:
            if (template, method) in index:
                continue
            if _matches(template, actual) and method in {
                    m.upper() for m in spec["paths"][template]}:
                index[(template, method)] = name
    return index


def _matches(template: str, actual: str) -> bool:
    """Whether a concrete path matches a templated one, segment by segment."""
    left, right = template.strip("/").split("/"), actual.strip("/").split("/")
    if len(left) != len(right):
        return False
    return all(a.startswith("{") or a == b for a, b in zip(left, right))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def build() -> dict[str, str]:
    """Produce every artifact as {relative path: text}."""
    clock = Clock(FROZEN)
    undo = freeze_clock(clock)
    try:
        config = load_config(None)
        config["dry_run"] = True
        config["database"]["path"] = ":memory:"
        # Wide enough that the example iteration reaches every source.
        config["tipping"]["max_queries_per_city"] = 99
        config["triage"]["batch_size"] = 20

        full = create_app(config, db=SurgeDB(":memory:"),
                          llm_client=StubModel())
        spec = full.openapi()

        offline = dict(config)
        offline["api"] = dict(config["api"], debug_endpoints=False)
        operational = create_app(offline, db=SurgeDB(":memory:")).openapi()

        import os
        previous = os.environ.get("SURGE_API_TOKEN")
        os.environ["SURGE_API_TOKEN"] = TOKEN
        try:
            with TestClient(full) as client:
                client.app.state.api_token = TOKEN
                capture = Capture(client)
                scenario(capture, clock)
        finally:
            if previous is None:
                os.environ.pop("SURGE_API_TOKEN", None)
            else:
                os.environ["SURGE_API_TOKEN"] = previous
    finally:
        for module, attribute, original in undo:
            setattr(module, attribute, original)

    artifacts = {
        "openapi.json": _json_text(spec),
        "openapi.yaml": yaml.safe_dump(spec, sort_keys=False,
                                       allow_unicode=True, width=100),
        "openapi-operational.json": _json_text(operational),
        "API.md": markdown(spec, operational, capture.exchanges) + "\n",
        # 8.3. Generated from services/governance.py so a rights reviewer reads
        # the rules the system actually enforces, not a parallel document that
        # drifted. The --check gate covers it like everything else here.
        "PROVIDERS.md": providers_markdown() + "\n",
    }
    for item in capture.exchanges:
        artifacts[f"examples/{item['name']}.json"] = _json_text(item)
    return artifacts


def providers_markdown() -> str:
    """The provider governance record, as a reviewable document (8.3)."""
    from surge_iw.services import governance

    out = [
        "# Provider governance",
        "",
        "Generated from `surge_iw/services/governance.py`. Do not edit — edit "
        "the record and regenerate, so what is reviewed here is what the "
        "system enforces.",
        "",
        f"Rules version: `{governance.RULES_VERSION}`",
        "",
        "## The distinction that matters",
        "",
        "**MEASURED** claims were observed against the live API and can be "
        "observed again. **ASSERTED** claims come from the vendor's terms or "
        "documentation and have not been independently verified.",
        "",
        "**No provider has verified downstream rights, and none can acquire "
        "them by being called.** A 200 means the vendor served the bytes, not "
        "that we may redistribute them. All four are intermediaries: the "
        "content belongs to a platform or a publisher whose terms bind us "
        "whatever the aggregator's say. Vendor intermediation is not proof of "
        "downstream rights, and the development ceiling is not spend "
        "authorisation.",
        "",
    ]
    for name, p in sorted(governance.POLICIES.items()):
        days = governance.retention_days(name)
        basis = ("contractual ceiling" if p.retention_days
                 else "our choice, not a granted permission")
        out += [
            f"## {name}",
            "",
            f"- **Families**: {', '.join(p.families)}",
            f"- **Billing unit**: {p.unit} — {p.unit_basis}",
            f"- **Retention**: {days} days ({basis}). {p.retention_basis}",
            f"- **Identifiers accepted**: {', '.join(p.identifiers)}",
            f"- **Provenance**: {p.provenance}",
            f"- **Rate limits**: {p.rate_limits}",
            "",
            "**Downstream rights — UNVERIFIED.** " + p.downstream_rights,
            "",
            "### Failure modes",
            "",
        ]
        out += [f"- `{k}` — {v}" for k, v in sorted(p.failure_modes.items())]
        out += ["", "### Measured", ""]
        out += [f"- **{k}**: {v}" for k, v in sorted(p.measured.items())]
        if p.asserted:
            out += ["", "### Asserted (from vendor terms, not verified)", ""]
            out += [f"- **{k}**: {v}" for k, v in sorted(p.asserted.items())]
        if p.drop_before_storage:
            out += ["", "### Never retained", "",
                    "Dropped before the payload is written, at any nesting "
                    "depth. Not secrets — material we have no analytical use "
                    "for and no business keeping.", ""]
            out += [f"- `{f}`" for f in p.drop_before_storage]
        if p.fixtures:
            out += ["", f"**Fixtures**: {', '.join(f'`{f}`' for f in p.fixtures)}"]
        out += [""]
    return "\n".join(out)


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def write(artifacts: Mapping[str, str], out: Path) -> None:
    (out / "examples").mkdir(parents=True, exist_ok=True)
    known = set(artifacts)
    for name, text in sorted(artifacts.items()):
        (out / name).write_text(text, encoding="utf-8")
    # Sweep files a previous run left behind: a stale example that no longer
    # corresponds to any endpoint is worse than a missing one, because it looks
    # current.
    for path in sorted(out.rglob("*")):
        if path.is_file() and str(path.relative_to(out)) not in known:
            path.unlink()
            print(f"  removed stale {path.relative_to(out)}")


def check(artifacts: Mapping[str, str], out: Path) -> int:
    """Compare against what is on disk. Returns a process exit code."""
    stale: list[str] = []
    for name, text in sorted(artifacts.items()):
        path = out / name
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != text:
            stale.append(name)
            diff = difflib.unified_diff(
                current.splitlines(), text.splitlines(),
                fromfile=f"a/{name}", tofile=f"b/{name}", lineterm="", n=1)
            print("\n".join(list(diff)[:40]))
    on_disk = {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()} \
        if out.exists() else set()
    orphans = sorted(on_disk - set(artifacts))
    for name in orphans:
        print(f"stale file with no generator: {name}")

    if stale or orphans:
        print(f"\n{len(stale)} artifact(s) out of date, "
              f"{len(orphans)} orphaned. Run:\n"
              f"  python scripts/build_api_contract.py")
        return 1
    print(f"{len(artifacts)} artifact(s) up to date.")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true",
                        help="Exit non-zero if the saved artifacts are stale.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    artifacts = build()
    if args.check:
        return check(artifacts, args.out)

    write(artifacts, args.out)
    print(f"Wrote {len(artifacts)} artifact(s) to "
          f"{args.out.relative_to(REPO) if args.out.is_relative_to(REPO) else args.out}")
    for name in sorted(artifacts):
        if not name.startswith("examples/"):
            print(f"  {name}")
    print(f"  examples/  ({sum(1 for n in artifacts if '/' in n)} exchanges)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
