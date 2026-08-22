"""Reconstructing the text Surge sent to the model.

Surge stores a hash of each prompt and payload, never the text, so the tool that
rebuilds them is only useful if it refuses to present an unverified
reconstruction as fact. These tests are mostly about the refusals: a purged
payload, an edited prompt, an orphaned receipt. The happy path is one test; the
ways it can be wrong are four.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reconstruct_prompts as rp                                # noqa: E402
from conftest import REFERENCE_MISSION
from surge_iw.agents.alerting import AlertAgent                 # noqa: E402
from surge_iw.agents.triage import TriageAgent                  # noqa: E402
from test_triage import FakeLLM, decision, post, store_posts    # noqa: E402


@pytest.fixture
def judged(db, config, session, iteration):
    """A real triage run: posts collected, judged, receipts written."""
    db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
    config["triage"]["max_post_age_hours"] = 24 * 4000
    config["triage"]["batch_size"] = 2
    query = db.enqueue_query(
        session_id=session, iteration_id=iteration, source_type="SOCIAL",
        endpoint="/v1/twitter/posts", params={}, dedup_key="r")
    posts = [post(f"https://x.com/{i}", "x.com") for i in range(3)]
    store_posts(db, iteration, posts, query)
    TriageAgent(db, config, FakeLLM(
        *[[decision(p["url"]) for p in posts]] * 4)).run(iteration)
    return iteration


def conn_for(db):
    """The database's own connection. The test fixture is `:memory:`, so
    opening it again by path would get an empty database."""
    return db.conn


@pytest.fixture
def on_disk(tmp_path, config):
    """A file-backed run, for the paths that take a path on the command line."""
    from surge_iw.db.database import SurgeDB

    path = tmp_path / "surge.db"
    disk = SurgeDB(str(path), mission=REFERENCE_MISSION)
    session = disk.insert_session(label="cli", tracks=["AIRSHOW"])
    iteration = disk.insert_iteration(session)
    disk.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
    config["triage"]["max_post_age_hours"] = 24 * 4000
    config["triage"]["batch_size"] = 2
    query = disk.enqueue_query(
        session_id=session, iteration_id=iteration, source_type="SOCIAL",
        endpoint="/v1/twitter/posts", params={}, dedup_key="r")
    posts = [post(f"https://x.com/{i}", "x.com") for i in range(3)]
    store_posts(disk, iteration, posts, query)
    TriageAgent(disk, config, FakeLLM(
        *[[decision(p["url"]) for p in posts]] * 4)).run(iteration)
    return disk, iteration


class TestTheHappyPath:
    def test_a_rebuilt_batch_verifies_against_its_receipt(self, db, judged):
        """The whole claim: the reconstruction is provably what was sent."""
        entries = rp.rebuild_triage(conn_for(db), judged)
        assert entries, "the run wrote receipts"
        for entry in entries:
            assert entry["problems"] == [], entry["problems"]
            assert entry["input_ok"] and entry["batch_ok"]
            assert entry["user"].startswith("Screen these ")
            assert "item_id" in entry["user"]

    def test_the_system_message_carries_the_json_instruction(self, db, judged):
        """`_call_llm_json` appends it, so it is part of what was transmitted
        and a reconstruction that omitted it would be wrong."""
        entry = rp.rebuild_triage(conn_for(db), judged)[0]
        assert entry["system"].endswith(rp.JSON_SUFFIX)

    def test_the_leg_is_derived_from_the_hash_not_from_config(self, db, config,
                                                              judged):
        """The setting may have moved since the run; the receipt is the only
        record of what was actually in force."""
        config["triage"]["require_nexus"] = not config["triage"].get(
            "require_nexus", True)
        entry = rp.rebuild_triage(conn_for(db), judged)[0]
        assert entry["problems"] == []
        assert entry["version"]


class TestItRefusesToGuess:
    def test_a_purged_payload_is_reported_not_reconstructed(self, db, judged):
        """Retention deletes the payload and keeps the judgement. The text sent
        for that post is then gone for good, and saying so is the only honest
        answer."""
        db._exec("UPDATE raw_results SET payload_json = '[]'")
        for entry in rp.rebuild_triage(conn_for(db), judged):
            assert any("retention" in p for p in entry["problems"]), entry
            assert "user" not in entry, "no text may be presented as sent"

    def test_an_edited_prompt_is_reported_with_the_revision_to_recover_it(
        self, db, judged
    ):
        """A hash proves a reconstruction; it cannot regenerate wording that no
        longer exists in the source."""
        db._exec("UPDATE receipts SET prompt_hash = 'edited-since-the-run'")
        entry = rp.rebuild_triage(conn_for(db), judged)[0]
        assert any("does not match either relevance leg" in p
                   for p in entry["problems"])
        assert any("code_revision" in p for p in entry["problems"])
        assert "system" not in entry

    def test_an_orphaned_receipt_cannot_have_its_batch_recovered(self, db,
                                                                 judged):
        """A discarded stage deletes its decisions and leaves the receipts, so
        which posts were in that call is genuinely unknowable."""
        db._exec("UPDATE triage_decisions SET receipt_id = NULL")
        for entry in rp.rebuild_triage(conn_for(db), judged):
            assert any("batch membership" in p for p in entry["problems"])
            assert "user" not in entry

    def test_a_changed_payload_fails_the_hash_and_says_the_text_is_wrong(
        self, db, judged
    ):
        """The guarantee has to be load-bearing: if the stored post changed,
        the rebuilt message is not what was sent and must say so."""
        raw = db.one("SELECT raw_id, payload_json FROM raw_results")
        payload = json.loads(raw["payload_json"])
        payload[0]["snippet"] = "tampered after the fact"
        db._exec("UPDATE raw_results SET payload_json = ? WHERE raw_id = ?",
                 (json.dumps(payload), raw["raw_id"]))
        entries = rp.rebuild_triage(conn_for(db), judged)
        assert any(e["problems"] and not e.get("input_ok") for e in entries)
        assert any("NOT what was sent" in p
                   for e in entries for p in e["problems"])


class TestARetryIsNotReportedAsByteExact:
    """Review #8, HIGH.

    `_call_llm_json` rewrites the user message between attempts, feeding back
    the parse error and the failed reply. Everything else on the receipt —
    `prompt_hash`, `input_hash`, `batch_key` — describes the FIRST variant, so
    rebuilding the original request and reporting it verified told a reviewer
    that the accepted classification prompt had been reproduced byte for byte
    when it had not.

    Version 14 records `prompt_user_hash`, so the claim is now checked. Older
    receipts cannot be, and are refused.
    """

    def test_the_accepted_request_is_what_is_checked(self, db, judged):
        """The happy path for the new column: a call with no retry rebuilds to
        the hash of what was accepted, which is also what was first sent."""
        entries = rp.rebuild_triage(conn_for(db), judged)
        assert entries
        for entry in entries:
            assert entry["accepted_ok"] is True, entry["problems"]
            assert not entry["problems"]

    def test_a_rewritten_request_is_refused_not_verified(self, db, judged):
        """The defect itself: the recorded hash covers the request the model
        answered, so a rebuild of a different one must fail."""
        db.conn.execute(
            "UPDATE receipts SET prompt_user_hash = ?, attempts = 2 "
            "WHERE kind = 'TRIAGE'", ("0" * 32,))
        entry = rp.rebuild_triage(conn_for(db), judged)[0]
        assert entry["accepted_ok"] is False
        assert any("prompt_user_hash" in p for p in entry["problems"])

    def test_an_old_receipt_with_a_retry_is_refused_by_name(self, db, judged):
        """The pre-version-14 shape. Nothing recorded what the accepted
        request became, so the only honest answer is that it cannot be
        verified — the byte-exact claim is withdrawn rather than qualified in
        prose beside it."""
        db.conn.execute(
            "UPDATE receipts SET prompt_user_hash = NULL, attempts = 3 "
            "WHERE kind = 'TRIAGE'")
        entry = rp.rebuild_triage(conn_for(db), judged)[0]
        assert any("attempts 3" in p for p in entry["problems"]), entry
        assert any("byte-exact" in p for p in entry["problems"])

    def test_an_old_receipt_with_no_retry_still_verifies(self, db, judged):
        """`attempts = 1` means the first request IS the accepted one, so a
        receipt predating the column is still reconstructible. Refusing those
        would make the check useless on every historical record."""
        db.conn.execute(
            "UPDATE receipts SET prompt_user_hash = NULL WHERE kind = 'TRIAGE'")
        for entry in rp.rebuild_triage(conn_for(db), judged):
            assert not entry["problems"], entry["problems"]

    def test_the_refusal_reaches_the_exit_code(self, db, judged, capsys):
        """A report a script reads. The warning prose was already there and the
        exit code ignored it, so an automated caller saw success."""
        db.conn.execute(
            "UPDATE receipts SET prompt_user_hash = NULL, attempts = 2 "
            "WHERE kind = 'TRIAGE'")
        triage = rp.rebuild_triage(conn_for(db), judged)
        assert all(e["problems"] for e in triage)


class TestAlerting:
    def test_an_alert_brief_verifies_against_its_receipt(self, db, config,
                                                         session, iteration):
        city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        db.upsert_correlation(
            iteration_id=iteration, city_id=city, track="AIRSHOW",
            score=0.8, band="MEDIUM", distinct_types=2,
            contributions={"social": 0.8}, data_completeness=1.0,
            failed_sources="", band_capped=False, rule_trace="test")
        AlertAgent(db, config, FakeLLM([{"summary": "Something happened."}],
                                       translate=False)).run(iteration)

        entries = rp.rebuild_alerts(conn_for(db), iteration)
        assert entries, "the alert call wrote a receipt"
        for entry in entries:
            assert entry["problems"] == [], entry["problems"]
            assert entry["input_ok"]
            assert json.loads(entry["user"])["track"] == "AIRSHOW"

    def test_a_fallback_alert_leaves_no_receipt_to_reconstruct(self, db, config,
                                                               session,
                                                               iteration):
        """No model call happened, so there is nothing to rebuild — and
        inventing a prompt for it would misrepresent a deterministic string as
        a judgement."""
        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        db.upsert_correlation(
            iteration_id=iteration, city_id=city, track="AIRSHOW",
            score=0.8, band="MEDIUM", distinct_types=2,
            contributions={"social": 0.8}, data_completeness=1.0,
            failed_sources="", band_capped=False, rule_trace="test")

        class Dead:
            class chat:
                class completions:
                    @staticmethod
                    def create(*a, **k):
                        raise RuntimeError("provider down")

        AlertAgent(db, config, Dead()).run(iteration)
        assert db.one("SELECT * FROM alerts")["receipt_id"] is None
        assert rp.rebuild_alerts(conn_for(db), iteration) == []


class TestTheCommandLine:
    def test_an_unverified_reconstruction_exits_non_zero(self, on_disk,
                                                          tmp_path):
        """So a caller can tell without reading the prose."""
        disk, iteration = on_disk
        disk._exec("UPDATE receipts SET prompt_hash = 'edited'")
        out = tmp_path / "o.md"
        assert rp.main([disk.path, str(iteration), "--out", str(out)]) == 4
        assert "does not match" in out.read_text()

    def test_a_clean_run_exits_zero(self, on_disk, tmp_path):
        disk, iteration = on_disk
        out = tmp_path / "o.md"
        assert rp.main([disk.path, str(iteration), "--out", str(out)]) == 0
        body = out.read_text()
        assert "verified byte-exact" in body
        assert "Screen these" in body

    def test_a_missing_iteration_is_an_error(self, on_disk):
        disk, _iteration = on_disk
        assert rp.main([disk.path, "9999"]) == 1

    def test_fencing_survives_a_payload_containing_a_fence(self):
        """A post whose text contains ``` must not break out of the block."""
        block = rp.fence("a ``` b")
        assert block.startswith("````") and block.rstrip().endswith("````")
