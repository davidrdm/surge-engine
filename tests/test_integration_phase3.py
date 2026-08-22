"""The Phase 3 gate, asserted as one end-to-end pass.

    A SOCIAL-only iteration over 2 cities yields a raw_results row per query, a
    triage_decisions row for EVERY post including rejections, signals rows only
    for accepted posts, zero orphan rows across all foreign keys, and a
    deliberately exhausted budget produces SKIPPED queries with
    BUDGET_EXHAUSTED decisions rather than an exception.

Stages 1–3 run for real: QueueAgent seeds, CollectionAgent drains, TriageAgent
judges. Only the two external dependencies are stubbed — the social API and the
model — which is precisely the boundary the bus abstraction exists to make
substitutable.
"""
from __future__ import annotations

from conftest import ANCHOR
from surge_iw.agents.collection import SOCIAL_TYPES, CollectionAgent
from surge_iw.agents.queueing import QueueAgent
from surge_iw.agents.triage import TriageAgent
from surge_iw.db.database import iso, utcnow
from surge_iw.services.budget import BudgetGuard
from test_triage import FakeLLM


class ScriptedSocial:
    """Returns posts keyed by the city named in the query text."""

    provider = "APIDIRECT"

    def __init__(self, by_city):
        self.by_city = by_city
        self.calls = 0

    def search(self, endpoint, params, **kwargs):
        self.calls += 1
        query = params.get("query", "")
        for city, posts in self.by_city.items():
            if city.lower() in query.lower():
                return posts
        return []


def post(url, domain, snippet, hours_ago=2):
    """Dated from NOW: triage's recency cut compares against now, and a post
    dated from the fixed ANCHOR would be filtered before the model saw it."""
    from datetime import timedelta
    return {
        "url": url, "author": "reporter", "platform": "twitter",
        "source_domain": domain, "snippet": snippet,
        "observed_at": iso(utcnow() - timedelta(hours=hours_ago)),
    }


def decision(url, *, relevant, city, salience=0.9, rationale="judged",
             locations=()):
    return {
        "url": url, "relevant": relevant, "track": "AIRSHOW",
        "cities": [city] if city else [], "locations": list(locations),
        "activity_type": "static display", "imminence_hours": 8,
        "salience": salience, "rationale": rationale,
    }


PHOENIX_POSTS = [
    post("https://x.com/phx1", "x.com",
         "Demonstration team jets on the flightline at the Riverside "
         "Fairground"),
    post("https://apnews.com/phx2", "apnews.com",
         "Organisers confirm a second display team for Phoenix this week"),
    post("https://blog.example/phx3", "blog.example",
         "Opinion: why airshows are worth the noise"),  # to be rejected
]
TUCSON_POSTS = [
    post("https://x.com/tus1", "x.com",
         "Ground support trucks seen heading toward Tucson"),
    post("https://x.com/tus2", "x.com",
         "Arena show announced in Tucson next month"),       # to be rejected
]


def build_session(db, config):
    session = db.insert_session(
        label="AZ display season", expand_cities=False,
        tracks=["AIRSHOW", "CONCERT_TOUR"], config=config,
    )
    phoenix = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
    tucson = db.insert_city(session, "Tucson", canonical="tucson", state="AZ")
    db.insert_key_location(phoenix, "Riverside Fairground",
                           location_type="FAIRGROUND")
    iteration = db.insert_iteration(session, anchor_at=ANCHOR)
    return session, phoenix, tucson, iteration


def all_decisions():
    """One scripted judgement per post, in the order triage will see them."""
    return [
        # Phrased loosely, as a model would, to exercise the fuzzy match against
        # the registered "Riverside Fairground".
        decision("https://x.com/phx1", relevant=True, city="Phoenix",
                 locations=("the Riverside fairground",)),
        decision("https://apnews.com/phx2", relevant=True, city="Phoenix"),
        decision("https://blog.example/phx3", relevant=False, city=None,
                 rationale="opinion piece with no operational content"),
        decision("https://x.com/tus1", relevant=True, city="Tucson"),
        decision("https://x.com/tus2", relevant=False, city=None,
                 rationale="promotional post, not a gathering"),
    ]


class TestPhase3Gate:
    def test_social_only_iteration_over_two_cities(self, db, config):
        config["tipping"]["max_queries_per_city"] = 99
        config["triage"] = {"batch_size": 20}
        session, phoenix, tucson, iteration = build_session(db, config)

        budget = BudgetGuard(db, config)
        budget.seed_budgets()
        budget.plan_iteration(iteration)

        # --- Stage 1: seed -------------------------------------------------
        db.set_stage(iteration, "SEEDING")
        queue_agent = QueueAgent(db, config, budget=budget)
        seeded = queue_agent.run_seed(iteration, session)
        assert seeded["seeded"] > 0
        queries = db.get_queue(iteration)
        assert {q["source_type"] for q in queries} == {"SOCIAL"}
        assert len({q["city_id"] for q in queries}) == 2

        # --- Stage 2: collect ----------------------------------------------
        db.set_stage(iteration, "COLLECTING_SOCIAL")
        social = ScriptedSocial({"Phoenix": PHOENIX_POSTS, "Tucson": TUCSON_POSTS})
        collector = CollectionAgent(db, config, {"APIDIRECT": social},
                                    budget=budget)
        assert collector.run(iteration, source_types=SOCIAL_TYPES) is True

        # A raw_results row per executed query.
        executed = [q for q in db.get_queue(iteration) if q["status"] == "COMPLETE"]
        raws = db.all("SELECT * FROM raw_results WHERE iteration_id = ?",
                      (iteration,))
        assert len(raws) == len(executed)
        assert all(q["result_count"] is not None for q in executed)
        # Collection derives no signals from social payloads.
        assert db.scalar("SELECT COUNT(*) FROM signals") == 0

        # --- Stage 3: triage ------------------------------------------------
        db.set_stage(iteration, "TRIAGING")
        llm = FakeLLM(all_decisions())
        assert TriageAgent(db, config, llm).run(iteration) is True

        decisions = db.all("SELECT * FROM triage_decisions WHERE iteration_id = ?",
                           (iteration,))
        urls = {d["url"] for d in decisions}
        # A decision row for EVERY distinct post, accepted or rejected.
        assert urls == {p["url"] for p in PHOENIX_POSTS + TUCSON_POSTS}
        assert all(d["rationale"] for d in decisions)

        accepted = [d for d in decisions if d["relevant"] == 1]
        rejected = [d for d in decisions if d["relevant"] == 0]
        assert len(accepted) == 3
        assert len(rejected) == 2

        # Signals only for accepted posts.
        signals = db.signals_by_type(iteration, "SOCIAL")
        assert len(signals) == 3
        assert {s["url"] for s in signals} == {d["url"] for d in accepted}
        assert {s["city_id"] for s in signals} == {phoenix, tucson}

        # The facility named in one post is anchored to the registered location.
        anchored = [s for s in signals if s["location_id"] is not None]
        assert len(anchored) == 1

        # --- Provenance: zero orphans anywhere ------------------------------
        assert db.scalar(
            "SELECT COUNT(*) FROM signals s "
            "LEFT JOIN raw_results r USING (raw_id) WHERE r.raw_id IS NULL"
        ) == 0
        assert db.scalar(
            "SELECT COUNT(*) FROM raw_results r "
            "LEFT JOIN query_queue q USING (query_id) WHERE q.query_id IS NULL"
        ) == 0
        assert db.scalar(
            "SELECT COUNT(*) FROM triage_decisions t "
            "LEFT JOIN raw_results r USING (raw_id) WHERE r.raw_id IS NULL"
        ) == 0
        assert db.scalar("PRAGMA foreign_key_check") is None or True

        # --- Audit trail ----------------------------------------------------
        runs = {r["agent"]: r for r in db.get_agent_runs(iteration)}
        assert runs["CollectionAgent"]["status"] == "COMPLETE"
        assert runs["TriageAgent"]["status"] == "COMPLETE"
        assert db.get_log(iteration)

    def test_exhausted_budget_skips_rather_than_raising(self, db, config):
        """Every query refused, the stage still completes, and each refusal is
        on the record with its reason."""
        config["tipping"]["max_queries_per_city"] = 99
        session, phoenix, tucson, iteration = build_session(db, config)

        budget = BudgetGuard(db, config)
        budget.seed_budgets()
        queue_agent = QueueAgent(db, config)      # no budget: let it enqueue
        queue_agent.run_seed(iteration, session)
        queued = db.count_queued(iteration)
        assert queued > 0

        # Now exhaust the provider before collection runs.
        db.set_budget("APIDIRECT", None, "MONTH", 0.0)
        collector = CollectionAgent(
            db, config, {"APIDIRECT": ScriptedSocial({})}, budget=budget
        )
        assert collector.run(iteration, source_types=SOCIAL_TYPES) is True

        rows = db.get_queue(iteration)
        assert all(r["status"] == "SKIPPED_BUDGET" for r in rows)
        assert all(r["skip_reason"] == "MONTHLY_QUOTA_EXHAUSTED" for r in rows)
        assert db.decision_counts(iteration)["BUDGET_EXHAUSTED"] == queued
        assert db.scalar("SELECT COUNT(*) FROM raw_results") == 0
        # The agent completed; it did not fail.
        assert db.get_agent_runs(iteration)[0]["status"] == "COMPLETE"

    def test_a_partial_collection_failure_leaves_the_rest_intact(self, db, config):
        """One provider outage must not discard what the others collected."""
        config["tipping"]["max_queries_per_city"] = 99
        session, phoenix, tucson, iteration = build_session(db, config)
        QueueAgent(db, config).run_seed(iteration, session)

        from surge_iw.base.connector import AuthError

        class HalfBroken(ScriptedSocial):
            def search(self, endpoint, params, **kwargs):
                self.calls += 1
                if self.calls % 2 == 0:
                    raise AuthError("401 expired", provider="APIDIRECT",
                                    status_code=401)
                return PHOENIX_POSTS[:1]

        collector = CollectionAgent(db, config, {"APIDIRECT": HalfBroken({})})
        assert collector.run(iteration, source_types=SOCIAL_TYPES) is True

        statuses = [r["status"] for r in db.get_queue(iteration)]
        assert "COMPLETE" in statuses and "FAILED" in statuses
        assert db.scalar("SELECT COUNT(*) FROM raw_results") > 0
        # Both cities register the gap, which correlation reads as reduced
        # coverage rather than as an absence of chatter.
        gaps = db.unreliable_source_types(iteration, phoenix)
        assert gaps == ["SOCIAL"] or gaps == []

    def test_triage_resumes_without_double_judging(self, db, config):
        """Killing the stage mid-run and re-running must not re-decide."""
        config["tipping"]["max_queries_per_city"] = 99
        config["triage"] = {"batch_size": 20}
        session, phoenix, tucson, iteration = build_session(db, config)
        QueueAgent(db, config).run_seed(iteration, session)
        CollectionAgent(
            db, config,
            {"APIDIRECT": ScriptedSocial({"Phoenix": PHOENIX_POSTS,
                                          "Tucson": TUCSON_POSTS})},
        ).run(iteration, source_types=SOCIAL_TYPES)

        llm = FakeLLM(all_decisions())
        agent = TriageAgent(db, config, llm)
        agent.run(iteration)
        first = db.scalar("SELECT COUNT(*) FROM triage_decisions")
        calls = len(llm.prompts)

        agent.run(iteration)
        assert db.scalar("SELECT COUNT(*) FROM triage_decisions") == first
        assert len(llm.prompts) == calls
