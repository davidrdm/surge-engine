"""Competing explanations for a correlation (9.6, issue #2).

The review's point was not that the arithmetic is wrong. It is that a reader
shown a score and its working, and nothing else, has to supply the alternatives
themselves — and the reader who most needs them is the one already persuaded.

One mitigation already existed and is worth stating precisely rather than
counting as a fix: baselines are weekday-aligned at +7 and +14 days, so
ordinary weekly demand is differenced out of the *score*. That is a different
job from recording the hypothesis for the *reader*, and these tests are partly
about keeping the two apart.
"""
from __future__ import annotations

import json

from conftest import REFERENCE_MISSION
from surge_iw.services import hypotheses
from test_orchestrator import wiring       # noqa: F401 — a fixture


def sig(signal_type, signal_id=1, **extra):
    return {"signal_id": signal_id, "signal_type": signal_type, **extra}


def codes(result):
    return [item["code"] for item in result]


class TestWhatEachFamilyAdmits:
    def test_a_booking_only_correlation_admits_ordinary_demand(self):
        result = hypotheses.for_correlation([sig("LODGING"), sig("CAR", 2)],
                                 mission=REFERENCE_MISSION)
        assert "CONVENTION" in codes(result)
        assert "HOLIDAY_TRAVEL" in codes(result)
        assert "FLEET_REPOSITIONING" in codes(result)

    def test_a_flight_correlation_admits_routine_movement(self):
        result = hypotheses.for_correlation([sig("FLIGHT", category_confidence="CONFIRMED")],
                                 mission=REFERENCE_MISSION)
        assert "ROUTINE_AVIATION" in codes(result)
        assert "UNRELATED_EVENT" in codes(result)
        assert "CONVENTION" not in codes(result), (
            "a convention does not book military transport")

    def test_an_ambiguous_flight_admits_a_civilian_aircraft(self):
        """Live positions carry no category field at all, so the record is
        presence rather than classification."""
        result = hypotheses.for_correlation([sig("FLIGHT", category_confidence="AMBIGUOUS")],
                                 mission=REFERENCE_MISSION)
        assert "CIVILIAN_AIRCRAFT" in codes(result)

    def test_a_confirmed_category_does_not_admit_it(self):
        result = hypotheses.for_correlation([sig("FLIGHT", category_confidence="CONFIRMED")],
                                 mission=REFERENCE_MISSION)
        assert "CIVILIAN_AIRCRAFT" not in codes(result)

    def test_social_alone_admits_amplification(self):
        result = hypotheses.for_correlation([sig("SOCIAL")],
                                 mission=REFERENCE_MISSION)
        assert "RUMOUR_AMPLIFICATION" in codes(result)
        assert "ANTICIPATORY_COMMENTARY" in codes(result)

    def test_social_alongside_movement_does_not(self):
        """Corroboration is the answer to the rumour hypothesis: it would have
        to explain the movement too, and it cannot."""
        result = hypotheses.for_correlation([sig("SOCIAL"), sig("FLIGHT", 2, category_confidence="CONFIRMED")],
                                 mission=REFERENCE_MISSION)
        assert "RUMOUR_AMPLIFICATION" not in codes(result)

    def test_nothing_contributing_produces_no_list(self):
        """A correlation scored entirely out of a coverage gap has no evidence
        to explain away, and a list of alternatives to nothing would read as
        rigour while saying nothing."""
        assert hypotheses.for_correlation([], mission=REFERENCE_MISSION) == []


class TestItStatesRatherThanOverstates:
    def test_the_baseline_alignment_is_named_not_assumed(self):
        """The mitigation exists in the arithmetic; a reader cannot see it
        without being told, and being told is the whole point."""
        result = hypotheses.for_correlation([sig("LODGING")],
                                 mission=REFERENCE_MISSION)
        convention = next(i for i in result if i["code"] == "CONVENTION")
        assert "+7 and +14" in convention["weakened_by"]

    def test_corroboration_weakens_a_demand_explanation_without_removing_it(
        self,
    ):
        """"Does not explain all of it" is not "is false". An operator deciding
        to act is entitled to both halves."""
        alone = hypotheses.for_correlation([sig("LODGING")],
                                 mission=REFERENCE_MISSION)
        with_flight = hypotheses.for_correlation([sig("LODGING"), sig("FLIGHT", 2, category_confidence="CONFIRMED")],
                                 mission=REFERENCE_MISSION)
        assert "CONVENTION" in codes(with_flight)
        weakened = next(i for i in with_flight if i["code"] == "CONVENTION")
        assert "independent families" in weakened["weakened_by"]
        assert "independent families" not in next(
            i for i in alone if i["code"] == "CONVENTION")["weakened_by"]

    def test_every_entry_has_a_stable_code_and_a_statement(self):
        """A reviewer suppressing one alternative across alerts should match on
        the code, never on prose that may be reworded."""
        result = hypotheses.for_correlation([sig("LODGING"), sig("FLIGHT", 2), sig("SOCIAL", 3)],
                                 mission=REFERENCE_MISSION)
        assert result
        for item in result:
            assert item["code"] and item["code"].isupper()
            assert item["statement"].endswith(".")
            assert set(item) == {"code", "statement", "weakened_by"}
        assert len(codes(result)) == len(set(codes(result))), "no duplicates"

    def test_no_alternative_claims_to_be_ruled_out(self):
        """`weakened_by` is what argues against an explanation, never a verdict
        that it is false. This system does not know that."""
        result = hypotheses.for_correlation([sig("LODGING"), sig("CAR", 2)],
                                 mission=REFERENCE_MISSION)
        for item in result:
            assert "ruled out" not in item["weakened_by"].lower()
            assert "impossible" not in item["weakened_by"].lower()


class TestItReachesTheCorrelation:
    def test_a_scored_correlation_stores_its_alternatives(self, db, config,
                                                          wiring):
        import test_orchestrator as T

        session, _city, connectors, llm = wiring
        orch = T.build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.run(iteration)

        rows = db.all("SELECT alternatives_json FROM correlations "
                      "WHERE iteration_id = ?", (iteration,))
        assert rows
        stored = [json.loads(r["alternatives_json"] or "[]") for r in rows]
        assert any(stored), "a scored correlation admits something"
        for entry in [item for group in stored for item in group]:
            assert entry["code"] and entry["statement"]

    def test_they_are_read_back_rather_than_recomputed(self, db, config,
                                                       wiring):
        """An alert written months ago must show the alternatives ITS rules
        produced, not today's."""
        import test_orchestrator as T

        session, _city, connectors, llm = wiring
        orch = T.build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.run(iteration)
        correlation = db.one("SELECT correlation_id FROM correlations "
                             "WHERE iteration_id = ?", (iteration,))
        db._exec("UPDATE correlations SET alternatives_json = ? "
                 "WHERE correlation_id = ?",
                 (json.dumps([{"code": "HISTORICAL", "statement": "x.",
                               "weakened_by": ""}]),
                  correlation["correlation_id"]))

        from surge_iw.api.routes import _evidence_for_correlation
        evidence = _evidence_for_correlation(
            db, config, int(correlation["correlation_id"]))
        assert [i["code"] for i in evidence.alternatives] == ["HISTORICAL"]

    def test_a_correlation_from_before_the_rules_reports_null(self, db,
                                                              session,
                                                              iteration,
                                                              config):
        """Null and empty are different answers: null is "these rules had not
        been written", empty is "nothing contributed"."""
        from surge_iw.api.routes import _evidence_for_correlation

        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        correlation_id = db.upsert_correlation(
            iteration_id=iteration, city_id=city, track="AIRSHOW",
            score=0.5, band="MEDIUM", distinct_types=2,
            contributions={"social": 0.5}, data_completeness=1.0,
            failed_sources="", band_capped=False, rule_trace="t")
        assert _evidence_for_correlation(
            db, config, correlation_id).alternatives is None
