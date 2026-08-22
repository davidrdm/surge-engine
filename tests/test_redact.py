"""Credential scrubbing.

The consequence of a miss is not a failed test — it is a live key written into a
log file, a database row, or an assistant's context and from there to a model
provider. So these tests cover the realistic carriers rather than tidy inputs:
httpx-style exception text, echoed request headers, keys in query strings, and
vendor session material nested inside a real response payload.
"""
from __future__ import annotations

import json

import pytest

from conftest import REFERENCE_MISSION
from surge_iw.db.database import SurgeDB
from surge_iw.services.redact import (
    PLACEHOLDER,
    Redactor,
    default_redactor,
)

FAKE_KEY = "sk_live_9f3a2b7c1d4e6f8a0b2c4d6e8f0a2b4c"
FAKE_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefghijklmnop"


@pytest.fixture
def redactor() -> Redactor:
    r = Redactor()
    r.register(FAKE_KEY)
    return r


class TestExactValueRedaction:
    def test_registered_secret_is_replaced_anywhere_in_a_string(self, redactor):
        text = f"GET /v1/twitter/posts failed with key {FAKE_KEY} rejected"
        result = redactor.text(text)
        assert FAKE_KEY not in result
        assert PLACEHOLDER in result

    def test_short_values_are_not_registered(self):
        """Redacting a short common string would corrupt text without
        protecting anything."""
        r = Redactor()
        r.register("abc")
        assert r.secret_count == 0
        assert r.text("abc def") == "abc def"

    def test_longest_secret_wins_so_no_fragment_survives(self):
        r = Redactor()
        r.register("secretvalue")
        r.register("secretvalue_extended_form")
        result = r.text("token=secretvalue_extended_form")
        assert "secretvalue" not in result

    def test_none_and_empty_pass_through(self, redactor):
        assert redactor.text(None) is None
        assert redactor.text("") == ""


class TestPatternRedaction:
    """Catches credentials the process never held, e.g. echoed back by a vendor."""

    @pytest.mark.parametrize(
        "text",
        [
            "Authorization: Bearer abcdef1234567890abcdef",
            "authorization=Bearer abcdef1234567890abcdef",
            "X-API-Key: abcdef1234567890abcdef",
            "x-rapidapi-key: abcdef1234567890abcdef",
            "https://api.example.com/v1?api_key=abcdef1234567890abcdef",
            "https://api.example.com/v1?token=abcdef1234567890abcdef&x=1",
        ],
    )
    def test_structural_carriers_are_scrubbed(self, text):
        result = default_redactor().text(text)
        assert "abcdef1234567890abcdef" not in result
        assert PLACEHOLDER in result

    def test_the_header_name_survives_for_debuggability(self):
        result = default_redactor().text("X-API-Key: abcdef1234567890abcdef")
        assert "X-API-Key" in result

    def test_unrelated_text_is_untouched(self):
        text = "FR24 returned 3 records for inbound:PHX in 412ms"
        assert default_redactor().text(text) == text


class TestExceptionScrubbing:
    def test_key_in_an_exception_message_is_scrubbed(self, redactor):
        exc = RuntimeError(
            f"401 Unauthorized for url "
            f"https://apidirect.io/v1/twitter/posts?api_key={FAKE_KEY}"
        )
        result = redactor.exception(exc)
        assert FAKE_KEY not in result
        assert "RuntimeError" in result
        assert "401 Unauthorized" in result

    def test_exception_type_survives_when_there_is_no_message(self, redactor):
        assert "ValueError" in redactor.exception(ValueError())


class TestPayloadScrubbing:
    def test_sensitive_keys_are_dropped_wholesale(self, redactor):
        payload = {"data": [{"authorization": FAKE_TOKEN, "name": "ok"}]}
        result = redactor.payload(payload)
        assert result["data"][0]["authorization"] == PLACEHOLDER
        assert result["data"][0]["name"] == "ok"

    def test_priceline_session_material_is_stripped(self, redactor):
        """checkoutUrl and detailsKey are not credentials, but they embed a
        booking refCode and session tokens and arrive in every car offer."""
        payload = {
            "data": {
                "totalResultsAvailable": 12,
                "vehicles": [{
                    "code": "ECAR",
                    "checkoutUrl": "/cart/checkout/rc/retail/JFKO03~refCode-27-"
                                   "e335a014-8258-42ac-97da-84adb41a98c4~...",
                    "detailsKey": "JFKO03~JFKO03~NM~Y~~19fa7574a89~~refCode-27~...",
                    "vehicleFeatures": {"peopleCapacity": 5},
                }],
            }
        }
        result = redactor.payload(payload)
        vehicle = result["data"]["vehicles"][0]
        assert vehicle["checkoutUrl"] == PLACEHOLDER
        assert vehicle["detailsKey"] == PLACEHOLDER
        # The fields the signal actually needs survive intact.
        assert vehicle["code"] == "ECAR"
        assert vehicle["vehicleFeatures"]["peopleCapacity"] == 5
        assert result["data"]["totalResultsAvailable"] == 12

    def test_key_matching_is_case_insensitive(self, redactor):
        result = redactor.payload({"Authorization": FAKE_TOKEN, "TOKEN": "x"})
        assert result["Authorization"] == PLACEHOLDER
        assert result["TOKEN"] == PLACEHOLDER

    def test_nested_string_values_are_scrubbed(self, redactor):
        payload = {"error": {"detail": f"bad key {FAKE_KEY}"}}
        assert FAKE_KEY not in json.dumps(redactor.payload(payload))

    def test_non_string_scalars_are_preserved(self, redactor):
        payload = {"count": 12, "ratio": 0.5, "ok": True, "missing": None}
        assert redactor.payload(payload) == payload

    def test_deeply_nested_structure_terminates(self, redactor):
        payload: dict = {"a": {}}
        node = payload["a"]
        for _ in range(50):
            node["a"] = {}
            node = node["a"]
        redactor.payload(payload)  # must not recurse without bound


class TestConfigRegistration:
    def test_credentials_are_discovered_via_env_var_names(self, monkeypatch):
        """config holds the NAME of a variable; the value comes from the env."""
        monkeypatch.setenv("TEST_FR24_KEY", FAKE_KEY)
        r = Redactor()
        found = r.register_from_config(
            {"flightradar": {"api_key_env": "TEST_FR24_KEY"},
             "llm": {"api_key_env": "TEST_ABSENT_KEY"}}
        )
        assert found == ["TEST_FR24_KEY"]
        assert r.secret_count == 1
        assert FAKE_KEY not in (r.text(f"boom {FAKE_KEY}") or "")

    def test_returned_names_are_safe_to_log(self, monkeypatch):
        """register_from_config returns variable NAMES, never values."""
        monkeypatch.setenv("TEST_FR24_KEY", FAKE_KEY)
        r = Redactor()
        found = r.register_from_config(
            {"flightradar": {"api_key_env": "TEST_FR24_KEY"}}
        )
        assert FAKE_KEY not in " ".join(found)


class TestDatabaseIntegration:
    """Scrubbing happens on the way in, so no caller has to remember it."""

    def test_api_call_error_is_scrubbed_before_storage(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY_VAR", FAKE_KEY)
        default_redactor().register(FAKE_KEY)
        with SurgeDB(":memory:", mission=REFERENCE_MISSION) as db:
            db.record_api_call(
                provider="FR24", endpoint="/api/live/flight-positions/full",
                units=1.0, http_status=401,
                error_message=f"401 for ...?api_key={FAKE_KEY}",
            )
            stored = db.one("SELECT error_message FROM api_calls")
            assert FAKE_KEY not in stored["error_message"]

    def test_raw_payload_is_scrubbed_before_storage(self):
        default_redactor().register(FAKE_KEY)
        with SurgeDB(":memory:", mission=REFERENCE_MISSION) as db:
            session = db.insert_session()
            iteration = db.insert_iteration(session)
            query_id = db.enqueue_query(
                session_id=session, iteration_id=iteration, source_type="CAR",
                endpoint="/search-rental-car", params={}, dedup_key="k1",
            )
            raw_id = db.insert_raw_result(
                query_id=query_id, iteration_id=iteration, source_type="CAR",
                provider="PRICELINE",
                payload=[{"checkoutUrl": "/cart/...refCode-27-e335a014",
                          "code": "ECAR"}],
                retention_days=90,
            )
            stored = json.loads(db.get_raw_result(raw_id)["payload_json"])
            # Dropped outright, not placeholdered (8.3). The governance record
            # says Priceline booking capability is never retained, and a key
            # present with a placeholder value still tells a reader the field
            # existed and invites a schema that expects it. The redactor's
            # placeholder is for material we must KEEP but must not expose;
            # this is material we should not have kept.
            assert "checkoutUrl" not in stored[0]
            assert stored[0]["code"] == "ECAR"

    def test_failed_query_message_is_scrubbed(self):
        default_redactor().register(FAKE_KEY)
        with SurgeDB(":memory:", mission=REFERENCE_MISSION) as db:
            session = db.insert_session()
            iteration = db.insert_iteration(session)
            query_id = db.enqueue_query(
                session_id=session, iteration_id=iteration, source_type="SOCIAL",
                endpoint="/v1/twitter/posts", params={}, dedup_key="k1",
            )
            db.fail_query(query_id, f"401 Unauthorized key={FAKE_KEY}")
            row = db.one(
                "SELECT error_message FROM query_queue WHERE query_id = ?",
                (query_id,),
            )
            assert FAKE_KEY not in row["error_message"]

    def test_agent_log_extra_fields_are_scrubbed(self):
        default_redactor().register(FAKE_KEY)
        with SurgeDB(":memory:", mission=REFERENCE_MISSION) as db:
            db.log("TestAgent", "ERROR", f"failed with {FAKE_KEY}",
                   detail=f"header was {FAKE_KEY}")
            row = db.one("SELECT message, extra_json FROM agent_log")
            assert FAKE_KEY not in row["message"]
            assert FAKE_KEY not in (row["extra_json"] or "")


class TestInstallationAtStartup:
    """9.1 / issue #10 — the service was correct and never started.

    `redact.install()` documented that it must run once at startup before any
    connector is constructed, and no production entry point called it. So the
    exact-value layer — the reliable one, the one that catches a key however it
    was embedded — protected nothing outside the tests that called the
    installer by hand.

    That is why these tests drive the real entry points instead. A test that
    called `install()` itself would have passed throughout the entire period
    the defect existed, which is the only useful thing to know about it.
    """

    SYNTHETIC = "surge_test_1a2b3c4d5e6f7g8h9i0j_not_a_real_key"

    @pytest.fixture(autouse=True)
    def isolated_default(self):
        """Keep a synthetic key out of every later test's strings.

        Reaching into the private set is deliberate: the redactor has no public
        way to forget a secret, and it should not have one. A production caller
        that could unregister a credential is a worse problem than an untidy
        test.
        """
        before = set(default_redactor()._secrets)
        yield
        default_redactor()._secrets = before

    @pytest.fixture
    def config_file(self, tmp_path, monkeypatch):
        """A config on disk naming an environment variable, as an operator
        writes it — the shape the real startup path actually reads."""
        monkeypatch.setenv("SURGE_TEST_FR24_KEY", self.SYNTHETIC)
        path = tmp_path / "config.yaml"
        path.write_text(
            "flightradar:\n"
            "  api_key_env: SURGE_TEST_FR24_KEY\n"
            "database:\n"
            f"  path: {tmp_path / 'surge.db'}\n",
            encoding="utf-8",
        )
        return path

    def test_loading_a_config_registers_the_credentials_it_names(
        self, config_file
    ):
        from surge_iw.config import load_config

        load_config(config_file)
        leaked = f"401 Unauthorized for ...?api_key={self.SYNTHETIC}"
        assert self.SYNTHETIC not in (default_redactor().text(leaked) or "")

    def test_redaction_is_installed_before_the_database_is_opened(
        self, config_file, monkeypatch, capsys
    ):
        """The ordering IS the fix.

        Installing eventually is not the same as installing first: `SurgeDB`
        writes `agent_log`, and a connector error logged before registration
        would be persisted in the clear and stay that way. So this asserts on
        the count observed at the moment the database is constructed, not at
        the end of the command.
        """
        import run

        observed: list[int] = []
        real = run.SurgeDB

        def watched(*args, **kwargs):
            observed.append(default_redactor().secret_count)
            return real(*args, **kwargs)

        monkeypatch.setattr(run, "SurgeDB", watched)
        assert run.main(["--config", str(config_file), "init-db"]) == 0
        capsys.readouterr()
        assert observed and observed[0] >= 1, (
            "the database was opened before any credential was registered")

    def test_an_injected_config_is_installed_too(self, monkeypatch):
        """`create_app` accepts a config the caller built, which never passes
        through `load_config`. The guarantee has to belong to the application,
        not to one way of constructing it."""
        from surge_iw.api.app import create_app

        monkeypatch.setenv("SURGE_TEST_STAYING_KEY", self.SYNTHETIC)
        monkeypatch.setenv("SURGE_API_TOKEN", "token-for-this-test-only")
        with SurgeDB(":memory:", mission=REFERENCE_MISSION) as db:
            create_app(
                {"database": {"path": ":memory:"},
                 "staying": {"api_key_env": "SURGE_TEST_STAYING_KEY"},
                 "api": {"token_env": "SURGE_API_TOKEN"}},
                db=db, connectors={}, llm_client=None,
            )
        assert self.SYNTHETIC not in (
            default_redactor().text(f"boom {self.SYNTHETIC}") or "")

    def test_a_configured_key_reaches_no_persisted_record(self, config_file):
        """The three carriers named in the issue, checked after the real
        startup path rather than after a hand-registered secret."""
        from surge_iw.config import load_config

        load_config(config_file)
        with SurgeDB(":memory:", mission=REFERENCE_MISSION) as db:
            db.log("CollectionAgent", "ERROR",
                   f"GET failed key={self.SYNTHETIC}",
                   detail=f"header {self.SYNTHETIC}")
            db.record_api_call(
                provider="FR24", endpoint="/api/live/flight-positions/full",
                units=1.0, http_status=401,
                error_message=f"401 for ...?api_key={self.SYNTHETIC}")
            session = db.insert_session()
            iteration = db.insert_iteration(session)
            query_id = db.enqueue_query(
                session_id=session, iteration_id=iteration,
                source_type="FLIGHT_LIVE",
                endpoint="/api/live/flight-positions/full",
                params={}, dedup_key="k1")
            raw_id = db.insert_raw_result(
                query_id=query_id, iteration_id=iteration,
                source_type="FLIGHT_LIVE", provider="FR24",
                payload=[{"echoed_request": f"?api_key={self.SYNTHETIC}"}],
                retention_days=90)

            log_row = db.one("SELECT message, extra_json FROM agent_log")
            call_row = db.one("SELECT error_message FROM api_calls")
            payload = db.get_raw_result(raw_id)["payload_json"]
            assert self.SYNTHETIC not in log_row["message"]
            assert self.SYNTHETIC not in (log_row["extra_json"] or "")
            assert self.SYNTHETIC not in call_row["error_message"]
            assert self.SYNTHETIC not in payload
            assert PLACEHOLDER in call_row["error_message"]
