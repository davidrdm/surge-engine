"""What the reference pack claims about itself.

A pack is the audit unit, so the claims it makes are its own to defend: that it
loads, that its tracks are covered end to end, and that it is deliberately
UNLIKE the mission the engine grew up with. The engine can assert that a pack's
tables are used; it cannot assert what is in them.

Collected by the engine's `pytest.ini` (`testpaths = tests missions`), and
carried in `tests/`, which the loader treats as filed-alongside: it is neither
read nor hashed, so adding a case here cannot move the digest that every
receipt records.
"""
from __future__ import annotations

from pathlib import Path

from surge_iw.config import DEFAULT_CONFIG
from surge_iw.services import facility
from surge_iw.services import mission as mission_service

PACK = Path(__file__).resolve().parents[1]


class TestItLoads:
    def test_the_manifest_is_valid_and_names_itself(self):
        loaded = mission_service.load(PACK)
        assert loaded.identifier == "reference"
        assert loaded.label == "reference/2"

    def test_every_stream_carries_every_track_and_every_track_a_weight(self):
        loaded = mission_service.load(PACK)
        assert loaded.streams, "version 2 is the streams exhibit"
        for stream in loaded.streams:
            for track in loaded.tracks:
                assert stream.lexicon[track], f"{stream.id}/{track}"
        for track in loaded.tracks:
            assert loaded.weights[track], track
            assert track in loaded.flight_categories, track

    def test_it_shows_both_stream_shapes(self):
        """The pack exists to exhibit the machinery, so it keeps one stream in
        each shape: a sub-kind of SOCIAL and a promoted family — and the
        promoted one carries its own relevance leg, so per-stream criteria
        have a shipped example too."""
        loaded = mission_service.load(PACK)
        families = {s.id: s.family for s in loaded.streams}
        assert families == {"chatter": "SOCIAL", "local_news": "LOCAL_NEWS"}
        by_id = {s.id: s for s in loaded.streams}
        assert set(by_id["local_news"].relevance) == {"relevance_strict"}
        assert by_id["chatter"].relevance == {}

    def test_the_stream_weights_preserve_the_v1_social_budget(self):
        """The split is a refinement, not a rebalance: per track, the two
        stream weights sum to what `social` carried in version 1, so scores
        stay comparable across the pack bump."""
        loaded = mission_service.load(PACK)
        v1_social = {"CONCERT_TOUR": 0.40, "SPORTING_EVENT": 0.35,
                     "AIRSHOW": 0.25}
        for track, total in v1_social.items():
            split = (loaded.weights[track]["chatter"]
                     + loaded.weights[track]["local_news"])
            assert abs(split - total) < 1e-9, track


class TestItIsFitToBeTheEnginesFixture:
    """Why this pack exists at all. The engine's suite and its published
    contract are built against it, so if it drifted into the shape of the
    mission the engine was extracted from, both would go back to proving that
    the engine works for that one mission."""

    def test_it_does_not_have_the_shape_the_engine_grew_up_with(self):
        """Three tracks, not two, and neither of the two the engine used to
        carry compiled in. A surviving `len(tracks) == 2` assumption must fail
        loudly rather than pass by luck, and a table left behind in the engine
        must not still resolve."""
        loaded = mission_service.load(PACK)
        assert set(loaded.tracks) == {"CONCERT_TOUR", "SPORTING_EVENT",
                                      "AIRSHOW"}

    def test_between_them_the_tracks_use_every_scored_flight_category(self):
        """Otherwise a scoring path is covered only by a pack that lives
        outside this repository."""
        loaded = mission_service.load(PACK)
        used = {code for codes in loaded.flight_categories.values()
                for code in codes}
        assert used == mission_service.FLIGHT_CODES


class TestItsTablesAreWellFormed:
    def test_it_sets_no_threshold_the_engine_does_not_read(self):
        """The loader refuses this, so a failure here means the pack is
        unloadable — stated separately because it is the pack's claim to keep,
        not a favour the loader does it."""
        loaded = mission_service.load(PACK)
        for section, values in loaded.thresholds.items():
            unknown = set(values) - set(DEFAULT_CONFIG.get(section) or {})
            assert not unknown, f"{section} sets {sorted(unknown)}"

    def test_its_facility_aliases_are_normalised_keys(self):
        """`normalise` runs before the lookup, so an alias whose key is not
        already normalised could never be found."""
        loaded = mission_service.load(PACK)
        assert loaded.facility_aliases
        for alias in loaded.facility_aliases:
            collapsed = facility._WS_RE.sub(
                " ", facility._PUNCT_RE.sub(" ", alias.lower())).strip()
            assert collapsed == alias, f"{alias!r} would never be looked up"
