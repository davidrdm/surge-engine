"""An alert that re-scores old collection says so (9.13).

The correlation window reads ACROSS iterations by design: a flight observed
thirty minutes before this iteration started is still live evidence, and
scoping to the current run would discard it purely because of when the operator
pressed the button.

The cost of that design is invisible repetition. Measured live: Atlanta
correlation 25 linked 98 signals, **none from its own iteration** — all from
two runs six days earlier — and was the fourth alert in a row produced from a
single day's collection. Each arrived as a fresh alert row with a fresh
model-written summary describing flights that had landed days before.

The scoring is right and the decay is visibly working. What was missing is the
disclosure, so the owner's decision was to record rather than suppress: say
which iteration last contributed and how old the evidence is, and let the
reader decide.
"""
from __future__ import annotations


import pytest

from surge_iw.base.scoring import correlate, staleness_note
from conftest import AIRLIFT, ANCHOR, CHARTER, flight


def score(signals, cfg, **kw):
    return correlate(signals, track=AIRLIFT, anchor_at=ANCHOR, cfg=cfg, **kw)


def aged(hours, iteration, signal_id, fr24_id=None):
    row = flight(category="M", fr24_id=fr24_id or f"m{signal_id}",
                 signal_id=signal_id, hours_ago=hours)
    row["iteration_id"] = iteration
    return row


class TestTheNote:
    def test_it_names_the_iteration_and_the_age_span(self):
        note = staleness_note({"signals": 98, "newest_iteration": 4,
                               "new_this_iteration": False,
                               "oldest_hours": 160.4, "newest_hours": 129.0})
        assert "nothing has contributed since iteration 4" in note
        assert "129-160.4h old" in note
        assert "98 contributing signal(s)" in note
        assert "re-scored, not a new observation" in note

    def test_one_age_reads_as_one_age(self):
        note = staleness_note({"signals": 1, "newest_iteration": 4,
                               "new_this_iteration": False,
                               "oldest_hours": 12.0, "newest_hours": 12.0})
        assert "are 12h old" in note

    def test_a_run_that_contributed_says_nothing(self):
        """A correlation mixing new evidence with old is an ordinary current
        assessment and needs no disclosure."""
        assert staleness_note({"new_this_iteration": True}) == ""

    def test_an_unanswerable_question_is_not_answered(self):
        """`correlate` called without an iteration id cannot know, so it must
        not claim either way."""
        assert staleness_note({"new_this_iteration": None}) == ""
        assert staleness_note({}) == ""
        assert staleness_note(None) == ""


class TestWhatCorrelateRecords:
    def test_a_run_contributing_nothing_is_flagged(self, corr_cfg):
        result = score([aged(120, iteration=4, signal_id=1),
                        aged(150, iteration=3, signal_id=2)],
                       corr_cfg, iteration_id=14)
        fresh = result.evidence_freshness
        assert fresh["new_this_iteration"] is False
        assert fresh["newest_iteration"] == 4
        assert fresh["signals"] == 2
        assert fresh["newest_hours"] == 120.0
        assert fresh["oldest_hours"] == 150.0

    def test_a_run_contributing_something_is_not(self, corr_cfg):
        result = score([aged(120, iteration=4, signal_id=1),
                        aged(1, iteration=14, signal_id=2)],
                       corr_cfg, iteration_id=14)
        assert result.evidence_freshness["new_this_iteration"] is True
        assert result.evidence_freshness["newest_iteration"] == 14

    def test_without_an_iteration_id_no_claim_is_made(self, corr_cfg):
        result = score([aged(120, iteration=4, signal_id=1)], corr_cfg)
        assert result.evidence_freshness["new_this_iteration"] is None
        assert staleness_note(result.evidence_freshness) == ""

    def test_only_CONTRIBUTING_signals_are_counted(self, corr_cfg):
        """A kind carrying no weight on this track is evidence in the trail but
        contributed nothing, so it must not set the age of what did."""
        contributing = aged(10, iteration=14, signal_id=1)
        # Military carries no weight on the CONCERT_TOUR track.
        result = correlate([contributing], track=CHARTER,
                           anchor_at=ANCHOR, cfg=corr_cfg, iteration_id=14)
        assert result.contributions.get("flight_M", 0.0) == 0.0
        assert result.evidence_freshness == {}

    def test_nothing_eligible_records_nothing(self, corr_cfg):
        assert score([], corr_cfg, iteration_id=14).evidence_freshness == {}


class TestItReachesTheCaveat:
    def test_a_stale_correlation_carries_the_note(self, corr_cfg):
        result = score([aged(120, iteration=4, signal_id=1),
                        aged(150, iteration=3, signal_id=2)],
                       corr_cfg, iteration_id=14)
        assert "No new evidence this iteration" in result.caveat()

    def test_it_appears_even_when_collection_was_complete(self, corr_cfg):
        """The existing caveat only fires on a coverage gap. Staleness is a
        different disclosure and must not depend on one."""
        result = score([aged(120, iteration=4, signal_id=1)], corr_cfg,
                       iteration_id=14)
        assert result.failed_sources == []
        assert result.caveat() is not None
        assert "Collection incomplete" not in result.caveat()

    def test_it_sits_alongside_a_coverage_gap(self, corr_cfg):
        result = score([aged(120, iteration=4, signal_id=1)], corr_cfg,
                       iteration_id=14, unreliable_source_types=["CAR"])
        caveat = result.caveat()
        assert "Collection incomplete" in caveat
        assert "No new evidence this iteration" in caveat

    def test_a_fresh_correlation_keeps_its_silence(self, corr_cfg):
        result = score([aged(1, iteration=14, signal_id=1)], corr_cfg,
                       iteration_id=14)
        assert result.caveat() is None


class TestTheAlertRebuildsIt:
    def test_the_written_alert_carries_the_note(self, db, config, session,
                                                iteration):
        """AlertAgent rebuilds the caveat from the STORED row, so the record
        has to be enough on its own."""
        from surge_iw.agents.alerting import AlertAgent
        from test_triage import FakeLLM

        city = db.insert_city(session, "Atlanta", canonical="atlanta")
        db.upsert_correlation(
            iteration_id=iteration, city_id=city, track=AIRLIFT.name,
            score=0.5, band="MEDIUM", distinct_types=2,
            contributions={"social": 0.5}, data_completeness=1.0,
            failed_sources=[], failed_families=[], band_capped=False,
            rule_trace="t",
            evidence_freshness={"signals": 98, "newest_iteration": 4,
                                "new_this_iteration": False,
                                "oldest_hours": 160.4, "newest_hours": 129.0})
        AlertAgent(db, config, FakeLLM([{"summary": "Something happened."}],
                                       translate=False)).run(iteration)
        caveat = db.one("SELECT caveat FROM alerts")["caveat"]
        assert caveat and "nothing has contributed since iteration 4" in caveat

    def test_a_correlation_predating_the_check_says_nothing(self, db, config,
                                                            session, iteration):
        from surge_iw.agents.alerting import AlertAgent
        from test_triage import FakeLLM

        city = db.insert_city(session, "Atlanta", canonical="atlanta")
        db.upsert_correlation(
            iteration_id=iteration, city_id=city, track=AIRLIFT.name,
            score=0.5, band="MEDIUM", distinct_types=2,
            contributions={"social": 0.5}, data_completeness=1.0,
            failed_sources=[], failed_families=[], band_capped=False,
            rule_trace="t")
        db._exec("UPDATE correlations SET evidence_freshness_json = NULL")
        AlertAgent(db, config, FakeLLM([{"summary": "Something happened."}],
                                       translate=False)).run(iteration)
        assert db.one("SELECT caveat FROM alerts")["caveat"] is None
