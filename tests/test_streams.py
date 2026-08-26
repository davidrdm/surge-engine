"""Mission streams over the social feed (v0.2).

Every pack here is SYNTHETIC — a tmp_path copy of the engine's reference
fixture, rewritten with invented stream vocabulary — because the property
under test is that the engine accommodates streams it has never seen.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from conftest import ANCHOR, REFERENCE
from surge_iw.agents.queueing import (
    SOCIAL_ENDPOINTS, QueueAgent, dedup_key)
from surge_iw.agents.triage import TriageAgent, build_system_prompt
from surge_iw.base import scoring
from surge_iw.base.scoring import TrackModel, correlate
from surge_iw.config import load_config
from surge_iw.db.database import SurgeDB
from surge_iw.services import mission as mission_service
from surge_iw.services.mission import MissionError
from test_triage import FakeLLM, decision, post, store_posts  # noqa: F401


# ---------------------------------------------------------------------------
# Building synthetic stream packs
# ---------------------------------------------------------------------------


def streamify(pack: Path, streams: dict, *, weights: dict | None = None,
              hypotheses_extra: dict | None = None) -> Path:
    """Rewrite a copied reference pack to declare `streams`."""
    manifest = yaml.safe_load((pack / "mission.yaml").read_text())
    manifest["files"] = [f for f in manifest["files"] if f != "lexicon.yaml"]
    if "streams.yaml" not in manifest["files"]:
        manifest["files"].append("streams.yaml")
    (pack / "streams.yaml").write_text(yaml.safe_dump(streams, sort_keys=False))
    if (pack / "lexicon.yaml").exists():
        (pack / "lexicon.yaml").unlink()
    scoring_data = yaml.safe_load((pack / "scoring.yaml").read_text())
    if weights is None:
        for track, table in scoring_data["weights"].items():
            social = table.pop("social")
            share = round(social / max(1, len(streams)), 4)
            for stream_id in streams:
                table[stream_id] = share
    else:
        scoring_data["weights"] = weights
    (pack / "scoring.yaml").write_text(
        yaml.safe_dump(scoring_data, sort_keys=False))
    if hypotheses_extra:
        hyp = yaml.safe_load((pack / "hypotheses.yaml").read_text())
        hyp.update(hypotheses_extra)
        (pack / "hypotheses.yaml").write_text(
            yaml.safe_dump(hyp, sort_keys=False))
    (pack / "mission.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return pack


def reference_lexicon(pack: Path | None = None) -> dict:
    """Every track's term groups, pooled across the reference pack's streams.

    The shipped pack is the streams exhibit since version 2, so a single
    lexicon has to be reassembled from its watches.
    """
    merged: dict = {}
    for stream in mission_service.load(REFERENCE).streams:
        for track, groups in stream.lexicon.items():
            merged.setdefault(track, []).extend(list(g) for g in groups)
    return merged


def destreamify(pack: Path) -> Path:
    """Rewrite a copied reference pack back to the v0.1 shape.

    One lexicon.yaml (the streams' groups pooled), `social` weight rows (the
    stream weights summed), engine-family hypotheses only. The synthetic
    packs here are built FROM a streamless base so that what `streamify`
    adds is exactly what each test exercises — and the equivalence tests
    need the streamless shape itself.
    """
    manifest = yaml.safe_load((pack / "mission.yaml").read_text())
    streams = yaml.safe_load((pack / "streams.yaml").read_text())
    stream_ids = list(streams)
    # Stream-only files: a leg file the manifest's own prompt slots also use
    # must survive, so it is excluded from the removal set.
    mission_prompt_files = {slot["file"]
                            for slot in manifest.get("prompts", {}).values()}
    stream_files = ({"streams.yaml"} | {
        leg["file"] for entry in streams.values()
        for slot in ("relevance_strict", "relevance_broad")
        if isinstance(leg := entry.get(slot), dict)}) - mission_prompt_files
    manifest["files"] = [f for f in manifest["files"]
                         if f not in stream_files] + ["lexicon.yaml"]
    merged: dict = {}
    for entry in streams.values():
        for track, groups in entry["lexicon"].items():
            merged.setdefault(track, []).extend(groups)
    (pack / "lexicon.yaml").write_text(yaml.safe_dump(merged, sort_keys=False))
    # Deleted, not just undeclared: the loader checks membership both ways,
    # and an undeclared file still present would be refused as unhashed.
    for name in stream_files:
        (pack / name).unlink()

    scoring_data = yaml.safe_load((pack / "scoring.yaml").read_text())
    for table in scoring_data["weights"].values():
        table["social"] = round(
            sum(table.pop(sid) for sid in stream_ids), 4)
    (pack / "scoring.yaml").write_text(
        yaml.safe_dump(scoring_data, sort_keys=False))

    hyp = yaml.safe_load((pack / "hypotheses.yaml").read_text())
    promoted = {entry.get("family") for entry in streams.values()} \
        - {None, "SOCIAL"}
    for family in promoted:
        hyp.pop(family, None)
    (pack / "hypotheses.yaml").write_text(yaml.safe_dump(hyp, sort_keys=False))
    (pack / "mission.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return pack


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    target = tmp_path / "missions" / "trial"
    shutil.copytree(REFERENCE, target)
    shutil.rmtree(target / "tests", ignore_errors=True)
    return destreamify(target)


@pytest.fixture
def two_streams(pack: Path):
    """chatter (twitter+reddit, sub-kind) + wire_desk (news, promoted)."""
    lex = reference_lexicon()
    streamify(pack, {
        "chatter": {"platforms": ["twitter", "reddit"], "lexicon": lex},
        "wire_desk": {"platforms": ["news"], "family": "NEWSPRINT",
                      "lexicon": lex,
                      "relevance_strict": {
                          "file": "prompts/relevance-broad.md",
                          "version": "trial-wire/1-strict"}},
    }, hypotheses_extra={"NEWSPRINT": [
        {"code": "ROUTINE_COVERAGE",
         "statement": "Ordinary press coverage of a scheduled announcement."}]})
    return mission_service.load(pack)


def wired(mission, config=None):
    """A database + config pair carrying the given mission."""
    config = config or load_config(None, mission=mission)
    return SurgeDB(":memory:", mission=mission), config


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


class TestTheLoader:
    def refuse(self, pack: Path, fragment: str):
        with pytest.raises(MissionError) as exc:
            mission_service.load(pack)
        assert fragment in str(exc.value), str(exc.value)

    def test_both_files_is_two_answers_to_one_question(self, pack: Path):
        manifest = yaml.safe_load((pack / "mission.yaml").read_text())
        manifest["files"].append("streams.yaml")
        (pack / "streams.yaml").write_text(yaml.safe_dump(
            {"chatter": {"platforms": ["twitter"],
                         "lexicon": reference_lexicon()}}))
        (pack / "mission.yaml").write_text(yaml.safe_dump(manifest))
        self.refuse(pack, "streams.yaml and lexicon.yaml are both declared")

    def test_neither_file_would_seed_nothing(self, pack: Path):
        manifest = yaml.safe_load((pack / "mission.yaml").read_text())
        manifest["files"] = [f for f in manifest["files"]
                             if f != "lexicon.yaml"]
        (pack / "lexicon.yaml").unlink()
        (pack / "mission.yaml").write_text(yaml.safe_dump(manifest))
        self.refuse(pack, "neither lexicon.yaml nor streams.yaml")

    def test_an_empty_streams_file_is_refused(self, pack: Path):
        streamify(pack, {})
        (pack / "streams.yaml").write_text("{}\n")
        self.refuse(pack, "declares no streams")

    @pytest.mark.parametrize("bad_id", ["Chatter", "9live", "wire-desk"])
    def test_a_stream_id_must_be_lower_snake(self, pack: Path, bad_id):
        streamify(pack, {bad_id: {"platforms": ["twitter"],
                                  "lexicon": reference_lexicon()}})
        self.refuse(pack, "lower snake")

    @pytest.mark.parametrize("reserved", ["lodging", "car", "flight_m"])
    def test_a_reserved_kind_cannot_be_a_stream(self, pack: Path, reserved):
        streamify(pack, {reserved: {"platforms": ["twitter"],
                                    "lexicon": reference_lexicon()}})
        self.refuse(pack, "collides with an engine scoring kind")

    def test_social_is_a_legal_stream_id(self, pack: Path):
        """It names the implicit stream, which is what makes the
        implicit-vs-explicit equivalence a statement a test can make."""
        streamify(pack, {"social": {
            "platforms": ["twitter", "reddit", "news"],
            "lexicon": reference_lexicon()}})
        loaded = mission_service.load(pack)
        assert loaded.social_kinds == ("social",)

    def test_an_unknown_platform_is_refused(self, pack: Path):
        streamify(pack, {"chatter": {"platforms": ["twitter", "myspace"],
                                     "lexicon": reference_lexicon()}})
        self.refuse(pack, "the engine collects")

    def test_a_duplicate_platform_is_refused(self, pack: Path):
        streamify(pack, {"chatter": {"platforms": ["twitter", "twitter"],
                                     "lexicon": reference_lexicon()}})
        self.refuse(pack, "twice")

    def test_platforms_are_required(self, pack: Path):
        streamify(pack, {"chatter": {"lexicon": reference_lexicon()}})
        self.refuse(pack, "platforms is required")

    @pytest.mark.parametrize("family,fragment", [
        ("FLIGHT", "collected by their own connectors"),
        ("UNKNOWN", "not attributed"),
        ("newsprint", "upper snake case"),
    ])
    def test_a_bad_family_is_refused(self, pack: Path, family, fragment):
        streamify(pack, {"chatter": {"platforms": ["twitter"],
                                     "family": family,
                                     "lexicon": reference_lexicon()}})
        self.refuse(pack, fragment)

    def test_a_missing_lexicon_is_refused(self, pack: Path):
        streamify(pack, {"chatter": {"platforms": ["twitter"]}})
        self.refuse(pack, "lexicon is required")

    def test_a_lexicon_missing_a_track_is_refused(self, pack: Path):
        partial = reference_lexicon()
        partial.pop(next(iter(partial)))
        streamify(pack, {"chatter": {"platforms": ["twitter"],
                                     "lexicon": partial}})
        self.refuse(pack, "no entry for track")

    def test_an_unknown_stream_key_is_refused(self, pack: Path):
        streamify(pack, {"chatter": {"platforms": ["twitter"],
                                     "lexicon": reference_lexicon(),
                                     "weight": 0.5}})
        self.refuse(pack, "unknown key")

    def test_weights_must_name_every_stream(self, pack: Path):
        lex = reference_lexicon()
        streamify(pack, {"chatter": {"platforms": ["twitter"], "lexicon": lex},
                         "wire_desk": {"platforms": ["news"], "lexicon": lex}})
        scoring_data = yaml.safe_load((pack / "scoring.yaml").read_text())
        for table in scoring_data["weights"].values():
            table.pop("wire_desk")
        (pack / "scoring.yaml").write_text(
            yaml.safe_dump(scoring_data, sort_keys=False))
        self.refuse(pack, "has no weight for wire_desk")

    def test_a_social_row_is_refused_when_streams_are_declared(self, pack: Path):
        lex = reference_lexicon()
        streamify(pack, {"chatter": {"platforms": ["twitter"], "lexicon": lex}})
        scoring_data = yaml.safe_load((pack / "scoring.yaml").read_text())
        for table in scoring_data["weights"].values():
            table["social"] = 0.1
        (pack / "scoring.yaml").write_text(
            yaml.safe_dump(scoring_data, sort_keys=False))
        self.refuse(pack, "unknown scoring kind")

    def test_hypotheses_accept_a_promoted_family(self, two_streams):
        assert "NEWSPRINT" in two_streams.hypotheses
        assert two_streams.families == (
            "SOCIAL", "FLIGHT", "LODGING", "CAR", "NEWSPRINT")

    def test_the_digest_covers_streams_yaml(self, pack: Path):
        streamify(pack, {"chatter": {"platforms": ["twitter"],
                                     "lexicon": reference_lexicon()}})
        before = mission_service.load(pack).digest
        text = (pack / "streams.yaml").read_text()
        (pack / "streams.yaml").write_text(text + "\n# edited\n")
        assert mission_service.load(pack).digest != before

    def test_the_funnel_refuses_an_unknown_stream_by_name(self, two_streams):
        db, _config = wired(two_streams)
        with db, pytest.raises(MissionError) as exc:
            db.insert_signal(iteration_id=1, signal_type="SOCIAL",
                             stream="nope", url="https://x.com/1")
        assert "nope" in str(exc.value)
        assert "chatter" in str(exc.value)


# ---------------------------------------------------------------------------
# Queueing
# ---------------------------------------------------------------------------


class TestQueueing:
    def seeded(self, mission, config):
        db, config = wired(mission, config)
        with db:
            session = db.insert_session(label="s", tracks=None, config={})
            db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
            iteration = db.insert_iteration(session, anchor_at=ANCHOR)
            agent = QueueAgent(db, config)
            agent.run_seed(iteration, session)
            return ([dict(r) for r in db.all(
                        "SELECT stream, endpoint, params_json FROM query_queue "
                        "ORDER BY query_id")],
                    [dict(r) for r in db.all(
                        "SELECT * FROM queue_decisions WHERE outcome = "
                        "'NO_MAPPING'")])

    def test_each_stream_seeds_its_own_platforms(self, two_streams):
        config = load_config(None, mission=two_streams)
        config["tipping"]["max_queries_per_city"] = 999
        rows, _ = self.seeded(two_streams, config)
        by_stream = {}
        for row in rows:
            by_stream.setdefault(row["stream"], set()).add(row["endpoint"])
        assert by_stream["chatter"] == {SOCIAL_ENDPOINTS["twitter"],
                                        SOCIAL_ENDPOINTS["reddit"]}
        assert by_stream["wire_desk"] == {SOCIAL_ENDPOINTS["news"]}

    def test_an_operator_disabled_stream_is_a_recorded_refusal(
        self, two_streams
    ):
        """apidirect.platforms is the kill switch; a stream it empties must
        be a NO_MAPPING decision naming the stream, never a quiet absence."""
        config = load_config(None, mission=two_streams)
        config["apidirect"]["platforms"] = ["twitter", "reddit"]  # no news
        config["tipping"]["max_queries_per_city"] = 999
        rows, refusals = self.seeded(two_streams, config)
        assert not any(r["stream"] == "wire_desk" for r in rows)
        assert len(refusals) == 1
        assert refusals[0]["stream"] == "wire_desk"
        assert "disables every platform" in refusals[0]["detail"]

    def test_identical_queries_in_two_streams_are_two_queries(self):
        params = {"query": "x", "pages": 2}
        keys = {dedup_key("/v1/twitter/posts", params, s)
                for s in ("chatter", "wire_desk", None)}
        assert len(keys) == 3, (
            "a DEDUPED refusal for the second stream would make its coverage "
            "silently depend on the first's fetch")

    def test_the_implicit_stream_key_is_byte_identical_to_v01(self):
        """The cooldown is keyed on dedup_key across a session's history, so
        a changed key would restart every cooldown on upgrade."""
        import hashlib, json
        params = {"query": "Phoenix (\"air show\")", "pages": 2,
                  "sort_by": "most_recent"}
        v01 = hashlib.sha256(json.dumps(
            ["/v1/twitter/posts", params], sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()[:32]
        assert dedup_key("/v1/twitter/posts", params) == v01
        assert dedup_key("/v1/twitter/posts", params, None) == v01

    def test_platform_names_match_the_loader(self):
        assert set(SOCIAL_ENDPOINTS) == set(mission_service.SOCIAL_PLATFORMS)


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


class TestTriage:
    def _session(self, db):
        session = db.insert_session(label="s", tracks=None, config={})
        db.insert_city(session, "Phoenix", canonical="phoenix")
        iteration = db.insert_iteration(session)
        return session, iteration

    def _query(self, db, session, iteration, stream, key):
        return db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="SOCIAL",
            endpoint="/v1/twitter/posts", params={}, dedup_key=key,
            stream=stream)

    def test_streams_are_judged_in_separate_batches_under_their_own_prompts(
        self, two_streams
    ):
        db, config = wired(two_streams)
        with db:
            session, iteration = self._session(db)
            q1 = self._query(db, session, iteration, "chatter", "k1")
            q2 = self._query(db, session, iteration, "wire_desk", "k2")
            store_posts(db, iteration, [post("https://x.com/a")], q1)
            store_posts(db, iteration, [post("https://x.com/b")], q2)
            llm = FakeLLM([decision("https://x.com/a")],
                          [decision("https://x.com/b")])
            TriageAgent(db, config, llm).run(iteration)
            assert len(llm.prompts) == 2, "one call per stream"
            hashes = {r["prompt_hash"] for r in db.all(
                "SELECT prompt_hash FROM receipts WHERE kind='TRIAGE'")}
            assert len(hashes) == 2, (
                "wire_desk overrides its strict leg, so its system prompt — "
                "and therefore its receipt hash — must differ from chatter's")
            versions = {r["prompt_version"] for r in db.all(
                "SELECT prompt_version FROM receipts")}
            assert "trial-wire/1-strict" in versions

    def test_the_same_url_is_judged_once_per_stream(self, two_streams):
        db, config = wired(two_streams)
        with db:
            session, iteration = self._session(db)
            url = "https://x.com/same"
            q1 = self._query(db, session, iteration, "chatter", "k1")
            q2 = self._query(db, session, iteration, "wire_desk", "k2")
            store_posts(db, iteration, [post(url)], q1)
            store_posts(db, iteration, [post(url)], q2)
            llm = FakeLLM([decision(url)], [decision(url)])
            TriageAgent(db, config, llm).run(iteration)
            rows = db.all("SELECT stream, url FROM triage_decisions")
            assert {(r["stream"], r["url"]) for r in rows} == {
                ("chatter", url), ("wire_desk", url)}
            signals = db.all("SELECT stream FROM signals")
            assert sorted(r["stream"] for r in signals) == [
                "chatter", "wire_desk"], (
                "the dedup index admits one signal per (stream, URL)")

    def test_resume_is_per_stream_and_url(self, two_streams):
        db, config = wired(two_streams)
        with db:
            session, iteration = self._session(db)
            url = "https://x.com/same"
            q1 = self._query(db, session, iteration, "chatter", "k1")
            q2 = self._query(db, session, iteration, "wire_desk", "k2")
            store_posts(db, iteration, [post(url)], q1)
            store_posts(db, iteration, [post(url)], q2)
            db.insert_triage_decision(
                iteration_id=iteration, raw_id=None, state="ACCEPTED",
                rationale="prior", model="stub", url=url, stream="chatter")
            llm = FakeLLM([decision(url)])
            TriageAgent(db, config, llm).run(iteration)
            assert len(llm.prompts) == 1, (
                "chatter's judgement stands; only wire_desk's is owed")
            rows = db.all("SELECT stream, COUNT(*) n FROM triage_decisions "
                          "GROUP BY stream")
            assert {r["stream"]: r["n"] for r in rows} == {
                "chatter": 1, "wire_desk": 1}

    def test_leg_inheritance_is_per_leg(self, two_streams):
        """wire_desk overrides strict only; its broad leg is the mission's."""
        wire = next(s for s in two_streams.streams if s.id == "wire_desk")
        strict, strict_version = build_system_prompt(
            two_streams, {"triage": {"require_nexus": True}}, wire)
        broad, broad_version = build_system_prompt(
            two_streams, {"triage": {"require_nexus": False}}, wire)
        assert strict_version == "trial-wire/1-strict"
        assert broad_version == two_streams.prompt_versions["relevance_broad"]
        mission_broad, _ = build_system_prompt(
            two_streams, {"triage": {"require_nexus": False}})
        assert broad == mission_broad


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def social_row(stream, signal_id, *, domain="a.com", url=None, track="AIRSHOW"):
    return {"signal_id": signal_id, "signal_type": "SOCIAL", "stream": stream,
            "track": track, "observed_at": ANCHOR.isoformat(),
            "salience": 0.9, "source_domain": domain,
            "publisher_key": domain, "publisher_method": "HOST",
            "claim_key": url or f"u:{signal_id}",
            "url": url or f"https://{domain}/{signal_id}"}


def flight_row(signal_id):
    return {"signal_id": signal_id, "signal_type": "FLIGHT",
            "track": "AIRSHOW", "observed_at": ANCHOR.isoformat(),
            "flight_category": "M", "category_confidence": "CONFIRMED",
            "fr24_id": f"f{signal_id}"}


class TestScoring:
    @pytest.fixture
    def model(self, two_streams):
        return TrackModel.from_mission(two_streams, "AIRSHOW")

    def cfg(self, two_streams):
        return load_config(None, mission=two_streams)["correlation"]

    def test_a_sub_kind_stream_stays_in_the_social_family(
        self, two_streams, model
    ):
        result = correlate(
            [social_row("chatter", 1), flight_row(2)],
            track=model, anchor_at=ANCHOR, cfg=self.cfg(two_streams))
        assert result.distinct_types == 2          # SOCIAL + FLIGHT
        assert result.contributions["chatter"] > 0

    def test_a_promoted_stream_is_its_own_family(self, two_streams, model):
        result = correlate(
            [social_row("chatter", 1), social_row("wire_desk", 2,
                                                  domain="b.org")],
            track=model, anchor_at=ANCHOR, cfg=self.cfg(two_streams))
        assert result.distinct_types == 2          # SOCIAL + NEWSPRINT

    def test_two_sub_kind_streams_are_one_family(self, pack):
        lex = reference_lexicon()
        streamify(pack, {
            "one": {"platforms": ["twitter"], "lexicon": lex},
            "two": {"platforms": ["reddit"], "lexicon": lex}})
        mission = mission_service.load(pack)
        model = TrackModel.from_mission(mission, "AIRSHOW")
        result = correlate(
            [social_row("one", 1), social_row("two", 2, domain="b.org")],
            track=model, anchor_at=ANCHOR,
            cfg=load_config(None, mission=mission)["correlation"])
        assert result.distinct_types == 1

    def test_any_stream_anchors(self, two_streams, model):
        """Promoted or not, a stream is "somebody said so" evidence: its
        contribution satisfies the anchor MEDIUM requires. Asserted on the
        rule itself — banding also needs the score floor, which these two
        synthetic rows are not meant to clear."""
        assert scoring.has_strong_anchor(model, {"wire_desk": 0.05})
        assert scoring.has_strong_anchor(model, {"chatter": 0.05})
        assert not scoring.has_strong_anchor(model, {"lodging": 0.5})

    def test_one_wire_story_in_two_streams_is_one_claim(
        self, two_streams, model
    ):
        """The union rule: claim identity is orthogonal to streams, so a
        syndicated story surfacing in both lenses cannot manufacture the
        two-report floor."""
        shared = dict(social_row("chatter", 1), claim_key="c:same")
        mirrored = dict(social_row("wire_desk", 2, domain="b.org"),
                        claim_key="c:same")
        grouped = {"chatter": [shared], "wire_desk": [mirrored]}
        contributions = {"chatter": 0.1, "wire_desk": 0.1}
        reports = scoring.independent_reports(
            grouped, contributions, model.social_kinds)
        assert reports == 1

    def test_completeness_denominator_counts_promoted_families(
        self, two_streams, model
    ):
        result = correlate(
            [social_row("chatter", 1)],
            track=model, anchor_at=ANCHOR, cfg=self.cfg(two_streams),
            social_stream_gaps=["wire_desk"],
            collected_source_types=["SOCIAL"])
        assert result.failed_families == ["NEWSPRINT"]
        assert result.data_completeness == round(1 - 1 / 5, 4)
        assert "SOCIAL(wire_desk)" in result.failed_sources

    def test_a_streamless_gap_reaches_every_social_family(
        self, two_streams, model
    ):
        result = correlate(
            [flight_row(1)],
            track=model, anchor_at=ANCHOR, cfg=self.cfg(two_streams),
            social_stream_gaps=[None],
            collected_source_types=["FLIGHT_LIVE"])
        assert set(result.failed_families) == {"SOCIAL", "NEWSPRINT"}

    def test_an_unknown_stream_scores_zero_and_is_named(
        self, two_streams, model
    ):
        result = correlate(
            [social_row("renamed_away", 1), flight_row(2)],
            track=model, anchor_at=ANCHOR, cfg=self.cfg(two_streams))
        assert result.contributions.get("renamed_away", 0.0) == 0.0
        assert "renamed_away" in result.rule_trace
        assert result.distinct_types == 1          # FLIGHT only


# ---------------------------------------------------------------------------
# Equivalence: the implicit stream IS v0.1
# ---------------------------------------------------------------------------


class TestEquivalence:
    def test_a_no_streams_pack_scores_exactly_as_before(self, pack: Path):
        # The destreamified copy: since v2 the SHIPPED reference pack is the
        # streams exhibit, so the streamless shape under test is rebuilt from
        # it — same tracks, same pooled lexicon, same summed weights.
        mission = mission_service.load(pack)
        model = TrackModel.from_mission(mission, "AIRSHOW")
        assert model.social_kinds == ("social",)
        assert model.families == ("SOCIAL", "FLIGHT", "LODGING", "CAR")
        row = social_row(None, 1)
        assert scoring.scoring_kind(row) == "social"
        result = correlate([row, flight_row(2)], track=model,
                           anchor_at=ANCHOR,
                           cfg=load_config(None, mission=mission)["correlation"])
        assert result.distinct_types == 2
        assert result.contributions["social"] > 0

    def test_an_explicit_social_stream_is_the_implicit_one(self, pack: Path):
        """One stream named `social` over every platform, same lexicon, same
        weight rows: the same queries, the same kinds, the same score."""
        implicit = mission_service.load(pack)     # Mission is immutable in
        streamify(pack, {"social": {              # memory, so it survives the
            "platforms": ["twitter", "reddit", "news"],   # rewrite below.
            "lexicon": reference_lexicon()}},
            weights={t: dict(w) for t, w in implicit.weights.items()})
        explicit = mission_service.load(pack)

        def queries(mission):
            db, config = wired(mission)
            with db:
                session = db.insert_session(label="s", tracks=None, config={})
                db.insert_city(session, "Phoenix", canonical="phoenix",
                               state="AZ")
                iteration = db.insert_iteration(session, anchor_at=ANCHOR)
                config["tipping"]["max_queries_per_city"] = 999
                QueueAgent(db, config).run_seed(iteration, session)
                return sorted(
                    (r["endpoint"], r["params_json"]) for r in db.all(
                        "SELECT endpoint, params_json FROM query_queue"))

        assert queries(implicit) == queries(explicit), (
            "the (endpoint, params) multiset is identical; only the dedup "
            "keys differ, by design")

        cfg_i = load_config(None, mission=implicit)["correlation"]
        cfg_e = load_config(None, mission=explicit)["correlation"]
        rows = [social_row("social", 1), flight_row(2)]
        implicit_result = correlate(
            [dict(rows[0], stream=None), rows[1]],
            track=TrackModel.from_mission(implicit, "AIRSHOW"),
            anchor_at=ANCHOR, cfg=cfg_i)
        explicit_result = correlate(
            rows, track=TrackModel.from_mission(explicit, "AIRSHOW"),
            anchor_at=ANCHOR, cfg=cfg_e)
        assert implicit_result.score == explicit_result.score
        assert implicit_result.band == explicit_result.band
        assert implicit_result.contributions == explicit_result.contributions

    def test_the_implicit_prompt_is_byte_identical(self, pack: Path):
        """A no-streams pack's system prompt did not move in v0.2."""
        mission = mission_service.load(pack)
        config = load_config(None, mission=mission)
        prompt, version = build_system_prompt(mission, config)
        agent_map_prompt = TriageAgent(
            SurgeDB(":memory:", mission=mission), config,
            FakeLLM()).prompts_by_stream[None]
        assert agent_map_prompt == (prompt, version)


# ---------------------------------------------------------------------------
# Pins: what must NOT change
# ---------------------------------------------------------------------------


class TestPins:
    def test_triaging_rollback_deletes_every_streams_signals(self, two_streams):
        """stages.py deletes by signal_type='SOCIAL'; promoted families store
        as SOCIAL too, so one delete covers every stream — pinned so nobody
        'fixes' the rollback to filter by family."""
        db, config = wired(two_streams)
        with db:
            session = db.insert_session(label="s", tracks=None, config={})
            city = db.insert_city(session, "Phoenix", canonical="phoenix")
            iteration = db.insert_iteration(session)
            for stream in ("chatter", "wire_desk"):
                db.insert_signal(iteration_id=iteration, signal_type="SOCIAL",
                                 city_id=city, stream=stream,
                                 url=f"https://x.com/{stream}")
            from surge_iw.services.stages import STAGE_EFFECTS
            for effect in STAGE_EFFECTS["TRIAGING"]:
                effect.delete(db, iteration)
            assert db.all("SELECT * FROM signals") == []

    def test_recovery_expects_no_signals_from_any_social_stream(self):
        from surge_iw.services.recovery import NO_SIGNAL_EXPECTED
        assert "SOCIAL" in NO_SIGNAL_EXPECTED, (
            "a SOCIAL query of any stream writes no signal at collection "
            "time; recovery keys on source_type, which streams do not change")
