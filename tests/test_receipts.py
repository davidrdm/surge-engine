"""Classification receipts — 8.1.

The point of a receipt is that a judgement can be re-examined later: under which
criteria, from which model snapshot, over which inputs. So these tests are about
the properties that make that possible, not about the columns existing.

The load-bearing one is `test_the_hash_separates_decisions_even_when_the_label_lies`:
a version label is written by a human who may forget, and a record whose
integrity depends on someone remembering is not a record.
"""
from __future__ import annotations

import json

import pytest

from surge_iw.agents.triage import TriageAgent
from surge_iw.db import enums
from surge_iw.services import receipts
from test_triage import FakeLLM, store_posts


class TestTheReceiptService:
    def test_the_hash_separates_decisions_even_when_the_label_lies(self):
        """The guarantee. Edit a prompt, forget the version bump, and the two
        wordings are still distinguishable in the record."""
        v1 = "Judge relevance conservatively."
        v2 = "Judge relevance conservatively. Ignore retractions."
        assert receipts.sha256_hex(v1) != receipts.sha256_hex(v2)
        # Same label, different criteria — the label cannot save you, the hash
        # can. This is why prompt_hash is NOT NULL and prompt_version is a note.
        assert receipts.sha256_hex(v1) == receipts.sha256_hex(v1)

    def test_the_config_hash_ignores_deployment_and_tracks_analysis(self):
        """Two deployments reasoning identically must compare equal; a moved
        threshold must not."""
        base = {"database": {"path": "a.db"}, "api": {"port": 8000},
                "correlation": {"radius_km": 15.0}}
        moved_deployment = {**base, "database": {"path": "/srv/b.db"},
                            "api": {"port": 9999}}
        moved_analysis = {**base, "correlation": {"radius_km": 50.0}}
        assert (receipts.config_fingerprint(base)
                == receipts.config_fingerprint(moved_deployment))
        assert (receipts.config_fingerprint(base)
                != receipts.config_fingerprint(moved_analysis))

    def test_the_config_hash_does_not_depend_on_key_order(self):
        a = {"correlation": {"radius_km": 15.0, "window_hours": 48}}
        b = {"correlation": {"window_hours": 48, "radius_km": 15.0}}
        assert receipts.config_fingerprint(a) == receipts.config_fingerprint(b)

    def test_the_input_hash_covers_the_truncation_window(self):
        """8.4's head+tail decides what the model actually saw, so a change
        there has to move the input hash — otherwise two judgements over
        different evidence would look identical."""
        assert (receipts.evidence_hash([{"text": "abc"}])
                != receipts.evidence_hash([{"text": "abc [...] xyz"}]))

    def test_an_absent_provider_field_stays_absent(self):
        """Most OpenAI-compatible endpoints omit system_fingerprint. Recording
        a default would later read as a fact about what served the answer."""
        class Bare:
            model = ""

        echo = receipts.ProviderEcho.from_response(Bare())
        assert echo.model_served is None
        assert echo.system_fingerprint is None
        assert echo.response_id is None

    def test_the_public_view_exposes_the_hash_and_never_the_prompt(self):
        row = {"receipt_id": 1, "prompt_hash": "abc", "prompt_version": "v/1",
               "model_served": "gemini-3.5-flash-002", "created_at": "now",
               "kind": "TRIAGE", "batch_key": "secret-ish", "input_hash": "h"}
        view = receipts.public_view(row)
        assert view["prompt_hash"] == "abc"
        assert view["model_served"] == "gemini-3.5-flash-002"
        # batch_key is internal plumbing, not evidence.
        assert "batch_key" not in view
        assert not any("prompt_text" in k for k in view)

    def test_public_view_of_nothing_is_nothing(self):
        assert receipts.public_view(None) is None


class TestTheWriter:
    def test_an_unknown_field_is_refused_rather_than_dropped(self, db):
        """A provenance field that vanishes without complaint is worse than no
        provenance: the record still looks complete."""
        row = receipts.Receipt(
            kind="TRIAGE", model_requested="m", prompt_version="v",
            prompt_hash="h", input_hash="i", config_hash="c").as_row()
        with pytest.raises(ValueError, match="unknown receipt field"):
            db.insert_receipt(None, {**row, "invented_column": "x"})

    def test_the_dataclass_and_the_writer_cannot_drift(self, db):
        """Every field of Receipt.as_row() must be a column the writer knows.
        This is the test that fails when someone adds one and forgets the
        other."""
        row = receipts.Receipt(
            kind="TRIAGE", model_requested="m", prompt_version="v",
            prompt_hash="h", input_hash="i", config_hash="c").as_row()
        assert set(row) <= set(db._RECEIPT_COLUMNS)
        assert db.insert_receipt(None, row) > 0

    def test_the_kind_is_validated(self, db):
        row = receipts.Receipt(
            kind="GUESSING", model_requested="m", prompt_version="v",
            prompt_hash="h", input_hash="i", config_hash="c").as_row()
        with pytest.raises(ValueError):
            db.insert_receipt(None, row)

    def test_schema_and_enum_agree_on_the_kinds(self, db):
        """The same drift guard TestSchemaEnumConsistency applies elsewhere."""
        sql = (db.one("SELECT sql FROM sqlite_master WHERE name = 'receipts'")
               ["sql"])
        for kind in enums.RECEIPT_KINDS:
            assert f"'{kind}'" in sql


class TestTriageStampsItsJudgements:
    def _judged(self, db, config, session, iteration, decisions):
        db.insert_city(session, "Phoenix", canonical="phoenix")
        config["triage"]["max_post_age_hours"] = 24 * 400
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="r")
        store_posts(db, iteration, [
            {"url": "https://example.com/a", "title": "Display team lands in Phoenix",
             "author": "x", "platform": "twitter", "source_domain": "example.com",
             "snippet": "vans massing", "observed_at": "2026-07-27T10:00:00+00:00"},
        ], query)
        TriageAgent(db, config, FakeLLM(decisions)).run(iteration)

    def test_every_decision_carries_a_receipt(self, db, config, session,
                                              iteration):
        self._judged(db, config, session, iteration, [{
            "url": "https://example.com/a", "relevant": False,
            "track": "UNKNOWN", "cities": [], "locations": [],
            "activity_type": "", "imminence_hours": None, "salience": 0.1,
            "rationale": "not relevant",
        }])
        row = db.one("SELECT * FROM triage_decisions")
        assert row["receipt_id"] is not None
        receipt = db.get_receipt(row["receipt_id"])
        assert receipt["kind"] == "TRIAGE"
        assert receipt["prompt_version"]
        assert receipt["prompt_hash"]
        assert receipt["config_hash"]
        assert receipt["input_hash"]

    def test_a_failed_call_still_leaves_a_receipt(self, db, config, session,
                                                  iteration):
        """A MODEL_ERROR row is a coverage gap. Without a receipt it would be
        the one judgement in the system with no account of itself — and which
        prompt version and configuration failed is precisely what a reader of
        that row needs."""
        class Dead:
            class chat:
                class completions:
                    @staticmethod
                    def create(*a, **k):
                        raise RuntimeError("provider down")

        self._judged(db, config, session, iteration, [])
        db._exec("DELETE FROM triage_decisions")
        TriageAgent(db, config, Dead()).run(iteration)
        row = db.one("SELECT * FROM triage_decisions")
        assert row["state"] == "MODEL_ERROR"
        assert row["receipt_id"] is not None
        receipt = db.get_receipt(row["receipt_id"])
        # No answer arrived, so nothing is echoed — recorded as absent.
        assert receipt["model_served"] is None
        # But what was attempted is fully recorded.
        assert receipt["prompt_hash"] and receipt["config_hash"]

    def test_one_batch_yields_one_receipt_shared_by_its_decisions(
        self, db, config, session, iteration
    ):
        """The reason receipts are a table and not fourteen more columns."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        config["triage"]["max_post_age_hours"] = 24 * 400
        config["triage"]["batch_size"] = 10
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="r")
        posts = [{"url": f"https://example.com/{i}", "title": "t", "author": "a",
                  "platform": "twitter", "source_domain": "example.com",
                  "snippet": "s", "observed_at": "2026-07-27T10:00:00+00:00"}
                 for i in range(3)]
        store_posts(db, iteration, posts, query)
        TriageAgent(db, config, FakeLLM([
            {"url": p["url"], "relevant": False, "track": "UNKNOWN",
             "cities": [], "locations": [], "activity_type": "",
             "imminence_hours": None, "salience": 0.1, "rationale": "no"}
            for p in posts
        ])).run(iteration)

        ids = {r["receipt_id"] for r in db.all("SELECT * FROM triage_decisions")}
        assert len(ids) == 1, "three decisions from one call share one receipt"
        assert db.scalar("SELECT COUNT(*) FROM receipts") == 1


class TestTheEvidenceSurface:
    """What a reader of GET /v1/alerts/{id}/evidence actually gets.

    Asserted against the generated contract example rather than a hand-built
    fixture, so the documented artifact and the behaviour cannot diverge.
    """

    @pytest.fixture(scope="class")
    def evidence(self):
        import pathlib
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
        return json.loads(path.read_text())["response"]["body"]

    def test_a_model_written_summary_carries_its_receipt(self, evidence):
        assert evidence["receipt"] is not None
        assert evidence["receipt"]["kind"] == "ALERT"

    def test_a_social_signal_traces_to_the_judgement_that_made_it(self, evidence):
        social = [s for s in evidence["signals"]
                  if s["signal"]["signal_type"] == "SOCIAL"]
        assert social, "the example must contain a social signal"
        assert all(s["receipt"] and s["receipt"]["kind"] == "TRIAGE"
                   for s in social)

    def test_a_deterministic_signal_says_no_model_was_involved(self, evidence):
        """`null` is the answer, not an omission. A flight record is normalised
        from a vendor payload; claiming a receipt would imply a judgement that
        never happened."""
        others = [s for s in evidence["signals"]
                  if s["signal"]["signal_type"] != "SOCIAL"]
        assert others, "the example must contain a non-social signal"
        assert all(s["receipt"] is None for s in others)

    def test_the_prompt_text_is_never_on_the_wire(self, evidence):
        blob = json.dumps(evidence)
        assert "prompt_hash" in blob
        assert "You are an intelligence analyst" not in blob
