"""The Phase 6 gate: the REST API, and the three stage-debugging endpoints.

Every test runs against an in-memory database and the stub connectors from
test_orchestrator, so the suite makes no network call and spends no vendor
credit. The connectors themselves are covered against respx in
test_connectors.py; what is under test here is the HTTP contract.

The gate: init → trigger → poll → alerts round-trips; the trigger returns 202
quickly; a second concurrent trigger on one session returns 409; an
unauthenticated call returns 401; ?format=tuple returns positional 4-arrays;
and the generated OpenAPI schema validates.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import test_orchestrator as T
from surge_iw.agents.orchestrator import PIPELINE_STAGES
from surge_iw.api import contract
from surge_iw.api.app import create_app
from surge_iw.connectors.flightradar import _normalise_live, _normalise_summary
from surge_iw.connectors.priceline import parse_rental_car_response
from test_collection import fixture
from test_triage import FakeLLM

TOKEN = "phase6-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

PHOENIX = {
    "name": "Phoenix", "state": "AZ",
    "key_locations": [{"name": "Riverside Fairground",
                       "location_type": "FAIRGROUND"}],
}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def stub_connectors():
    return {
        "APIDIRECT": T.StubSocial({"Phoenix": [
            T.post("https://x.com/1", "x.com"),
            T.post("https://apnews.com/2", "apnews.com")]}),
        "FR24": T.StubFlights(
            live=[_normalise_live(f)
                  for f in fixture("fr24_live_positions.json")["data"]],
            summary=[_normalise_summary(f)
                     for f in fixture("fr24_flight_summary.json")["data"]]),
        "STAYING": T.StubStaying(T.LISTINGS, [T.NEAR, T.BASE]),
        "PRICELINE": T.StubCars(
            parse_rental_car_response(fixture("priceline_cars_near.json")),
            parse_rental_car_response(fixture("priceline_cars.json"))),
    }


@pytest.fixture
def app_config(config):
    config["tipping"]["max_queries_per_city"] = 99
    config["triage"] = {"batch_size": 20}
    return config


@pytest.fixture
def client(db, app_config, monkeypatch):
    monkeypatch.setenv("SURGE_API_TOKEN", TOKEN)
    # A LIST of responses, each a full batch. `[a, b] * 20` would be ONE
    # response of forty items for two posts — which the pre-Phase-7 boundary
    # silently deduplicated and the strict one correctly rejects as duplicate
    # judgements.
    llm = FakeLLM(*[[T.decision("https://x.com/1", "Phoenix"),
                     T.decision("https://apnews.com/2", "Phoenix")]] * 20)
    app = create_app(app_config, db=db, connectors=stub_connectors(),
                     llm_client=llm)
    with TestClient(app) as test_client:
        yield test_client


def make_session(client, **overrides):
    body = {"label": "AZ display season", "expand_cities": False,
            "tracks": ["AIRSHOW"], "cities": [PHOENIX]}
    body.update(overrides)
    response = client.post("/v1/sessions", headers=AUTH, json=body)
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def run_iteration(client, session_id):
    response = client.post(f"/v1/sessions/{session_id}/iterations?wait=true",
                           headers=AUTH)
    assert response.status_code in (200, 202), response.text
    return response.json()["iteration_id"]


# ===========================================================================
# Authentication
# ===========================================================================


class TestAuth:
    @pytest.mark.parametrize("path", [
        "/v1/sessions/1", "/v1/sessions/1/alerts", "/v1/sessions/1/queue",
        "/v1/iterations/1", "/v1/iterations/1/stages",
    ])
    def test_every_route_but_health_needs_a_token(self, client, path):
        assert client.get(path).status_code == 401

    def test_a_wrong_token_is_401_not_403(self, client):
        response = client.get("/v1/sessions/1",
                              headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    def test_the_scheme_must_be_bearer(self, client):
        assert client.get(
            "/v1/sessions/1",
            headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401

    def test_health_is_open(self, client):
        assert client.get("/v1/healthz").status_code == 200

    def test_but_probing_the_vendors_is_not(self, client):
        """An unauthenticated deep check would be a free way to burn the
        rate-limit budget the system collects on."""
        assert client.get("/v1/healthz?deep=true").status_code == 401
        assert client.get("/v1/healthz?deep=true",
                          headers=AUTH).status_code == 200

    def test_a_connector_whose_health_check_raises_is_reported_not_thrown(
        self, client
    ):
        """The endpoint that exists to report failures must not become one —
        and an operator seeing a 500 has no idea which provider caused it."""
        class Exploding:
            provider = "FR24"

            def health_check(self):
                raise RuntimeError("connection reset")

        client.app.state.connectors["FR24"] = Exploding()
        body = client.get("/v1/healthz?deep=true", headers=AUTH).json()
        assert body["status"] == "degraded"
        assert body["connectors"]["FR24"]["healthy"] is False
        assert "connection reset" in body["connectors"]["FR24"]["detail"]

    def test_a_connector_with_no_health_endpoint_is_not_a_degradation(
        self, client
    ):
        """`healthy` is tri-state: None means "no free endpoint to prove a
        credential with" — BaseConnector's default — and treating that as
        unhealthy would cry wolf on a system that is working."""
        class Silent:
            provider = "FR24"

            def health_check(self):
                return {"provider": "FR24", "healthy": None,
                        "detail": "no health endpoint implemented"}

        for name in list(client.app.state.connectors):
            client.app.state.connectors[name] = Silent()
        body = client.get("/v1/healthz?deep=true", headers=AUTH).json()
        assert body["status"] == "ok"
        assert all(report["healthy"] is None
                   for report in body["connectors"].values())


class TestBudgetLedger:
    def test_the_api_wires_connectors_to_the_budget_ledger(self, db,
                                                           app_config,
                                                           monkeypatch):
        """`api_calls` is the only record of spend and connectors are the only
        thing that writes it. Built without the hook they ledger zero forever,
        so every cap in services/budget.py reads a spend that never grows."""
        monkeypatch.setenv("SURGE_API_TOKEN", TOKEN)
        app_config["dry_run"] = True
        app = create_app(app_config, db=db)          # real registry, no network
        recorded = []
        for connector in app.state.connectors.values():
            recorded.append(connector._on_call)
        assert recorded and all(hook is not None for hook in recorded)
        assert all(hook.__self__ is app.state.budget for hook in recorded)

    def test_the_orchestrator_shares_that_guard(self, db, app_config,
                                                monkeypatch):
        """A second BudgetGuard would enforce caps against a ledger the
        connectors are not writing to."""
        monkeypatch.setenv("SURGE_API_TOKEN", TOKEN)
        app_config["dry_run"] = True
        app = create_app(app_config, db=db)
        assert app.state.build_orchestrator().budget is app.state.budget

    def test_an_unset_token_refuses_rather_than_disabling_the_check(
        self, db, app_config, monkeypatch
    ):
        monkeypatch.delenv("SURGE_API_TOKEN", raising=False)
        app = create_app(app_config, db=db, connectors=stub_connectors())
        with TestClient(app) as bare:
            assert bare.get("/v1/sessions/1", headers=AUTH).status_code == 503


# ===========================================================================
# Sessions
# ===========================================================================


class TestSessions:
    def test_init_resolves_the_geography_up_front(self, client):
        response = client.post("/v1/sessions", headers=AUTH, json={
            "label": "AZ", "cities": [PHOENIX]})
        assert response.status_code == 201
        city = response.json()["cities"][0]
        assert city["airports"] == ["PHX"]
        assert city["key_locations"] == ["Riverside Fairground"]

    def test_an_unmappable_city_warns_at_init_not_mid_iteration(self, client):
        """A SKIPPED_NO_MAPPING query is correct but arrives hours later and
        reads as a data gap rather than a setup mistake."""
        response = client.post("/v1/sessions", headers=AUTH, json={
            "cities": [{"name": "Nowheresville", "state": "ZZ",
                        "key_locations": [{"name": "Town Hall"}]}]})
        assert response.status_code == 201
        assert any("no airport mapping" in w
                   for w in response.json()["warnings"])

    def test_a_city_with_no_key_location_is_warned_about(self, client):
        response = client.post("/v1/sessions", headers=AUTH, json={
            "cities": [{"name": "Phoenix", "state": "AZ"}]})
        assert any("no key locations" in w for w in response.json()["warnings"])

    def test_an_empty_city_list_is_422(self, client):
        assert client.post("/v1/sessions", headers=AUTH,
                           json={"cities": []}).status_code == 422

    def test_an_unknown_track_is_422(self, client):
        assert client.post("/v1/sessions", headers=AUTH, json={
            "cities": [PHOENIX], "tracks": ["MARTIAN"]}).status_code == 422

    def test_cities_can_be_added_between_iterations(self, client):
        session_id = make_session(client)
        response = client.post(f"/v1/sessions/{session_id}/cities", headers=AUTH,
                               json={"cities": [{"name": "Tucson", "state": "AZ"}]})
        assert response.status_code == 201
        assert {c["name"] for c in response.json()["cities"]} == {
            "Phoenix", "Tucson"}

    def test_an_unknown_session_is_404(self, client):
        assert client.get("/v1/sessions/999", headers=AUTH).status_code == 404

    # -- 9.2 / issue #11 ---------------------------------------------------

    def test_a_supported_tunable_is_echoed_with_the_hash_it_produces(
        self, client
    ):
        """`config_hash` is the point. A client can compare it against the
        `config_hash` on any receipt and confirm the judgement was made under
        the settings it asked for — which is exactly what it could not do while
        tunables were stored and never read."""
        plain = client.post("/v1/sessions", headers=AUTH,
                            json={"cities": [PHOENIX]}).json()
        tuned = client.post("/v1/sessions", headers=AUTH, json={
            "cities": [PHOENIX],
            "tunables": {"correlation": {"window_hours": 48}}})
        assert tuned.status_code == 201, tuned.text
        body = tuned.json()
        assert body["tunables"] == {"correlation": {"window_hours": 48}}
        assert body["config_hash"] and body["config_hash"] != \
            plain["config_hash"]

    def test_the_hash_survives_a_re_read(self, client):
        session_id = client.post("/v1/sessions", headers=AUTH, json={
            "cities": [PHOENIX],
            "tunables": {"triage": {"batch_size": 4}}}).json()["session_id"]
        body = client.get(f"/v1/sessions/{session_id}", headers=AUTH).json()
        assert body["tunables"] == {"triage": {"batch_size": 4}}
        assert body["config_hash"]

    def test_a_server_owned_tunable_is_422_naming_the_field(self, client):
        response = client.post("/v1/sessions", headers=AUTH, json={
            "cities": [PHOENIX],
            "tunables": {"staying": {"retention_days": 3650}}})
        assert response.status_code == 422
        assert "staying" in response.json()["detail"]

    def test_an_unknown_tunable_is_refused_rather_than_ignored(self, client):
        """Accepting it and doing nothing is the defect, not the fallback."""
        response = client.post("/v1/sessions", headers=AUTH, json={
            "cities": [PHOENIX], "tunables": {"triage": {"max_post_age": 24}}})
        assert response.status_code == 422
        assert "max_post_age_hours" in response.json()["detail"]

    def test_a_raised_spending_cap_is_refused(self, client):
        response = client.post("/v1/sessions", headers=AUTH, json={
            "cities": [PHOENIX],
            "tunables": {"budget": {"monthly_limit": {"FR24": 1e9}}}})
        assert response.status_code == 422
        assert "never raise" in response.json()["detail"]

    def test_nothing_is_created_when_a_tunable_is_refused(self, client, db):
        """Validation runs before the session row is written. A refused
        request that still left a half-built session would be worse than the
        silence it replaced."""
        before = db.scalar("SELECT COUNT(*) FROM sessions")
        client.post("/v1/sessions", headers=AUTH, json={
            "cities": [PHOENIX], "tunables": {"dry_run": True}})
        assert db.scalar("SELECT COUNT(*) FROM sessions") == before


# ===========================================================================
# The round trip — the Phase 6 gate
# ===========================================================================


class TestRoundTrip:
    def test_init_trigger_poll_alerts(self, client):
        session_id = make_session(client)
        iteration_id = run_iteration(client, session_id)

        poll = client.get(f"/v1/iterations/{iteration_id}", headers=AUTH).json()
        assert poll["outcome"] in ("COMPLETE", "PARTIAL")
        assert poll["stage"] == "COMPLETE"
        assert poll["counts"]["queries_executed"] > 0
        assert poll["counts"]["alerts"] >= 1
        assert poll["running"] is False

        alerts = client.get(f"/v1/sessions/{session_id}/alerts",
                            headers=AUTH).json()
        assert alerts
        alert = alerts[0]
        assert alert["city"] == "Phoenix, AZ"
        assert alert["confidence"]["band"] in ("LOW", "MEDIUM", "HIGH")
        assert alert["summary"]
        assert alert["evidence_url"] == f"/v1/alerts/{alert['alert_id']}/evidence"

    def test_the_trigger_returns_202_promptly(self, client):
        """The gate: under 500 ms. An iteration takes minutes, so the trigger
        cannot be the thing that waits for it."""
        session_id = make_session(client)
        started = time.monotonic()
        response = client.post(f"/v1/sessions/{session_id}/iterations",
                               headers=AUTH)
        elapsed_ms = (time.monotonic() - started) * 1000

        assert response.status_code == 202
        assert elapsed_ms < 500, f"trigger took {elapsed_ms:.0f} ms"
        body = response.json()
        assert body["status"] == "RUNNING"
        assert body["poll_url"] == f"/v1/iterations/{body['iteration_id']}"

    def test_a_second_trigger_on_one_session_is_409(self, client, db):
        """Two concurrent iterations would seed the same queries, race the
        dedup index, and split one city's evidence across two windows."""
        session_id = make_session(client)
        release = threading.Event()

        # Hold the first iteration inside its first stage so the second
        # trigger arrives while the lock is genuinely held.
        original = T.StubSocial.search

        def blocking_search(self, endpoint, params, **kwargs):
            release.wait(timeout=5)
            return original(self, endpoint, params, **kwargs)

        T.StubSocial.search = blocking_search
        try:
            first = client.post(f"/v1/sessions/{session_id}/iterations",
                                headers=AUTH)
            assert first.status_code == 202
            second = client.post(f"/v1/sessions/{session_id}/iterations",
                                 headers=AUTH)
            assert second.status_code == 409
            assert str(first.json()["iteration_id"]) in second.json()["detail"]
        finally:
            T.StubSocial.search = original
            release.set()

    def test_different_sessions_do_not_block_each_other(self, client):
        one, two = make_session(client), make_session(
            client, cities=[{"name": "Tucson", "state": "AZ",
                             "key_locations": [{"name": "Desert Speedway"}]}])
        assert client.post(f"/v1/sessions/{one}/iterations?wait=true",
                           headers=AUTH).status_code in (200, 202)
        assert client.post(f"/v1/sessions/{two}/iterations?wait=true",
                           headers=AUTH).status_code in (200, 202)

    def test_a_session_with_no_cities_is_422_not_500(self, client, db):
        session_id = db.insert_session(label="empty")
        response = client.post(f"/v1/sessions/{session_id}/iterations",
                               headers=AUTH)
        assert response.status_code == 422
        assert "no cities" in response.json()["detail"]


# ===========================================================================
# Alerts and evidence
# ===========================================================================


class TestAlerts:
    @pytest.fixture
    def alerted(self, client):
        session_id = make_session(client)
        iteration_id = run_iteration(client, session_id)
        alerts = client.get(f"/v1/sessions/{session_id}/alerts",
                            headers=AUTH).json()
        assert alerts
        return session_id, iteration_id, alerts[0]

    def test_tuple_format_returns_four_positional_arrays(self, client, alerted):
        session_id, _iteration_id, _alert = alerted
        response = client.get(f"/v1/sessions/{session_id}/alerts?format=tuple",
                              headers=AUTH)
        assert response.status_code == 200
        for row in response.json():
            assert len(row["evidence"]) == 4
            assert all(isinstance(group, list) for group in row["evidence"])
            # The summary and the score stay addressable rather than becoming
            # positions five and six of an array.
            assert row["summary"]
            assert set(row["confidence"]) == {"score", "band"}

    def test_the_named_and_tuple_forms_carry_the_same_evidence(
        self, client, alerted
    ):
        session_id, _iteration_id, _alert = alerted
        named = client.get(f"/v1/sessions/{session_id}/alerts",
                           headers=AUTH).json()[0]
        positional = client.get(f"/v1/sessions/{session_id}/alerts?format=tuple",
                                headers=AUTH).json()[0]
        assert [len(g) for g in positional["evidence"]] == [
            len(named["social_posts"]), len(named["flights"]),
            len(named["lodging"]), len(named["rental_cars"])]

    def test_evidence_resolves_every_signal_to_a_query(self, client, alerted):
        """The property that makes a score arguable: no analytical record
        without provenance."""
        _session_id, _iteration_id, alert = alerted
        evidence = client.get(f"/v1/alerts/{alert['alert_id']}/evidence",
                              headers=AUTH).json()
        assert evidence["rule_trace"]
        assert evidence["contributions"]
        assert evidence["signals"]
        for item in evidence["signals"]:
            assert item["query"] is not None, item["signal"]["signal_type"]
            assert item["query"]["endpoint"]
            assert item["raw"] is not None

    def test_evidence_counts_how_directly_it_is_known(self, client, alerted):
        """9.4. Per-signal provenance already existed and was not comparable;
        the summary is what stops an analyst having to read eight signal
        records to notice that three came from a vendor's cache."""
        _session_id, _iteration_id, alert = alerted
        evidence = client.get(f"/v1/alerts/{alert['alert_id']}/evidence",
                              headers=AUTH).json()
        assert evidence["collection"] == {
            "INTERMEDIARY_LIVE": len(evidence["signals"])}
        assert "DIRECT" not in evidence["collection"], (
            "every provider here is an intermediary; claiming otherwise would "
            "be the one thing this field must never do")
        for item in evidence["signals"]:
            assert item["signal"]["collection_basis"]

    def test_the_score_in_the_alert_equals_the_correlation(
        self, client, alerted, db
    ):
        _session_id, _iteration_id, alert = alerted
        evidence = client.get(f"/v1/alerts/{alert['alert_id']}/evidence",
                              headers=AUTH).json()
        row = db.one("SELECT * FROM correlations WHERE correlation_id = "
                     "(SELECT correlation_id FROM alerts WHERE alert_id = ?)",
                     (alert["alert_id"],))
        assert evidence["confidence"]["score"] == row["score"]
        assert alert["confidence"]["score"] == row["score"]

    def test_filters_narrow_rather_than_error(self, client, alerted):
        session_id, iteration_id, _alert = alerted
        assert client.get(f"/v1/sessions/{session_id}/alerts?min_confidence=HIGH",
                          headers=AUTH).status_code == 200
        assert client.get(f"/v1/sessions/{session_id}/alerts?city=Nowhere",
                          headers=AUTH).json() == []
        by_iteration = client.get(
            f"/v1/sessions/{session_id}/alerts?iteration_id={iteration_id}",
            headers=AUTH).json()
        assert by_iteration

    def test_a_malformed_since_is_422_not_a_silent_empty_list(self, client,
                                                              alerted):
        session_id, _iteration_id, _alert = alerted
        assert client.get(f"/v1/sessions/{session_id}/alerts?since=yesterday",
                          headers=AUTH).status_code == 422

    def test_an_unknown_alert_is_404(self, client):
        assert client.get("/v1/alerts/999/evidence",
                          headers=AUTH).status_code == 404

    def test_an_alerted_correlation_says_so_and_carries_its_id(
        self, client, alerted
    ):
        """The alert route now resolves its correlation and delegates, so the
        two surfaces cannot drift — and the decision is stated rather than
        implied by the alert's existence."""
        _session_id, _iteration_id, alert = alerted
        evidence = client.get(f"/v1/alerts/{alert['alert_id']}/evidence",
                              headers=AUTH).json()
        assert evidence["alert_id"] == alert["alert_id"]
        assert evidence["correlation_id"] > 0
        assert evidence["alert_decision"] == "ALERTED"
        assert "alert_min_score" in evidence["alert_decision_reason"]
        assert evidence["summary"]


# ===========================================================================
# 8.7(b) — a correlation that becomes no alert
#
# Every route into the evidence surface used to resolve `alerts.correlation_id`
# first, so a correlation below `correlation.alert_min_score` was reachable from
# nowhere: not from the alerts listing (it has no alert), not from the
# CORRELATING stage view (counts only), and not from the ALERTING log (an
# aggregate `below_floor: N` that names none of them). It was visible only by
# opening SQLite.
#
# Those rows are the near misses, and the near misses are what the interim
# floors in `correlation` and `sensitivity` are supposed to be calibrated from.
# ===========================================================================


class TestTypedTriageOutcomes:
    """Review #8, HIGH (interoperability).

    The database has carried five durable triage states since Phase 7, and the
    operational contract exposed only `counts.triage_decisions` — a total. A
    server-to-server client could not tell a REJECTED item (judged, and not
    relevant) from an UNDECIDED, INVALID_OUTPUT or MODEL_ERROR one (collected,
    paid for, never judged, and therefore a coverage gap) without parsing
    free-text `degradations`.
    """

    def test_the_poll_response_breaks_decisions_down_by_state(self, client):
        session_id = make_session(client)
        client.post(f"/v1/sessions/{session_id}/iterations?wait=true",
                    headers=AUTH)
        iteration = client.get(f"/v1/sessions/{session_id}/iterations",
                               headers=AUTH).json()[0]["iteration_id"]
        body = client.get(f"/v1/iterations/{iteration}", headers=AUTH).json()

        assert body["triage_states"], "no typed outcomes were reported"
        assert set(body["triage_states"]) <= {
            "ACCEPTED", "REJECTED", "UNDECIDED", "INVALID_OUTPUT",
            "MODEL_ERROR"}
        assert sum(body["triage_states"].values()) == \
            body["counts"]["triage_decisions"], (
                "the typed breakdown must account for every decision the "
                "total claims")

    def test_a_gap_is_distinguishable_from_a_rejection(self, db, client):
        """The distinction the field exists for, asserted on rows rather than
        on a run: both are 'not a signal', and only one is a coverage gap."""
        session_id = make_session(client)
        iteration = db.insert_iteration(session_id)
        for state in ("REJECTED", "MODEL_ERROR", "MODEL_ERROR"):
            db.insert_triage_decision(
                iteration_id=iteration, raw_id=None, state=state,
                rationale="x", model="stub", url=f"https://x.com/{state}"
                f"{db.scalar('SELECT COUNT(*) FROM triage_decisions')}",
                track="AIRSHOW", cities=[], salience=0.1, signal_id=None)
        body = client.get(f"/v1/iterations/{iteration}", headers=AUTH).json()
        assert body["triage_states"] == {"REJECTED": 1, "MODEL_ERROR": 2}


class TestSubThresholdCorrelations:
    @pytest.fixture
    def unalerted(self, client, app_config, db):
        """An iteration whose correlations all score below the floor."""
        app_config["correlation"]["alert_min_score"] = 0.99
        session_id = make_session(client)
        iteration_id = run_iteration(client, session_id)
        assert client.get(f"/v1/sessions/{session_id}/alerts",
                          headers=AUTH).json() == [], "the premise: no alerts"
        rows = db.get_correlations(iteration_id)
        assert rows, "but the iteration DID correlate"
        return session_id, iteration_id, rows

    def test_the_drop_reason_is_recorded_not_inferable(self, unalerted, db):
        """`score` versus a config value is a reconstruction, and only for a
        reader who already knows that is the rule. AlertAgent knows it at the
        moment it decides."""
        _session_id, _iteration_id, rows = unalerted
        for row in rows:
            fresh = db.get_correlation(int(row["correlation_id"]))
            assert fresh["alert_decision"] in ("BELOW_FLOOR", "BAND_NONE")
            reason = fresh["alert_decision_reason"]
            assert reason
            if fresh["alert_decision"] == "BELOW_FLOOR":
                assert "alert_min_score" in reason
                assert f"{float(fresh['score']):.3f}" in reason

    def test_the_iteration_lists_them(self, client, unalerted):
        _session_id, iteration_id, rows = unalerted
        body = client.get(f"/v1/iterations/{iteration_id}/correlations",
                          headers=AUTH).json()
        assert len(body["correlations"]) == len(rows)
        assert body["counts"]
        assert all(c["alerted"] is False for c in body["correlations"])
        for correlation in body["correlations"]:
            assert correlation["evidence_url"].endswith("/evidence")
            assert correlation["rule_trace"]
            assert correlation["alert_decision"]

    def test_the_evidence_drill_down_works_without_an_alert(self, client,
                                                            unalerted):
        """The whole point. Same shape as the alert route, minus the fields
        that genuinely do not exist."""
        _session_id, iteration_id, rows = unalerted
        correlation_id = int(rows[0]["correlation_id"])
        evidence = client.get(f"/v1/correlations/{correlation_id}/evidence",
                              headers=AUTH).json()

        assert evidence["correlation_id"] == correlation_id
        assert evidence["alert_id"] is None
        assert evidence["signals"], "the evidence itself is what was missing"
        assert evidence["rule_trace"]
        assert evidence["contributions"]
        assert evidence["confidence"]["score"] == rows[0]["score"]
        assert evidence["city"]
        for item in evidence["signals"]:
            assert item["query"] is not None
            assert item["contribution"] is not None

    def test_it_invents_no_summary(self, client, unalerted):
        """No model was asked to write one. A sentence here would present a
        judgement nobody made — the same reason a fallback alert carries a null
        receipt."""
        _session_id, _iteration_id, rows = unalerted
        evidence = client.get(
            f"/v1/correlations/{int(rows[0]['correlation_id'])}/evidence",
            headers=AUTH).json()
        assert evidence["summary"] is None
        assert evidence["caveat"] is None
        assert evidence["receipt"] is None

    def test_the_correlating_stage_shows_what_it_scored(self, client, unalerted):
        """The placement the report asked for. `wrote: {correlations: 2}` was
        the entire account of the stage whose output is the conclusion."""
        _session_id, iteration_id, rows = unalerted
        stage = client.get(f"/v1/iterations/{iteration_id}/stages/CORRELATING",
                           headers=AUTH).json()
        assert len(stage["correlations"]) == len(rows)
        entry = stage["correlations"][0]
        assert entry["rule_trace"]
        assert entry["score"] is not None
        assert entry["evidence_url"].endswith("/evidence")

    def test_other_stages_carry_no_correlations(self, client, unalerted):
        _session_id, iteration_id, _rows = unalerted
        stage = client.get(f"/v1/iterations/{iteration_id}/stages/TRIAGING",
                           headers=AUTH).json()
        assert stage["correlations"] == []

    def test_an_unknown_correlation_is_404(self, client):
        assert client.get("/v1/correlations/999/evidence",
                          headers=AUTH).status_code == 404

    def test_a_band_NONE_correlation_is_returned_not_a_500(self, client, db):
        """The case these routes exist for, and the one they used to crash on.

        `NONE` is a real computed outcome — no band rule fired — and is stored
        so that finding nothing is on the record. The response model reused the
        ALERT's band type, where NONE is impossible by construction, so the
        listing 500'd. Every other test missed it because they raise
        `alert_min_score` to force BELOW_FLOOR, which leaves a real band.
        """
        session_id = make_session(client)
        iteration_id = run_iteration(client, session_id)
        db._exec("UPDATE correlations SET band = 'NONE', score = 0.0 "
                 "WHERE iteration_id = ?", (iteration_id,))

        listing = client.get(f"/v1/iterations/{iteration_id}/correlations",
                             headers=AUTH)
        assert listing.status_code == 200, listing.text
        rows = listing.json()["correlations"]
        assert rows and all(c["confidence"]["band"] == "NONE" for c in rows)

        evidence = client.get(
            f"/v1/correlations/{rows[0]['correlation_id']}/evidence",
            headers=AUTH)
        assert evidence.status_code == 200, evidence.text
        assert evidence.json()["confidence"]["band"] == "NONE"

    def test_an_alert_can_still_never_be_band_NONE(self):
        """The alert contract keeps the narrow type. Widening `Confidence`
        instead of adding a second model would have removed a guarantee that
        holds by construction."""
        import pydantic

        from surge_iw.api import schemas
        assert schemas.CorrelationConfidence(score=0.0, band="NONE")
        with pytest.raises(pydantic.ValidationError):
            schemas.Confidence(score=0.0, band="NONE")

    def test_it_is_operational_not_debug(self, db, app_config, monkeypatch):
        """An analyst calibrating floors on a deployment serving an
        operations team must not have to mount the endpoint that deletes
        analytical records."""
        monkeypatch.setenv("SURGE_API_TOKEN", TOKEN)
        app_config["api"]["debug_endpoints"] = False
        app = create_app(app_config, db=db, connectors=stub_connectors())
        with TestClient(app) as locked:
            paths = locked.get("/openapi.json").json()["paths"]
            assert "/v1/correlations/{correlation_id}/evidence" in paths
            assert "/v1/iterations/{iteration_id}/correlations" in paths
            assert not any("discard" in path for path in paths)

    def test_a_correlation_alerting_has_not_reached_says_so(self, client, db):
        """Null is not a decision. An iteration stepped only as far as
        CORRELATING has genuinely decided nothing, and reporting that as
        'below the floor' would be a conclusion nobody reached."""
        session_id = make_session(client)
        iteration_id = client.post(
            f"/v1/sessions/{session_id}/iterations", headers=AUTH,
            json={"mode": "manual"}).json()["iteration_id"]
        for _ in range(6):                      # SEEDING .. CORRELATING
            client.post(f"/v1/iterations/{iteration_id}/step", headers=AUTH)

        body = client.get(f"/v1/iterations/{iteration_id}/correlations",
                          headers=AUTH).json()
        assert body["correlations"], "CORRELATING ran"
        assert all(c["alert_decision"] is None for c in body["correlations"])
        assert body["counts"] == {"NOT_DECIDED": len(body["correlations"])}


# ===========================================================================
# 8.8 — re-triage over the API
# ===========================================================================


class TestRetryTriageRoute:
    def lose_a_batch(self, client, db):
        """A finished iteration whose triage batch was lost to truncation."""
        session_id = make_session(client)
        client.app.state.llm_client = _TruncatingClient()
        iteration_id = run_iteration(client, session_id)
        client.app.state.llm_client = _healthy_llm()
        assert db.uncovered_triage_decisions(iteration_id), "the premise"
        return session_id, iteration_id

    def test_it_creates_a_child_and_leaves_the_parent_alone(self, client, db):
        session_id, parent = self.lose_a_batch(client, db)
        before = dict(db.get_iteration(parent))

        response = client.post(f"/v1/iterations/{parent}/retry-triage?wait=true",
                               headers=AUTH)
        assert response.status_code in (200, 202), response.text
        body = response.json()
        child = body["iteration_id"]

        assert child != parent
        assert body["retry_of_iteration_id"] == parent
        assert dict(db.get_iteration(parent)) == before
        assert db.get_iteration(child)["retry_of_iteration_id"] == parent

    def test_the_child_reports_its_parent(self, client, db):
        session_id, parent = self.lose_a_batch(client, db)
        child = client.post(f"/v1/iterations/{parent}/retry-triage?wait=true",
                            headers=AUTH).json()["iteration_id"]
        body = client.get(f"/v1/iterations/{child}", headers=AUTH).json()
        assert body["retry_of_iteration_id"] == parent

    def test_an_open_parent_is_a_409(self, client, db):
        """A retry is a new iteration, and a session runs one at a time."""
        session_id = make_session(client)
        parent = client.post(f"/v1/sessions/{session_id}/iterations",
                             headers=AUTH,
                             json={"mode": "manual"}).json()["iteration_id"]
        response = client.post(f"/v1/iterations/{parent}/retry-triage",
                               headers=AUTH)
        assert response.status_code == 409
        assert "has not closed" in response.json()["detail"]

    def test_nothing_to_retry_is_a_422(self, client):
        """Refused rather than answered with an empty iteration: a run that
        judged nothing and closed COMPLETE looks like one that worked."""
        session_id = make_session(client)
        iteration_id = run_iteration(client, session_id)
        response = client.post(f"/v1/iterations/{iteration_id}/retry-triage",
                               headers=AUTH)
        assert response.status_code == 422
        assert "no unjudged posts" in response.json()["detail"]

    def test_a_refusal_creates_no_iteration(self, client, db):
        session_id = make_session(client)
        iteration_id = run_iteration(client, session_id)
        before = db.scalar("SELECT COUNT(*) FROM iterations")
        client.post(f"/v1/iterations/{iteration_id}/retry-triage", headers=AUTH)
        assert db.scalar("SELECT COUNT(*) FROM iterations") == before, (
            "a refusal must not leave a hole in seq or an empty iteration")

    def test_an_unknown_iteration_is_404(self, client):
        assert client.post("/v1/iterations/999/retry-triage",
                           headers=AUTH).status_code == 404

    def test_it_is_operational_not_debug(self, db, app_config, monkeypatch):
        """Recovering lost judgements is not a debugging aid."""
        monkeypatch.setenv("SURGE_API_TOKEN", TOKEN)
        app_config["api"]["debug_endpoints"] = False
        app = create_app(app_config, db=db, connectors=stub_connectors())
        with TestClient(app) as locked:
            paths = locked.get("/openapi.json").json()["paths"]
            assert "/v1/iterations/{iteration_id}/retry-triage" in paths

    def test_a_repeated_key_replays_instead_of_retrying_again(self, client, db):
        """8.2's rule, and this endpoint needs it MORE than the trigger does.

        The parent's MODEL_ERROR rows are deliberately never edited, so a
        successful retry leaves the candidate set unchanged — a client that
        lost the response and retried would create another child and spend
        again, and could keep doing so indefinitely.
        """
        _session_id, parent = self.lose_a_batch(client, db)
        headers = {**AUTH, "Idempotency-Key": "retry-demo-key-01"}

        first = client.post(f"/v1/iterations/{parent}/retry-triage?wait=true",
                            headers=headers)
        assert first.headers.get(contract.IDEMPOTENCY_HEADER)
        repeat = client.post(f"/v1/iterations/{parent}/retry-triage?wait=true",
                             headers=headers)

        assert repeat.json()["iteration_id"] == first.json()["iteration_id"]
        assert repeat.headers.get("Idempotent-Replay") == "true"
        assert db.scalar(
            "SELECT COUNT(*) FROM iterations WHERE retry_of_iteration_id = ?",
            (parent,)) == 1, "exactly one child, not one per attempt"

    def test_a_key_reused_for_a_different_request_is_refused(self, client, db):
        """Replaying the old response would let a caller believe it started a
        retry with its new batch size."""
        _session_id, parent = self.lose_a_batch(client, db)
        headers = {**AUTH, "Idempotency-Key": "retry-demo-key-02"}
        client.post(f"/v1/iterations/{parent}/retry-triage?wait=true",
                    headers=headers, json={"batch_size": 4})
        clash = client.post(f"/v1/iterations/{parent}/retry-triage?wait=true",
                            headers=headers, json={"batch_size": 1})
        assert clash.status_code == 422

    def test_the_open_iteration_409_carries_no_retry_after(self, client, db):
        """Same rule as the trigger's: waiting never clears it."""
        session_id, parent = self.lose_a_batch(client, db)
        client.post(f"/v1/sessions/{session_id}/iterations", headers=AUTH,
                    json={"mode": "manual"})
        response = client.post(f"/v1/iterations/{parent}/retry-triage",
                               headers=AUTH)
        assert response.status_code == 409
        assert "retry-after" not in {k.lower() for k in response.headers}

    def test_cancellation_reaches_a_retry_child(self, client, db):
        """A retry issues paid tipped collection, so it is exactly as
        cancellable as an ordinary run. A contract that held for one path and
        not the other would be worse than one that held for neither."""
        _session_id, parent = self.lose_a_batch(client, db)
        child = client.post(f"/v1/iterations/{parent}/retry-triage",
                            headers=AUTH).json()["iteration_id"]
        client.post(f"/v1/iterations/{child}/cancel", headers=AUTH,
                    json={"reason": "audit"})
        # However the race lands, the run must not report a clean full pass.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = db.get_iteration(child)
            if row["finished_at"]:
                break
            time.sleep(0.05)
        assert db.get_iteration(child)["outcome"] in ("PARTIAL", "FAILED")


def _healthy_llm():
    return FakeLLM(*[[T.decision("https://x.com/1", "Phoenix"),
                      T.decision("https://apnews.com/2", "Phoenix")]] * 20)


class _TruncatingClient:
    """Every reply overruns `llm.max_tokens`, so the whole batch is lost."""

    def __init__(self):
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        class _Msg:
            content = '[{"item_id": "trun'

        class _Choice:
            message = _Msg()
            finish_reason = "length"

        class _Resp:
            choices = [_Choice()]
            usage = None
        return _Resp()


# ===========================================================================
# 8.7(c) — creating a session from an input file
# ===========================================================================


class TestSessionFromAnInputSet:
    @pytest.fixture
    def input_dir(self, tmp_path, client):
        (tmp_path / "venues.yaml").write_text(
            "Phoenix, AZ:\n"
            "  - name: Riverside Fairground\n"
            "    location_type: FAIRGROUND\n"
            "Chicago, IL:\n"
            "  - Northside Exhibition Center\n",
            encoding="utf-8")
        # On app.state.config, which is what the route reads. Setting it on the
        # config fixture would be too late: `client` builds the app first.
        client.app.state.config["inputs"] = {"dir": str(tmp_path)}
        return tmp_path

    def test_it_creates_the_session_and_echoes_what_resolved(self, client,
                                                             input_dir):
        """An operator must be able to see what they created, not trust that a
        file still says what they remember."""
        response = client.post("/v1/sessions", headers=AUTH, json={
            "label": "from file", "input_set": "venues",
            "tracks": ["AIRSHOW"]})
        assert response.status_code == 201, response.text
        body = response.json()

        names = {c["name"] for c in body["cities"]}
        assert names == {"Phoenix", "Chicago"}
        for city in body["cities"]:
            assert city["airports"], city["name"]
            assert city["pickup_location"], city["name"]
            assert city["key_locations"]
        assert any("venues" in w for w in body["warnings"])

    def test_an_unresolvable_city_refuses_the_whole_request(self, client,
                                                            input_dir):
        """422 naming the city, not a session missing one. A city dropped
        here would produce no query, no refusal and no warning — its absence
        would be indistinguishable from finding nothing there."""
        (input_dir / "bad.yaml").write_text(
            "Phoenix, AZ:\n  - X\nNowheresville, ZZ:\n  - Y\n", encoding="utf-8")
        response = client.post("/v1/sessions", headers=AUTH,
                               json={"input_set": "bad"})
        assert response.status_code == 422
        assert "Nowheresville" in response.json()["detail"]

    def test_nothing_is_written_when_the_file_is_refused(self, client, db,
                                                          input_dir):
        (input_dir / "bad.yaml").write_text(
            "Nowheresville, ZZ:\n  - Y\n", encoding="utf-8")
        before = db.scalar("SELECT COUNT(*) FROM sessions")
        client.post("/v1/sessions", headers=AUTH, json={"input_set": "bad"})
        assert db.scalar("SELECT COUNT(*) FROM sessions") == before

    def test_a_city_with_no_locations_is_reported_not_refused(self, client,
                                                              input_dir):
        (input_dir / "bare.yaml").write_text("Phoenix, AZ:\n", encoding="utf-8")
        body = client.post("/v1/sessions", headers=AUTH,
                           json={"input_set": "bare"}).json()
        assert body["cities"]
        assert any("lodging family will be absent" in w
                   for w in body["warnings"])

    @pytest.mark.parametrize("name", [
        "../../etc/passwd", "/etc/passwd", "sub/dir", "..",
    ])
    def test_a_path_is_refused(self, client, input_dir, name):
        """The field takes a NAME. Authenticated is not the same as trusted
        with the filesystem, and a front end is not the operator at a shell."""
        response = client.post("/v1/sessions", headers=AUTH,
                               json={"input_set": name})
        assert response.status_code == 422
        assert "not a valid input set name" in response.json()["detail"]

    def test_an_unknown_set_says_what_is_available(self, client, input_dir):
        response = client.post("/v1/sessions", headers=AUTH,
                               json={"input_set": "absent"})
        assert response.status_code == 422
        assert "venues" in response.json()["detail"]

    def test_both_sources_is_refused(self, client, input_dir):
        """Two answers to one question. Merging them silently is how a session
        ends up with a city nobody asked for."""
        response = client.post("/v1/sessions", headers=AUTH, json={
            "cities": [PHOENIX], "input_set": "venues"})
        assert response.status_code == 422
        assert "not both" in json.dumps(response.json())

    def test_neither_source_is_refused(self, client):
        response = client.post("/v1/sessions", headers=AUTH, json={})
        assert response.status_code == 422

    def test_the_inline_path_still_works_unchanged(self, client):
        assert client.post("/v1/sessions", headers=AUTH, json={
            "cities": [PHOENIX]}).status_code == 201


# ===========================================================================
# Queue visibility
# ===========================================================================


class TestQueue:
    def test_refusals_are_surfaced_with_their_reasons(self, client):
        session_id = make_session(client)
        iteration_id = run_iteration(client, session_id)
        # A second iteration refuses the identical queries on cooldown.
        run_iteration(client, session_id)

        queue = client.get(f"/v1/sessions/{session_id}/queue"
                           f"?iteration_id={iteration_id}", headers=AUTH).json()
        assert queue["status_counts"]
        assert queue["decision_counts"]["ENQUEUED"] > 0
        assert all("params" in q for q in queue["queries"])
        assert any(d["outcome"] == "ENQUEUED" for d in queue["decisions"])

    def test_every_decision_names_the_stage_that_made_it(self, client):
        """rule_code cannot answer this — the flight escalation re-uses
        R4_LODGING and R5_CAR, so two stages emit the same code."""
        session_id = make_session(client)
        iteration_id = run_iteration(client, session_id)
        queue = client.get(f"/v1/sessions/{session_id}/queue"
                           f"?iteration_id={iteration_id}", headers=AUTH).json()
        assert queue["decisions"]
        for decision in queue["decisions"]:
            assert decision["stage"] in PIPELINE_STAGES, decision


# ===========================================================================
# Debug: stepping
# ===========================================================================


class TestStepping:
    def test_a_manual_iteration_walks_one_stage_at_a_time(self, client):
        session_id = make_session(client)
        created = client.post(f"/v1/sessions/{session_id}/iterations",
                              headers=AUTH, json={"mode": "manual"})
        assert created.status_code == 202
        body = created.json()
        assert body["status"] == "PENDING"
        assert body["next_stage"] == "SEEDING"
        iteration_id = body["iteration_id"]

        walked = []
        while True:
            response = client.post(f"/v1/iterations/{iteration_id}/step",
                                   headers=AUTH)
            assert response.status_code == 200, response.text
            step = response.json()
            walked.append(step["stage"])
            assert step["ok"], step["report"]["error_message"]
            if step["next_stage"] is None:
                assert step["outcome"] in ("COMPLETE", "PARTIAL")
                break

        assert tuple(walked) == PIPELINE_STAGES
        alerts = client.get(f"/v1/sessions/{session_id}/alerts",
                            headers=AUTH).json()
        assert alerts, "a stepped iteration must produce the same alerts"

    def test_stepping_past_the_end_is_409(self, client):
        session_id = make_session(client)
        iteration_id = run_iteration(client, session_id)
        response = client.post(f"/v1/iterations/{iteration_id}/step",
                               headers=AUTH)
        assert response.status_code == 409
        assert "no next stage" in response.json()["detail"]

    def test_the_expect_guard_refuses_the_wrong_stage(self, client):
        """A client that lost track of the pointer must not silently run the
        wrong stage and spend money doing it."""
        session_id = make_session(client)
        iteration_id = client.post(
            f"/v1/sessions/{session_id}/iterations", headers=AUTH,
            json={"mode": "manual"}).json()["iteration_id"]
        response = client.post(f"/v1/iterations/{iteration_id}/step",
                               headers=AUTH, json={"expect": "ALERTING"})
        assert response.status_code == 409
        assert "SEEDING" in response.json()["detail"]

        ok = client.post(f"/v1/iterations/{iteration_id}/step", headers=AUTH,
                         json={"expect": "SEEDING"})
        assert ok.status_code == 200

    def test_an_invalid_stage_name_is_422(self, client):
        session_id = make_session(client)
        iteration_id = client.post(
            f"/v1/sessions/{session_id}/iterations", headers=AUTH,
            json={"mode": "manual"}).json()["iteration_id"]
        assert client.post(f"/v1/iterations/{iteration_id}/step", headers=AUTH,
                           json={"expect": "MIDDLE"}).status_code == 422

    def test_stepping_and_running_reach_the_same_place(self, client, db):
        """Half stepped, half driven — the pointer is the only state either
        entry point reads, so they must compose."""
        session_id = make_session(client)
        iteration_id = client.post(
            f"/v1/sessions/{session_id}/iterations", headers=AUTH,
            json={"mode": "manual"}).json()["iteration_id"]
        for _ in range(3):
            client.post(f"/v1/iterations/{iteration_id}/step", headers=AUTH)

        orchestrator = client.app.state.build_orchestrator()
        assert orchestrator.run(iteration_id) in ("COMPLETE", "PARTIAL")
        assert db.get_iteration(iteration_id)["stage"] == "COMPLETE"
        assert db.get_alerts(session_id)


# ===========================================================================
# Debug: verifying a stage
# ===========================================================================


class TestStageReports:
    @pytest.fixture
    def stepped(self, client):
        session_id = make_session(client)
        iteration_id = run_iteration(client, session_id)
        return session_id, iteration_id

    def test_all_eight_stages_are_reported(self, client, stepped):
        _session_id, iteration_id = stepped
        body = client.get(f"/v1/iterations/{iteration_id}/stages",
                          headers=AUTH).json()
        assert [s["stage"] for s in body["stages"]] == list(PIPELINE_STAGES)
        assert all(s["status"] == "COMPLETE" for s in body["stages"])

    def test_a_stage_reports_what_it_wrote(self, client, stepped):
        _session_id, iteration_id = stepped
        seeding = client.get(f"/v1/iterations/{iteration_id}/stages/SEEDING",
                             headers=AUTH).json()
        assert seeding["wrote"]["query_queue"] > 0
        assert seeding["decisions"]["ENQUEUED"] > 0

        triaging = client.get(f"/v1/iterations/{iteration_id}/stages/TRIAGING",
                              headers=AUTH).json()
        assert triaging["wrote"]["triage_decisions"] > 0
        assert triaging["wrote"]["signals"] > 0
        assert any(a["agent"] == "TriageAgent" for a in triaging["agents"])

        alerting = client.get(f"/v1/iterations/{iteration_id}/stages/ALERTING",
                              headers=AUTH).json()
        assert alerting["wrote"]["alerts"] >= 1
        assert alerting["log"]

    def test_a_stage_that_never_ran_says_so(self, client):
        session_id = make_session(client)
        iteration_id = client.post(
            f"/v1/sessions/{session_id}/iterations", headers=AUTH,
            json={"mode": "manual"}).json()["iteration_id"]
        body = client.get(f"/v1/iterations/{iteration_id}/stages",
                          headers=AUTH).json()
        assert all(s["status"] == "PENDING" for s in body["stages"])
        assert body["next_stage"] == "SEEDING"

    def test_the_counts_reconcile_with_the_iteration_totals(self, client,
                                                            stepped):
        _session_id, iteration_id = stepped
        stages = client.get(f"/v1/iterations/{iteration_id}/stages",
                            headers=AUTH).json()["stages"]
        by_stage = {s["stage"]: s for s in stages}
        totals = client.get(f"/v1/iterations/{iteration_id}",
                            headers=AUTH).json()["counts"]

        raw = sum(by_stage[s]["wrote"].get("raw_results", 0)
                  for s in ("COLLECTING_SOCIAL", "COLLECTING_TIPPED"))
        assert raw == totals["raw_results"]
        signals = sum(by_stage[s]["wrote"].get("signals", 0)
                      for s in ("TRIAGING", "COLLECTING_TIPPED"))
        assert signals == totals["signals"]

    def test_an_unknown_stage_is_422(self, client, stepped):
        _session_id, iteration_id = stepped
        assert client.get(f"/v1/iterations/{iteration_id}/stages/HALFWAY",
                          headers=AUTH).status_code == 422


# ===========================================================================
# Debug: discarding a stage
# ===========================================================================


class TestDiscard:
    @pytest.fixture
    def finished(self, client):
        session_id = make_session(client)
        return session_id, run_iteration(client, session_id)

    def discard(self, client, iteration_id, **body):
        return client.post(f"/v1/iterations/{iteration_id}/discard-last-stage",
                           headers=AUTH, json=body or None)

    def test_discarding_removes_the_last_stage_and_rewinds(self, client,
                                                           finished, db):
        _session_id, iteration_id = finished
        assert db.scalar("SELECT COUNT(*) FROM alerts WHERE iteration_id = ?",
                         (iteration_id,)) >= 1

        assert self.discard(client, iteration_id).json()["stage"] == "SCHEDULING"
        response = self.discard(client, iteration_id)
        assert response.status_code == 200
        body = response.json()
        assert body["stage"] == "ALERTING"
        assert body["deleted"]["alerts"] >= 1
        assert body["next_stage"] == "ALERTING"
        assert db.scalar("SELECT COUNT(*) FROM alerts WHERE iteration_id = ?",
                         (iteration_id,)) == 0
        assert db.get_iteration(iteration_id)["outcome"] is None

    def test_a_discarded_stage_can_be_re_run(self, client, finished, db):
        _session_id, iteration_id = finished
        self.discard(client, iteration_id)                     # SCHEDULING
        self.discard(client, iteration_id)                     # ALERTING
        step = client.post(f"/v1/iterations/{iteration_id}/step",
                           headers=AUTH).json()
        assert step["stage"] == "ALERTING"
        assert step["ok"]
        assert db.scalar("SELECT COUNT(*) FROM alerts WHERE iteration_id = ?",
                         (iteration_id,)) >= 1

    def test_only_the_last_stage_can_be_discarded(self, client, finished):
        """Discarding TRIAGING under a live TIPPING would leave queries tipped
        by signals that no longer exist."""
        _session_id, iteration_id = finished
        response = self.discard(client, iteration_id, expect="TRIAGING")
        assert response.status_code == 422
        assert "SCHEDULING" in response.json()["detail"]

    def test_discarding_a_paid_stage_needs_confirmation(self, client, finished):
        """Re-running collection buys the same data again at the vendor."""
        _session_id, iteration_id = finished
        for _ in range(3):                    # SCHEDULING, ALERTING, CORRELATING
            assert self.discard(client, iteration_id).status_code == 200

        refused = self.discard(client, iteration_id)
        assert refused.status_code == 409
        assert "confirm" in refused.json()["detail"]

        allowed = self.discard(client, iteration_id, confirm=True)
        assert allowed.status_code == 200
        assert allowed.json()["stage"] == "COLLECTING_TIPPED"

    def test_a_discarded_collection_stage_returns_its_queries_to_pending(
        self, client, finished, db
    ):
        _session_id, iteration_id = finished
        for _ in range(3):
            self.discard(client, iteration_id)
        body = self.discard(client, iteration_id, confirm=True).json()
        assert body["queries_reset"] > 0
        assert db.scalar(
            "SELECT COUNT(*) FROM query_queue WHERE iteration_id = ? "
            "AND source_type != 'SOCIAL' AND status = 'PENDING'",
            (iteration_id,)) == body["queries_reset"]

    def test_spend_and_the_audit_trail_survive_a_discard(self, client, finished,
                                                         db):
        """The money is a fact about the world, not an output of the stage, and
        the one destructive operation has to leave a record of itself."""
        _session_id, iteration_id = finished
        calls_before = db.scalar(
            "SELECT COUNT(*) FROM api_calls WHERE iteration_id = ?",
            (iteration_id,))
        logs_before = db.scalar(
            "SELECT COUNT(*) FROM agent_log WHERE iteration_id = ?",
            (iteration_id,))

        self.discard(client, iteration_id)

        assert db.scalar("SELECT COUNT(*) FROM api_calls WHERE iteration_id = ?",
                         (iteration_id,)) == calls_before
        assert db.scalar("SELECT COUNT(*) FROM agent_log WHERE iteration_id = ?",
                         (iteration_id,)) > logs_before
        assert db.one(
            "SELECT * FROM agent_log WHERE iteration_id = ? AND agent = ? "
            "ORDER BY log_id DESC", (iteration_id, "StageRollback"))

    def test_discarding_with_nothing_to_discard_is_422(self, client):
        session_id = make_session(client)
        iteration_id = client.post(
            f"/v1/sessions/{session_id}/iterations", headers=AUTH,
            json={"mode": "manual"}).json()["iteration_id"]
        assert self.discard(client, iteration_id).status_code == 422


# ===========================================================================
# The debug switch, and the schema
# ===========================================================================


class TestDebugSwitch:
    def test_disabled_debug_routes_are_absent_not_merely_refused(
        self, db, app_config, monkeypatch
    ):
        """A discard endpoint that still existed would be one config read away
        from deleting analytical records in production."""
        monkeypatch.setenv("SURGE_API_TOKEN", TOKEN)
        app_config["api"]["debug_endpoints"] = False
        app = create_app(app_config, db=db, connectors=stub_connectors())
        with TestClient(app) as locked:
            session_id = make_session(locked)
            iteration_id = locked.post(
                f"/v1/sessions/{session_id}/iterations", headers=AUTH,
                json={"mode": "manual"}).json()["iteration_id"]
            for method, path in (
                ("post", f"/v1/iterations/{iteration_id}/step"),
                ("get", f"/v1/iterations/{iteration_id}/stages"),
                ("post", f"/v1/iterations/{iteration_id}/discard-last-stage"),
            ):
                assert getattr(locked, method)(
                    path, headers=AUTH).status_code == 404, path
            paths = locked.get("/openapi.json").json()["paths"]
            assert not any("discard" in p for p in paths)

    def test_the_operational_routes_are_unaffected(self, db, app_config,
                                                   monkeypatch):
        monkeypatch.setenv("SURGE_API_TOKEN", TOKEN)
        app_config["api"]["debug_endpoints"] = False
        app = create_app(app_config, db=db, connectors=stub_connectors(),
                         llm_client=FakeLLM(*[[
                             T.decision("https://x.com/1", "Phoenix"),
                             T.decision("https://apnews.com/2", "Phoenix")]] * 20))
        with TestClient(app) as locked:
            session_id = make_session(locked)
            run_iteration(locked, session_id)
            assert locked.get(f"/v1/sessions/{session_id}/alerts",
                              headers=AUTH).json()


class TestStageAttribution:
    """The declaration in services/stages.py drives both inspection and
    rollback, so a stage whose effects are described wrongly inspects wrongly
    AND rolls back wrongly. These are the tests that catch the drift."""

    def test_every_stage_declares_its_effects(self):
        from surge_iw.services.stages import STAGE_EFFECTS
        assert set(STAGE_EFFECTS) == set(PIPELINE_STAGES)

    @pytest.mark.parametrize("table,total_key", [
        ("query_queue", None), ("queue_decisions", None),
        ("raw_results", "raw_results"), ("signals", "signals"),
        ("triage_decisions", "triage_decisions"),
        ("correlations", "correlations"), ("alerts", "alerts"),
    ])
    def test_the_per_stage_counts_account_for_every_row(
        self, client, db, table, total_key
    ):
        """An undeclared write is a row rollback would orphan and inspection
        would never show."""
        session_id = make_session(client)
        iteration_id = run_iteration(client, session_id)

        stages = client.get(f"/v1/iterations/{iteration_id}/stages",
                            headers=AUTH).json()["stages"]
        attributed = sum(s["wrote"].get(table, 0) for s in stages)

        if total_key is not None:
            total = client.get(f"/v1/iterations/{iteration_id}",
                               headers=AUTH).json()["counts"][total_key]
        else:
            column = ("created_iteration_id" if table == "query_queue"
                      else "iteration_id")
            total = db.scalar(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
                (iteration_id,))
        assert attributed == total, f"{table}: {attributed} attributed of {total}"

    def test_scheduled_work_is_attributed_to_the_iteration_that_wrote_it(
        self, client, db
    ):
        """Follow-ons own no iteration until a later stage 1 claims them, and
        two iterations can each write an identical one — SQLite treats NULLs as
        distinct in the dedup index. Rolling back one must not delete the
        other's work."""
        session_id = make_session(client)
        first = run_iteration(client, session_id)
        rows = db.all(
            "SELECT * FROM query_queue WHERE session_id = ? "
            "AND iteration_id IS NULL", (session_id,))
        for row in rows:
            assert row["created_iteration_id"] == first
            assert row["created_at"]


class TestStagesReconcileAfterDegradation:
    def test_a_stepped_iteration_that_degrades_finishes_partial(
        self, db, app_config, monkeypatch
    ):
        """A stepped run carries no list across HTTP calls, so its degradations
        live only on the row — `_finish` has to read them back or a degraded
        stepped iteration would report COMPLETE."""
        monkeypatch.setenv("SURGE_API_TOKEN", TOKEN)
        # No LLM client: TRIAGING and ALERTING degrade rather than run.
        app = create_app(app_config, db=db, connectors=stub_connectors(),
                         llm_client=None)
        with TestClient(app) as stepper:
            session_id = make_session(stepper)
            iteration_id = stepper.post(
                f"/v1/sessions/{session_id}/iterations", headers=AUTH,
                json={"mode": "manual"}).json()["iteration_id"]

            degraded = []
            while True:
                step = stepper.post(f"/v1/iterations/{iteration_id}/step",
                                    headers=AUTH).json()
                if not step["ok"]:
                    degraded.append(step["stage"])
                if step["next_stage"] is None:
                    assert step["outcome"] == "PARTIAL"
                    break

            assert "TRIAGING" in degraded
            body = stepper.get(f"/v1/iterations/{iteration_id}",
                               headers=AUTH).json()
            assert body["outcome"] == "PARTIAL"
            assert body["degradations"]

    def test_discarding_a_stage_retracts_what_it_said_about_itself(
        self, client, db
    ):
        """Found in a real database.

        TRIAGING truncated at a low token ceiling and recorded "10 of 20
        post(s) were not judged". The operator discarded back to TRIAGING,
        raised `llm.max_tokens` and re-ran; all twenty were judged. The note
        survived, and `_finish` reads degradations to decide PARTIAL — so the
        iteration stayed degraded by a gap that no longer existed, and there was
        no way to tell that from the row.

        The rows a stage wrote and the notes it wrote about what it could not
        write are one record.
        """
        session_id = make_session(client)
        iteration_id = run_iteration(client, session_id)
        db.append_degradation(iteration_id, "10 of 20 post(s) were not judged",
                              source="TRIAGING")
        db.append_degradation(iteration_id, "an operator abandoned something",
                              source="recovery")

        # Walk back to TRIAGING, the way an operator does.
        for _ in range(6):
            response = client.post(
                f"/v1/iterations/{iteration_id}/discard-last-stage",
                headers=AUTH, json={"confirm": True})
            assert response.status_code == 200, response.text
            if response.json()["stage"] == "TRIAGING":
                assert response.json()["degradations_retracted"] == 1
                break
        else:
            pytest.fail("never reached TRIAGING")

        notes = db.degradation_notes(iteration_id)
        assert not any("were not judged" in n for n in notes), notes
        assert any("an operator abandoned" in n for n in notes), (
            "a recovery note is not a stage's and must survive its rollback")

    def test_a_derived_gap_summary_is_replaced_not_accumulated(self, db,
                                                                config,
                                                                iteration):
        """`collection gaps: ...` is recomputed on every `_finish`. A resume
        that closes some gaps must not leave the older, now wrong, summary
        standing beside the new one."""
        from surge_iw.db.database import SurgeDB

        db.replace_degradation(iteration, SurgeDB.DEGRADATION_GAPS,
                               "collection gaps: FAILED×3")
        db.replace_degradation(iteration, SurgeDB.DEGRADATION_GAPS,
                               "collection gaps: FAILED×1")
        assert db.degradation_notes(iteration) == ["collection gaps: FAILED×1"]

    def test_a_failed_stage_is_reported_as_failed_not_missing(
        self, db, app_config, monkeypatch
    ):
        monkeypatch.setenv("SURGE_API_TOKEN", TOKEN)
        app = create_app(app_config, db=db, connectors=stub_connectors(),
                         llm_client=None)
        with TestClient(app) as stepper:
            session_id = make_session(stepper)
            iteration_id = run_iteration(stepper, session_id)
            stages = {s["stage"]: s for s in stepper.get(
                f"/v1/iterations/{iteration_id}/stages",
                headers=AUTH).json()["stages"]}
        assert stages["TRIAGING"]["status"] == "FAILED"
        assert stages["TRIAGING"]["error_message"]
        assert stages["CORRELATING"]["status"] == "COMPLETE"


class TestMigration:
    def test_a_database_missing_the_new_columns_gains_them(self, tmp_path,
                                                           mission):
        """CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so a
        schema edit reaches an older database file only through an ALTER."""
        import sqlite3

        from surge_iw.db.database import SurgeDB

        path = tmp_path / "old.db"
        SurgeDB(path, mission=mission).close()
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE query_queue DROP COLUMN created_at")
            conn.execute(
                "ALTER TABLE query_queue DROP COLUMN created_iteration_id")
            # DROP COLUMN cannot remove queue_decisions.stage: it edits the
            # stored CREATE TABLE text and the comment above that column leaves
            # it unparseable. Rebuild the v1 shape instead.
            conn.execute("ALTER TABLE queue_decisions RENAME TO qd_v1")
            conn.execute(
                "CREATE TABLE queue_decisions AS SELECT decision_id, "
                "iteration_id, rule_code, outcome, source_type, city_name, "
                "dedup_key, signal_id, detail, decided_at FROM qd_v1")
            conn.execute("DROP TABLE qd_v1")

        db = SurgeDB(path, mission=mission)
        columns = {row["name"] for row in
                   db.conn.execute("PRAGMA table_info(query_queue)")}
        assert {"created_at", "created_iteration_id"} <= columns
        assert "stage" in {row["name"] for row in
                           db.conn.execute("PRAGMA table_info(queue_decisions)")}

        session = db.insert_session(label="post-migration")
        query_id = db.enqueue_query(
            session_id=session, iteration_id=None, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="k")
        assert db.get_query(query_id)["created_at"]
        db.close()


class TestOpenAPI:
    def test_the_schema_is_generated_and_valid(self, client):
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert schema["openapi"].startswith("3.")
        assert schema["info"]["title"] == "Surge I&W"

        expected = {
            "/v1/sessions", "/v1/sessions/{session_id}",
            "/v1/sessions/{session_id}/cities",
            "/v1/sessions/{session_id}/iterations",
            "/v1/iterations/{iteration_id}",
            "/v1/sessions/{session_id}/alerts",
            "/v1/alerts/{alert_id}/evidence",
            "/v1/sessions/{session_id}/queue", "/v1/healthz",
            "/v1/iterations/{iteration_id}/step",
            "/v1/iterations/{iteration_id}/stages",
            "/v1/iterations/{iteration_id}/discard-last-stage",
        }
        assert expected <= set(schema["paths"])

    def test_every_response_model_resolves(self, client):
        """A $ref to a component that does not exist generates a schema that
        looks fine and breaks the first client to read it."""
        schema = client.get("/openapi.json").json()
        defined = set(schema.get("components", {}).get("schemas", {}))
        referenced: set[str] = set()

        def walk(node):
            if isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                    referenced.add(ref.rsplit("/", 1)[1])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(schema)
        assert referenced <= defined, sorted(referenced - defined)

    def test_docs_render(self, client):
        assert client.get("/docs").status_code == 200


class TestShutdown:
    def test_shutdown_waits_for_a_running_iteration(self, db, app_config,
                                                    monkeypatch):
        """Not politeness. The caller closes the database next, and sqlite3
        segfaults if a connection is closed while another thread is mid-
        statement on it — an unwaited shutdown during collection is a crash."""
        monkeypatch.setenv("SURGE_API_TOKEN", TOKEN)
        llm = FakeLLM(*[[T.decision("https://x.com/1", "Phoenix")]] * 20)
        app = create_app(app_config, db=db, connectors=stub_connectors(),
                         llm_client=llm)
        release = threading.Event()
        original = T.StubSocial.search

        def blocking_search(self, endpoint, params, **kwargs):
            release.wait(timeout=5)
            return original(self, endpoint, params, **kwargs)

        T.StubSocial.search = blocking_search
        try:
            with TestClient(app) as client:
                session_id = make_session(client)
                response = client.post(
                    f"/v1/sessions/{session_id}/iterations", headers=AUTH)
                iteration_id = response.json()["iteration_id"]
                assert app.state.runner.is_running(iteration_id)
                release.set()
            # The context manager ran the lifespan shutdown. Nothing is still
            # running, and the iteration was allowed to record its outcome.
            assert app.state.runner.running() == []
            assert db.get_iteration(iteration_id)["outcome"] is not None
        finally:
            T.StubSocial.search = original
            release.set()

    def test_a_timed_out_shutdown_closes_nothing_and_exits_hard(
        self, db, app_config, monkeypatch
    ):
        """The bounded wait alone is not safety. If it expires a worker is
        still inside sqlite3, and closing the connection under it is a known
        SIGSEGV — which is why the wait exists in the first place."""
        import surge_iw.api.app as app_module

        monkeypatch.setenv("SURGE_API_TOKEN", TOKEN)
        app_config["api"]["shutdown_timeout_s"] = 0.2
        connectors = stub_connectors()
        closed: list[str] = []
        for name, connector in connectors.items():
            connector.close = (lambda n=name: closed.append(n))

        exits: list[int] = []
        monkeypatch.setattr(app_module, "_HARD_EXIT", exits.append)

        app = create_app(app_config, db=db, connectors=connectors,
                         llm_client=FakeLLM([]))
        release = threading.Event()
        original = T.StubSocial.search

        def stuck_search(self, endpoint, params, **kwargs):
            release.wait(timeout=10)
            return original(self, endpoint, params, **kwargs)

        T.StubSocial.search = stuck_search
        try:
            with TestClient(app) as client:
                session_id = make_session(client)
                iteration_id = client.post(
                    f"/v1/sessions/{session_id}/iterations",
                    headers=AUTH).json()["iteration_id"]

            assert exits == [app_module.STRANDED_EXIT_CODE]
            assert closed == [], "a connector must not be closed under a worker"
            assert db.scalar("SELECT 1") == 1, "the database must stay usable"

            epoch = db.get_epoch(app.state.epoch_id)
            assert epoch["shutdown_kind"] == "TIMEOUT"
            assert json.loads(epoch["stranded_json"]) == [iteration_id]
            # Deliberately NOT marked interrupted here: the worker may still
            # finish, and two contradictory records would be worse than one
            # late one. The next startup's reconcile is the single writer.
            assert db.get_iteration(iteration_id)["interrupted_at"] is None
        finally:
            T.StubSocial.search = original
            release.set()
            time.sleep(0.3)

    def test_a_clean_shutdown_closes_everything_and_records_it(
        self, db, app_config, monkeypatch
    ):
        monkeypatch.setenv("SURGE_API_TOKEN", TOKEN)
        connectors = stub_connectors()
        closed: list[str] = []
        for name, connector in connectors.items():
            connector.close = (lambda n=name: closed.append(n))

        app = create_app(app_config, db=db, connectors=connectors,
                         llm_client=FakeLLM([]))
        with TestClient(app) as client:
            make_session(client)
        assert sorted(closed) == sorted(connectors)
        assert db.get_epoch(app.state.epoch_id)["shutdown_kind"] == "CLEAN"


class TestRecoveryRoutes:
    """Recovery over HTTP. On the operational router, not the debug one:
    `api.debug_endpoints` is false for a deployment serving an operations
    team, and that is exactly the deployment that must be able to recover from
    a crash."""

    def strand(self, client, db):
        """Leave an iteration exactly as a killed process would, then restart.

        The restart is the second `open_epoch` — that is the whole crash
        simulation, and it needs no subprocess or signal.
        """
        from surge_iw.services.recovery import RecoveryService

        session_id = make_session(client)
        epoch = client.app.state.epoch_id
        iteration_id = db.insert_iteration(session_id, owner_epoch_id=epoch)
        for stage in ("SEEDING", "COLLECTING_SOCIAL", "TRIAGING", "TIPPING"):
            run_id = db.start_agent_run(iteration_id, "IterationOrchestrator",
                                        stage)
            db.finish_agent_run(run_id, "COMPLETE")
        db.start_agent_run(iteration_id, "IterationOrchestrator",
                           "COLLECTING_TIPPED")
        db.set_stage(iteration_id, "COLLECTING_TIPPED")
        city = db.get_cities(session_id)[0]["city_id"]
        db.enqueue_query(session_id=session_id, iteration_id=iteration_id,
                         source_type="CAR", endpoint="/search-rental-car",
                         params={}, dedup_key="stranded", city_id=city)
        db.claim_next_query(iteration_id, ["CAR"])

        report = RecoveryService(db, client.app.state.config).open_epoch("test")
        client.app.state.epoch_id = report.epoch_id
        client.app.state.runner.epoch_id = report.epoch_id
        client.app.state.reconcile = report
        return session_id, iteration_id

    def test_the_poll_terminates_on_interrupted(self, client, db):
        """Before 6a this returned running:false, outcome:null forever and a
        client watching poll_url never stopped."""
        _session_id, iteration_id = self.strand(client, db)
        body = client.get(f"/v1/iterations/{iteration_id}", headers=AUTH).json()
        assert body["status"] == "INTERRUPTED"
        assert body["resumable"] is True
        assert body["interrupted_stage"] == "COLLECTING_TIPPED"
        assert body["outcome"] is None and body["running"] is False

    def test_recovery_lists_it_with_both_exits(self, client, db):
        _session_id, iteration_id = self.strand(client, db)
        body = client.get("/v1/recovery", headers=AUTH).json()
        assert body["epoch"]["epoch_id"]
        entry = next(i for i in body["interrupted"]
                     if i["iteration_id"] == iteration_id)
        assert entry["resume_url"].endswith("/resume")
        assert entry["abandon_url"].endswith("/abandon")
        assert entry["interrupted_stage"] == "COLLECTING_TIPPED"
        assert entry["kind"] == "INTERRUPTED"


    def test_the_plan_is_read_only_and_names_the_cost(self, client, db):
        _session_id, iteration_id = self.strand(client, db)
        body = client.get(f"/v1/iterations/{iteration_id}/recovery-plan",
                          headers=AUTH).json()
        assert body["resume_from"] == "COLLECTING_TIPPED"
        assert body["derived_by"] == "RECONCILED"
        assert body["paid"] is True
        assert body["queries_to_recollect"]
        assert body["estimated_units_upper_bound"]
        # Unchanged by looking at it.
        assert db.get_query(
            body["queries_to_recollect"][0]["query_id"])["status"] \
            == "IN_PROGRESS"

    def test_the_plan_is_409_when_nothing_is_interrupted(self, client):
        session_id = make_session(client)
        iteration_id = run_iteration(client, session_id)
        assert client.get(f"/v1/iterations/{iteration_id}/recovery-plan",
                          headers=AUTH).status_code == 409

    def test_resume_refuses_without_confirmation(self, client, db):
        _session_id, iteration_id = self.strand(client, db)
        refused = client.post(f"/v1/iterations/{iteration_id}/resume",
                              headers=AUTH, json={})
        assert refused.status_code == 409
        assert "confirm_respend" in refused.json()["detail"]

    def test_resume_runs_and_closes_the_iteration(self, client, db):
        _session_id, iteration_id = self.strand(client, db)
        response = client.post(
            f"/v1/iterations/{iteration_id}/resume?wait=true", headers=AUTH,
            json={"confirm_respend": True})
        assert response.status_code in (200, 202), response.text

        body = client.get(f"/v1/iterations/{iteration_id}", headers=AUTH).json()
        assert body["status"] == "FINISHED"
        # Never COMPLETE: the reconcile's degradation note makes _finish's
        # existing rule reach PARTIAL for free.
        assert body["outcome"] == "PARTIAL"
        assert body["interrupted_at"], "the history must survive the resume"

    def test_a_later_from_stage_is_refused(self, client, db):
        """Skipping a stage that never ran leaves its output permanently
        missing; discard-last-stage is the way to go further back."""
        _session_id, iteration_id = self.strand(client, db)
        response = client.post(f"/v1/iterations/{iteration_id}/resume",
                               headers=AUTH,
                               json={"from_stage": "ALERTING",
                                     "confirm_respend": True})
        assert response.status_code == 409
        assert "later than" in response.json()["detail"]

    def test_abandon_counts_the_gap_and_still_alerts(self, client, db):
        _session_id, iteration_id = self.strand(client, db)
        # Give it evidence worth alerting on.
        city = db.get_cities(
            db.get_iteration(iteration_id)["session_id"])[0]["city_id"]
        for index, domain in enumerate(("a.com", "b.org", "c.net"), start=1):
            db.insert_signal(
                iteration_id=iteration_id, signal_type="SOCIAL", city_id=city,
                track="AIRSHOW", observed_at=T.iso(T.utcnow()),
                url=f"https://{domain}/{index}", source_domain=domain,
                salience=0.95, quality=0.95)

        response = client.post(f"/v1/iterations/{iteration_id}/abandon",
                               headers=AUTH,
                               json={"reason": "operator", "confirm": True})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["queries_marked_interrupted"] == 1
        assert body["coverage_gaps"] == {"CAR": 1}
        assert body["outcome"] == "PARTIAL"
        assert body["scheduling_skipped"] is True

        correlation = db.one(
            "SELECT * FROM correlations WHERE iteration_id = ?",
            (iteration_id,))
        assert correlation is not None, "abandon must still score"
        assert correlation["data_completeness"] < 1.0
        assert correlation["band"] != "HIGH"

    def test_abandon_refuses_without_confirmation(self, client, db):
        _session_id, iteration_id = self.strand(client, db)
        assert client.post(f"/v1/iterations/{iteration_id}/abandon",
                           headers=AUTH,
                           json={"reason": "x"}).status_code == 409

    def test_a_new_iteration_is_refused_while_one_is_interrupted(self, client,
                                                                 db):
        """The cooldown is keyed across ALL iterations, so the interrupted
        run's recent executions would silently suppress the new run's."""
        session_id, _iteration_id = self.strand(client, db)
        response = client.post(f"/v1/sessions/{session_id}/iterations",
                               headers=AUTH)
        assert response.status_code == 409
        assert "under-collected" in response.json()["detail"]

    def test_healthz_is_degraded_while_anything_is_unrecovered(self, client,
                                                              db):
        _session_id, iteration_id = self.strand(client, db)
        body = client.get("/v1/healthz").json()
        assert body["status"] == "degraded"
        assert body["interrupted_iterations"] == [iteration_id]

    def test_recovery_survives_debug_endpoints_being_off(self, db, app_config,
                                                         monkeypatch):
        """Recovery is not a debugging aid."""
        monkeypatch.setenv("SURGE_API_TOKEN", TOKEN)
        app_config["api"]["debug_endpoints"] = False
        app = create_app(app_config, db=db, connectors=stub_connectors())
        with TestClient(app) as locked:
            assert locked.get("/v1/recovery", headers=AUTH).status_code == 200
            paths = locked.get("/openapi.json").json()["paths"]
            assert "/v1/recovery" in paths
            assert not any("discard" in p for p in paths)


class TestAnOpenIterationBlocksTheNextOne:
    """8.7(a), over the API — the defect as the operator met it.

    Iteration 1 is created in `manual` mode and stepped partway, which is what
    driving the API by hand produces. Nothing crashes, so nothing stamps
    `interrupted_at`, so the old guard saw nothing and the old
    `GET /v1/iterations/{id}` reported PENDING. A second trigger was accepted.
    """

    def open_one(self, client):
        session_id = make_session(client)
        response = client.post(f"/v1/sessions/{session_id}/iterations",
                               headers=AUTH, json={"mode": "manual"})
        assert response.status_code == 202, response.text
        iteration_id = response.json()["iteration_id"]
        client.post(f"/v1/iterations/{iteration_id}/step", headers=AUTH)
        return session_id, iteration_id

    def test_the_second_trigger_is_refused(self, client, db):
        session_id, iteration_id = self.open_one(client)
        assert db.get_iteration(iteration_id)["interrupted_at"] is None, (
            "the premise: open, but nothing crashed")

        response = client.post(f"/v1/sessions/{session_id}/iterations",
                               headers=AUTH)
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert f"iteration {iteration_id} is open" in detail
        assert f"/v1/iterations/{iteration_id}/resume" in detail
        assert f"/v1/iterations/{iteration_id}/abandon" in detail

    def test_the_refusal_carries_no_retry_after(self, client):
        """Waiting never clears it. Same reasoning as the interrupted 409 —
        a `Retry-After` here would send a client into a loop that cannot end."""
        session_id, _iteration_id = self.open_one(client)
        response = client.post(f"/v1/sessions/{session_id}/iterations",
                               headers=AUTH)
        assert response.status_code == 409
        assert "retry-after" not in {k.lower() for k in response.headers}

    def test_it_appears_in_blocking_but_not_in_interrupted(self, client):
        """The listing an operator is sent to must contain the thing they were
        told to resolve. Before this it contained only crash-stamped runs, so a
        merely-open iteration blocked a session while being invisible to the
        endpoint that exists to unblock it."""
        _session_id, iteration_id = self.open_one(client)
        body = client.get("/v1/recovery", headers=AUTH).json()

        assert iteration_id not in [i["iteration_id"] for i in
                                    body["interrupted"]]
        entry = next(i for i in body["blocking"]
                     if i["iteration_id"] == iteration_id)
        assert entry["kind"] == "OPEN"
        assert entry["interrupted_at"] is None
        assert entry["resume_url"].endswith("/resume")
        assert entry["abandon_url"].endswith("/abandon")

    def test_the_iteration_itself_advertises_the_remedy(self, client):
        _session_id, iteration_id = self.open_one(client)
        body = client.get(f"/v1/iterations/{iteration_id}", headers=AUTH).json()
        assert body["status"] == "PENDING"
        assert body["resumable"] is True

    def test_abandon_is_a_working_exit_for_it(self, client, db):
        """The half that makes the refusal legitimate. `_plan_or_409` used to
        require `interrupted_at`, so widening the guard alone would have left a
        blocked session with no way out."""
        session_id, iteration_id = self.open_one(client)
        assert client.get(f"/v1/iterations/{iteration_id}/recovery-plan",
                          headers=AUTH).status_code == 200

        response = client.post(f"/v1/iterations/{iteration_id}/abandon",
                               headers=AUTH,
                               json={"reason": "left open by hand",
                                     "confirm": True})
        assert response.status_code == 200, response.text
        assert db.get_iteration(iteration_id)["finished_at"] is not None

        # And the session is usable again.
        assert client.post(f"/v1/sessions/{session_id}/iterations",
                           headers=AUTH,
                           json={"mode": "manual"}).status_code == 202

    def test_resume_is_a_working_exit_for_it(self, client, db):
        """The other exit. `?wait=true` so the run has closed before the next
        trigger — otherwise the 409 would be the ordinary busy-session one and
        the test would prove nothing about the guard."""
        session_id, iteration_id = self.open_one(client)
        response = client.post(
            f"/v1/iterations/{iteration_id}/resume?wait=true", headers=AUTH,
            json={"confirm_respend": True})
        assert response.status_code in (200, 202), response.text
        assert client.get(f"/v1/iterations/{iteration_id}",
                          headers=AUTH).json()["status"] == "FINISHED"

        assert client.post(f"/v1/sessions/{session_id}/iterations",
                           headers=AUTH,
                           json={"mode": "manual"}).status_code == 202


class TestContractArtifacts:
    """`docs/api/` is generated. A stale artifact is worse than a missing one,
    because a client integrating against it has no way to tell."""

    def test_the_saved_artifacts_match_the_application(self):
        import subprocess

        repo = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "scripts/build_api_contract.py", "--check"],
            cwd=repo, capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            "docs/api/ is out of date with the code. Regenerate it:\n"
            "  python scripts/build_api_contract.py\n\n"
            + result.stdout[-3000:] + result.stderr[-2000:]
        )

    @pytest.fixture
    def artifacts(self):
        return Path(__file__).resolve().parents[1] / "docs" / "api"

    @pytest.fixture
    def spec(self, artifacts):
        import json
        return json.loads((artifacts / "openapi.json").read_text())

    @pytest.fixture
    def examples(self, artifacts):
        """The captured exchanges, asserted to EXIST before anything iterates
        them.

        Two tests below loop over this directory. A loop over nothing passes,
        so when `docs/api/examples/` was moved out from under `docs/api/`
        during the mission/engine doc split, both went green while asserting
        nothing at all — and only the artifacts-match gate and the broken-link
        check went red. A gate that degrades to silence when its subject
        disappears is how a contract goes stale without a failure that names
        the reason.
        """
        paths = sorted((artifacts / "examples").glob("*.json"))
        assert paths, (
            "docs/api/examples/ is empty or missing. Regenerate it:\n"
            "  python scripts/build_api_contract.py")
        return paths

    def test_the_yaml_and_json_specs_are_the_same_document(self, artifacts,
                                                           spec):
        import yaml
        assert yaml.safe_load((artifacts / "openapi.yaml").read_text()) == spec

    def test_the_operational_spec_is_the_full_one_minus_debug(self, artifacts,
                                                              spec):
        """A client generated from the wrong file calls paths that 404."""
        import json
        operational = json.loads(
            (artifacts / "openapi-operational.json").read_text())
        assert set(operational["paths"]) < set(spec["paths"])
        debug = {"/v1/iterations/{iteration_id}/step",
                 "/v1/iterations/{iteration_id}/stages",
                 "/v1/iterations/{iteration_id}/stages/{stage}",
                 "/v1/iterations/{iteration_id}/discard-last-stage"}
        assert set(spec["paths"]) - set(operational["paths"]) == debug
        # Recovery shares that prefix and must survive: a deployment with
        # debug_endpoints false is exactly the one that has to recover.
        for path in ("/v1/recovery",
                     "/v1/iterations/{iteration_id}/resume",
                     "/v1/iterations/{iteration_id}/abandon",
                     "/v1/iterations/{iteration_id}/recovery-plan"):
            assert path in operational["paths"], path

    def test_every_endpoint_and_schema_is_documented(self, artifacts, spec):
        reference = (artifacts / "API.md").read_text()
        for path, methods in spec["paths"].items():
            for method in methods:
                assert f"### `{method.upper()} {path}`" in reference, path
        for name in spec["components"]["schemas"]:
            assert f"### `{name}`" in reference, name

    def test_the_reference_has_no_broken_links(self, artifacts):
        """A table of contents whose links all 404 is worse than none, because
        it looks navigable."""
        import re
        reference = (artifacts / "API.md").read_text()
        headings = set()
        for line in reference.splitlines():
            if line.startswith("#"):
                text = line.lstrip("#").strip().replace("`", "")
                slug = "".join(c for c in text.lower()
                               if c.isalnum() or c in " -_")
                headings.add(slug.replace(" ", "-"))
        assert not set(re.findall(r"\]\(#([^)]+)\)", reference)) - headings
        for target in re.findall(r"\]\((examples/[^)]+)\)", reference):
            assert (artifacts / target).exists(), target

    def test_the_captured_examples_are_real_responses(self, examples, spec):
        """Every example must name a path the spec actually declares — an
        example of an endpoint that does not exist is a client's first bug."""
        import json
        templates = list(spec["paths"])
        for path in examples:
            exchange = json.loads(path.read_text())
            actual = exchange["request"]["path"].split("?")[0].strip("/")
            assert any(
                len(t.strip("/").split("/")) == len(actual.split("/"))
                and all(a.startswith("{") or a == b for a, b in
                        zip(t.strip("/").split("/"), actual.split("/")))
                for t in templates), exchange["request"]["path"]
            assert 200 <= exchange["response"]["status"] < 600

    def test_the_generator_is_deterministic(self, tmp_path):
        """Without this the --check gate would fail on unrelated runs and be
        turned off, which is how a contract goes stale."""
        import subprocess

        repo = Path(__file__).resolve().parents[1]
        for _ in range(2):
            assert subprocess.run(
                [sys.executable, "scripts/build_api_contract.py",
                 "--out", str(tmp_path)],
                cwd=repo, capture_output=True, text=True).returncode == 0
        assert subprocess.run(
            [sys.executable, "scripts/build_api_contract.py",
             "--out", str(tmp_path), "--check"],
            cwd=repo, capture_output=True, text=True).returncode == 0

    # -- 9.3 / issue #12 ---------------------------------------------------
    #
    # Runtime auth was always correct and the contract never mentioned it, so
    # a client generated from `openapi.json` omitted the header and failed
    # every operational request. The reason it went unnoticed for so long is
    # visible in the tests above: they assert routes, schemas, examples and
    # links, and none of them ever asked whether the security metadata was
    # there at all. These do.

    def test_the_bearer_scheme_is_declared(self, spec):
        scheme = spec["components"]["securitySchemes"]["BearerToken"]
        assert scheme["type"] == "http"
        assert scheme["scheme"] == "bearer"
        assert scheme.get("description"), (
            "a generated client shows this to whoever has to find the token")

    def test_every_protected_operation_declares_the_requirement(self, spec):
        """Scheme-in-components is not enough. A client sends the header
        because the *operation* asks for it."""
        undeclared = [
            f"{method.upper()} {path}"
            for path, methods in spec["paths"].items()
            for method, operation in methods.items()
            if path != "/v1/healthz"
            and operation.get("security") != [{"BearerToken": []}]
        ]
        assert not undeclared, undeclared

    def test_liveness_declares_authentication_as_optional(self, spec):
        """`/v1/healthz` is anonymous for liveness and authenticated for
        `?deep=true`. OpenAPI says that with an empty requirement beside the
        scheme, and saying it either other way is wrong: "required" makes a
        generated client send a token for a liveness probe, and silence leaves
        `?deep=true` unreachable from generated code."""
        assert spec["paths"]["/v1/healthz"]["get"]["security"] == [
            {}, {"BearerToken": []}]

    def test_no_captured_example_pins_a_git_revision(self, examples):
        """The staleness half of the same issue.

        `code_revision` tracks git HEAD, so a captured receipt went stale on
        every commit rather than on an API change — and a drift gate that
        cries wolf is one an operator learns to silence. An example is a shape,
        not a snapshot of the machine that produced it.
        """
        import json
        import re

        found = []
        for path in examples:
            for revision in re.findall(r'"code_revision":\s*("[^"]*"|null)',
                                       path.read_text()):
                if revision not in ('"0000000"', "null"):
                    found.append(f"{path.name}: {revision}")
        assert not found, found

    # -- Hardening, re-checked after Phase 9 -------------------------------

    def test_a_retryable_409_declares_retry_after(self, spec):
        """The runtime has always sent it; the contract did not say so — the
        same defect as the missing bearer scheme. A generated client without
        this either backs off on a 409 that never clears or hammers one that
        would."""
        for path in ("/v1/sessions/{session_id}/iterations",
                     "/v1/iterations/{iteration_id}/retry-triage"):
            headers = spec["paths"][path]["post"]["responses"]["409"]["headers"]
            assert "Retry-After" in headers, path

    def test_an_unwaitable_409_does_not_declare_it(self, spec):
        """Resume's 409s are "not interrupted" and "this would re-collect".
        Neither clears by waiting, and declaring the header would tell a
        generated client to back off forever."""
        resume = spec["paths"]["/v1/iterations/{iteration_id}/resume"]["post"]
        assert "headers" not in resume["responses"]["409"]

    def test_the_replay_header_is_declared(self, spec):
        """A replayed body is byte-identical to a fresh one, so the header is
        the only way a caller can tell — which makes leaving it undeclared the
        same as not having it."""
        trigger = spec["paths"]["/v1/sessions/{session_id}/iterations"]["post"]
        for status in ("200", "202"):
            assert "Idempotent-Replay" in trigger["responses"][status]["headers"]

    def test_no_request_body_silently_ignores_an_unknown_field(self, spec):
        """Issue #11 one level up. `tunables` being accepted and never read was
        the issue; the field NAME had the same problem — `tunabels` returned a
        201 for a session running on settings the client had not chosen."""
        undeclared = [
            name for name, model in spec["components"]["schemas"].items()
            if name.endswith("In") and model.get("additionalProperties") is not False
        ]
        assert not undeclared, undeclared

    def test_fields_whose_meaning_changed_carry_a_description(self, spec):
        """A STALE description is worse than none — a client reads it and
        builds on a false statement.

        Found re-auditing after 9.11 and 9.13: `caveat` still said it was
        present "whenever a source could not be collected", which 9.13 made
        untrue (staleness fires it with no gap at all); `failed_sources` had
        changed shape to `SOURCE_TYPE:endpoint` and said nothing; and
        `data_completeness` had changed what it counts.
        """
        schemas = spec["components"]["schemas"]
        for model, field in (
            ("AlertOut", "caveat"),
            ("EvidenceOut", "failed_sources"),
            ("EvidenceOut", "failed_families"),
            ("EvidenceOut", "data_completeness"),
            ("CorrelationOut", "failed_sources"),
            ("CorrelationOut", "failed_families"),
            ("CorrelationOut", "data_completeness"),
            # The mission block carries the permitted `track` and
            # `location_type` values, which this contract deliberately no
            # longer declares as enums. A client that cannot find out where
            # the vocabulary lives has lost a control and gained nothing.
            ("CapabilitiesOut", "mission"),
            # Every field that carries mission vocabulary. Dropping the
            # `Literal` from these was a deliberate trade, and `mission` is
            # the mitigation — but a bare `string` in a RESPONSE schema names
            # no mitigation at all, and a client reading `AlertOut` has no
            # reason to go looking at a different model. Six surfaces went out
            # of the split describing the vocabulary on input and saying
            # nothing about it on output.
            ("AlertOut", "track"),
            ("AlertTupleOut", "track"),
            ("CorrelationOut", "track"),
            ("EvidenceOut", "track"),
            ("SessionOut", "tracks"),
            ("CapabilitiesOut", "tracks"),
            ("SessionIn", "tracks"),
            ("KeyLocationIn", "location_type"),
            # The other half of the same mitigation: capabilities says to
            # compare its digest against the receipt, and the receipt is a
            # loose object, so without this the client is told to compare a
            # value against a field the contract never names.
            ("EvidenceOut", "receipt"),
            # issue #2 / 9.4 and 9.6. Both are the ANSWER to a review finding
            # — one comparable acquisition value, and the competing
            # explanations for the evidence — and both were shipped as bare
            # objects. A field a reader is told to reason with, whose
            # vocabulary the contract never states, is not the control the
            # finding asked for.
            ("EvidenceOut", "collection"),
            ("EvidenceOut", "alternatives"),
        ):
            described = schemas[model]["properties"][field].get("description")
            assert described, f"{model}.{field} has no description"

    def test_the_mission_block_says_where_the_vocabularies_come_from(self, spec):
        """The contract's answer to "what values may I send for track?" is
        this field. Saying so is the whole mitigation for dropping the enums,
        so it is pinned rather than left to whoever edits the docstring next.
        """
        described = (spec["components"]["schemas"]["CapabilitiesOut"]
                     ["properties"]["mission"]["description"])
        assert "track" in described
        assert "location_type" in described
        assert "digest" in described

    def _mission_prose(self):
        """The vocabulary the packs on disk actually use — see
        `conftest.mission_vocabulary`. Built, never transcribed: a scan that
        spells the words it forbids becomes the last place in the engine
        holding them."""
        from conftest import mission_vocabulary
        return mission_vocabulary()

    def test_the_scan_below_can_actually_see_a_mission_concept(self):
        """Probes taken from the packs, so a rotted pattern cannot pass by
        matching nothing, and no sentence has to be written down here."""
        from conftest import mission_installed, mission_terms

        if not mission_installed():
            pytest.skip("no second pack to harvest a probe from")
        pattern = self._mission_prose()
        probes = mission_terms()
        assert len(probes) > 20, "the packs supplied almost no vocabulary"
        for term in probes:
            assert pattern.search(term), term
        assert not pattern.search(
            "Demonstration team jets on the flightline at the Riverside "
            "Fairground ahead of this weekend's flying display.")

    def test_the_published_contract_names_no_mission(self, artifacts):
        """The contract is an ENGINE artifact. It is generated against the
        reference pack, so a client reading it must not be able to tell what
        any other deployment is looking for.

        This is the scan `test_mission.py` does not do: that one stops at
        `surge_iw/`, and the contract is built from three things outside it —
        the generator, the dry-run fixtures it serves, and the artifacts
        themselves. Every mission concept that reached a published example got
        there through one of the three.
        """
        from conftest import mission_installed
        if not mission_installed():
            pytest.skip(
                "no pack other than `reference` is installed, so the contract "
                "has no second mission's vocabulary to leak.")
        from surge_iw.connectors.registry import _DRY_RUN_FIXTURES, FIXTURE_DIR

        repo = Path(__file__).resolve().parents[1]
        sources = [p for p in sorted(artifacts.rglob("*")) if p.is_file()]
        sources.append(repo / "scripts" / "build_api_contract.py")
        # Named from the registry rather than listed here: a fixture added for
        # a new endpoint is served to the capture whether or not this test
        # remembers it.
        sources += [FIXTURE_DIR / name
                    for name in sorted(set(_DRY_RUN_FIXTURES.values()))]
        assert len(sources) > 45, "the scan found almost nothing to read"

        pattern = self._mission_prose()
        offenders = [
            f"{p.relative_to(repo)}:{n}: {m.group(0)}"
            for p in sources
            for n, line in enumerate(
                p.read_text(errors="ignore").splitlines(), 1)
            for m in [pattern.search(line)] if m
        ]
        assert offenders == [], offenders

    def test_the_digest_a_client_is_told_to_compare_is_on_the_receipt(
        self, artifacts, spec
    ):
        """`CapabilitiesOut.mission` promises that `digest` is "the hash
        stamped on every receipt, so a client holding an alert can tell
        whether the definition has changed since it was written".

        That promise is the mitigation for dropping the enums, and it spans
        two endpoints — so nothing inside either schema can tell whether it is
        still true. Checked here against the captured exchanges, which are the
        only artifact that sees both ends at once.
        """
        import json
        capabilities = json.loads(
            (artifacts / "examples/28-capabilities.json").read_text()
        )["response"]["body"]["mission"]
        receipt = json.loads(
            (artifacts / "examples/08-evidence.json").read_text()
        )["response"]["body"]["receipt"]
        assert receipt, "the evidence example must carry a receipt"
        assert receipt["mission_hash"] == capabilities["digest"], (
            "capabilities reports one digest and the receipt stamps another; "
            "a client comparing them would conclude the mission had changed")
        assert receipt["mission_id"].startswith(capabilities["id"])

    def test_the_typed_triage_outcomes_are_in_the_operational_contract(
        self, artifacts, spec
    ):
        """Review #8 asked for this on the OPERATIONAL surface specifically: a
        deployment with `api.debug_endpoints` false is the one an external
        consumer integrates against, and the stage-report endpoints that carry
        pre-model skips are not on it."""
        import json
        operational = json.loads(
            (artifacts / "openapi-operational.json").read_text())
        for document in (spec, operational):
            field = (document["components"]["schemas"]["IterationOut"]
                     ["properties"]["triage_states"])
            assert field["type"] == "object"
            for state in ("ACCEPTED", "REJECTED", "UNDECIDED",
                          "INVALID_OUTPUT", "MODEL_ERROR"):
                assert state in field["description"], state

        captured = json.loads(
            (artifacts / "examples/05-poll-iteration.json").read_text())
        assert captured["response"]["body"]["triage_states"], (
            "the captured poll must SHOW the field populated; a documented "
            "field nobody has seen carry a value is a promise, not a contract")

    def test_the_receipt_records_the_request_that_was_accepted(self, artifacts):
        """Review #8, HIGH. `prompt_hash` covers the system prompt and
        `input_hash` the first-variant payload; neither moves when a retry
        rewrites the user message. `prompt_user_hash` is what makes the
        byte-exact claim checkable rather than assumed."""
        import json
        receipt = json.loads(
            (artifacts / "examples/08-evidence.json").read_text()
        )["response"]["body"]["receipt"]
        assert receipt["prompt_user_hash"], receipt
        assert receipt["prompt_user_hash"] != receipt["prompt_hash"]

    def test_the_completeness_number_can_be_checked_by_its_reader(self, spec):
        """`data_completeness` is computed from `failed_families`, so a surface
        that shows the number without its inputs cannot be argued with — and
        arguing with the conclusion is what the evidence endpoint is for."""
        for model in ("EvidenceOut", "CorrelationOut"):
            props = spec["components"]["schemas"][model]["properties"]
            assert "data_completeness" in props
            assert "failed_families" in props, (
                f"{model} shows completeness without what it counts")

    def test_the_caveat_description_admits_both_of_its_triggers(self, spec):
        text = spec["components"]["schemas"]["AlertOut"]["properties"]["caveat"][
            "description"].lower()
        assert "collect" in text, "must still describe the coverage gap"
        assert "iteration" in text, "must describe the staleness trigger (9.13)"
