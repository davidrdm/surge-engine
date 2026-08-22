"""Every reason a collected post never reached the model — 8.9.

`TriageAgent._gather()` had five drops. One was a bare count and four left no
trace at all, two of those discarding a whole vendor payload — so evidence that
was collected and paid for could vanish with nothing to say it had. An absence
of evidence produced by a parse failure is indistinguishable from an absence of
the thing being watched for, which is the failure this system is organised
against.

Two tests carry the design:

  * `test_a_stale_post_writes_no_decision_row` — the invariant. Skips outnumber
    judgements, so putting them in `triage_decisions` would move every count
    over that table, including the SOCIAL coverage gap and 8.8's retry set.
  * `test_a_lost_payload_degrades_the_iteration` — the reason this is more than
    bookkeeping. A run that lost a vendor response must not close COMPLETE.
"""
from __future__ import annotations

import json

import pytest

from surge_iw.agents.triage import TriageAgent
from surge_iw.db import enums
from surge_iw.db.database import parse_iso
from test_triage import FakeLLM, decision, post, store_posts


def stored(db, iteration_id, query_id, payload):
    """Store a raw SOCIAL payload verbatim, including a malformed one."""
    return db.insert_raw_result(
        query_id=query_id, iteration_id=iteration_id, source_type="SOCIAL",
        provider="APIDIRECT", payload=payload, retention_days=90)


@pytest.fixture
def social_query(db, session, iteration):
    return db.enqueue_query(
        session_id=session, iteration_id=iteration, source_type="SOCIAL",
        endpoint="/v1/twitter/posts", params={}, dedup_key="k1",
    )


@pytest.fixture
def city(db, session):
    return db.insert_city(session, "Phoenix", canonical="phoenix")


class TestTheStaleSkip:
    def test_a_stale_post_writes_no_decision_row(self, db, config, session,
                                                 iteration, social_query, city):
        """The invariant everything else depends on. `triage_decisions` means
        'a model call was made and this came back'; a skipped post was never
        asked about."""
        store_posts(db, iteration, [post("https://x.com/old",
                                         hours_ago=24 * 400)], social_query)
        TriageAgent(db, config, FakeLLM([])).run(iteration)

        assert db.scalar("SELECT COUNT(*) FROM triage_decisions") == 0
        assert db.triage_skip_counts(iteration) == {"STALE": 1}

    def test_it_records_the_cutoff_not_just_the_fact(self, db, config, session,
                                                     iteration, social_query,
                                                     city):
        """The cutoff is `utcnow() - max_post_age_hours` at gather time, so a
        reader recomputing it later gets a different answer than the run did.
        Storing it makes the decision reproducible from the row."""
        store_posts(db, iteration, [post("https://x.com/old",
                                         hours_ago=24 * 400)], social_query)
        TriageAgent(db, config, FakeLLM([])).run(iteration)

        row = db.triage_skips(iteration, "STALE")[0]
        assert row["url"] == "https://x.com/old"
        assert row["raw_id"] is not None
        assert row["observed_at"] and row["cutoff_at"]
        assert parse_iso(row["observed_at"]) < parse_iso(row["cutoff_at"])
        assert row["max_post_age_hours"] == config["triage"]["max_post_age_hours"]

    def test_a_stale_post_is_not_a_coverage_gap(self, db, config, session,
                                                iteration, social_query, city):
        """Choosing not to judge a week-old post is the system working as
        configured. Counting it would cap the band on every city in every
        ordinary run — the median collected post was 206 days old."""
        store_posts(db, iteration, [post("https://x.com/old",
                                         hours_ago=24 * 400)], social_query)
        TriageAgent(db, config, FakeLLM([])).run(iteration)

        assert db.triage_uncovered(iteration) == 0
        assert db.degradation_notes(iteration) == []

    def test_a_stale_post_is_not_retryable(self, db, config, session,
                                           iteration, social_query, city):
        """8.8's candidate set must not reach it. Guaranteed by the row living
        in a different table, not by a state being absent from an enum."""
        store_posts(db, iteration, [post("https://x.com/old",
                                         hours_ago=24 * 400)], social_query)
        TriageAgent(db, config, FakeLLM([])).run(iteration)
        assert db.uncovered_triage_decisions(iteration) == []


class TestMalformedPayloads:
    def test_an_unparseable_payload_is_recorded(self, db, config, session,
                                                iteration, social_query, city):
        db._exec(
            "INSERT INTO raw_results (query_id, iteration_id, source_type, "
            "provider, payload_json, retrieved_at, purge_after) "
            "VALUES (?,?,?,?,?,datetime('now'),datetime('now','+90 day'))",
            (social_query, iteration, "SOCIAL", "APIDIRECT", "{not json"))
        TriageAgent(db, config, FakeLLM([])).run(iteration)

        rows = db.triage_skips(iteration, "PAYLOAD_UNPARSEABLE")
        assert len(rows) == 1
        assert rows[0]["url"] is None, "nothing to name; never invented"
        assert rows[0]["detail"]

    def test_a_payload_that_is_not_a_list_is_recorded(self, db, config,
                                                      session, iteration,
                                                      social_query, city):
        stored(db, iteration, social_query, {"posts": []})
        TriageAgent(db, config, FakeLLM([])).run(iteration)

        rows = db.triage_skips(iteration, "PAYLOAD_NOT_A_LIST")
        assert len(rows) == 1
        assert "dict" in rows[0]["detail"]

    def test_a_lost_payload_degrades_the_iteration(self, db, config, session,
                                                   iteration, social_query,
                                                   city):
        """A malformed payload is a DEFECT, not a decision: it removes evidence
        that was collected and paid for, and takes every post in the response
        with it. A run that lost one must not close COMPLETE."""
        stored(db, iteration, social_query, {"posts": []})
        TriageAgent(db, config, FakeLLM([])).run(iteration)

        notes = db.degradation_notes(iteration)
        assert any("unusable" in n and "SOCIAL coverage is incomplete" in n
                   for n in notes), notes

    def test_a_stale_post_alone_does_not_degrade(self, db, config, session,
                                                 iteration, social_query, city):
        """The other half of the same distinction, so the two cannot drift."""
        store_posts(db, iteration, [post("https://x.com/old",
                                         hours_ago=24 * 400)], social_query)
        TriageAgent(db, config, FakeLLM([])).run(iteration)
        assert db.degradation_notes(iteration) == []

    def test_the_split_is_driven_by_the_enum(self):
        """One definition of which skips cost a whole response."""
        assert enums.PAYLOAD_LEVEL_SKIPS < enums.TRIAGE_SKIP_REASONS
        assert "STALE" not in enums.PAYLOAD_LEVEL_SKIPS


class TestItemLevelDrops:
    def test_a_non_object_element_is_recorded(self, db, config, session,
                                              iteration, social_query, city):
        stored(db, iteration, social_query, ["just a string", 42])
        TriageAgent(db, config, FakeLLM([])).run(iteration)

        assert db.triage_skip_counts(iteration) == {"ITEM_NOT_AN_OBJECT": 2}

    def test_an_item_with_no_url_is_recorded(self, db, config, session,
                                             iteration, social_query, city):
        """A post with no URL has nothing to bind a judgement to — `item_id`
        binding needs one, and inventing a key is what the Phase 6 review
        removed from this boundary."""
        stored(db, iteration, social_query,
               [{"title": "t", "snippet": "s", "observed_at": ""}])
        TriageAgent(db, config, FakeLLM([])).run(iteration)

        rows = db.triage_skips(iteration, "ITEM_NO_URL")
        assert len(rows) == 1
        assert rows[0]["url"] is None

    def test_a_deduplicated_post_is_not_a_skip(self, db, config, session,
                                               iteration, social_query, city):
        """The same article legitimately surfaces from several queries. It is
        judged once and that judgement IS the record — counting it here would
        inflate the skip count with work that was done."""
        duplicate = post("https://x.com/1", hours_ago=2)
        stored(db, iteration, social_query, [duplicate])
        stored(db, iteration, social_query, [duplicate])

        TriageAgent(db, config,
                    FakeLLM([decision("https://x.com/1")])).run(iteration)

        assert db.triage_skip_counts(iteration) == {}
        assert db.scalar("SELECT COUNT(*) FROM triage_decisions") == 1


class TestTheRecordIsUsable:
    def test_counts_group_by_reason(self, db, config, session, iteration,
                                    social_query, city):
        stored(db, iteration, social_query, ["a string"])
        stored(db, iteration, social_query, {"not": "a list"})
        store_posts(db, iteration, [post("https://x.com/old",
                                         hours_ago=24 * 400)], social_query)
        TriageAgent(db, config, FakeLLM([])).run(iteration)

        counts = db.triage_skip_counts(iteration)
        assert counts == {"ITEM_NOT_AN_OBJECT": 1, "PAYLOAD_NOT_A_LIST": 1,
                          "STALE": 1}
        assert set(counts) <= enums.TRIAGE_SKIP_REASONS

    def test_an_unknown_reason_is_refused(self, db, iteration):
        with pytest.raises(ValueError):
            db.insert_triage_skip(iteration_id=iteration, reason="BECAUSE")

    def test_schema_and_enum_agree(self, db):
        """The same drift guard the other vocabularies carry."""
        sql = db.one("SELECT sql FROM sqlite_master "
                     "WHERE name = 'triage_skips'")["sql"]
        for reason in enums.TRIAGE_SKIP_REASONS:
            assert f"'{reason}'" in sql

    def test_the_log_names_every_reason(self, db, config, session, iteration,
                                        social_query, city):
        stored(db, iteration, social_query, {"not": "a list"})
        TriageAgent(db, config, FakeLLM([])).run(iteration)
        row = db.one("SELECT * FROM agent_log "
                     "WHERE message LIKE '%did not reach the model%'")
        assert row and "PAYLOAD_NOT_A_LIST" in row["message"]

    def test_nothing_is_logged_when_nothing_was_dropped(self, db, config,
                                                        session, iteration,
                                                        social_query, city):
        store_posts(db, iteration, [post("https://x.com/1", hours_ago=2)],
                    social_query)
        TriageAgent(db, config,
                    FakeLLM([decision("https://x.com/1")])).run(iteration)
        assert db.triage_skip_counts(iteration) == {}
        assert db.one("SELECT * FROM agent_log "
                      "WHERE message LIKE '%did not reach the model%'") is None

    def test_the_triaging_stage_view_carries_them(self, db, config, session,
                                                  iteration, social_query,
                                                  city):
        """Recording without a route answers the operator's question only for
        someone willing to open the database — the 8.7(b) argument."""
        from surge_iw.services.stages import StageInspector

        stored(db, iteration, social_query, {"not": "a list"})
        store_posts(db, iteration, [post("https://x.com/old",
                                         hours_ago=24 * 400)], social_query)
        TriageAgent(db, config, FakeLLM([])).run(iteration)
        db.finish_agent_run(
            db.start_agent_run(iteration, "IterationOrchestrator", "TRIAGING"),
            "COMPLETE")

        inspector = StageInspector(db)
        assert inspector.report(iteration, "TRIAGING").skips == {
            "PAYLOAD_NOT_A_LIST": 1, "STALE": 1}
        assert inspector.report(iteration, "SEEDING").skips == {}
