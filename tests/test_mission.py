"""Loading a mission definition from a pack of files.

The engine's whole reason for reading a mission from disk is that the thing it
is looking for should be changeable without changing the code. That is only
worth anything if a pack that is *wrong* is refused rather than half-applied.

So the tests that matter here are the refusals. A lexicon whose track name is
misspelled must not simply search nothing for that track: it would collect
nothing, score nothing, and report a quiet city — which is indistinguishable
from a city where nothing is happening, and is the failure mode this system is
organised against.

The second theme is the digest. `receipts.mission_hash` is a claim that a
judgement can be reconstructed from named bytes, so the digest has to notice an
edit to any member, and the pack has to refuse a file that is present but
unhashed.
"""
from __future__ import annotations

import shutil
import re
from pathlib import Path

import pytest
import yaml

from conftest import REFERENCE_MISSION, mission_vocabulary

from surge_iw.config import load_config
from surge_iw.services import mission
from surge_iw.services.mission import MissionError

REFERENCE = Path(__file__).resolve().parents[1] / "missions" / "reference"


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    """A private copy of the reference pack, safe to break."""
    target = tmp_path / "missions" / "trial"
    shutil.copytree(REFERENCE, target)
    return target


def _edit_manifest(pack: Path, mutate) -> None:
    data = yaml.safe_load((pack / mission.MANIFEST).read_text())
    mutate(data)
    (pack / mission.MANIFEST).write_text(yaml.safe_dump(data, sort_keys=False))


def _write(pack: Path, name: str, data) -> None:
    (pack / name).write_text(yaml.safe_dump(data, sort_keys=False))


@pytest.fixture
def lexicon_pack(pack: Path) -> Path:
    """The copied pack rewritten to the v0.1 shape: one lexicon.yaml, no
    streams. `_lexicon`'s rules are shared by both paths; the tests that
    drive them through the classic file need a pack of the classic shape,
    which the shipped pack stopped being at version 2."""
    manifest = yaml.safe_load((pack / mission.MANIFEST).read_text())
    streams = yaml.safe_load((pack / "streams.yaml").read_text())
    merged: dict = {}
    for entry in streams.values():
        for track, groups in entry["lexicon"].items():
            merged.setdefault(track, []).extend(groups)
    _write(pack, "lexicon.yaml", merged)
    (pack / "streams.yaml").unlink()
    (pack / "prompts" / "local-news-strict.md").unlink()
    manifest["files"] = [f for f in manifest["files"] if f not in
                         ("streams.yaml", "prompts/local-news-strict.md")]
    manifest["files"].append("lexicon.yaml")
    scoring_data = yaml.safe_load((pack / "scoring.yaml").read_text())
    for table in scoring_data["weights"].values():
        table["social"] = round(table.pop("chatter")
                                + table.pop("local_news"), 4)
    _write(pack, "scoring.yaml", scoring_data)
    hyp = yaml.safe_load((pack / "hypotheses.yaml").read_text())
    hyp.pop("LOCAL_NEWS", None)
    _write(pack, "hypotheses.yaml", hyp)
    (pack / mission.MANIFEST).write_text(
        yaml.safe_dump(manifest, sort_keys=False))
    return pack


# ---------------------------------------------------------------------------
# The shipped pack
# ---------------------------------------------------------------------------


#: What the engine must not say, taken from the packs that say it.
#:
#: Built rather than written out — see `conftest.mission_vocabulary`. A scan for
#: "the engine names no mission" that spells the words itself becomes the last
#: file in the engine holding them, and only ever knows the terms whoever wrote
#: it thought of.
MISSION_PROSE = mission_vocabulary()


class TestWhichPackIsLoaded:
    """The engine's half. WHAT is in the reference pack is the pack's claim and
    is tested in `missions/reference/tests/`; what the engine owes is that the
    configured one is found, and that no mission at all is a legible state
    rather than a crash."""

    def test_the_default_config_names_a_pack_that_loads(self):
        config = load_config(None)
        assert config["mission"]["name"] == "reference"
        assert mission.load_configured(config).identifier == "reference"

    def test_no_mission_configured_is_not_an_error(self):
        """`init-db` and contract generation need a schema, not a mission."""
        assert mission.load_configured({"mission": {"name": ""}}) is None
        assert mission.load_configured({}) is None


# ---------------------------------------------------------------------------
# The pack is the audit unit
# ---------------------------------------------------------------------------


class TestTheDigest:
    def test_it_covers_every_declared_member(self, pack: Path):
        loaded = mission.load(pack)
        names = {name for name, _ in loaded.members}
        assert mission.MANIFEST in names
        assert "prompts/triage.md" in names
        assert "streams.yaml" in names
        assert len(names) == 11

    def test_editing_any_member_changes_it(self, pack: Path):
        before = mission.load(pack).digest
        path = pack / "prompts" / "triage.md"
        path.write_text(path.read_text() + "\n")
        assert mission.load(pack).digest != before

    def test_it_is_stable_across_loads(self, pack: Path):
        assert mission.load(pack).digest == mission.load(pack).digest

    def test_two_packs_with_the_same_bytes_agree(self, pack: Path,
                                                 tmp_path: Path):
        twin = tmp_path / "twin"
        shutil.copytree(pack, twin)
        assert mission.load(twin).digest == mission.load(pack).digest

    def test_an_undeclared_file_is_refused_not_ignored(self, pack: Path):
        """A file that is neither loaded nor hashed is a change nothing would
        record — which is exactly what the digest exists to prevent."""
        (pack / "extra.yaml").write_text("note: hello\n")
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "extra.yaml" in str(exc.value)

    def test_carried_files_are_exempt_and_do_not_move_the_digest(
        self, pack: Path
    ):
        """A pack carries its own prose and its own geography. Requiring every
        note to be declared would make writing one a schema change — and the
        digest must not move either, or a receipt would appear to name a
        different definition because somebody added a comment.

        `inputs/` is exempt for a different reason: the loader never reads it,
        and which input set a session used is recorded on the session itself.
        `tests/` is the pack's own checks — and a test that could move the
        digest would make every receipt appear to name a different definition
        the moment somebody added a case.
        """
        before = mission.load(pack).digest
        (pack / "docs").mkdir(exist_ok=True)
        (pack / "docs" / "why-these-weights.md").write_text("# Notes\n")
        (pack / "inputs").mkdir(exist_ok=True)
        (pack / "inputs" / "other.yaml").write_text("Phoenix, AZ:\n  - A Place\n")
        (pack / "tests").mkdir(exist_ok=True)
        (pack / "tests" / "test_trial_pack.py").write_text(
            "def test_it_still_loads():\n    assert True\n")
        (pack / "README.md").write_text("# A pack introducing itself\n")

        loaded = mission.load(pack)
        assert loaded.identifier == "reference"
        assert loaded.digest == before, (
            "a file the loader never reads must not change what the digest "
            "claims about the definition")

    def test_a_declared_file_that_is_missing_is_refused(self, pack: Path):
        (pack / "streams.yaml").unlink()
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "streams.yaml" in str(exc.value)

    def test_a_member_outside_the_pack_is_refused(self, pack: Path):
        _edit_manifest(pack, lambda d: d["files"].append("../../etc/passwd"))
        with pytest.raises(MissionError):
            mission.load(pack)

    def test_a_member_declared_twice_is_refused(self, pack: Path):
        _edit_manifest(pack, lambda d: d["files"].append("lexicon.yaml"))
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "twice" in str(exc.value)


# ---------------------------------------------------------------------------
# Unknown is refused, never ignored
# ---------------------------------------------------------------------------


class TestTheManifest:
    def test_an_unknown_key_is_refused_by_name(self, pack: Path):
        _edit_manifest(pack, lambda d: d.update({"tracsk": ["A"]}))
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "tracsk" in str(exc.value)

    @pytest.mark.parametrize(
        "key", ["id", "version", "tracks", "location_types", "prompts"])
    def test_a_required_key_is_refused_when_absent(self, pack: Path, key: str):
        _edit_manifest(pack, lambda d: d.pop(key))
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert key in str(exc.value)

    def test_a_lowercase_track_is_refused(self, pack: Path):
        """These are stored in the database and compared exactly. Lowercase
        works until two packs disagree about the case of one word."""
        _edit_manifest(pack, lambda d: d.update({"tracks": ["concert_tour"]}))
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "concert_tour" in str(exc.value)

    def test_a_duplicated_track_is_refused(self, pack: Path):
        _edit_manifest(pack, lambda d: d.update({"tracks": ["A", "A"]}))
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "twice" in str(exc.value)

    def test_an_empty_track_list_is_refused(self, pack: Path):
        _edit_manifest(pack, lambda d: d.update({"tracks": []}))
        with pytest.raises(MissionError):
            mission.load(pack)

    def test_a_mission_may_not_set_an_operator_owned_section(self, pack: Path):
        """Credentials, endpoints and the database belong to whoever runs the
        deployment, not to whoever wrote the mission."""
        _edit_manifest(
            pack,
            lambda d: d["thresholds"].update({"llm": {"model": "evil"}}))
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "llm" in str(exc.value)


class TestCollects:
    """Which engine families a pack collects at all. A pack that scores only
    chatter should not be made to buy flight, lodging and rental-car data to
    ignore it — three vendors, three credentials and a per-iteration spend for
    evidence no weight will ever read."""

    def _set(self, pack: Path, value) -> None:
        _edit_manifest(pack, lambda d: d.__setitem__("collects", value))

    def _social_only(self, pack: Path) -> Path:
        """Everything a pack must drop along with the paid families: their
        weight rows, their flight filter, and their hypotheses. Each is a
        statement about a family this pack no longer has, and the loader
        refuses all three by name — which is the rule, not an inconvenience."""
        data = yaml.safe_load((pack / "scoring.yaml").read_text())
        for table in data["weights"].values():
            for kind in ("flight_M", "flight_J", "lodging", "car"):
                table.pop(kind)
        data.pop("flight_categories")
        _write(pack, "scoring.yaml", data)
        hypotheses = yaml.safe_load((pack / "hypotheses.yaml").read_text())
        for family in ("FLIGHT", "LODGING", "CAR"):
            hypotheses.pop(family, None)
        _write(pack, "hypotheses.yaml", hypotheses)
        self._set(pack, ["SOCIAL"])
        return pack

    def test_omitting_it_collects_all_four(self, pack: Path):
        """Every pack written before this key existed keeps its behaviour."""
        loaded = mission.load(pack)
        assert loaded.collects == mission.FAMILIES
        assert loaded.families[:4] == mission.FAMILIES

    def test_a_social_only_pack_drops_the_paid_kinds_and_families(
        self, lexicon_pack: Path
    ):
        loaded = mission.load(self._social_only(lexicon_pack))
        assert loaded.collects == ("SOCIAL",)
        assert loaded.families == ("SOCIAL",)
        assert loaded.scoring_kinds == ("social",)
        # Not collected is not failed: the three families leave the
        # completeness denominator rather than reading as permanent outages.
        assert all(loaded.flight_categories[t] == () for t in loaded.tracks)

    def test_a_paid_row_left_behind_is_refused_by_name(
        self, lexicon_pack: Path
    ):
        """The declaration and the weight table must not disagree."""
        self._set(lexicon_pack, ["SOCIAL"])
        with pytest.raises(MissionError) as exc:
            mission.load(lexicon_pack)
        assert "flight_M" in str(exc.value)

    def test_a_hypothesis_for_an_uncollected_family_is_refused(
        self, lexicon_pack: Path
    ):
        """Competing explanations for evidence this pack can never have."""
        self._set(lexicon_pack, ["SOCIAL"])
        data = yaml.safe_load((lexicon_pack / "scoring.yaml").read_text())
        for table in data["weights"].values():
            for kind in ("flight_M", "flight_J", "lodging", "car"):
                table.pop(kind)
        data.pop("flight_categories")
        _write(lexicon_pack, "scoring.yaml", data)
        with pytest.raises(MissionError) as exc:
            mission.load(lexicon_pack)
        assert "unknown family/families" in str(exc.value)

    @pytest.mark.parametrize("value,fragment", [
        ([], "non-empty list"),
        (["SOCIAL", "TRAINS"], "not an engine family"),
        (["SOCIAL", "SOCIAL"], "twice"),
        (["FLIGHT", "LODGING"], "must include SOCIAL"),
        ("SOCIAL", "non-empty list"),
    ])
    def test_the_refusals_name_the_offender(self, pack: Path, value, fragment):
        self._set(pack, value)
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert fragment in str(exc.value), str(exc.value)

    def test_a_promoted_stream_family_does_not_belong_here(self, pack: Path):
        """Promoted families are collected THROUGH the social feed; naming one
        here would be a second, disagreeing statement of where it comes from."""
        self._set(pack, ["SOCIAL", "LOCAL_NEWS"])
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "streams.yaml" in str(exc.value)

    def test_startup_says_which_families_are_off(self, lexicon_pack: Path):
        """A pack that has stopped buying three vendors' data must say so out
        loud: the absence is otherwise indistinguishable from an outage."""
        described = "\n".join(
            mission.load(self._social_only(lexicon_pack)).describe())
        assert "NOT collecting FLIGHT, LODGING, CAR" in described
        assert "never a coverage gap" in described

    def test_collecting_all_four_says_nothing_at_startup(self, pack: Path):
        """The warning has to stay rare enough to mean something."""
        assert not [line for line in mission.load(pack).describe()
                    if "collects:" in line]


class TestTheLexicon:
    def test_a_track_the_mission_does_not_define_is_refused(
        self, lexicon_pack: Path
    ):
        data = yaml.safe_load((lexicon_pack / "lexicon.yaml").read_text())
        data["CONCERT_TOURS"] = data.pop("CONCERT_TOUR")
        _write(lexicon_pack, "lexicon.yaml", data)
        with pytest.raises(MissionError) as exc:
            mission.load(lexicon_pack)
        assert "CONCERT_TOURS" in str(exc.value)

    def test_a_track_with_no_entry_is_refused(self, lexicon_pack: Path):
        """The failure this prevents: a misspelled track searches nothing, and
        the run reports a quiet city."""
        data = yaml.safe_load((lexicon_pack / "lexicon.yaml").read_text())
        data.pop("AIRSHOW")
        _write(lexicon_pack, "lexicon.yaml", data)
        with pytest.raises(MissionError) as exc:
            mission.load(lexicon_pack)
        assert "AIRSHOW" in str(exc.value)

    def test_an_empty_term_group_is_refused(self, lexicon_pack: Path):
        _write(lexicon_pack, "lexicon.yaml",
               {"CONCERT_TOUR": [[]], "SPORTING_EVENT": [["a"]],
                "AIRSHOW": [["b"]]})
        with pytest.raises(MissionError) as exc:
            mission.load(lexicon_pack)
        assert "non-empty list" in str(exc.value)

    def test_terms_are_stripped(self, lexicon_pack: Path):
        _write(lexicon_pack, "lexicon.yaml",
               {"CONCERT_TOUR": [["  tour dates  "]],
                "SPORTING_EVENT": [["a"]], "AIRSHOW": [["b"]]})
        assert mission.load(lexicon_pack).lexicon["CONCERT_TOUR"] == \
            (("tour dates",),)


class TestScoring:
    def test_a_missing_weight_is_refused_rather_than_defaulted(self, pack: Path):
        """Zero and absent score identically, and mean opposite things: one is
        a decision that the track produces no such signal, the other is nobody
        having considered it."""
        data = yaml.safe_load((pack / "scoring.yaml").read_text())
        data["weights"]["AIRSHOW"].pop("flight_M")
        _write(pack, "scoring.yaml", data)
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "flight_M" in str(exc.value)

    def test_an_explicit_zero_is_accepted(self, pack: Path):
        loaded = mission.load(pack)
        assert loaded.weights["CONCERT_TOUR"]["flight_M"] == 0.0

    def test_an_unknown_scoring_kind_is_refused(self, pack: Path):
        data = yaml.safe_load((pack / "scoring.yaml").read_text())
        data["weights"]["AIRSHOW"]["rail"] = 0.2
        _write(pack, "scoring.yaml", data)
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "rail" in str(exc.value)

    def test_a_weight_outside_zero_to_one_is_refused(self, pack: Path):
        data = yaml.safe_load((pack / "scoring.yaml").read_text())
        data["weights"]["AIRSHOW"]["chatter"] = 1.4
        _write(pack, "scoring.yaml", data)
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "1.4" in str(exc.value)

    def test_an_unknown_flight_category_is_refused(self, pack: Path):
        data = yaml.safe_load((pack / "scoring.yaml").read_text())
        data["flight_categories"]["AIRSHOW"] = ["M", "Z"]
        _write(pack, "scoring.yaml", data)
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "Z" in str(exc.value)

    def test_a_track_with_no_flight_filter_is_refused(self, pack: Path):
        data = yaml.safe_load((pack / "scoring.yaml").read_text())
        data["flight_categories"].pop("AIRSHOW")
        _write(pack, "scoring.yaml", data)
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "AIRSHOW" in str(exc.value)


class TestPrompts:
    def test_every_slot_is_required(self, pack: Path):
        _edit_manifest(pack, lambda d: d["prompts"].pop("alert"))
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "alert" in str(exc.value)

    def test_an_unknown_slot_is_refused(self, pack: Path):
        _edit_manifest(
            pack,
            lambda d: d["prompts"].update(
                {"summary": {"file": "prompts/alert.md", "version": "x"}}))
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "summary" in str(exc.value)

    def test_a_slot_needs_a_version(self, pack: Path):
        _edit_manifest(pack, lambda d: d["prompts"]["alert"].pop("version"))
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "version" in str(exc.value)

    def test_the_triage_prompt_must_have_the_relevance_slot(self, pack: Path):
        """Without it the strict/broad clause is silently never inserted, and
        the model screens on criteria nobody chose."""
        path = pack / "prompts" / "triage.md"
        path.write_text(path.read_text().replace("{relevance}", ""))
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "relevance" in str(exc.value)

    def test_an_empty_prompt_is_refused(self, pack: Path):
        (pack / "prompts" / "alert.md").write_text("   \n")
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "alert.md" in str(exc.value)

    def test_prompt_text_is_read_verbatim(self, pack: Path):
        """`receipts.prompt_hash` is taken over the exact text, so whitespace
        YAML would normalise is not cosmetic."""
        loaded = mission.load(pack)
        assert loaded.prompts["alert"] == (
            pack / "prompts" / "alert.md").read_text(encoding="utf-8")


class TestHypotheses:
    def test_an_unknown_family_is_refused(self, pack: Path):
        data = yaml.safe_load((pack / "hypotheses.yaml").read_text())
        data["RAIL"] = [{"code": "X", "statement": "y"}]
        _write(pack, "hypotheses.yaml", data)
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "RAIL" in str(exc.value)

    def test_weakened_by_is_optional(self, pack: Path):
        loaded = mission.load(pack)
        codes = {h["code"]: h for h in loaded.hypotheses["LODGING"]}
        assert codes["SUPPLY_SIDE"]["weakened_by"] == ""
        assert codes["HOLIDAY_TRAVEL"]["weakened_by"]

    def test_an_unknown_key_is_refused(self, pack: Path):
        data = yaml.safe_load((pack / "hypotheses.yaml").read_text())
        data["SOCIAL"][0]["rebuttal"] = "no"
        _write(pack, "hypotheses.yaml", data)
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "rebuttal" in str(exc.value)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestResolvingAPack:
    def test_a_name_resolves_inside_the_configured_directory(self, pack: Path):
        config = {"mission": {"dir": str(pack.parent), "name": "trial"}}
        assert mission.load_configured(config).identifier == "reference"

    @pytest.mark.parametrize(
        "name", ["../secrets", "/etc/passwd", "a/b", ".", "..", "-x"])
    def test_a_path_is_refused_where_a_name_belongs(self, pack: Path,
                                                    name: str):
        """A mission name reaching this from configuration is one thing; the
        same field reading an arbitrary path is a file-disclosure primitive."""
        config = {"mission": {"dir": str(pack.parent), "name": name}}
        with pytest.raises(MissionError):
            mission.load_configured(config)

    def test_an_unknown_name_lists_what_was_available(self, pack: Path):
        config = {"mission": {"dir": str(pack.parent), "name": "nosuch"}}
        with pytest.raises(MissionError) as exc:
            mission.load_configured(config)
        assert "trial" in str(exc.value)

    def test_a_directory_with_no_manifest_is_refused(self, tmp_path: Path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(MissionError) as exc:
            mission.load(tmp_path / "empty")
        assert mission.MANIFEST in str(exc.value)


class TestValidatingAgainstAMission:
    def test_a_track_it_defines_is_returned(self):
        loaded = mission.load(REFERENCE)
        assert loaded.track("AIRSHOW") == "AIRSHOW"

    def test_a_track_it_does_not_define_is_refused_by_name(self):
        loaded = mission.load(REFERENCE)
        with pytest.raises(MissionError) as exc:
            loaded.track("SUMMIT")
        assert "SUMMIT" in str(exc.value)
        assert "AIRSHOW" in str(exc.value)

    def test_a_location_type_it_does_not_define_is_refused(self):
        loaded = mission.load(REFERENCE)
        with pytest.raises(MissionError) as exc:
            loaded.location_type("COMMAND_POST")
        assert "COMMAND_POST" in str(exc.value)


class TestTheMissionIsServerOwned:
    def test_a_session_cannot_switch_it(self, config):
        from surge_iw.services import tunables
        with pytest.raises(tunables.TunableError) as exc:
            tunables.validate({"mission": {"name": "other"}}, config)
        assert "mission" in str(exc.value)


# ---------------------------------------------------------------------------
# What a client can see
# ---------------------------------------------------------------------------


class TestCapabilitiesReportsTheMission:
    """The contract cannot declare mission vocabularies as enums — they come
    from a pack read at startup. `/v1/capabilities` is where a client learns
    them instead, so it has to actually carry them."""

    @pytest.fixture
    def client(self, monkeypatch, tmp_path: Path):
        from fastapi.testclient import TestClient
        from surge_iw.api.app import create_app

        monkeypatch.setenv("SURGE_API_TOKEN", "t" * 16)
        config = load_config(None)
        config["database"]["path"] = ":memory:"
        config["dry_run"] = True
        app = create_app(config)
        with TestClient(app) as client:
            client.headers.update({"Authorization": f"Bearer {'t' * 16}"})
            yield client

    def test_it_names_the_loaded_pack_and_its_digest(self, client):
        block = client.get("/v1/capabilities").json()["mission"]
        assert block["configured"] is True
        assert block["id"] == "reference"
        assert block["digest"] == mission.load(REFERENCE).digest

    def test_it_reports_the_vocabularies_a_client_must_use(self, client):
        block = client.get("/v1/capabilities").json()["mission"]
        assert block["tracks"] == ["CONCERT_TOUR", "SPORTING_EVENT", "AIRSHOW"]
        assert "VENUE" in block["location_types"]

    def test_the_digest_matches_the_bytes_on_disk(self, client):
        """The whole claim of the digest is that it identifies content. If it
        were computed from anything else this would still pass by accident, so
        it is checked against a hash taken independently of the loader."""
        import hashlib
        block = client.get("/v1/capabilities").json()["mission"]
        pairs = sorted(
            (str(p.relative_to(REFERENCE)),
             hashlib.sha256(p.read_bytes()).hexdigest())
            for p in REFERENCE.rglob("*")
            # Read from the loader's own set. A second copy of it here went
            # stale the moment `tests/` was added to the pack, and the failure
            # read as "the digest is wrong" rather than "this test is".
            if p.is_file()
            and not (set(p.relative_to(REFERENCE).parts)
                     & mission.CARRIED_ALONGSIDE)
            and p.name not in mission.CARRIED_FILES
        )
        expected = hashlib.sha256(
            "\n".join(f"{n}:{d}" for n, d in pairs).encode()).hexdigest()
        assert block["digest"] == expected


class TestNoMissionConfigured:
    def test_the_api_serves_and_says_so(self, monkeypatch):
        """A deployment that has only initialised its database is a legitimate
        state. It must be distinguishable from one running a mission, not
        silently identical to it."""
        from fastapi.testclient import TestClient
        from surge_iw.api.app import create_app

        monkeypatch.setenv("SURGE_API_TOKEN", "t" * 16)
        config = load_config(None)
        config["database"]["path"] = ":memory:"
        config["dry_run"] = True
        config["mission"]["name"] = ""
        app = create_app(config)
        with TestClient(app) as client:
            block = client.get(
                "/v1/capabilities",
                headers={"Authorization": f"Bearer {'t' * 16}"},
            ).json()["mission"]
        assert block["configured"] is False
        assert "cannot run" in block["effect"]

    def test_a_broken_pack_refuses_to_start_rather_than_degrading(self,
                                                                 tmp_path: Path):
        """There is no default to fall back to, so a pack that fails to load
        must stop the application rather than leaving it running against a
        definition nobody wrote."""
        from surge_iw.api.app import create_app

        broken = tmp_path / "missions" / "broken"
        shutil.copytree(REFERENCE, broken)
        (broken / "streams.yaml").unlink()

        config = load_config(None)
        config["database"]["path"] = ":memory:"
        config["dry_run"] = True
        config["mission"] = {"dir": str(broken.parent), "name": "broken"}
        with pytest.raises(MissionError) as exc:
            create_app(config)
        assert "streams.yaml" in str(exc.value)


# ---------------------------------------------------------------------------
# The tables that moved in P6
# ---------------------------------------------------------------------------


class TestTheEngineHoldsNoMissionData:
    """The property the whole refactor exists to get, asserted rather than
    believed. A leftover table is not merely untidy: it is a second source of
    truth that a mission cannot override, so a pack that disagreed with it
    would be silently half-applied."""

    ENGINE = Path(__file__).resolve().parents[1] / "surge_iw"

    #: Everything that is not a mission pack and not a recorded payload.
    #:
    #: `missions/` is where a mission is ALLOWED to be. `tests/fixtures/` holds
    #: responses captured from vendors, and a vendor's product names are facts
    #: about their catalogue rather than about anyone's mission — one rental
    #: model name collides with a search term in a pack shipped here.
    #: Everything else in the repository is the engine.
    SKIP = {".git", "__pycache__", ".venv", ".pytest_cache", ".hypothesis",
            "missions", "fixtures", "node_modules"}

    def engine_files(self):
        root = self.ENGINE.parent
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix in (".db", ".bak", ".shm",
                                                     ".wal"):
                continue
            if set(path.relative_to(root).parts) & self.SKIP:
                continue
            yield path

    def test_the_scan_reads_the_whole_repository(self):
        """It would pass just as well against nothing."""
        files = list(self.engine_files())
        assert len(files) > 150, len(files)
        names = {f.name for f in files}
        for expected in ("routes.py", "schema.sql", "README.md",
                         "build_api_contract.py", "config.example.yaml",
                         "missions.md", "behaviors_scoring.md", "pytest.ini"):
            assert expected in names, expected

    def test_no_file_in_the_engine_names_a_mission(self):
        """The property the whole refactor exists to get, over the WHOLE tree.

        Not just `surge_iw/`: docstrings, DDL comments, YAML, the README, the
        generated contract, the suite and its fixtures. Three narrower scans
        used to cover three of those and nothing covered the rest, which is
        how one mission's vocabulary sat in the scripts, the plans and the
        captured social payloads without anything failing.

        The vocabulary is harvested from the packs on disk — see
        `conftest.mission_vocabulary` — so this file does not hold the words it
        forbids, and a pack added tomorrow is covered the day it lands.
        """
        from conftest import mission_installed
        if not mission_installed():
            pytest.skip(
                "no pack other than `reference` is installed, so there is no "
                "second mission's vocabulary to leak. The scan is inert here "
                "by construction — this is what shipping the engine alone "
                "looks like — and skips rather than passing quietly.")
        root = self.ENGINE.parent
        offenders = [
            f"{path.relative_to(root)}:{n}: {match.group(0)}"
            for path in self.engine_files()
            for n, line in enumerate(
                path.read_text(errors="ignore").splitlines(), 1)
            for match in [MISSION_PROSE.search(line)] if match
        ]
        assert offenders == [], offenders

    @pytest.mark.parametrize("module,attribute", [
        ("surge_iw.services.geo", "COUNTY_SEATS"),
        ("surge_iw.services.facility", "FACILITY_ALIASES"),
        ("surge_iw.db.enums", "ACTOR_TRACKS"),
        ("surge_iw.db.enums", "LOCATION_TYPES"),
        ("surge_iw.db.enums", "TRACK_FLIGHT_CATEGORIES"),
        ("surge_iw.base.scoring", "WEIGHTS"),
        ("surge_iw.agents.queueing", "LEXICON"),
    ])
    def test_the_table_is_gone_not_merely_unused(self, module, attribute):
        """Deleted rather than left in place. A leftover constant is what a
        future validator reaches for by mistake."""
        import importlib
        assert not hasattr(importlib.import_module(module), attribute)


class TestTheSuiteRunsOnTheReferencePack:
    """The engine scan above stops at `surge_iw/`. The suite itself sat
    outside it, and ran for months on another mission's scenarios — its
    facilities, its actors, its search terms — all of it given to an engine that
    had the REFERENCE pack loaded.

    Nothing failed, because none of it is wrong at the type level: a location
    type accepts any string for a name. But a suite that proves the engine
    works on one mission's prose while claiming to be mission-neutral is
    proving the thing the refactor exists to stop assuming — and it is where
    the published contract's examples came from.
    """

    SUITE = Path(__file__).resolve().parent

    MISSION_PROSE = MISSION_PROSE

    #: Modules that may name one mission's vocabulary, and why. Every entry
    #: is a test that is ABOUT a pack, or the machinery for keeping the rest
    #: clean — never one that merely runs on data borrowed from a pack, which
    #: Modules that may still name a mission's vocabulary. Empty, and meant
    #: to stay empty: the scans no longer spell the words they look for, so
    #: nothing in this tree needs an exemption to hold the pattern, and the
    #: pack tests that assert a pack's own claims live with the pack.
    ABOUT_A_PACK: dict[str, str] = {}

    def test_the_allowlist_has_not_gone_stale(self):
        """An exemption for a file that no longer exists, or that no longer
        touches a pack, is an exemption nobody is checking."""
        for name, why in self.ABOUT_A_PACK.items():
            path = self.SUITE / name
            assert path.exists(), f"{name} is exempted and does not exist"
            assert why, name
            assert MISSION_PROSE.search(path.read_text()), (
                f"{name} no longer names a mission concept; drop its "
                "exemption rather than leaving a hole in the scan")

    def test_the_scan_can_see_what_it_is_looking_for(self):
        """Probes built FROM the packs, not written out here.

        A self-check that spelled a sentence would put the vocabulary back in
        the engine's tree — the exact thing the scan exists to keep out — and
        would only ever prove the pattern matches the words whoever wrote it
        happened to pick.
        """
        from conftest import mission_installed, mission_terms

        if not mission_installed():
            pytest.skip("no second pack to harvest a probe from")
        probes = mission_terms()
        assert len(probes) > 20, "the packs supplied almost no vocabulary"
        for term in probes:
            assert MISSION_PROSE.search(term), term
        # And the pack the engine DOES run on is not a violation.
        for allowed in (REFERENCE_MISSION.tracks
                        + REFERENCE_MISSION.location_types):
            assert not MISSION_PROSE.search(allowed), allowed

    #: How an engine test names a pack directory — both idioms the suite
    #: uses: a path segment after the literal missions, and a module-level
    #: PACKS constant. (Written without the literal form, so this comment is
    #: not itself a match.)
    NAMES_A_PACK = re.compile(r'"missions"\s*/\s*"([^"]+)"'
                              r'|\bPACKS\s*/\s*"([^"]+)"')

    def test_the_pack_scan_can_see_a_pack_being_named(self):
        """This file names the reference pack that way, so the pattern has
        something real to find. A regex that matched nothing would make the
        test below pass forever."""
        found = {m.group(1) or m.group(2)
                 for m in self.NAMES_A_PACK.finditer(
                     (self.SUITE / "test_mission.py").read_text())}
        assert "reference" in found

    def test_no_engine_test_names_a_pack_but_the_reference_one(self):
        """The point of moving the pack tests out.

        A pack is meant to be maintainable outside this repository — the
        shipped pack's own manifest says so. An engine suite that cannot run
        without one is not testing an engine, it is testing a deployment.
        `missions/reference` is the exception by design: it ships here
        precisely so the suite and the published contract have a mission to
        run against.

        Read from the SOURCE rather than from the filesystem. Checking which
        packs are on disk would make this test pass by the accident of a pack
        having been deleted, which is the state it is supposed to certify as
        survivable.
        """
        offenders = [
            f"{path.name}:{n}: {m.group(1) or m.group(2)}"
            for path in sorted(self.SUITE.rglob("*.py"))
            for n, line in enumerate(path.read_text().splitlines(), 1)
            # `tmp_path` builds a throwaway COPY of the reference pack to
            # break in place; it is not a second shipped mission.
            if "tmp_path" not in line
            for m in [self.NAMES_A_PACK.search(line)]
            if m and (m.group(1) or m.group(2)) != "reference"
        ]
        assert offenders == [], (
            "engine tests reaching for a pack other than the reference one: "
            f"{offenders}. What a pack contains is its own claim; put the "
            "test in missions/<name>/tests/, which pytest.ini collects.")


class TestTheMissionSuppliesTheTables:
    """Each table arrives from the pack. Asserted by loading a SECOND mission
    and showing the same input resolves differently — which a leftover
    hardcoded table could not produce.

    The second mission is SYNTHETIC, built by replacing the reference pack's
    tables rather than by loading one of the shipped packs. Two reasons, and
    the first is the important one: a pack is meant to be maintainable outside
    this repository, so an engine suite that cannot run without one is not
    testing an engine. The second is that an arbitrary vocabulary is a
    stronger fixture than a real one — nothing in the engine could have been
    written to accommodate it.

    What each shipped pack actually contains is that pack's claim, and is
    tested in `missions/<name>/tests/`.
    """

    @pytest.fixture(scope="class")
    def other(self):
        import dataclasses
        from surge_iw.services import geo
        # Both names are in the ENGINE's geo table; what no engine can know
        # is that they are one operational unit, which is why "Miami-Dade"
        # alone is ambiguous until a mission says so.
        equivalents = {"miami-dade county": "miami"}
        base = mission.load(REFERENCE)
        return dataclasses.replace(
            base,
            identifier="second",
            facility_aliases={"the big shed": "riverside fairground"},
            facility_tokens=frozenset({"exhibition"}),
            equivalents=equivalents,
            jurisdictions=geo.Equivalents.of(equivalents),
            publishers={"the phoenix bugle": "bugle.example"},
            hypotheses={**base.hypotheses, "FLIGHT": (
                {"code": "FERRY_FLIGHT",
                 "statement": "Aircraft repositioning between two bases.",
                 "when": "ALWAYS", "weakened_by": ""},)},
        )

    def test_facility_aliases_come_from_the_pack(self, other):
        from surge_iw.services import facility
        rows = [{"location_id": 7, "name": "Riverside Fairground"}]
        assert facility.resolve(["The Big Shed"], rows,
                                other).location_id == 7
        # The reference pack makes no such claim, so the same words do not
        # resolve. Nothing in the engine decides this either way.
        assert facility.resolve(["The Big Shed"], rows,
                                mission.load(REFERENCE)).location_id is None

    def test_generic_tokens_come_from_the_pack(self, other):
        """Which further words are too common to discriminate is the
        mission's domain knowledge, ADDED to the engine's structural core."""
        from surge_iw.services import facility
        rows = [{"location_id": 1, "name": "North Exhibition Center"},
                {"location_id": 2, "name": "South Exhibition Center"}]
        assert facility.resolve(
            ["Exhibition Center"], rows, other).method == "TOO_GENERIC"
        assert facility.resolve(
            ["Exhibition Center"], rows,
            mission.load(REFERENCE)).method == "AMBIGUOUS"

    def test_jurisdiction_equivalence_comes_from_the_pack(self, other):
        from surge_iw.services import geo
        assert geo.resolve_city("Miami-Dade", other.jurisdictions)[0] == \
            "miami-dade county"
        assert geo.resolve_city(
            "Miami-Dade", mission.load(REFERENCE).jurisdictions)[0] is None

    def test_publishers_come_from_the_pack(self, other):
        from surge_iw.services import provenance
        post = {"source_domain": "The Phoenix Bugle"}
        assert provenance.publisher_of(post, other).key == "bugle.example"
        assert provenance.publisher_of(post).method == "UNKNOWN"

    def test_the_engines_own_publishers_survive(self, other):
        """A mission ADDS regional outlets; it does not replace the wire
        services, or every pack would have to restate them."""
        from surge_iw.services import provenance
        for pack in (other, mission.load(REFERENCE), None):
            assert provenance.publisher_of(
                {"source_domain": "Reuters"}, pack).key == "reuters.com"

    def test_competing_explanations_come_from_the_pack(self, other):
        from surge_iw.services import hypotheses
        rows = [{"signal_id": 1, "signal_type": "FLIGHT",
                 "category_confidence": "CONFIRMED"}]
        codes = {h["code"] for h in
                 hypotheses.for_correlation(rows, mission=other)}
        assert "FERRY_FLIGHT" in codes
        assert "FERRY_FLIGHT" not in {
            h["code"] for h in
            hypotheses.for_correlation(rows, mission=mission.load(REFERENCE))}

    def test_without_a_mission_nothing_is_invented(self):
        """An explanation the engine made up would read exactly like one an
        analyst wrote."""
        from surge_iw.services import hypotheses
        assert hypotheses.for_correlation(
            [{"signal_id": 1, "signal_type": "LODGING"}]) == []


class TestTheAuthoringGuideMatchesTheLoader:
    """`docs/missions.md` documents every refusal by its message.

    A documentation table that drifts is worse than none: someone debugging a
    pack reads it, looks for a message that no longer exists, and concludes the
    loader is broken. Cheap to check, so it is checked.
    """

    GUIDE = Path(__file__).resolve().parents[1] / "docs" / "missions.md"
    SOURCES = ("surge_iw/services/mission.py", "surge_iw/services/geo.py")

    def _source(self):
        import re
        root = Path(__file__).resolve().parents[1]
        joined = " ".join((root / s).read_text() for s in self.SOURCES)
        # Message text is f-strings wrapped across lines: collapse whitespace
        # and drop the interpolations so a phrase can be found whole.
        joined = re.sub(r'"\s*(?:f)?"', "", joined)
        joined = re.sub(r"\{[^}]*\}", "\x00", joined)
        return re.sub(r"\s+", " ", joined)

    def _claims(self):
        import re
        guide = self.GUIDE.read_text()
        table = guide[guide.index("| Refusal | Cause |"):]
        return [re.match(r"\| `([^`]+)`", line).group(1)
                for line in table.splitlines() if line.startswith("| `")]

    def test_the_table_is_not_empty(self):
        assert len(self._claims()) > 20

    def test_every_documented_refusal_exists_in_the_loader(self):
        import re
        source = self._source()
        missing = []
        for claim in self._claims():
            # A claim may span an interpolation; match its literal fragments.
            fragments = [f.strip() for f in re.split(r"<[^>]*>", claim)
                         if f.strip()]
            fragments = [re.sub(r"\s+", " ", f) for f in fragments]
            if not all(f in source for f in fragments):
                missing.append(claim)
        assert missing == [], missing

    def test_the_vocabularies_it_quotes_are_the_real_ones(self):
        """The guide lists the scoring kinds, families, flight codes, prompt
        slots and hypothesis conditions. Each is a closed engine set a pack
        author has to match exactly."""
        from surge_iw.services.hypotheses import CONDITIONS
        guide = self.GUIDE.read_text()
        for value in (list(mission.SCORING_KINDS) + list(mission.FAMILIES)
                      + sorted(mission.FLIGHT_CODES) + list(mission.PROMPTS)
                      + sorted(CONDITIONS)
                      + sorted(mission.THRESHOLD_SECTIONS)):
            assert f"`{value}`" in guide or value in guide, value

    def test_every_key_the_loader_accepts_is_documented(self):
        """The pack FORMAT, pinned to the loader that enforces it.

        A key added to `mission.py` and not to the guide is a format change an
        author would discover only by having their pack refused — and the
        refusal names the key, not what it is for.
        """
        guide = self.GUIDE.read_text()
        undocumented = [
            key
            for group in (mission.MANIFEST_KEYS, mission.SCORING_KEYS,
                          mission.GEOGRAPHY_KEYS, mission.FACILITY_KEYS,
                          mission.THRESHOLD_SECTIONS, set(mission.PROMPTS),
                          mission.STREAM_KEYS,
                          set(mission.SOCIAL_PLATFORMS))
            for key in sorted(group)
            # Backticked, or written as the YAML key it is. A bare substring
            # match let `collects` pass on the word "collects" in a sentence
            # about what the engine does — which is how an undocumented key
            # reaches an author as a refusal instead of as a paragraph.
            if f"`{key}`" not in guide and f"{key}:" not in guide
        ]
        assert undocumented == [], undocumented

    def test_it_says_which_keys_are_required(self):
        guide = self.GUIDE.read_text()
        for key in mission.REQUIRED_MANIFEST_KEYS:
            assert f"{key}:" in guide, key

    def test_the_engine_doc_scan_reads_more_than_the_guide(self):
        """It would pass just as well against an empty directory."""
        docs = self.GUIDE.parent
        found = {p.name for p in docs.rglob("*.md")}
        assert {"missions.md", "config.md", "behaviors_scoring.md"} <= found, found

    def test_it_states_the_engine_core_sizes_correctly(self):
        """The guide tells an author how many words they are ADDING to. A
        stale count would have them restate what is already there, or assume
        cover they do not have."""
        from surge_iw.services import facility
        guide = self.GUIDE.read_text()
        assert f"engine's {len(facility.GENERIC_TOKENS)} structural" in guide
        assert f"engine's {len(facility._SPELLINGS)} generic" in guide


class TestAPackCannotSetWhatTheEngineDoesNotRead:
    """`thresholds` is checked key by key, not only section by section.

    A pack setting a key nothing reads is the same defect as a session tunable
    that is accepted and ignored: it would be merged, hashed into every
    receipt, and change nothing — so the record would claim it shaped a
    judgement it never touched.

    Found when `correlation.lodging_drop_min` was removed from the engine as
    inert: both shipped packs still set it, and the loader accepted it, because
    it validated the section name and stopped there.
    """

    def test_an_unread_threshold_key_is_refused_by_name(self, pack: Path):
        _edit_manifest(
            pack,
            lambda d: d["thresholds"]["correlation"].update(
                {"lodging_drop_min": 20.0}))
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "lodging_drop_min" in str(exc.value)
        assert "does not read" in str(exc.value)

    def test_the_refusal_says_what_was_settable(self, pack: Path):
        _edit_manifest(
            pack,
            lambda d: d["thresholds"]["windows"].update({"near_term_hrs": 48}))
        with pytest.raises(MissionError) as exc:
            mission.load(pack)
        assert "near_term_hours" in str(exc.value), (
            "a refusal that does not name the alternative makes a typo a "
            "guessing game")

    def test_the_pack_the_engine_runs_on_sets_nothing_unread(self):
        """The instance that prompted the rule. Every shipped pack repeats
        this check in its own tests; the engine asserts it for the one its
        suite and its published contract are built against."""
        from surge_iw.config import DEFAULT_CONFIG
        loaded = mission.load(REFERENCE)
        for section, values in loaded.thresholds.items():
            unknown = set(values) - set(DEFAULT_CONFIG.get(section) or {})
            assert not unknown, f"{section} sets {sorted(unknown)}"
