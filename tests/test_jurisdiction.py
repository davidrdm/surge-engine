"""Two place names, one operational unit (9.9).

A session named one place and a source reported the same activity under the
containing administrative unit's name. Found live, and the failure was total: a
Miami session collected an article naming "Miami-Dade County", the model judged
it relevant at salience 0.85, and it was refused with `city not in user list` —
because `miami-dade county` is not the string `miami`. Evidence this system
exists to surface was collected, paid for, judged, and dropped on a name, and
the iteration reported a quiet city.

**WHICH names are equivalent is a mission's judgement**, so the table lives in
the pack. What the engine owns is the rule, and the rule is narrow: an
equivalence is a statement that two names mean one unit, it is deliberately not
transitive, and it never merges two units into one. That narrowness is the
safety property — the same live article also named a neighbouring unit, and
admitting that one would manufacture evidence rather than find it.

WHICH names a mission calls equivalent is that pack's claim, so the table and
the tests that hold it to account live with the pack, under
`missions/<name>/tests/`.
"""
from __future__ import annotations

import pytest

from conftest import REFERENCE_MISSION
from surge_iw.agents.queueing import admit_city
from surge_iw.services import geo


#: A fixture table, so these tests state every input they depend on. Two real
#: places from the engine's geography, declared equivalent here and nowhere
#: else — no mission in this repository makes this claim.
FIXTURE = geo.Equivalents.of({"miami-dade county": "miami"})


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class TestTheEquivalenceRule:
    def test_it_reads_both_ways(self):
        assert FIXTURE.others("miami-dade county") == ["miami"]
        assert FIXTURE.others("miami") == ["miami-dade county"]

    def test_the_unit_is_the_key_that_names_it(self):
        assert FIXTURE.unit_of("miami-dade county") == "miami-dade county"
        assert FIXTURE.unit_of("miami") == "miami-dade county"

    def test_a_place_it_says_nothing_about_stands_alone(self):
        assert FIXTURE.others("boston") == []
        assert FIXTURE.unit_of("boston") == "boston"

    def test_it_is_not_transitive(self):
        """Two neighbouring units are two units. Merging them would let a
        report about one admit to a session that named the other."""
        table = geo.Equivalents.of({"miami-dade county": "miami",
                                    "broward county": "fort lauderdale"})
        assert "miami" not in table.others("broward county")
        assert "broward county" not in table.others("miami")

    def test_two_units_cannot_claim_one_name(self):
        """Otherwise the reverse lookup silently picks one, and "which unit is
        this?" becomes a guess."""
        with pytest.raises(ValueError) as exc:
            geo.Equivalents.of({"a county": "springfield",
                                "b county": "springfield"})
        assert "springfield" in str(exc.value)

    def test_the_engine_claims_nothing_on_its_own(self):
        """`NO_EQUIVALENTS` is the default everywhere a mission is absent. The
        engine does not know that any two place names mean one place."""
        assert geo.NO_EQUIVALENTS.others("miami") == []
        assert geo.NO_EQUIVALENTS.unit_of("miami") == "miami"


class TestResolvingTheName:
    def test_a_hyphenated_short_form_resolves_under_an_equivalence(self):
        """The live miss. `Miami-Dade` prefix-matches BOTH `miami` and
        `miami-dade county`, and without an equivalence the resolver refuses to
        choose — correctly, under a rule that cannot tell two names for one
        place from two different places."""
        assert geo.resolve_city("Miami-Dade", FIXTURE) == (
            "miami-dade county", "PREFIX")

    def test_every_spelling_lands_in_the_same_place(self):
        for spelling in ("Miami-Dade", "Miami Dade", "Miami-Dade County",
                         "miami dade county", "Miami-Dade, FL"):
            assert geo.resolve_city(spelling, FIXTURE)[0] == \
                "miami-dade county", spelling

    def test_without_an_equivalence_it_refuses_rather_than_guesses(self):
        """The safe direction, and the engine's default. A refusal is a
        recorded UNRESOLVED; a guess is a confident answer for the wrong
        place."""
        assert geo.resolve_city("Miami-Dade")[0] is None

    def test_a_genuinely_ambiguous_prefix_still_resolves_to_nothing(self):
        """The rule this relaxes is the one that stops `San` becoming San
        Diego. It must still stop it, equivalence table or not."""
        for ambiguous in ("San", "Fort", "New", "Char"):
            assert geo.resolve_city(ambiguous, FIXTURE) == (None, "UNRESOLVED")

    def test_the_collapse_only_fires_when_one_unit_is_named(self):
        assert len(geo._candidates("san")) > 1
        assert len({FIXTURE.unit_of(m) for m in geo._candidates("san")}) > 1
        assert geo.resolve_city("San", FIXTURE)[0] is None

        assert len(geo._candidates("miami-dade")) > 1
        assert len({FIXTURE.unit_of(m)
                    for m in geo._candidates("miami-dade")}) == 1

    def test_an_unknown_place_is_still_refused(self):
        assert geo.resolve_city("Sayre", FIXTURE)[0] is None


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


class TestAdmission:
    """`admit_city` reads the equivalence table off the loaded mission, so
    these run against a database carrying one."""

    @pytest.fixture
    def db(self, db):
        # A database whose mission declares the fixture equivalence, built by
        # replacing just that field — the rest of the reference pack is
        # unchanged, so nothing else about the run moves.
        import dataclasses
        db.mission = dataclasses.replace(
            REFERENCE_MISSION,
            equivalents={"miami-dade county": "miami"},
            jurisdictions=FIXTURE)
        return db

    @pytest.fixture
    def miami(self, db, session):
        return db.insert_city(session, "Miami", canonical="miami", state="FL")

    def evidence(self, salience=0.85):
        return [{"publisher_key": "local10.com", "publisher_method": "HOST",
                 "claim_key": "c1", "salience": salience}]

    def admit(self, db, session, iteration, name, expand=False):
        return admit_city(
            db, {"min_independent_domains": 2}, iteration_id=iteration,
            session_id=session, name=name, signals=self.evidence(),
            expand_cities=expand, stage="TRIAGING")

    def test_the_live_miss_now_admits(self, db, session, iteration, miami):
        assert self.admit(db, session, iteration, "Miami-Dade") == miami

    def test_a_neighbouring_unit_is_still_refused(self, db, session, iteration,
                                                  miami):
        """Named in the SAME article, and not this session's unit. Nothing
        declares it equivalent, so nothing admits it."""
        assert self.admit(db, session, iteration, "Broward") is None
        row = db.one("SELECT outcome, detail FROM queue_decisions "
                     "WHERE city_name = 'Broward'")
        assert row["outcome"] == "CITY_NOT_ADMITTED"

    def test_a_city_far_away_is_still_refused(self, db, session, iteration,
                                              miami):
        for elsewhere in ("San Diego", "Sayre", "Chicago"):
            assert self.admit(db, session, iteration, elsewhere) is None

    def test_it_works_the_other_way_round(self, db, session, iteration):
        """An operator may register the larger unit and the model name the
        smaller one."""
        county = db.insert_city(session, "Miami-Dade County",
                                canonical="miami-dade county", state="FL")
        assert self.admit(db, session, iteration, "Miami") == county

    def test_an_exact_match_still_wins(self, db, session, iteration, miami):
        """The equivalence path runs only after the exact one fails, so a
        session holding both keeps them distinct."""
        county = db.insert_city(session, "Miami-Dade County",
                                canonical="miami-dade county", state="FL")
        assert self.admit(db, session, iteration, "Miami") == miami
        assert self.admit(db, session, iteration, "Miami-Dade County") == county

    def test_the_match_is_recorded(self, db, session, iteration, miami):
        """Deciding that one place name means another is an analytical
        decision. A reader seeing a Miami signal is otherwise given no account
        of how a Miami-Dade report produced it."""
        self.admit(db, session, iteration, "Miami-Dade")
        row = db.one("SELECT message, extra_json FROM agent_log "
                     "WHERE agent = 'admit_city'")
        assert row is not None, "the match left no record"
        assert "Miami-Dade" in row["message"]
        assert "one jurisdiction" in row["message"]
        assert "miami-dade county" in row["extra_json"]

    def test_a_deployment_with_no_equivalences_admits_nothing_extra(
        self, db, session, iteration
    ):
        """The reference mission declares none, which is the common case: the
        feature costs nothing and claims nothing until a mission says so."""
        db.mission = REFERENCE_MISSION
        db.insert_city(session, "Miami", canonical="miami", state="FL")
        assert self.admit(db, session, iteration, "Miami-Dade") is None
