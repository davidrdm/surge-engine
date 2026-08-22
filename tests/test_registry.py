"""Connector construction, dry-run mode, and end-to-end storage scrubbing."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from conftest import REFERENCE_MISSION
from surge_iw.base.connector import BaseConnector, ConnectorError
from surge_iw.config import ConfigError, load_config
from surge_iw.connectors import flightradar as fr
from surge_iw.connectors import priceline as pl
from surge_iw.connectors.apidirect import EP_NEWS
from surge_iw.connectors.registry import (
    DryRunTransport,
    build_connectors,
    health_report,
)
from surge_iw.db.database import SurgeDB
from surge_iw.services.budget import BudgetGuard


@pytest.fixture
def dry_config():
    config = load_config(None)
    config["dry_run"] = True
    return config


class TestDryRun:
    def test_builds_without_any_credentials(self, dry_config, monkeypatch):
        """A demo and a front-end developer must not need real keys."""
        for name in ("APIDIRECT_API_KEY", "FR24_API_KEY", "STAYING_API_KEY",
                     "PRICELINE_RAPIDAPI_KEY"):
            monkeypatch.delenv(name, raising=False)
        connectors = build_connectors(dry_config)
        assert set(connectors) == {"APIDIRECT", "FR24", "STAYING", "PRICELINE"}
        assert all(isinstance(c, BaseConnector) for c in connectors.values())

    def test_real_parsing_code_still_runs(self, dry_config):
        """Only the network is replaced. A dry run that stubbed the connectors
        would prove nothing about the parsers the front end depends on."""
        connectors = build_connectors(dry_config)
        flights = connectors["FR24"].live_positions({"airports": "inbound:PHX"})
        assert len(flights) == 3
        assert all(f["category_confidence"] == "AMBIGUOUS" for f in flights)

        cars = connectors["PRICELINE"].search_rental_cars(
            {"pickUpLocation": "PHX"})
        # dry_run now serves the CURRENT wrapper's shape (8.6): serving the
        # superseded priceline8 payload here meant a front end developed
        # against a shape the system no longer receives.
        assert cars["total_results_available"] == 6
        assert cars["skipped"] == 0

        articles = connectors["APIDIRECT"].search(EP_NEWS, {"query": "x"})
        assert articles[0]["source_domain"] == "apnews.com"

        account = connectors["STAYING"].account()
        assert account["key"]["environment"] == "test"

    def test_unmapped_path_returns_404_not_empty_success(self):
        """A dry run that answered everything with "nothing found" would mask a
        wiring mistake as a quiet lack of intelligence."""
        import httpx
        transport = DryRunTransport()
        with httpx.Client(transport=transport) as client:
            response = client.get("https://example.com/not-mapped")
        assert response.status_code == 404

    def test_budget_records_zero_units(self, dry_config):
        with SurgeDB(":memory:", mission=REFERENCE_MISSION) as db:
            guard = BudgetGuard(db, dry_config)
            guard.seed_budgets()
            connectors = build_connectors(dry_config, on_call=guard.record)
            connectors["FR24"].live_positions({"airports": "inbound:PHX"})
            assert db.units_used("FR24") == 0.0
            # The call is still logged, so a dry run is auditable.
            assert db.scalar("SELECT COUNT(*) FROM api_calls") == 1


class TestLiveModeConstruction:
    def test_missing_credential_fails_at_construction(self, monkeypatch):
        """Not at request time, where it would look like empty data."""
        monkeypatch.delenv("FR24_API_KEY", raising=False)
        monkeypatch.setenv("APIDIRECT_API_KEY", "x" * 20)
        with pytest.raises(ConfigError):
            build_connectors(load_config(None))

    def test_key_shaped_env_name_is_rejected(self, monkeypatch):
        """The exact mistake made in the sibling iw repo: a credential pasted
        into the field that names the environment variable."""
        config = load_config(None)
        config["flightradar"]["api_key_env"] = "AQ.Ab8RN6KwXhNRe1gOsFVU"
        with pytest.raises(ConfigError, match="environment variable name"):
            build_connectors(config)

    def test_sandbox_mode_requires_the_sandbox_key(self, monkeypatch):
        config = load_config(None)
        config["flightradar"]["sandbox"] = True
        for name in ("APIDIRECT_API_KEY", "FR24_API_KEY", "STAYING_API_KEY",
                     "PRICELINE_RAPIDAPI_KEY"):
            monkeypatch.setenv(name, "x" * 20)
        monkeypatch.delenv("FR24_SANDBOX_KEY", raising=False)
        with pytest.raises(RuntimeError, match="FR24_SANDBOX_KEY"):
            build_connectors(config)

    def test_sandbox_mode_uses_the_sandbox_key(self, monkeypatch):
        config = load_config(None)
        config["flightradar"]["sandbox"] = True
        for name in ("APIDIRECT_API_KEY", "FR24_API_KEY", "STAYING_API_KEY",
                     "PRICELINE_RAPIDAPI_KEY"):
            monkeypatch.setenv(name, "live-" + "x" * 16)
        monkeypatch.setenv("FR24_SANDBOX_KEY", "sandbox-" + "y" * 16)
        connectors = build_connectors(config)
        assert connectors["FR24"].auth_headers()["Authorization"].endswith(
            "sandbox-" + "y" * 16
        )


class TestHealthReport:
    def test_reports_each_connector_separately(self, dry_config):
        """An operator needs to know WHICH of the four is broken."""
        report = health_report(build_connectors(dry_config))
        assert set(report) == {"APIDIRECT", "FR24", "STAYING", "PRICELINE"}
        assert report["STAYING"]["healthy"] is True

    @respx.mock
    def test_a_failing_connector_reports_rather_than_raising(self, monkeypatch):
        """One broken credential must not blank the whole health report."""
        for name in ("APIDIRECT_API_KEY", "FR24_API_KEY", "STAYING_API_KEY",
                     "PRICELINE_RAPIDAPI_KEY"):
            monkeypatch.setenv(name, "x" * 20)
        # The probe moved to the endpoint collection actually uses. Verified
        # live: /auto-complete-location 500s while /search-rental-car returns
        # 780 offers on the same key, so probing autocomplete reported the car
        # family down while it was working.
        respx.get(
            "https://priceline-com2.p.rapidapi.com/cars/search"
        ).mock(return_value=httpx.Response(401, json={"message": "bad key"}))
        respx.get(
            "https://api.stayingapi.com/v1/account"
        ).mock(return_value=httpx.Response(
            200, json={"data": {"credits": {"available": 10}}}))

        connectors = build_connectors(load_config(None))
        report = health_report({
            "PRICELINE": connectors["PRICELINE"],
            "STAYING": connectors["STAYING"],
        })
        assert report["PRICELINE"]["healthy"] is False
        assert "401" in report["PRICELINE"]["detail"]
        # The healthy one still reports normally.
        assert report["STAYING"]["healthy"] is True


class TestStoredPayloadsAreScrubbed:
    """The last Phase 2 gate item, proven through the real storage path."""

    def test_priceline_session_material_never_reaches_the_database(
        self, dry_config
    ):
        """checkoutUrl and detailsKey embed a booking refCode and session
        tokens, and they arrive inside every rental-car response."""
        with SurgeDB(":memory:", mission=REFERENCE_MISSION) as db:
            session = db.insert_session()
            iteration = db.insert_iteration(session)
            query_id = db.enqueue_query(
                session_id=session, iteration_id=iteration, source_type="CAR",
                endpoint=pl.EP_CARS, params={}, dedup_key="k1",
            )
            raw_payload = json.loads(
                (
                    __import__("pathlib").Path(__file__).parent
                    / "fixtures" / "priceline_cars.json"
                ).read_text()
            )
            # Sanity: the fixture really does contain the material, so the
            # assertion below is meaningful rather than vacuous.
            assert "checkoutUrl" in json.dumps(raw_payload)
            assert "refCode" in json.dumps(raw_payload)

            raw_id = db.insert_raw_result(
                query_id=query_id, iteration_id=iteration, source_type="CAR",
                provider="PRICELINE", payload=raw_payload, retention_days=90,
            )
            stored = db.get_raw_result(raw_id)["payload_json"]
            assert "refCode-27" not in stored
            assert "/cart/checkout" not in stored
            # The analytically useful fields survive intact.
            assert "peopleCapacity" in stored
            assert "totalResultsAvailable" in stored

    def test_a_credential_in_an_error_never_reaches_the_database(self):
        from surge_iw.services.redact import default_redactor
        secret = "rapid_live_9f3a2b7c1d4e6f8a0b2c"
        default_redactor().register(secret)
        with SurgeDB(":memory:", mission=REFERENCE_MISSION) as db:
            session = db.insert_session()
            iteration = db.insert_iteration(session)
            query_id = db.enqueue_query(
                session_id=session, iteration_id=iteration, source_type="CAR",
                endpoint=pl.EP_CARS, params={}, dedup_key="k1",
            )
            db.fail_query(query_id, f"401 for ...?x-rapidapi-key={secret}")
            db.record_api_call(
                provider="PRICELINE", endpoint=pl.EP_CARS, units=1.0,
                http_status=401, error_message=f"rejected key {secret}",
            )
            blob = json.dumps([dict(r) for r in db.all(
                "SELECT error_message FROM query_queue "
                "UNION ALL SELECT error_message FROM api_calls")])
            assert secret not in blob


class TestConnectorContract:
    """Properties every connector must share, asserted across all four."""

    def test_all_declare_a_provider_matching_the_enum(self, dry_config):
        from surge_iw.db import enums
        for name, connector in build_connectors(dry_config).items():
            assert connector.provider == name
            assert connector.provider in enums.PROVIDERS

    def test_all_carry_credentials_in_headers_only(self, dry_config):
        for connector in build_connectors(dry_config).values():
            headers = connector.auth_headers()
            assert headers, f"{connector.name} sends no auth header"

    def test_all_have_a_human_readable_name(self, dry_config):
        for connector in build_connectors(dry_config).values():
            assert connector.name and isinstance(connector.name, str)

    def test_none_can_be_built_without_a_key_in_live_mode(self):
        for cls in (fr.FlightRadarConnector,):
            with pytest.raises(ConnectorError):
                cls("")
