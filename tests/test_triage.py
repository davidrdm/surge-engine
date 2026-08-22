"""TriageAgent — the first LLM stage.

The model is stubbed throughout. What is under test is not the model's judgement
but the machinery around it: that every post is accounted for, that the model
cannot admit a city on its own, that malformed output degrades rather than
crashes, and that a signal is only written when a post was accepted AND its city
was admitted.
"""
from __future__ import annotations

import json

import pytest

from conftest import ANCHOR
from surge_iw.agents.triage import TriageAgent
from surge_iw.base.agent import AgentError
from surge_iw.db.database import iso, utcnow


class FakeLLM:
    """Stands in for the OpenAI client. Returns queued responses in order.

    Judgements are written URL-keyed here because that is how a test reads, but
    the real contract is `item_id`-keyed — so the stub translates, exactly as a
    compliant model would by echoing the id it was given. A test that wants to
    exercise a NON-compliant model passes `translate=False` and supplies raw
    output.
    """

    def __init__(self, *responses, error=None, translate=True):
        self._responses = list(responses)
        self.error = error
        self.translate = translate
        self.prompts: list[str] = []
        self.chat = self          # client.chat.completions.create(...)
        self.completions = self

    def create(self, **kwargs):
        prompt = kwargs["messages"][-1]["content"]
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        payload = (self._responses.pop(0) if self._responses
                   else self._responses[-1] if self._responses else "[]")
        if self.translate:
            payload = _bind_to_item_ids(payload, prompt)
        text = payload if isinstance(payload, str) else json.dumps(payload)

        class _Msg:
            content = text

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]
            usage = None

        return _Resp()


def _bind_to_item_ids(payload, prompt):
    """Rewrite URL-keyed judgements into the id-keyed contract.

    `url` is dropped rather than kept alongside: the schema forbids extra
    fields, and that is the point — an unexpected key is a malformed answer.
    """
    if isinstance(payload, str):
        return payload
    items = payload
    wrapper_key = None
    if isinstance(payload, dict):
        for key in ("items", "results", "decisions", "data"):
            if isinstance(payload.get(key), list):
                wrapper_key, items = key, payload[key]
                break
        else:
            return payload
    if not isinstance(items, list):
        return payload

    start = prompt.find("[")
    try:
        sent = json.loads(prompt[start:]) if start >= 0 else []
    except ValueError:
        sent = []
    by_url = {item.get("url"): item.get("item_id")
              for item in sent if isinstance(item, dict)}

    bound = []
    for item in items:
        if not isinstance(item, dict):
            bound.append(item)
            continue
        entry = dict(item)
        url = entry.pop("url", None)
        if "item_id" not in entry and url in by_url:
            entry["item_id"] = by_url[url]
        bound.append(entry)
    if wrapper_key is not None:
        return {**payload, wrapper_key: bound}
    return bound


def _strip_url(entry):
    return {k: v for k, v in entry.items() if k != "url"}


def store_posts(db, iteration, posts, query_id):
    return db.insert_raw_result(
        query_id=query_id, iteration_id=iteration, source_type="SOCIAL",
        provider="APIDIRECT", payload=posts, retention_days=90,
    )


@pytest.fixture
def social_query(db, session, iteration):
    return db.enqueue_query(
        session_id=session, iteration_id=iteration, source_type="SOCIAL",
        endpoint="/v1/twitter/posts", params={}, dedup_key="k1",
    )


def post(url, domain="x.com",
         snippet="crews staging at the fairground", hours_ago=2):
    """A freshly collected post.

    Dated relative to NOW rather than to the fixed ANCHOR, because that is what
    a post the connector just returned looks like — and because triage's
    recency cut compares against now, not against the iteration anchor.
    """
    from datetime import timedelta
    return {
        "url": url, "author": "reporter", "platform": "twitter",
        "source_domain": domain, "snippet": snippet,
        "observed_at": iso(utcnow() - timedelta(hours=hours_ago)),
    }


def decision(url, *, relevant=True, cities=("Phoenix",), salience=0.9,
             track="AIRSHOW", locations=(),
             rationale="names a venue and a date"):
    return {
        "url": url, "relevant": relevant, "track": track,
        "cities": list(cities), "locations": list(locations),
        "activity_type": "static display", "imminence_hours": 12,
        "salience": salience, "rationale": rationale,
    }


# ===========================================================================


class TestEveryPostIsAccountedFor:
    """The gate: a decision row for every post, accepted or not."""

    def test_accepted_and_rejected_both_get_rows(
        self, db, config, session, iteration, social_query
    ):
        db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        posts = [post("https://x.com/1"), post("https://y.org/2", domain="y.org")]
        store_posts(db, iteration, posts, social_query)
        llm = FakeLLM([
            decision("https://x.com/1"),
            decision("https://y.org/2", relevant=False,
                     rationale="general political commentary"),
        ])
        assert TriageAgent(db, config, llm).run(iteration) is True

        # Keyed by url rather than asserted in order: rejections are written as
        # they are judged, while accepted posts are deferred until city
        # admission has seen the whole iteration's evidence, so rejected rows
        # land first. That ordering is a consequence of the corroboration gate,
        # not something callers should depend on.
        rows = {r["url"]: r for r in db.all("SELECT * FROM triage_decisions")}
        assert len(rows) == 2
        assert rows["https://x.com/1"]["relevant"] == 1
        assert rows["https://y.org/2"]["relevant"] == 0
        assert all(r["rationale"] for r in rows.values())
        assert all(r["model"] for r in rows.values())

    def test_a_post_the_model_ignored_is_still_recorded(
        self, db, config, session, iteration, social_query
    ):
        """An unexplained omission is indistinguishable from a considered
        rejection, and for an audit trail that difference is the point."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        posts = [post("https://x.com/1"), post("https://x.com/2")]
        store_posts(db, iteration, posts, social_query)
        llm = FakeLLM([decision("https://x.com/1")])      # only one returned

        TriageAgent(db, config, llm).run(iteration)
        rows = db.all("SELECT * FROM triage_decisions ORDER BY triage_id")
        assert len(rows) == 2
        undecided = [r for r in rows if r["state"] == "UNDECIDED"]
        assert len(undecided) == 1
        assert undecided[0]["relevant"] == 0
        assert "no judgement was returned" in undecided[0]["rationale"]

    def test_a_model_failure_records_every_post_as_undecided(
        self, db, config, session, iteration, social_query
    ):
        """Losing a batch of judgements is a coverage gap, not a crash."""
        import openai
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        llm = FakeLLM(error=openai.APIError("boom", request=None, body=None))
        agent = TriageAgent(db, config, llm)
        agent._call_llm = lambda *a, **k: (_ for _ in ()).throw(
            AgentError("LLM unavailable"))

        assert agent.run(iteration) is True      # the stage survives
        rows = db.all("SELECT * FROM triage_decisions")
        assert len(rows) == 1
        assert rows[0]["relevant"] == 0
        assert db.scalar("SELECT COUNT(*) FROM signals") == 0

    def test_rejected_posts_produce_no_signals(
        self, db, config, session, iteration, social_query
    ):
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        llm = FakeLLM([decision("https://x.com/1", relevant=False)])
        TriageAgent(db, config, llm).run(iteration)
        assert db.scalar("SELECT COUNT(*) FROM signals") == 0


class TestSignalWriting:
    def test_an_accepted_post_becomes_a_signal(
        self, db, config, session, iteration, social_query
    ):
        city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        TriageAgent(db, config, FakeLLM([decision("https://x.com/1")])).run(iteration)

        signals = db.signals_by_type(iteration, "SOCIAL")
        assert len(signals) == 1
        assert signals[0]["city_id"] == city
        assert signals[0]["track"] == "AIRSHOW"
        assert signals[0]["salience"] == pytest.approx(0.9)
        assert signals[0]["source_domain"] == "x.com"
        assert signals[0]["observed_at"]

    def test_the_signal_links_back_to_its_triage_decision(
        self, db, config, session, iteration, social_query
    ):
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        TriageAgent(db, config, FakeLLM([decision("https://x.com/1")])).run(iteration)
        row = db.one("SELECT * FROM triage_decisions")
        assert row["signal_id"] is not None
        assert db.one("SELECT * FROM signals WHERE signal_id = ?",
                      (row["signal_id"],)) is not None

    def test_a_named_facility_is_matched_to_a_key_location(
        self, db, config, session, iteration, social_query
    ):
        """Spatial anchoring is what earns full quality in scoring, and the
        model will not phrase the name exactly as the operator registered it."""
        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        location = db.insert_key_location(
            city, "Riverside Fairground", location_type="FAIRGROUND")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        TriageAgent(db, config, FakeLLM([
            decision("https://x.com/1",
                     locations=("the Riverside Fairground",)),
        ])).run(iteration)
        assert db.signals_by_type(iteration, "SOCIAL")[0]["location_id"] == location

    def test_an_unmatched_facility_is_not_fatal(
        self, db, config, session, iteration, social_query
    ):
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        TriageAgent(db, config, FakeLLM([
            decision("https://x.com/1", locations=("Somewhere Else",)),
        ])).run(iteration)
        assert db.signals_by_type(iteration, "SOCIAL")[0]["location_id"] is None

    def test_a_relevant_post_naming_no_city_yields_no_signal(
        self, db, config, session, iteration, social_query
    ):
        """It cannot correlate, but the judgement is still on the record."""
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        TriageAgent(db, config, FakeLLM([
            decision("https://x.com/1", cities=()),
        ])).run(iteration)
        assert db.scalar("SELECT COUNT(*) FROM signals") == 0
        assert db.scalar("SELECT COUNT(*) FROM triage_decisions") == 1

    def test_duplicate_urls_across_payloads_are_triaged_once(
        self, db, config, session, iteration, social_query
    ):
        """The same article legitimately surfaces from several queries, and
        counting it twice would double-count it as corroboration."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        second = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/news/articles", params={}, dedup_key="k2",
        )
        store_posts(db, iteration, [post("https://x.com/1")], second)
        TriageAgent(db, config, FakeLLM([decision("https://x.com/1")])).run(iteration)
        assert db.scalar("SELECT COUNT(*) FROM triage_decisions") == 1


class TestCityAdmissionIsNotTheModelsDecision:
    def test_an_unlisted_city_is_refused_when_expansion_is_off(
        self, db, config, session, iteration, social_query
    ):
        """The model proposes; the rule disposes."""
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        TriageAgent(db, config, FakeLLM([
            decision("https://x.com/1", cities=("Tucson",)),
        ])).run(iteration)

        assert db.scalar("SELECT COUNT(*) FROM signals") == 0
        assert db.decision_counts(iteration).get("CITY_NOT_ADMITTED") == 1
        # The judgement is still recorded, with the city it named.
        row = db.one("SELECT * FROM triage_decisions")
        assert "Tucson" in row["cities_json"]

    def test_expansion_requires_two_independent_domains(
        self, db, config, session, iteration, social_query
    ):
        db.close_session(session)
        session2 = db.insert_session(label="expand", expand_cities=True)
        it2 = db.insert_iteration(session2, anchor_at=ANCHOR)
        query2 = db.enqueue_query(
            session_id=session2, iteration_id=it2, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="k2",
        )
        # Two posts, same domain: corroboration fails.
        store_posts(db, it2, [post("https://x.com/1"), post("https://x.com/2")],
                    query2)
        TriageAgent(db, config, FakeLLM([
            decision("https://x.com/1", cities=("Tucson",)),
            decision("https://x.com/2", cities=("Tucson",)),
        ])).run(it2)
        assert db.scalar("SELECT COUNT(*) FROM signals") == 0

    def test_corroborated_expansion_admits_the_city_and_writes_signals(
        self, db, config, session, iteration, social_query
    ):
        session2 = db.insert_session(label="expand", expand_cities=True)
        it2 = db.insert_iteration(session2, anchor_at=ANCHOR)
        query2 = db.enqueue_query(
            session_id=session2, iteration_id=it2, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="k2",
        )
        store_posts(db, it2, [post("https://x.com/1"),
                              post("https://apnews.com/2", domain="apnews.com")],
                    query2)
        TriageAgent(db, config, FakeLLM([
            decision("https://x.com/1", cities=("Tucson",)),
            decision("https://apnews.com/2", cities=("Tucson",)),
        ])).run(it2)

        city = db.find_city(session2, "tucson")
        assert city is not None
        assert city["admitted_by"] == "TIP"
        assert len(db.signals_by_type(it2, "SOCIAL")) == 2

    def test_corroboration_counts_across_batches(
        self, db, config, session, iteration
    ):
        """Cities are admitted from the whole iteration's evidence, so a batch
        boundary must not break the two-domain gate."""
        config["triage"] = {"batch_size": 1}
        session2 = db.insert_session(label="expand", expand_cities=True)
        it2 = db.insert_iteration(session2, anchor_at=ANCHOR)
        query2 = db.enqueue_query(
            session_id=session2, iteration_id=it2, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="k2",
        )
        store_posts(db, it2, [post("https://x.com/1"),
                              post("https://apnews.com/2", domain="apnews.com")],
                    query2)
        TriageAgent(db, config, FakeLLM(
            [decision("https://x.com/1", cities=("Tucson",))],
            [decision("https://apnews.com/2", cities=("Tucson",))],
        )).run(it2)
        assert db.find_city(session2, "tucson") is not None


class TestMalformedModelOutput:
    def test_a_wrapper_object_is_tolerated(
        self, db, config, session, iteration, social_query
    ):
        """Models add one despite the prompt saying not to."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        TriageAgent(db, config, FakeLLM(
            {"items": [decision("https://x.com/1")]}
        )).run(iteration)
        assert len(db.signals_by_type(iteration, "SOCIAL")) == 1

    def test_a_rewritten_identifier_is_rejected_not_guessed_at(
        self, db, config, session, iteration, social_query
    ):
        """The positional fallback is gone. It moved one post's judgement —
        its city, its facility, its rationale — onto a different post whenever
        the model dropped an item and rewrote the rest."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        TriageAgent(db, config, FakeLLM(
            [{**decision("https://x.com/1"), "item_id": "iDEADBEEF000"}],
            translate=False,
        )).run(iteration)

        assert db.signals_by_type(iteration, "SOCIAL") == []
        row = db.one("SELECT * FROM triage_decisions")
        assert row["state"] == "UNDECIDED"

    def test_a_duplicate_identifier_discards_both(
        self, db, config, session, iteration, social_query
    ):
        """A duplicate used to overwrite the first judgement AND manufacture a
        spurious undecided record for the post whose slot it took."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1"),
                                    post("https://y.org/2", domain="y.org")],
                    social_query)
        agent = TriageAgent(db, config, FakeLLM())
        posts = agent._gather(iteration)
        from surge_iw.agents.triage_schema import build_request
        _payload, index = build_request(posts, iteration)
        first = list(index)[0]

        TriageAgent(db, config, FakeLLM([
            {**_strip_url(decision("https://x.com/1")), "item_id": first},
            {**_strip_url(decision("https://y.org/2")), "item_id": first},
        ], translate=False)).run(iteration)

        assert db.signals_by_type(iteration, "SOCIAL") == []
        states = {r["state"] for r in db.all("SELECT * FROM triage_decisions")}
        assert states == {"INVALID_OUTPUT", "UNDECIDED"}

    @pytest.mark.parametrize("field,value", [
        ("relevant", "false"),          # a non-empty string used to be TRUE
        ("relevant", 1),
        ("salience", 5),               # used to clamp to 1.0, silently
        ("salience", -2),
        ("salience", "0.7"),
        ("salience", None),
        ("cities", "Phoenix"),         # used to iterate as CHARACTERS
        ("locations", "Fairground"),
        ("track", "aliens"),
        ("imminence_hours", -3),
    ])
    def test_malformed_fields_are_rejected_not_coerced(
        self, db, config, session, iteration, social_query, field, value
    ):
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        TriageAgent(db, config, FakeLLM(
            [{**decision("https://x.com/1"), field: value}]
        )).run(iteration)

        assert db.signals_by_type(iteration, "SOCIAL") == [], (
            f"{field}={value!r} produced a signal")
        row = db.one("SELECT * FROM triage_decisions")
        assert row["state"] == "INVALID_OUTPUT"
        assert row["fault_detail"]

    def test_a_bare_nan_is_refused_at_the_json_layer(
        self, db, config, session, iteration, social_query
    ):
        """json.loads accepts a bare NaN token, and min(1.0, nan) returns 1.0 —
        so `salience: NaN` used to become the highest salience in the run."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        nan = ('[{"item_id": "iX", "relevant": true, "salience": NaN, '
               '"rationale": "x"}]')
        TriageAgent(db, config, FakeLLM(nan, nan, nan)).run(iteration)

        assert db.signals_by_type(iteration, "SOCIAL") == []
        assert db.one("SELECT * FROM triage_decisions")["state"] == "MODEL_ERROR"

    def test_a_model_that_corrects_itself_is_accepted(
        self, db, config, session, iteration, social_query
    ):
        """The retry feeds the parse error back, so a non-finite number is a
        recoverable mistake rather than a lost batch."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        llm = FakeLLM(
            '[{"item_id": "iX", "relevant": true, "salience": NaN, '
            '"rationale": "x"}]',
            [decision("https://x.com/1")],
        )
        TriageAgent(db, config, llm).run(iteration)
        assert len(llm.prompts) == 2
        assert db.one("SELECT * FROM triage_decisions")["state"] == "ACCEPTED"

    def test_an_unexpected_key_is_a_malformed_answer(
        self, db, config, session, iteration, social_query
    ):
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        TriageAgent(db, config, FakeLLM(
            [{**decision("https://x.com/1"), "confidence_band": "HIGH"}]
        )).run(iteration)
        assert db.one("SELECT * FROM triage_decisions")["state"] == "INVALID_OUTPUT"


class TestOneDecisionPerPost:
    def test_a_post_naming_two_cities_gets_exactly_one_decision(
        self, db, config, session, iteration, social_query
    ):
        """The decision is about the POST. Recording it once per city inflated
        every per-post count, including the coverage figure that now caps the
        band — found by the live matrix reporting 19 rows for 16 posts.

        Only one SIGNAL is written either way: `idx_sig_dedup` keys on
        (iteration, type, url) without a city, so the second city's row is
        refused by the index. That is pre-existing and arguably right — one
        post is one piece of evidence — but the refusal is currently silent.
        """
        db.insert_city(session, "Phoenix", canonical="phoenix")
        db.insert_city(session, "Tucson", canonical="tucson")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        TriageAgent(db, config, FakeLLM([
            decision("https://x.com/1", cities=("Phoenix", "Tucson"))
        ])).run(iteration)

        assert db.scalar("SELECT COUNT(*) FROM triage_decisions") == 1
        assert db.triage_state_counts(iteration) == {"ACCEPTED": 1}
        assert len(db.signals_by_type(iteration, "SOCIAL")) == 1

    def test_the_decision_points_at_a_signal_it_produced(
        self, db, config, session, iteration, social_query
    ):
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        TriageAgent(db, config,
                    FakeLLM([decision("https://x.com/1")])).run(iteration)
        row = db.one("SELECT * FROM triage_decisions")
        assert row["signal_id"] == db.signals_by_type(
            iteration, "SOCIAL")[0]["signal_id"]


class TestRecencyCut:
    """Measured live: the median collected post was 206 days old and only 1%
    fell inside the 48-hour correlation window. Judging that tail exhausted the
    model quota before the recent posts were reached."""

    def test_an_ancient_post_never_reaches_the_model(
        self, db, config, session, iteration, social_query
    ):
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration,
                    [post("https://x.com/old", hours_ago=24 * 365 * 5)],
                    social_query)
        llm = FakeLLM([decision("https://x.com/old")])
        TriageAgent(db, config, llm).run(iteration)

        assert llm.prompts == [], "a 5-year-old post must not cost a model call"
        assert db.signals_by_type(iteration, "SOCIAL") == []

    def test_a_recent_post_is_kept(self, db, config, session, iteration,
                                   social_query):
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/new", hours_ago=3)],
                    social_query)
        TriageAgent(db, config,
                    FakeLLM([decision("https://x.com/new")])).run(iteration)
        assert len(db.signals_by_type(iteration, "SOCIAL")) == 1

    def test_the_cut_is_not_a_rejection(self, db, config, session, iteration,
                                        social_query):
        """A post outside the window was never judged, so it is not recorded as
        a judgement.

        It IS recorded, in `triage_skips` (8.9) — but keeping it out of
        `triage_decisions` is the invariant: the median collected post was
        measured at 206 days old, so hundreds of `too old` rows per iteration
        would bury the real judgements and move every count over that table.
        """
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration,
                    [post("https://x.com/old", hours_ago=24 * 400)],
                    social_query)
        TriageAgent(db, config, FakeLLM([])).run(iteration)
        assert db.scalar("SELECT COUNT(*) FROM triage_decisions") == 0
        assert db.triage_skip_counts(iteration) == {"STALE": 1}
        assert db.one("SELECT * FROM agent_log "
                      "WHERE message LIKE '%did not reach the model%'")

    def test_an_undated_post_is_kept_rather_than_guessed_at(
        self, db, config, session, iteration, social_query
    ):
        """No date is not an old date. The sensitivity gate decides what an
        undated post may do; the recency cut must not pre-empt it."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        undated = {**post("https://x.com/1"), "observed_at": ""}
        store_posts(db, iteration, [undated], social_query)
        llm = FakeLLM([decision("https://x.com/1")])
        TriageAgent(db, config, llm).run(iteration)
        assert llm.prompts, "an undated post must still be judged"
        signals = db.signals_by_type(iteration, "SOCIAL")
        assert signals and signals[0]["signal_state"] == "CANDIDATE"

    def test_the_window_is_configurable(self, db, config, session, iteration,
                                        social_query):
        config["triage"]["max_post_age_hours"] = 24.0
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1", hours_ago=48)],
                    social_query)
        llm = FakeLLM([decision("https://x.com/1")])
        TriageAgent(db, config, llm).run(iteration)
        assert llm.prompts == []


class TestTheBestCopyIsJudgedNotTheFirst:
    """Review #8, MEDIUM. The same article arrives from several queries.

    A URL was marked seen on FIRST sight, before the freshness cut ran, so the
    representative was whichever copy the provider happened to return first.
    An undated or stale copy therefore suppressed a fresh one, and payload
    ORDER decided whether eligible evidence ever reached the model.
    """

    def _two_copies(self, db, session, iteration, social_query, first, second):
        store_posts(db, iteration, [first], social_query)
        other = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key="k2")
        store_posts(db, iteration, [second], other)

    @pytest.mark.parametrize("stale_first", [True, False])
    def test_a_fresh_copy_wins_whichever_arrived_first(
        self, db, config, session, iteration, social_query, stale_first
    ):
        db.insert_city(session, "Phoenix", canonical="phoenix")
        url = "https://x.com/dup"
        stale = post(url, hours_ago=24 * 365)
        fresh = post(url, hours_ago=2)
        self._two_copies(db, session, iteration, social_query,
                         *(stale, fresh) if stale_first else (fresh, stale))

        llm = FakeLLM([decision(url)])
        TriageAgent(db, config, llm).run(iteration)
        assert len(llm.prompts) == 1, "the fresh copy never reached the model"
        assert db.scalar(
            "SELECT COUNT(*) FROM triage_decisions WHERE iteration_id = ?",
            (iteration,)) == 1, "and it is judged exactly once"

    def test_a_dated_copy_is_preferred_over_an_undated_one(
        self, db, config, session, iteration, social_query
    ):
        """Undated cannot be shown to be inside the window, so a dated copy of
        the same article is strictly more useful — whichever came first."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        url = "https://x.com/dup"
        undated = {k: v for k, v in post(url).items() if k != "observed_at"}
        self._two_copies(db, session, iteration, social_query,
                         undated, post(url, hours_ago=2))

        TriageAgent(db, config, FakeLLM([decision(url)])).run(iteration)
        row = db.all("SELECT * FROM signals WHERE iteration_id = ?",
                     (iteration,))
        assert row and row[0]["observed_at"], "the undated copy was chosen"

    def test_every_copy_stale_is_skipped_once_not_once_per_copy(
        self, db, config, session, iteration, social_query
    ):
        """One article that cannot be judged is one loss, however many queries
        returned it. Counting it per copy would overstate the coverage gap."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        url = "https://x.com/old"
        self._two_copies(db, session, iteration, social_query,
                         post(url, hours_ago=24 * 400),
                         post(url, hours_ago=24 * 380))
        TriageAgent(db, config, FakeLLM()).run(iteration)
        assert db.triage_skip_counts(iteration) == {"STALE": 1}


class TestResumability:
    def test_already_triaged_payloads_are_not_reprocessed(
        self, db, config, session, iteration, social_query
    ):
        """A partially completed stage must resume without deciding twice."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [post("https://x.com/1")], social_query)
        llm = FakeLLM([decision("https://x.com/1")], [decision("https://x.com/1")])
        agent = TriageAgent(db, config, llm)
        agent.run(iteration)
        calls_after_first = len(llm.prompts)
        agent.run(iteration)
        assert len(llm.prompts) == calls_after_first
        assert db.scalar("SELECT COUNT(*) FROM triage_decisions") == 1

    def test_resume_is_per_post_not_per_payload(
        self, db, config, session, iteration, social_query
    ):
        """Review #8, HIGH. One response carries many posts.

        `untriaged_raw_results` excluded a payload as soon as ANY decision
        referenced it, so a crash after the first batch persisted left every
        remaining post in that response permanently unjudged — with no
        decision, no skip and no coverage gap, while the API went on
        describing triage as re-entrant. Collected evidence became apparent
        absence, which is the one outcome this system exists to prevent.
        """
        db.insert_city(session, "Phoenix", canonical="phoenix")
        first, second = post("https://x.com/a"), post("https://x.com/b")
        store_posts(db, iteration, [first, second], social_query)
        raw_id = db.all("SELECT raw_id FROM raw_results")[0]["raw_id"]
        # Exactly what a crash after one persisted batch leaves behind.
        db.insert_triage_decision(
            iteration_id=iteration, raw_id=raw_id, state="ACCEPTED",
            rationale="decided before the crash", model="stub",
            url=first["url"], track="AIRSHOW", cities=["Phoenix"],
            salience=0.9, signal_id=None)

        TriageAgent(db, config, FakeLLM([decision(second["url"])])).run(iteration)

        decided = {r["url"] for r in db.all(
            "SELECT url FROM triage_decisions WHERE iteration_id = ?",
            (iteration,))}
        assert decided == {first["url"], second["url"]}
        assert db.scalar(
            "SELECT COUNT(*) FROM triage_decisions WHERE url = ?",
            (first["url"],)) == 1, "the surviving decision must not be repeated"

    def test_a_resumed_scan_does_not_write_the_same_refusal_twice(
        self, db, config, session, iteration, social_query
    ):
        """The cost of rescanning everything. Structural refusals and the
        staleness cut are recomputed on every pass, so without a check against
        what is already recorded the coverage report double-counts losses that
        happened once."""
        db.insert_city(session, "Phoenix", canonical="phoenix")
        store_posts(db, iteration, [
            {"url": ""},                                   # ITEM_NO_URL
            "not an object",                               # ITEM_NOT_AN_OBJECT
            post("https://x.com/old", hours_ago=24 * 365),  # STALE
        ], social_query)

        agent = TriageAgent(db, config, FakeLLM())
        agent.run(iteration)
        after_first = db.triage_skip_counts(iteration)
        agent.run(iteration)
        assert db.triage_skip_counts(iteration) == after_first
        assert after_first == {"ITEM_NO_URL": 1, "ITEM_NOT_AN_OBJECT": 1,
                               "STALE": 1}, after_first

    def test_no_payloads_is_a_success_not_a_failure(
        self, db, config, session, iteration
    ):
        agent = TriageAgent(db, config, FakeLLM())
        assert agent.run(iteration) is True
        assert db.get_agent_runs(iteration)[0]["status"] == "COMPLETE"


class TestTruncationIsNamedNotGuessedAt:
    """Measured while broadening the triage criteria (the relevance-leg
    switch): the longer rationales overflowed 4096 tokens at batch_size 10 and
    lost 40 posts to a message that only said "invalid JSON after 3 attempts".

    A truncated batch is a TOKEN BUDGET problem the operator can fix. Malformed
    output is a model problem they cannot. Reporting the first as the second
    sends them looking in the wrong place.
    """

    class _Truncating:
        """A client that hits its ceiling mid-answer, as the live model did."""

        class chat:
            class completions:
                @staticmethod
                def create(*a, **k):
                    import types
                    choice = types.SimpleNamespace(
                        finish_reason="length",
                        message=types.SimpleNamespace(
                            content='[{"item_id": "a", "relevant": true, "rat'),
                    )
                    return types.SimpleNamespace(
                        choices=[choice], model="m", id="r",
                        usage=types.SimpleNamespace(prompt_tokens=1,
                                                    completion_tokens=2))

    def test_each_decision_names_the_call_that_judged_it(
        self, db, config, session, iteration, social_query
    ):
        """Review #8, HIGH. A split batch is several model calls.

        The loop kept ONE `receipt_id` and overwrote it per sub-call, then
        stamped every decision with the last one. Two of four decisions
        therefore referenced a receipt whose `input_hash` and `batch_key`
        cover a request that did not contain them — the evidence API
        attributing a judgement to a call that never saw it, which defeats the
        one thing a receipt is for.
        """
        from surge_iw.base.agent import TruncatedResponse

        db.insert_city(session, "Phoenix", canonical="phoenix")
        posts = [post(f"https://x.com/{i}") for i in range(4)]
        store_posts(db, iteration, posts, social_query)

        class Splitting(FakeLLM):
            """Refuses four at a time, answers two, exactly as the live model
            did at batch_size 10."""

            def create(self, **kwargs):
                if kwargs["messages"][-1]["content"].count('"item_id"') > 2:
                    raise TruncatedResponse("over max_tokens")
                self._responses = [[decision(p["url"]) for p in posts]]
                return super().create(**kwargs)

        config["triage"] = {"batch_size": 4}
        TriageAgent(db, config, Splitting()).run(iteration)

        rows = db.all(
            "SELECT d.url, r.receipt_id, r.batch_key FROM triage_decisions d "
            "JOIN receipts r ON r.receipt_id = d.receipt_id "
            "WHERE d.iteration_id = ? ORDER BY d.triage_id", (iteration,))
        assert len(rows) == 4
        assert len({r["receipt_id"] for r in rows}) == 2, (
            "two sub-calls judged these four posts, so they cannot share one "
            "receipt")

        # The real property, not merely "more than one receipt": each
        # decision must reference the receipt whose batch CONTAINED it.
        # `batch_key` is recomputed here from the ids of the sub-call that
        # actually carried each post, so a wrong attribution cannot pass by
        # having the right number of distinct receipts.
        from surge_iw.agents.triage_schema import item_id
        from surge_iw.services import receipts as receipts_module

        raw_id = int(db.all("SELECT raw_id FROM raw_results")[0]["raw_id"])
        expected: dict[str, str] = {}
        for group in (posts[0:2], posts[2:4]):
            key = receipts_module.sha256_hex(
                ",".join(item_id(iteration, raw_id, p["url"]) for p in group),
                length=16)
            expected.update({p["url"]: key for p in group})
        for row in rows:
            assert row["batch_key"] == expected[row["url"]], (
                f"{row['url']} references a call whose input did not "
                f"contain it")

    def test_a_truncated_answer_says_so(self, db, config):
        from surge_iw.base.agent import TruncatedResponse
        from surge_iw.agents.triage import TriageAgent

        agent = TriageAgent(db, config, self._Truncating())
        with pytest.raises(TruncatedResponse) as exc:
            agent._call_llm(
                "prompt", "system",
                ceiling_setting="llm.max_tokens (or lower triage.batch_size)")
        assert "output ceiling" in str(exc.value)
        assert "batch_size" in str(exc.value), (
            "the message must name the knob the operator can turn")

    def test_the_message_names_the_CALLER_S_ceiling(self, db, config):
        """The knob is whichever setting produced this ceiling.

        The message used to be hardcoded to "lower triage.batch_size or raise
        llm.max_tokens" on every call — including the alert call, where neither
        knob is in play. An error that sends an operator to the wrong dial is
        worse than a vague one.
        """
        from surge_iw.base.agent import TruncatedResponse
        from surge_iw.agents.alerting import AlertAgent

        agent = AlertAgent(db, config, self._Truncating())
        with pytest.raises(TruncatedResponse) as exc:
            agent._call_llm("prompt", "system",
                            ceiling_setting="alerting.max_tokens")
        message = str(exc.value)
        assert "alerting.max_tokens" in message
        assert "batch_size" not in message
        # And it says WHY the number has to be generous on a reasoning model.
        assert "reasoning" in message

    def test_it_is_not_retried(self, db, config):
        """The same prompt at the same ceiling truncates again, and each retry
        appends MORE context — so retrying makes it strictly worse."""
        from surge_iw.base.agent import TruncatedResponse
        from surge_iw.agents.triage import TriageAgent

        calls = {"n": 0}
        client = self._Truncating()
        original = client.chat.completions.create

        def counting(*a, **k):
            calls["n"] += 1
            return original(*a, **k)
        client.chat.completions.create = counting

        with pytest.raises(TruncatedResponse):
            TriageAgent(db, config, client)._call_llm("p", "s", attempts=3)
        assert calls["n"] == 1, "truncation must not burn the retry budget"
