"""The relevance switch — `triage.require_nexus`.

A mission supplies two relevance clauses, strict and broad, and this switch
chooses between them. The engine has no opinion about what either one says;
what it must guarantee is that the choice is made, applied, and *recorded* —
because a broad leg is a materially different instrument from a strict one, not
a slightly more sensitive version of it, and two runs under different criteria
have to be separable in the audit trail afterwards.

Every test here uses the synthetic reference mission, so they assert the
plumbing rather than any particular wording. What each leg SAYS is a claim
about a pack, and lives with the pack that makes it, under
`missions/<name>/tests/`.
"""
from __future__ import annotations

import pytest

from surge_iw.agents.triage import TriageAgent, build_system_prompt
from surge_iw.services import receipts
from test_triage import FakeLLM, store_posts


POST = {
    "title": "Airshow practice day announced for Riverside Fairground",
    "url": "https://example.org/airshow-practice-day",
    "author": "Example Herald",
    "platform": "news",
    "source_domain": "example.org",
    "snippet": "Display teams are expected to arrive from Thursday for a "
               "practice day ahead of the weekend event.",
    "observed_at": "",
}


def seed(db, config, session, iteration, post=POST):
    if db.find_city(session, "atlanta") is None:
        db.insert_city(session, "Atlanta", canonical="atlanta", state="GA")
    config["triage"]["max_post_age_hours"] = 24 * 400
    query = db.enqueue_query(
        session_id=session, iteration_id=iteration, source_type="SOCIAL",
        endpoint="/v1/news/articles", params={}, dedup_key="nexus")
    store_posts(db, iteration, [post], query)
    return query


# ---------------------------------------------------------------------------
# The mechanism
# ---------------------------------------------------------------------------


class TestTheSwitchSelectsTheCriteria:
    def test_the_default_uses_the_strict_leg(self, mission):
        text, version = build_system_prompt(mission, {})
        assert version == mission.prompt_versions["relevance_strict"]
        assert mission.prompts["relevance_strict"] in text
        assert mission.prompts["relevance_broad"] not in text

    def test_turning_it_off_uses_the_broad_leg(self, mission):
        text, version = build_system_prompt(
            mission, {"triage": {"require_nexus": False}})
        assert version == mission.prompt_versions["relevance_broad"]
        assert mission.prompts["relevance_broad"] in text

    def test_the_body_is_the_same_either_way(self, mission):
        """Only the relevance clause moves. If the whole prompt changed, the
        switch would be doing more than it says on the label."""
        narrow, _ = build_system_prompt(mission, {})
        broad, _ = build_system_prompt(
            mission, {"triage": {"require_nexus": False}})
        assert narrow.replace(mission.prompts["relevance_strict"], "") == \
            broad.replace(mission.prompts["relevance_broad"], "")

    def test_the_two_legs_are_distinguishable_in_the_record(self, mission):
        """The version label can be forgotten; the hash cannot. Two runs under
        different criteria must always be separable in the receipts."""
        narrow, nv = build_system_prompt(mission, {})
        broad, bv = build_system_prompt(
            mission, {"triage": {"require_nexus": False}})
        assert nv != bv
        assert receipts.sha256_hex(narrow) != receipts.sha256_hex(broad)

    def test_there_is_no_prompt_without_a_mission(self):
        """No engine fallback, deliberately: a default prompt would screen on
        criteria nobody chose while looking exactly like criteria somebody
        did."""
        with pytest.raises(RuntimeError) as exc:
            build_system_prompt(None, {})
        assert "mission" in str(exc.value)


class TestTheAgentUsesAndRecordsIt:
    def _run(self, db, config, session, iteration, *, nexus, verdict):
        config["triage"]["require_nexus"] = nexus
        seed(db, config, session, iteration)
        TriageAgent(db, config, FakeLLM([{
            "url": POST["url"], "relevant": verdict,
            "track": "AIRSHOW", "cities": ["Atlanta"], "locations": [],
            "activity_type": "practice day",
            "imminence_hours": 0.0, "salience": 0.6,
            "rationale": "display aviation at a named locality",
        }])).run(iteration)
        return db.one("SELECT * FROM triage_decisions")

    def test_the_strict_leg_stamps_the_strict_criteria(
        self, db, config, session, iteration, mission
    ):
        row = self._run(db, config, session, iteration,
                        nexus=True, verdict=False)
        receipt = db.get_receipt(row["receipt_id"])
        assert receipt["prompt_version"] == \
            mission.prompt_versions["relevance_strict"]
        narrow, _ = build_system_prompt(mission, {})
        assert receipt["prompt_hash"] == receipts.sha256_hex(narrow)

    def test_the_broad_leg_stamps_the_broad_criteria(
        self, db, config, session, iteration, mission
    ):
        row = self._run(db, config, session, iteration,
                        nexus=False, verdict=True)
        receipt = db.get_receipt(row["receipt_id"])
        assert receipt["prompt_version"] == \
            mission.prompt_versions["relevance_broad"]
        broad, _ = build_system_prompt(
            mission, {"triage": {"require_nexus": False}})
        assert receipt["prompt_hash"] == receipts.sha256_hex(broad)

    def test_the_receipt_names_the_pack_that_supplied_the_prompt(
        self, db, config, session, iteration, mission
    ):
        """`code_revision` stopped being sufficient provenance the moment the
        prompt left the repository: a pack can be edited with no commit at
        all."""
        row = self._run(db, config, session, iteration,
                        nexus=True, verdict=False)
        receipt = db.get_receipt(row["receipt_id"])
        assert receipt["mission_id"] == mission.label
        assert receipt["mission_hash"] == mission.digest

    def test_the_broad_leg_announces_itself(self, db, config, session,
                                            iteration):
        """An analyst reading the log must be able to see which instrument
        ran without reconstructing it from a prompt hash."""
        self._run(db, config, session, iteration, nexus=False, verdict=True)
        warnings = db.all(
            "SELECT message FROM agent_log WHERE agent='TriageAgent' "
            "AND level='WARNING'")
        assert any("BROAD relevance criteria" in w["message"]
                   for w in warnings)

    def test_the_default_leg_is_quiet(self, db, config, session, iteration):
        self._run(db, config, session, iteration, nexus=True, verdict=False)
        warnings = db.all(
            "SELECT message FROM agent_log WHERE agent='TriageAgent' "
            "AND level='WARNING'")
        assert not any("relevance criteria" in w["message"] for w in warnings)

    def test_an_accepted_item_becomes_a_signal(
        self, db, config, session, iteration
    ):
        """The point of the switch: under the broad leg an item reaches the
        scoring layer where under the strict leg it never did."""
        self._run(db, config, session, iteration, nexus=False, verdict=True)
        signals = db.signals_by_type(iteration, "SOCIAL")
        assert signals, "an accepted item must produce a signal"
        assert signals[0]["track"] == "AIRSHOW"
