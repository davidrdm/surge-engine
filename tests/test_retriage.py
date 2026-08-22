"""Re-triage: recovering the posts a model failure lost — 8.8.

A batch that overruns `llm.max_tokens` records **every** post in it as
`MODEL_ERROR`. The evidence was collected and paid for; only the judgement is
missing. Measured at 40 posts across four batches during the broad-leg run.

Two tests carry the design and the rest support them:

  * `test_only_uncovered_decisions_are_retried` — ACCEPTED and REJECTED are
    completed judgements and a rejection is a conclusion, not a failure.
  * `test_the_child_inherits_the_parents_anchor` — the correlation window is
    measured from `anchor_at`, so a fresh anchor would slide it off the very
    evidence the retry exists to complete.
"""
from __future__ import annotations

import json

import pytest

import test_orchestrator as T
from surge_iw.agents.orchestrator import (
    IterationOrchestrator, SessionHasOpenIteration,
)
from surge_iw.connectors.flightradar import _normalise_live, _normalise_summary
from surge_iw.connectors.priceline import parse_rental_car_response
from surge_iw.db import enums
from surge_iw.db.database import parse_iso
from surge_iw.services.budget import BudgetGuard
from test_collection import fixture
from test_triage import FakeLLM


def build(db, config, connectors, llm=None):
    return IterationOrchestrator(db, config, connectors, llm_client=llm,
                                 budget=BudgetGuard(db, config))


@pytest.fixture
def wiring(db, config):
    """A Phoenix session plus stub connectors, as in test_orchestrator."""
    config["tipping"]["max_queries_per_city"] = 99
    config["triage"]["batch_size"] = 20
    session = db.insert_session(label="AZ", expand_cities=False,
                                tracks=["AIRSHOW"], config=config)
    city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
    db.insert_key_location(city, "Riverside Fairground",
                           location_type="FAIRGROUND")
    connectors = {
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
    llm = FakeLLM(*[[T.decision("https://x.com/1", "Phoenix"),
                     T.decision("https://apnews.com/2", "Phoenix")]] * 40)
    return session, city, connectors, llm


class Truncating:
    """A model whose reply always overruns the ceiling, until it doesn't.

    `fails_above` is the batch size at or below which it starts answering —
    which is exactly the behaviour the halving loop exists to exploit.
    """

    def __init__(self, inner: FakeLLM, fails_above: int = 0):
        self.inner = inner
        self.fails_above = fails_above
        self.sizes: list[int] = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        prompt = kwargs["messages"][-1]["content"]
        size = prompt.count('"item_id"')
        self.sizes.append(size)
        if size > self.fails_above:
            class _Msg:
                content = '[{"item_id": "trunc'          # cut off mid-answer

            class _Choice:
                message = _Msg()
                finish_reason = "length"

            class _Resp:
                choices = [_Choice()]
                usage = None
            return _Resp()
        return self.inner.create(**kwargs)


def social_posts(n: int) -> list[dict]:
    return [T.post(f"https://x.com/{i}", "x.com") for i in range(n)]


@pytest.fixture
def parent_with_gaps(db, config, wiring):
    """A finished iteration whose whole triage batch was lost to truncation."""
    session, city, connectors, llm = wiring
    config["triage"]["batch_size"] = 20
    orch = build(db, config, connectors, Truncating(llm, fails_above=0))
    iteration = orch.start(session)
    orch.run(iteration)
    return session, iteration, connectors, llm


class TestTheCandidateSet:
    def test_only_uncovered_decisions_are_retried(self, db, config,
                                                  parent_with_gaps):
        """ACCEPTED and REJECTED are completed judgements. A rejection is a real
        analytical conclusion — re-judging it would spend tokens to overwrite an
        answer the system already has."""
        _session, parent, _connectors, _llm = parent_with_gaps
        rows = db.uncovered_triage_decisions(parent)
        assert rows, "the premise: the parent lost judgements"
        assert {r["state"] for r in rows} <= enums.TRIAGE_UNCOVERED

        db._exec("UPDATE triage_decisions SET state = 'REJECTED' "
                 "WHERE iteration_id = ? AND triage_id = "
                 "(SELECT MIN(triage_id) FROM triage_decisions "
                 " WHERE iteration_id = ?)", (parent, parent))
        assert len(db.uncovered_triage_decisions(parent)) == len(rows) - 1

    def test_a_post_dropped_for_age_cannot_appear(self, db, config, session,
                                                   iteration):
        """It leaves no decision row at all, so the requirement holds by
        construction rather than by a second freshness filter that could drift
        from the first."""
        from test_triage import store_posts

        db.insert_city(session, "Phoenix", canonical="phoenix")
        config["triage"]["max_post_age_hours"] = 1.0
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="stale")
        store_posts(db, iteration, [{
            "url": "https://x.com/old", "title": "t", "author": "a",
            "platform": "twitter", "source_domain": "x.com", "snippet": "s",
            "observed_at": "2020-01-01T00:00:00+00:00"}], query)

        from surge_iw.agents.triage import TriageAgent
        TriageAgent(db, config, FakeLLM([])).run(iteration)

        assert db.all("SELECT * FROM triage_decisions") == []
        assert db.uncovered_triage_decisions(iteration) == []

    def test_it_reads_decisions_not_raw_results(self, db, config,
                                                parent_with_gaps):
        """The staleness cutoff is computed from `utcnow()` at gather time, so
        re-gathering later would silently process a different set — the retry
        would cover less than asked and nothing would say so."""
        _session, parent, _connectors, _llm = parent_with_gaps
        config["triage"]["max_post_age_hours"] = 0.0001   # everything is stale
        rows = db.uncovered_triage_decisions(parent)
        assert rows, "unaffected by a cutoff that would drop them on a re-gather"


class TestTheChild:
    def test_it_is_a_new_iteration_and_the_parent_is_untouched(
        self, db, config, parent_with_gaps
    ):
        session, parent, connectors, llm = parent_with_gaps
        before = dict(db.get_iteration(parent))

        orch = build(db, config, connectors, llm)
        child, outcome = orch.retry_triage(parent)

        assert child != parent
        assert db.get_iteration(child)["retry_of_iteration_id"] == parent
        after = dict(db.get_iteration(parent))
        assert after == before, "the parent is a record, not a draft"
        assert outcome in ("COMPLETE", "PARTIAL")

    def test_the_child_inherits_the_parents_anchor(self, db, config,
                                                   parent_with_gaps):
        """The load-bearing one. The correlation window is `anchor - window`,
        so a fresh anchor slides it forward and drops the oldest of the parent's
        evidence — the evidence the retry exists to complete."""
        session, parent, connectors, llm = parent_with_gaps
        orch = build(db, config, connectors, llm)
        child, _outcome = orch.retry_triage(parent)

        assert (db.get_iteration(child)["anchor_at"]
                == db.get_iteration(parent)["anchor_at"])
        # started_at still records when it actually ran: the two facts stay
        # separable rather than one being overwritten by the other.
        assert (parse_iso(db.get_iteration(child)["started_at"])
                >= parse_iso(db.get_iteration(parent)["started_at"]))

    def test_the_judgements_are_recovered(self, db, config, parent_with_gaps):
        session, parent, connectors, llm = parent_with_gaps
        lost = len(db.uncovered_triage_decisions(parent))
        assert lost

        orch = build(db, config, connectors, llm)
        child, _outcome = orch.retry_triage(parent)

        judged = db.all("SELECT * FROM triage_decisions WHERE iteration_id = ?",
                        (child,))
        assert len(judged) == lost
        assert all(r["state"] not in enums.TRIAGE_UNCOVERED for r in judged), \
            "the retry answered them"
        # And the parent's rows are still its own record of what it lost.
        assert len(db.uncovered_triage_decisions(parent)) == lost

    def test_each_attempt_carries_its_own_receipt(self, db, config,
                                                  parent_with_gaps):
        """Two rows for one URL across two iterations is correct: they are two
        attempts recorded against the two runs that made them."""
        session, parent, connectors, llm = parent_with_gaps
        orch = build(db, config, connectors, llm)
        child, _outcome = orch.retry_triage(parent)

        url = db.one("SELECT url FROM triage_decisions WHERE iteration_id = ?",
                     (child,))["url"]
        rows = db.all("SELECT iteration_id, receipt_id FROM triage_decisions "
                      "WHERE url = ? ORDER BY iteration_id", (url,))
        assert len(rows) == 2
        assert {r["iteration_id"] for r in rows} == {parent, child}
        assert rows[0]["receipt_id"] != rows[1]["receipt_id"]


class TestWhichStagesRun:
    def _stages(self, db, iteration_id):
        return {r["stage"] for r in db.all(
            "SELECT DISTINCT stage FROM agent_runs WHERE iteration_id = ?",
            (iteration_id,))}

    def test_collection_is_inherited_not_re_run(self, db, config,
                                                parent_with_gaps):
        """Re-seeding would enqueue the whole social set again and re-buy
        evidence already held."""
        session, parent, connectors, llm = parent_with_gaps
        orch = build(db, config, connectors, llm)
        child, _outcome = orch.retry_triage(parent)

        ran = self._stages(db, child)
        assert "SEEDING" not in ran
        assert "COLLECTING_SOCIAL" not in ran
        assert {"TRIAGING", "TIPPING", "CORRELATING", "ALERTING"} <= ran
        assert db.all(
            "SELECT * FROM query_queue WHERE iteration_id = ? "
            "AND source_type = 'SOCIAL'", (child,)) == []

    def test_scheduling_is_skipped_and_recorded(self, db, config,
                                                parent_with_gaps):
        """Owner decision. The parent already queued follow-ons for this
        evidence, and those rows carry `iteration_id IS NULL`, so a duplicate
        set would all be adopted by the next ordinary iteration."""
        session, parent, connectors, llm = parent_with_gaps
        orch = build(db, config, connectors, llm)
        child, _outcome = orch.retry_triage(parent)

        assert "SCHEDULING" not in self._stages(db, child)
        assert db.skipped_stages(child) == ["SCHEDULING"]

    def test_the_inherited_stages_are_not_recorded_as_skipped(
        self, db, config, parent_with_gaps
    ):
        """The distinction that matters. COLLECTING_SOCIAL is in
        `STAGE_SOURCE_TYPES`, so recording it as skipped would tell CORRELATING
        that SOCIAL is uncollected — on the one run whose entire purpose is to
        improve social coverage."""
        from surge_iw.base.scoring import source_types_for_skipped

        session, parent, connectors, llm = parent_with_gaps
        orch = build(db, config, connectors, llm)
        child, _outcome = orch.retry_triage(parent)

        skipped = db.skipped_stages(child)
        assert "COLLECTING_SOCIAL" not in skipped
        assert "SEEDING" not in skipped
        assert source_types_for_skipped(skipped) == [], (
            "skipping SCHEDULING costs this iteration no coverage")

    def test_the_child_says_what_it_is(self, db, config, parent_with_gaps):
        session, parent, connectors, llm = parent_with_gaps
        orch = build(db, config, connectors, llm)
        child, _outcome = orch.retry_triage(parent)
        notes = db.degradation_notes(child)
        assert any(f"Re-triage of iteration {parent}" in n for n in notes)


class TestRefusals:
    def test_an_open_parent_is_refused(self, db, config, wiring):
        """A retry is a new iteration, and a session runs one at a time."""
        session, _city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        parent = orch.start(session)                 # created, never finished
        with pytest.raises(ValueError, match="has not closed"):
            orch.retry_triage(parent)

    def test_an_open_sibling_is_refused(self, db, config, parent_with_gaps):
        session, parent, connectors, llm = parent_with_gaps
        orch = build(db, config, connectors, llm)
        orch.start(session)                          # a second, still open
        with pytest.raises(SessionHasOpenIteration):
            orch.retry_triage(parent)

    def test_nothing_to_retry_is_refused(self, db, config, wiring):
        """Refused rather than answered with an empty iteration: a run that
        judged nothing and closed COMPLETE is indistinguishable from one that
        worked."""
        session, _city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        parent = orch.start(session)
        orch.run(parent)                             # succeeds; no gaps
        assert db.uncovered_triage_decisions(parent) == []
        with pytest.raises(ValueError, match="no unjudged posts"):
            orch.retry_triage(parent)

    def test_a_refusal_creates_no_iteration(self, db, config, wiring):
        session, _city, connectors, llm = wiring
        orch = build(db, config, connectors, llm)
        parent = orch.start(session)
        before = db.scalar("SELECT COUNT(*) FROM iterations")
        with pytest.raises(ValueError):
            orch.retry_triage(parent)
        assert db.scalar("SELECT COUNT(*) FROM iterations") == before


class TestTruncationIsDistinguishedFromAnOutage:
    def test_the_outcome_flags_truncation(self, db, config, session, iteration):
        """Recorded structurally, not parsed out of a message. Only one of the
        two failures is fixed by sending fewer items."""
        from surge_iw.agents.triage import TriageAgent
        from test_triage import store_posts

        db.insert_city(session, "Phoenix", canonical="phoenix")
        config["triage"]["max_post_age_hours"] = 24 * 4000
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="t")
        store_posts(db, iteration, social_posts(2), query)

        agent = TriageAgent(db, config, Truncating(FakeLLM([]), fails_above=0))
        payload = [{"item_id": "i1", "text": "x"}]
        outcome, _receipt = agent._judge(payload, ["i1"], iteration)
        assert outcome.truncated is True
        assert "TruncatedResponse" in outcome.batch_error

    def test_an_outage_is_not_flagged_as_truncation(self, db, config, session,
                                                    iteration):
        from surge_iw.agents.triage import TriageAgent

        agent = TriageAgent(
            db, config, FakeLLM([], error=RuntimeError("provider down")))
        outcome, _receipt = agent._judge(
            [{"item_id": "i1", "text": "x"}], ["i1"], iteration)
        assert outcome.truncated is False
        assert "RuntimeError" in outcome.batch_error

    def test_the_retry_halves_the_batch_rather_than_repeating_it(
        self, db, config, session, iteration
    ):
        """A batch that failed for being too large fails identically if
        re-sent, spending the quota and looking like a decision."""
        from surge_iw.agents.triage import TriageAgent
        from test_triage import store_posts

        db.insert_city(session, "Phoenix", canonical="phoenix")
        config["triage"]["max_post_age_hours"] = 24 * 4000
        config["triage"]["batch_size"] = 8
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="halve")
        posts = social_posts(8)
        store_posts(db, iteration, posts, query)

        inner = FakeLLM(*[[T.decision(p["url"], "Phoenix") for p in posts]] * 40)
        model = Truncating(inner, fails_above=2)
        TriageAgent(db, config, model).run(iteration)

        assert model.sizes[0] == 8, "first attempt at the configured size"
        assert min(model.sizes) <= 2, "shrank until the model could answer"
        rows = db.all("SELECT state FROM triage_decisions")
        assert len(rows) == 8
        assert all(r["state"] not in enums.TRIAGE_UNCOVERED for r in rows), \
            "all eight recovered rather than recorded as a coverage gap"


class TestPurgedEvidence:
    def test_a_purged_payload_is_reported_not_invented(self, db, config,
                                                       parent_with_gaps):
        """Retention deletes the payload but keeps the decision. Sending an
        empty body to a model would manufacture a judgement about nothing."""
        session, parent, connectors, llm = parent_with_gaps
        db._exec("UPDATE triage_decisions SET raw_id = NULL "
                 "WHERE iteration_id = ?", (parent,))
        assert all(r["payload_json"] is None
                   for r in db.uncovered_triage_decisions(parent))

        orch = build(db, config, connectors, llm)
        child, _outcome = orch.retry_triage(parent)
        assert db.all("SELECT * FROM triage_decisions WHERE iteration_id = ?",
                      (child,)) == []
        notes = db.degradation_notes(child)
        assert any("could not be re-judged" in n for n in notes)
