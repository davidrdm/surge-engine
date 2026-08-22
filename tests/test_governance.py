"""Provider governance — 8.3.

The record is code because a document drifts from the rules it describes within
a phase. These tests hold the record to that standard: every claim it makes has
to be one the system actually enforces, and the two kinds of claim it carries —
measured and asserted — must stay apart.

What is NOT tested here, and cannot be: whether we hold the downstream rights.
No assertion and no API call establishes that. `rights_verified` is False on
every provider and `test_no_provider_may_claim_verified_rights` keeps it that
way, so the claim cannot be quietly upgraded by someone in a hurry.
"""
from __future__ import annotations

import json

import pytest

from surge_iw.db import enums
from surge_iw.services import governance
from surge_iw.services.retention import retention_days as service_retention_days


class TestTheRecordIsComplete:
    def test_every_provider_the_system_can_call_has_a_policy(self):
        """A provider with no governance record could be collected from with
        no retention limit, no field policy and no stated provenance."""
        assert set(governance.POLICIES) == set(enums.PROVIDERS)

    def test_every_policy_states_its_provenance_and_rights_question(self):
        for name, policy in governance.POLICIES.items():
            assert policy.provenance, f"{name} has no provenance chain"
            assert policy.downstream_rights, f"{name} has no rights statement"
            assert policy.unit_basis, f"{name} does not say what a unit is"
            assert policy.retention_basis, f"{name} does not say why"

    def test_every_policy_distinguishes_failure_from_absence(self):
        """The correctness requirement the whole system rests on: a failure
        must never be readable as 'no threat detected'."""
        for name, policy in governance.POLICIES.items():
            assert policy.failure_modes, f"{name} documents no failure modes"


class TestMeasuredAndAssertedStayApart:
    def test_no_provider_may_claim_verified_rights(self):
        """The one assertion that must never be relaxed.

        A 200 means the vendor served the bytes. It is not evidence that we may
        redistribute them, and all four providers are intermediaries whose
        terms do not displace the platforms' underneath.
        """
        for name, policy in governance.POLICIES.items():
            assert policy.rights_verified is False, (
                f"{name} claims verified downstream rights. No API call can "
                f"establish that; only a legal review can.")

    def test_open_questions_are_listed_not_summarised(self):
        questions = governance.open_rights_questions()
        assert len(questions) == len(governance.POLICIES), (
            "these do not resolve together — each chain is different")
        assert all(q["question"] and q["provenance"] for q in questions)


class TestRetentionIsEnforcedNotDescribed:
    def test_a_contractual_ceiling_cannot_be_raised_by_config(self):
        """A config file must not be able to buy a licence term."""
        assert governance.retention_days("FR24", 365) == 30
        assert governance.retention_days("FR24", 3650) == 30

    def test_config_may_shorten(self):
        assert governance.retention_days("FR24", 7) == 7

    def test_reporting_uses_the_SHIPPED_window_not_the_bare_default(self):
        """Found during the 8.6 soak. `DEFAULT_RETENTION_DAYS` is the fallback
        for a provider the config says nothing about, not what the system
        keeps: the shipped config asks for 90 days for API Direct, so
        PROVIDERS.md and the evidence surface were documenting a 30-day window
        over payloads retained for 90."""
        from surge_iw.config import DEFAULT_CONFIG
        shipped = DEFAULT_CONFIG["apidirect"]["retention_days"]
        assert governance.retention_days("APIDIRECT", None) == shipped

    def test_the_bare_default_still_covers_an_unconfigured_provider(self):
        assert (governance.shipped_retention_days("NOT_A_PROVIDER")
                == governance.DEFAULT_RETENTION_DAYS)

    def test_a_ceiling_still_beats_the_shipped_value(self):
        """FR24's contractual 30 must survive whatever the config asks for."""
        from surge_iw.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["flightradar"]["retention_days"] <= 30
        assert governance.retention_days("FR24", None) == 30

    def test_the_retention_service_defers_to_the_record(self, config):
        """One authority. Before 8.3 the FR24 cap was an `if` in retention.py,
        which is where a second provider's term would have been forgotten."""
        config.setdefault("flightradar", {})["retention_days"] = 365
        assert service_retention_days(config, "FR24") == 30

    def test_a_stored_payload_gets_the_governed_deadline(self, db, iteration,
                                                        session):
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="FLIGHT_LIVE",
            endpoint="/api/live/flight-positions/full", params={}, dedup_key="g")
        raw_id = db.insert_raw_result(
            query_id=query_id, iteration_id=iteration,
            source_type="FLIGHT_LIVE", provider="FR24",
            payload={"data": []},
            retention_days=governance.retention_days("FR24", 365))
        row = db.get_raw_result(raw_id)
        from surge_iw.db.database import parse_iso
        span = parse_iso(row["purge_after"]) - parse_iso(row["retrieved_at"])
        assert span.days <= 30


class TestFieldPolicyIsEnforcedAtStorage:
    def test_booking_capability_never_reaches_the_database(self):
        """Dropped, not placeholdered. A key present with a placeholder still
        tells a reader the field existed and invites a schema that expects it."""
        payload = {"vehicles": [{"code": "ECAR", "checkoutUrl": "/cart/x",
                                 "detailsKey": "k",
                                 "vehicleFeatures": {"peopleCapacity": 5}}]}
        stripped = governance.strip_for_storage("PRICELINE", payload)
        blob = json.dumps(stripped)
        assert "checkoutUrl" not in blob and "detailsKey" not in blob
        # The analytically useful fields survive untouched.
        assert stripped["vehicles"][0]["vehicleFeatures"]["peopleCapacity"] == 5

    def test_it_reaches_arbitrary_nesting(self):
        deep = {"a": [{"b": {"c": [{"checkoutUrl": "x", "keep": 1}]}}]}
        assert "checkoutUrl" not in json.dumps(
            governance.strip_for_storage("PRICELINE", deep))
        assert "keep" in json.dumps(governance.strip_for_storage("PRICELINE", deep))

    def test_a_provider_with_no_drop_list_is_untouched(self):
        payload = {"posts": [{"url": "https://x.com/1", "snippet": "text"}]}
        assert governance.strip_for_storage("APIDIRECT", payload) == payload

    def test_the_real_storage_path_applies_it(self, db, iteration, session):
        """Through insert_raw_result, not the helper in isolation."""
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="CAR",
            endpoint="/search-rental-car", params={}, dedup_key="car-gov")
        raw_id = db.insert_raw_result(
            query_id=query_id, iteration_id=iteration, source_type="CAR",
            provider="PRICELINE",
            payload={"vehicles": [{"code": "ECAR", "checkoutUrl": "/cart/x"}]},
            retention_days=30)
        assert "checkoutUrl" not in db.get_raw_result(raw_id)["payload_json"]


class TestAZeroBaselineIsNoDataNotNoDrop:
    """Found live on 2026-08-09 while verifying the governance record.

    Priceline returned `success: true` with `totalResultsAvailable: 0` for
    every PHX and JFK window at +1, +2, +7 and +14 days, where an earlier live
    run had measured 272 offers. `car_signal_rows` emits only classes present
    in BOTH windows, so an all-zero feed produced zero rows, zero signals, and
    a query still marked COMPLETE — CAR reporting FULL coverage having observed
    nothing, with a dead feed reading as a quiet market.

    The discriminator is the baseline. We cannot tell a sold-out airport from a
    broken feed on the near window alone, but a baseline exists to establish the
    normal level: zero there establishes nothing and leaves no denominator.
    """

    def _agent(self, db, config):
        from surge_iw.agents.collection import CollectionAgent
        return CollectionAgent(db, config, {})

    def _connector(self, near_total, base_total):
        class Stub:
            def __init__(self) -> None:
                self.calls = 0

            def search_rental_cars(self, params, **kw):
                self.calls += 1
                total = near_total if self.calls == 1 else base_total
                return {"total_results_available": total, "offers": [],
                        "truncated": False}
        return Stub()

    def test_a_zero_baseline_raises_rather_than_reporting_no_drop(
        self, db, config, session, iteration
    ):
        from surge_iw.base.connector import PlatformUnavailableError

        query = {"query_id": 1, "source_type": "CAR",
                 "endpoint": "/search-rental-car", "rule_code": "R5_CAR",
                 "priority": 30}
        with pytest.raises(PlatformUnavailableError, match="BASELINE"):
            self._agent(db, config)._collect_car(
                iteration, 1, query, self._connector(0, 0),
                {"pickUpLocation": "PHX"})

    def test_a_live_baseline_still_collects(self, db, config, session,
                                            iteration):
        """The guard must not fire on a working feed."""
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="CAR",
            endpoint="/search-rental-car", params={}, dedup_key="car-ok")
        query = {"query_id": query_id, "source_type": "CAR",
                 "endpoint": "/search-rental-car", "rule_code": "R5_CAR",
                 "priority": 30}
        rows, written = self._agent(db, config)._collect_car(
            iteration, query_id, query, self._connector(120, 140),
            {"pickUpLocation": "PHX"})
        assert (rows, written) == (0, 0)  # no offers paired, but no raise

    def test_the_failure_mode_is_in_the_record(self):
        modes = governance.POLICIES["PRICELINE"].failure_modes
        assert any("totalResultsAvailable 0" in k for k in modes)
        assert "zero_inventory_2026_08_09" in governance.POLICIES[
            "PRICELINE"].measured


class TestTheRecordReachesOperators:
    def test_the_evidence_note_names_the_chain_and_the_limit(self):
        note = governance.evidence_note("FR24")
        assert "30 days" in note and "contractual" in note
        assert "not verified" in note

    def test_a_provider_with_no_term_says_the_window_is_ours(self):
        note = governance.evidence_note("APIDIRECT")
        assert "not a granted permission" in note, (
            "a window we chose must not read as one we were granted")
        assert "90 days" in note, "and it must state the window actually used"

    def test_an_unknown_provider_does_not_invent_reassurance(self):
        assert "No governance record" in governance.evidence_note("NOPE")
