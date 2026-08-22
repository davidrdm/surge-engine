"""API contract hardening — 8.2.

Six mechanisms, each tested for the property that made it worth building rather
than for its happy path:

  idempotency   a lost response can be retried without buying collection twice
  Retry-After   a retryable refusal says when; a permanent one deliberately does not
  cancellation  stopping a run cannot discard the evidence it already paid for
  review        an unreviewed alert is distinguishable from a cleared one
  capabilities  an unsupported jurisdiction reports as unsupported, not as quiet
  evidence      the vendor payload is not the evidence surface
"""
from __future__ import annotations

import pytest

from surge_iw.api import contract
from surge_iw.api.routes import _capability_for
# Reusing test_api's app fixtures: one wired application, one place to keep it
# right. Importing a fixture by name registers it in this module.
from test_api import AUTH, app_config, client, make_session  # noqa: F401


@pytest.fixture
def session_id(client):
    return make_session(client)


@pytest.fixture
def alert_id(db, session, iteration):
    """One alert, built directly. The route under test governs distribution,
    so how the finding was reached does not matter here."""
    city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
    correlation_id = db.upsert_correlation(
        iteration_id=iteration, city_id=city, track="AIRSHOW",
        score=0.62, band="MEDIUM", distinct_types=2,
        contributions={"social": 0.3}, data_completeness=1.0,
        rule_trace="test")
    return db.insert_alert(
        correlation_id=correlation_id, session_id=session,
        iteration_id=iteration, city_id=city, track="AIRSHOW",
        confidence_score=0.62, confidence_band="MEDIUM",
        summary="Crews and equipment staging at the fairground.",
        model="test-model")


class TestIdempotency:
    """The endpoint spends money, so a retry has to be safe."""

    def test_a_repeated_key_replays_instead_of_starting_a_second_run(
        self, client, session_id
    ):
        first = client.post(f"/v1/sessions/{session_id}/iterations",
                            json={"mode": "manual"},
                            headers={**AUTH, "Idempotency-Key": "abc-12345678"})
        assert first.status_code == 202
        again = client.post(f"/v1/sessions/{session_id}/iterations",
                            json={"mode": "manual"},
                            headers={**AUTH, "Idempotency-Key": "abc-12345678"})
        assert again.status_code == 202
        assert again.json() == first.json()
        assert again.headers.get(contract.REPLAY_HEADER) == "true"
        assert (again.json()["iteration_id"]
                == first.json()["iteration_id"]), "no second iteration"

    def test_a_key_reused_for_a_different_request_is_refused(
        self, client, session_id
    ):
        """Answering it with the old response would let the caller believe it
        started a run with its new parameters. It did not."""
        client.post(f"/v1/sessions/{session_id}/iterations",
                    json={"mode": "manual"},
                    headers={**AUTH, "Idempotency-Key": "reuse-12345678"})
        clash = client.post(f"/v1/sessions/{session_id}/iterations",
                            json={"mode": "auto"},
                            headers={**AUTH, "Idempotency-Key": "reuse-12345678"})
        assert clash.status_code == 422
        assert "already used" in clash.json()["detail"]

    def test_a_short_key_is_refused(self, client, session_id):
        r = client.post(f"/v1/sessions/{session_id}/iterations",
                        json={"mode": "manual"},
                        headers={**AUTH, "Idempotency-Key": "xy"})
        assert r.status_code == 422

    def test_idempotency_is_opt_in(self, client, session_id):
        """A CLI making one call by hand should not have to invent a key."""
        r = client.post(f"/v1/sessions/{session_id}/iterations",
                        json={"mode": "manual"}, headers=AUTH)
        assert r.status_code == 202
        assert contract.REPLAY_HEADER not in r.headers

    def test_wait_is_part_of_request_identity(self):
        """A replay must not hand a ?wait=true caller a 202 it never asked
        for, so the two are different requests."""
        assert (contract.request_fingerprint(1, {"mode": "auto"}, False)
                != contract.request_fingerprint(1, {"mode": "auto"}, True))

    def test_an_expired_key_is_a_new_request(self, db, session, iteration):
        db.record_idempotency_key(
            session_id=session, key="expiring-1234", request_hash="h",
            iteration_id=iteration, status_code=202, response={},
            ttl_hours=-1.0)
        assert db.find_idempotency_key(session, "expiring-1234") is None
        assert db.purge_expired_idempotency_keys() == 1


class TestRetryAfter:
    def test_a_busy_session_says_when_to_come_back(self, client, session_id):
        """An iteration runs for minutes. A client told only "409" either
        gives up or hammers; both are wrong.

        The lock is held directly rather than by racing a real run — the
        contract under test is what a blocked caller is told, not the timing
        that blocked it.
        """
        with client.app.state.runner.claim(session_id):
            busy = client.post(f"/v1/sessions/{session_id}/iterations",
                               json={"mode": "manual"}, headers=AUTH)
        assert busy.status_code == 409
        assert int(busy.headers["Retry-After"]) > 0

    def test_an_unrecovered_interruption_carries_no_retry_after(
        self, client, session_id, db
    ):
        """Waiting will never clear it — an operator has to resume or abandon.
        Telling a client to retry would be a lie that costs it a retry loop."""
        made = client.post(f"/v1/sessions/{session_id}/iterations",
                           json={"mode": "manual"}, headers=AUTH).json()
        db._exec("UPDATE iterations SET interrupted_at = ?, "
                 "interrupted_stage = 'TRIAGING' WHERE iteration_id = ?",
                 ("2026-07-27T12:00:00+00:00", made["iteration_id"]))
        blocked = client.post(f"/v1/sessions/{session_id}/iterations",
                              json={"mode": "manual"}, headers=AUTH)
        assert blocked.status_code == 409
        assert "Retry-After" not in blocked.headers

    def test_the_error_carries_the_header_it_was_built_with(self):
        exc = contract.RetryableError(409, "busy", 90)
        assert exc.headers["Retry-After"] == "90"
        assert exc.retry_after == 90


class TestCancellation:
    """Cooperative by design. The property under test is that stopping a run
    cannot throw away the collection it already paid for."""

    def test_cancelling_still_scores_and_alerts(self, db, iteration):
        from surge_iw.agents.orchestrator import FINALISE_STAGES
        assert FINALISE_STAGES == {"CORRELATING", "ALERTING"}
        assert "SCHEDULING" not in FINALISE_STAGES, (
            "follow-ons for a run nobody finished would surprise the next one")

    def test_a_skipped_stage_is_a_coverage_gap(self, db, iteration):
        """The defect a live cancellation found, and the reason this class
        exists.

        A cancelled run collected 12 social queries — all COMPLETE — then
        skipped TRIAGING and TIPPING and reported NO correlation at all: not a
        gap, not a capped band, nothing. Every existing detector missed it, each
        correctly by its own terms. The queries succeeded, so nothing was
        status-unreliable; the skipped stage wrote no decisions, so nothing was
        triage-uncovered; and it enqueued nothing, so there was no refusal to
        find. The run read as a quiet city on evidence it had paid for.
        """
        from surge_iw.base.scoring import source_types_for_skipped

        db.record_skipped_stages(iteration, ["TRIAGING", "TIPPING"])
        assert db.skipped_stages(iteration) == ["TIPPING", "TRIAGING"]

        gaps = source_types_for_skipped(db.skipped_stages(iteration))
        assert "SOCIAL" in gaps, "collected but never judged is not 'nothing'"
        assert {"FLIGHT_LIVE", "LODGING", "CAR"} <= set(gaps), (
            "tipping is what enqueues the paid families at all")

    def test_an_ordinary_run_reports_no_skipped_stages(self, db, iteration):
        """The gap must not fire on a healthy iteration."""
        from surge_iw.base.scoring import source_types_for_skipped

        assert db.skipped_stages(iteration) == []
        assert source_types_for_skipped([]) == []

    def test_the_request_is_recorded_before_it_is_honoured(self, db, iteration):
        assert db.cancel_requested(iteration) is False
        db.request_cancel(iteration, requested_by="analyst", reason="mistake")
        assert db.cancel_requested(iteration) is True
        row = db.get_iteration(iteration)
        assert row["cancel_requested_by"] == "analyst"
        assert row["cancel_reason"] == "mistake"

    def test_cancelling_a_finished_iteration_is_a_conflict(
        self, client, session_id
    ):
        """Reporting success would imply the run was shortened when it was
        not."""
        made = client.post(f"/v1/sessions/{session_id}/iterations",
                           json={"mode": "manual"}, headers=AUTH).json()
        iid = made["iteration_id"]
        client.app.state.db.finish_iteration(iid, outcome="COMPLETE")
        r = client.post(f"/v1/iterations/{iid}/cancel", json={}, headers=AUTH)
        assert r.status_code == 409
        assert "nothing to cancel" in r.json()["detail"]

    def test_a_live_iteration_accepts_cancellation(self, client, session_id):
        made = client.post(f"/v1/sessions/{session_id}/iterations",
                           json={"mode": "manual"}, headers=AUTH).json()
        r = client.post(f"/v1/iterations/{made['iteration_id']}/cancel",
                        json={"requested_by": "analyst", "reason": "wrong city"},
                        headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["cancel_requested_at"]
        assert sorted(body["will_still_run"]) == ["ALERTING", "CORRELATING"]


class TestTheEvidenceSurfaceIsNotTheVendorPayload:
    """8.2. What may be redistributed from a provider response is a rights
    question per provider (8.3), not something the evidence endpoint should
    assume. Withheld is stated, never silently omitted."""

    def _raw(self):
        return {"raw_id": 7, "provider": "FR24",
                "retrieved_at": "2026-07-27T12:00:00+00:00",
                "purge_after": "2026-08-26T12:00:00+00:00",
                "payload_json": '{"flights": [{"reg": "N123"}]}'}

    def test_the_payload_is_withheld_by_default_and_says_so(self):
        from surge_iw.api.routes import _raw_view
        view = _raw_view(self._raw(), expose=False)
        assert "payload" not in view
        assert "expose_raw_payloads" in view["payload_withheld"]

    def test_what_is_withheld_is_still_identified(self):
        """A reader who cannot see the payload still learns it exists, who
        served it, when it is deleted, and a hash to demand it by."""
        from surge_iw.api.routes import _raw_view
        view = _raw_view(self._raw(), expose=False)
        assert view["provider"] == "FR24"
        assert view["retrieved_at"] and view["purge_after"]
        assert len(view["payload_hash"]) == 32

    def test_opting_in_returns_it(self):
        from surge_iw.api.routes import _raw_view
        view = _raw_view(self._raw(), expose=True)
        assert view["payload"] == {"flights": [{"reg": "N123"}]}
        assert "payload_withheld" not in view

    def test_a_purged_payload_is_absent_not_withheld(self):
        """Retention deletion and a policy withhold are different facts."""
        from surge_iw.api.routes import _raw_view
        assert _raw_view(None, expose=True) is None

    def test_the_endpoint_withholds_query_parameters_too(self, client,
                                                         session_id):
        """They carry the search lexicon and facility coordinates."""
        from surge_iw.api.routes import read_evidence  # noqa: F401
        import json, pathlib
        path = (pathlib.Path(__file__).parent.parent
                / "docs/api/examples/08-evidence.json")
        assert path.exists(), (
            # Was a skip. `docs/api/` is generated AND committed, so a missing
            # example is a broken repository, not an unbuilt one — and a skip
            # reads as green. Five assertions about the evidence surface
            # stopped running when the examples were moved out during the
            # doc split, and nothing said so.
            f"{path} is missing. It is a committed artifact; regenerate it:\n"
            "  python scripts/build_api_contract.py")
        body = json.loads(path.read_text())["response"]["body"]
        for signal in body["signals"]:
            query = signal.get("query")
            if query:
                assert "params" not in query
                assert query["params_withheld"]


class TestEveryStatusARouteCanReturnIsDeclared:
    """The drift that arrives with a feature, not with a refactor.

    Adding a route, or a new refusal inside an existing one, is two edits: the
    `raise HTTPException(...)` and the `responses=` declaration beside the
    decorator. Nothing makes the second happen. A behaviour test proves the
    status is RETURNED; none of them asks whether it is DOCUMENTED, so an
    undeclared refusal passes the suite and reaches a generated client as an
    unhandled response.

    Read from the source rather than from the app, because an undeclared
    status is invisible in the generated schema by definition.
    """

    @staticmethod
    def _routes():
        import ast
        import pathlib

        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "surge_iw" / "api" / "routes.py")
        tree = ast.parse(source.read_text())

        def codes(node, out):
            if isinstance(node, ast.Call):                    # errors(401, ...)
                out |= {a.value for a in node.args
                        if isinstance(a, ast.Constant)}
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if key is None:                           # **errors(...)
                        codes(value, out)
                    elif isinstance(key, ast.Constant):
                        out.add(key.value)

        def declared(fn):
            out, path, method = set(), None, None
            for dec in fn.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                if (isinstance(func, ast.Attribute)
                        and func.attr in ("get", "post", "patch", "put",
                                          "delete")):
                    method = func.attr
                    if dec.args and isinstance(dec.args[0], ast.Constant):
                        path = dec.args[0].value
                    for kw in dec.keywords:
                        if (kw.arg == "status_code"
                                and isinstance(kw.value, ast.Constant)):
                            out.add(kw.value.value)
                        elif kw.arg == "responses":
                            codes(kw.value, out)
            return method, path, out

        # Every module-level function, so a route that refuses via a shared
        # helper (`_session_or_404`) is credited with the helper's statuses.
        raises = {
            node.name: {n.exc.args[0].value for n in ast.walk(node)
                        if isinstance(n, ast.Raise)
                        and isinstance(n.exc, ast.Call)
                        and getattr(n.exc.func, "id", None) == "HTTPException"
                        and n.exc.args
                        and isinstance(n.exc.args[0], ast.Constant)}
            for node in tree.body if isinstance(node, ast.FunctionDef)
        }

        import ast as _ast
        for node in tree.body:
            if not isinstance(node, _ast.FunctionDef):
                continue
            method, path, decl = declared(node)
            if method is None:
                continue
            reachable = set(raises[node.name])
            for call in _ast.walk(node):
                if (isinstance(call, _ast.Call)
                        and isinstance(call.func, _ast.Name)
                        and call.func.id in raises):
                    reachable |= raises[call.func.id]
            yield f"{method.upper()} {path}", reachable, decl

    def test_the_parser_finds_the_routes_it_is_meant_to_check(self):
        """A scan that silently matches nothing passes forever. It has to be
        able to see a route, and a route's declarations, before its silence
        means anything."""
        routes = dict((r, d) for r, _, d in self._routes())
        assert len(routes) >= 20, routes
        assert 422 in routes["POST /v1/sessions"]
        assert 409 in routes["POST /v1/sessions/{session_id}/iterations"]

    def test_no_route_can_refuse_with_a_status_it_does_not_declare(self):
        undeclared = {route: sorted(reachable - decl)
                      for route, reachable, decl in self._routes()
                      if reachable - decl}
        assert not undeclared, (
            "these routes raise an HTTPException the contract never mentions; "
            "add the status to `responses=errors(...)`: " + repr(undeclared))


class TestMissionVocabularyIsRefusedNotCrashed:
    """The seventh mechanism, added when the mission left the schema.

    `track` and `location_type` were `Literal`s until v12. A client sending a
    bad one got a 422 from pydantic with nothing written. Now the vocabulary
    comes from a pack read at startup, so no enum can describe it and the
    refusal has to happen at runtime — which is a strictly weaker control, and
    only worth the trade if the runtime refusal is as good as the enum was:
    the right status, naming the value, and BEFORE anything is written.

    `location_type` failed all three. Its field validator went with the enum
    and nothing replaced it at the boundary, so validation happened at the
    INSERT — reached only after the session and its cities already existed.
    The result was a 500 the contract does not declare, over a session that
    exists with its lodging anchor silently absent.
    """

    BAD_CITY = {"name": "Phoenix", "state": "AZ",
                "key_locations": [{"name": "Convention Center",
                                   "location_type": "NOT_A_TYPE"}]}

    def test_an_unknown_location_type_is_refused_by_name(self, client):
        response = client.post(
            "/v1/sessions", headers=AUTH,
            json={"label": "x", "tracks": ["AIRSHOW"], "cities": [self.BAD_CITY]})
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "NOT_A_TYPE" in detail
        assert "STADIUM" in detail, "must list what was allowed"

    def test_the_refusal_writes_nothing(self, client):
        """The half-built session is the real defect. A 500 is loud; a session
        that exists with no key location is not — its lodging family produces
        nothing, and that reads as an absence of evidence rather than as a
        request that was never accepted."""
        client.post("/v1/sessions", headers=AUTH,
                    json={"label": "x", "tracks": ["AIRSHOW"],
                          "cities": [self.BAD_CITY]})
        assert client.get("/v1/sessions/1", headers=AUTH).status_code == 404

    def test_the_same_holds_when_adding_cities_later(self, client, session_id):
        """The second write path to the same table. It was missing the check
        too, and there the city IS created and only its location vanishes."""
        response = client.post(f"/v1/sessions/{session_id}/cities", headers=AUTH,
                               json={"cities": [dict(self.BAD_CITY,
                                                     name="Tucson")]})
        assert response.status_code == 422, response.text
        cities = client.get(f"/v1/sessions/{session_id}",
                            headers=AUTH).json()["cities"]
        assert "Tucson" not in [c["name"] for c in cities]

    def test_an_unknown_track_is_refused_the_same_way(self, client):
        """This half was done correctly at the split. Pinned beside the other
        so the pair cannot diverge again."""
        response = client.post(
            "/v1/sessions", headers=AUTH,
            json={"label": "x", "tracks": ["NOT_A_TRACK"],
                  "cities": [{"name": "Phoenix", "state": "AZ"}]})
        assert response.status_code == 422, response.text
        assert "NOT_A_TRACK" in response.json()["detail"]
        assert "AIRSHOW" in response.json()["detail"]

    def test_no_route_declares_500(self, client):
        """The status these refusals used to produce. Nothing may declare it,
        because nothing may reach it: a 500 tells a client the server broke,
        not that its request was wrong."""
        spec = client.get("/openapi.json").json()
        declared = {f"{method.upper()} {path}"
                    for path, methods in spec["paths"].items()
                    for method, operation in methods.items()
                    if "500" in operation.get("responses", {})}
        assert not declared, declared


class TestReviewState:
    def test_an_alert_starts_unreviewed(self, db, alert_id):
        assert db.get_alert(alert_id)["review_state"] == "UNREVIEWED"

    def test_review_moves_distribution_and_nothing_analytical(self, db, alert_id):
        before = db.get_alert(alert_id)
        db.set_review_state(alert_id, "WITHHELD", reviewed_by="analyst",
                            note="single source")
        after = db.get_alert(alert_id)
        assert after["review_state"] == "WITHHELD"
        assert after["reviewed_by"] == "analyst"
        # The finding itself is untouched: an alert withheld for being
        # unhelpful must stay in the record at its computed confidence, or the
        # audit trail records what was published rather than what was found.
        assert after["confidence_score"] == before["confidence_score"]
        assert after["confidence_band"] == before["confidence_band"]
        assert after["summary"] == before["summary"]

    def test_returning_to_unreviewed_clears_the_attribution(self, db, alert_id):
        db.set_review_state(alert_id, "RELEASED", reviewed_by="analyst")
        db.set_review_state(alert_id, "UNREVIEWED")
        row = db.get_alert(alert_id)
        assert row["reviewed_by"] is None and row["reviewed_at"] is None

    def test_an_unknown_state_is_refused(self, db, alert_id):
        with pytest.raises(ValueError):
            db.set_review_state(alert_id, "PROBABLY_FINE")


class TestCapabilities:
    """"No alerts here" and "this was never collectable" must not be the same
    answer on the wire."""

    def test_an_unknown_jurisdiction_reports_as_unsupported(self):
        cap = _capability_for("Nowheresville, ZZ")
        assert cap.resolved is False
        assert "FLIGHT" in cap.unsupported_sources
        assert "CAR" in cap.unsupported_sources
        assert any("does not resolve" in limit for limit in cap.limitations)

    def test_a_known_city_reports_what_it_can_collect(self):
        cap = _capability_for("Phoenix, AZ")
        assert cap.resolved is True
        assert cap.airports, "Phoenix must map to at least one airport"
        assert "FLIGHT" in cap.supported_sources

    def test_social_survives_an_unmapped_city(self):
        """Partially covered is the honest answer, not unusable."""
        cap = _capability_for("Nowheresville, ZZ")
        assert "SOCIAL" in cap.supported_sources

    def test_the_endpoint_states_its_limits(self, client):
        body = client.get("/v1/capabilities", headers=AUTH).json()
        assert body["geography"]["country"] == "US"
        assert body["retention"]["fr24_days"] <= 30
        assert any("not a calibrated probability" in c.lower()
                   or "calibrated" in c for c in body["caveats"])

    def test_the_endpoint_answers_per_city(self, client):
        body = client.get("/v1/capabilities", headers=AUTH,
                          params={"city": ["Phoenix, AZ", "Nowheresville, ZZ"]}
                          ).json()
        assert len(body["cities"]) == 2
        assert body["cities"][0]["resolved"] is True
        assert body["cities"][1]["resolved"] is False
