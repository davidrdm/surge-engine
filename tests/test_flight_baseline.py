"""The non-military flight signal, measured against normal (9.10).

`flight_quality` was a saturating count of distinct airframes with a full scale
of 3, so three business jets at a quiet regional field and three at a major
general-aviation hub scored identically — and the hub scored three every day of
the year. Measured live: Atlanta returned 13, 13 and 14 distinct J-category
airframes on three different days, all pinned at 1.0. A constant offset that
says only that Atlanta has business-jet traffic.

That is the same defect the failed calibration attempt found from the other
side: the top-scoring metro's flight score traced to airport density rather
than to surge activity.

Owner decisions implemented here:

  * cold start   fall back to the absolute count and mark it un-baselined,
                 with the minimum sample count configurable
  * median       over a trailing window, excluding iterations that alerted at
                 MEDIUM or above
  * bucketing    NOT approved — no weekday or hour buckets
  * military     stays an absolute count; its baseline is ~0 and one transport
                 inbound is meaningful at a count of one
"""
from __future__ import annotations

import pytest

from surge_iw.base import scoring
from surge_iw.base.scoring import (KIND_FLIGHT_AMBIGUOUS, KIND_FLIGHT_J,
                                   KIND_FLIGHT_M, correlate, flight_excess,
                                   median)
from conftest import AIRLIFT, ANCHOR, flight


def score(signals, cfg, **kw):
    return correlate(signals, track=AIRLIFT, anchor_at=ANCHOR, cfg=cfg, **kw)


def jets(n, category="J", **kw):
    return [flight(category=category, fr24_id=f"{category}{i}", signal_id=i,
                   hours_ago=0.0, **kw) for i in range(1, n + 1)]


class TestTheMeasure:
    def test_normal_traffic_scores_nothing(self):
        """Atlanta's 13/13/14 against a baseline of 13. The whole point."""
        assert flight_excess(13, 13, 100.0) == 0.0
        assert flight_excess(14, 13, 100.0) == pytest.approx(0.077, abs=0.01)

    def test_a_real_surge_saturates(self):
        assert flight_excess(26, 13, 100.0) == 1.0

    def test_below_normal_is_not_negative_evidence(self):
        """A quiet day is not evidence against a gathering; it is just quiet."""
        assert flight_excess(5, 13, 100.0) == 0.0

    def test_it_is_proportional_so_airports_are_comparable(self):
        """Three extra jets at a hub is noise; at a quiet field it is a
        doubling. An absolute count cannot tell those apart, which is why the
        busy field scored maximum every day."""
        assert flight_excess(16, 13, 100.0) < flight_excess(6, 3, 100.0)

    def test_a_zero_baseline_yields_nothing_to_divide_by(self):
        assert flight_excess(5, 0, 100.0) == 0.0

    def test_the_median_resists_one_exceptional_day(self):
        """Median rather than mean, on the owner's decision: a mean lets one
        surge drag the notion of normal with it."""
        assert median([12, 13, 14, 13, 60]) == 13
        assert median([]) == 0.0
        assert median([10, 20]) == 15


class TestOnlyTheNonMilitaryKindsAreBaselined:
    def test_military_is_never_baselined(self, corr_cfg):
        """Its baseline at a civilian field is ~0, and one military transport
        inbound is meaningful at a count of one."""
        assert KIND_FLIGHT_M not in scoring.BASELINED_FLIGHT_KINDS
        with_baseline = score(jets(3, "M"), corr_cfg,
                              flight_baselines={KIND_FLIGHT_M: 99.0})
        without = score(jets(3, "M"), corr_cfg)
        assert with_baseline.contributions[KIND_FLIGHT_M] == \
            without.contributions[KIND_FLIGHT_M]

    def test_business_and_general_aviation_are(self, corr_cfg):
        assert KIND_FLIGHT_J in scoring.BASELINED_FLIGHT_KINDS
        normal = score(jets(13), corr_cfg,
                       flight_baselines={KIND_FLIGHT_J: 13.0})
        assert normal.contributions[KIND_FLIGHT_J] == 0.0

    def test_uncategorised_records_are_too(self, corr_cfg):
        """An uncategorised live-positions record is not military — it is the
        general-aviation background the baseline exists to subtract."""
        assert KIND_FLIGHT_AMBIGUOUS in scoring.BASELINED_FLIGHT_KINDS

    def test_t_and_h_share_the_business_jet_baseline(self, corr_cfg):
        """They score as one kind, so they must be baselined as one."""
        from surge_iw.base.scoring import scoring_kind
        for category in ("J", "T", "H"):
            assert scoring_kind({"signal_type": "FLIGHT",
                                 "flight_category": category,
                                 "category_confidence": "CONFIRMED"}) == \
                KIND_FLIGHT_J


class TestColdStart:
    def test_without_a_baseline_the_absolute_count_stands(self, corr_cfg):
        """Falling back is the owner's decision. The alternative — scoring
        nothing until a baseline exists — would blind the system for its first
        weeks in a new city."""
        result = score(jets(3), corr_cfg)
        assert result.contributions[KIND_FLIGHT_J] > 0.0

    def test_and_the_correlation_says_so(self, corr_cfg):
        result = score(jets(3), corr_cfg)
        assert result.flight_baseline[KIND_FLIGHT_J]["state"] == "UNBASELINED"
        assert result.flight_baseline[KIND_FLIGHT_J]["baseline"] is None
        assert result.flight_baseline[KIND_FLIGHT_J]["observed"] == 3
        assert "NOT baselined" in result.rule_trace

    def test_a_baselined_kind_records_what_it_was_measured_against(self,
                                                                   corr_cfg):
        result = score(jets(20), corr_cfg,
                       flight_baselines={KIND_FLIGHT_J: 13.0})
        entry = result.flight_baseline[KIND_FLIGHT_J]
        assert entry == {"state": "BASELINED", "baseline": 13.0, "observed": 20}
        assert "excess over a baseline of 13" in result.rule_trace

    def test_the_two_states_are_distinguishable(self, corr_cfg):
        """A reader must never have to guess whether a count was compared to
        anything — the same discipline as `category_confidence: AMBIGUOUS`."""
        cold = score(jets(20), corr_cfg).flight_baseline[KIND_FLIGHT_J]
        warm = score(jets(20), corr_cfg,
                     flight_baselines={KIND_FLIGHT_J: 13.0}
                     ).flight_baseline[KIND_FLIGHT_J]
        assert cold["state"] != warm["state"]


class TestTheSamples:
    def _history(self, db, session, city, counts, band=None):
        """Write `counts` J-airframes in one iteration, optionally alerting."""
        iteration = db.insert_iteration(session)
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration,
            source_type="FLIGHT_LIVE", city_id=city,
            endpoint="/api/live/flight-positions/full", params={},
            dedup_key=f"f{iteration}")
        db.complete_query(query, 0)
        for i in range(counts):
            db.insert_signal(
                iteration_id=iteration, signal_type="FLIGHT", city_id=city,
                fr24_id=f"j{iteration}-{i}", flight_category="J",
                category_confidence="CONFIRMED", observed_at=ANCHOR)
        if band:
            db.upsert_correlation(
                iteration_id=iteration, city_id=city, track=AIRLIFT.name,
                score=0.5, band=band, distinct_types=2, contributions={},
                data_completeness=1.0, failed_sources="", band_capped=False,
                rule_trace="t")
        return iteration

    @pytest.fixture
    def city(self, db, session):
        return db.insert_city(session, "Atlanta", canonical="atlanta")

    def test_prior_iterations_only(self, db, session, city):
        """The run being scored must not help set the normal it is compared
        against."""
        first = self._history(db, session, city, 10)
        current = self._history(db, session, city, 40)
        rows = db.flight_baseline_samples(
            city, before_iteration=current, since=ANCHOR.replace(year=2000))
        assert {r[0] for r in rows} == {first}

    def test_an_iteration_that_alerted_is_excluded(self, db, session, city):
        clean = self._history(db, session, city, 10)
        self._history(db, session, city, 40, band="MEDIUM")
        rows = db.flight_baseline_samples(
            city, before_iteration=9999, since=ANCHOR.replace(year=2000))
        assert {r[0] for r in rows} == {clean}

    def test_a_low_band_is_not_contamination(self, db, session, city):
        low = self._history(db, session, city, 10, band="LOW")
        rows = db.flight_baseline_samples(
            city, before_iteration=9999, since=ANCHOR.replace(year=2000))
        assert low in {r[0] for r in rows}

    def test_a_category_absent_that_iteration_counts_as_zero(self, db, session,
                                                             city):
        """Omitting it would bias the median upward — the direction that hides
        a surge."""
        self._history(db, session, city, 5)
        empty = self._history(db, session, city, 0)
        rows = db.flight_baseline_samples(
            city, before_iteration=9999, since=ANCHOR.replace(year=2000))
        assert (empty, "J", "CONFIRMED", 0) in rows

    def test_a_query_that_never_completed_is_not_a_sample(self, db, session,
                                                          city):
        iteration = db.insert_iteration(session)
        db.enqueue_query(
            session_id=session, iteration_id=iteration,
            source_type="FLIGHT_LIVE", city_id=city,
            endpoint="/api/live/flight-positions/full", params={},
            dedup_key="never")
        rows = db.flight_baseline_samples(
            city, before_iteration=9999, since=ANCHOR.replace(year=2000))
        assert iteration not in {r[0] for r in rows}


class TestItIsConfigurable:
    def test_the_three_settings_are_session_tunable(self, config):
        from surge_iw.services import tunables
        clean = tunables.validate({"correlation": {
            "flight_excess_full_scale": 50.0,
            "flight_baseline_min_samples": 5,
            "flight_baseline_window_days": 14}}, config)
        assert tunables.effective(config, clean)["correlation"][
            "flight_baseline_min_samples"] == 5

    def test_the_shipped_defaults_are_present(self, corr_cfg):
        assert corr_cfg["flight_excess_full_scale"] > 0
        assert corr_cfg["flight_baseline_min_samples"] >= 1
        assert corr_cfg["flight_baseline_window_days"] >= 1


class TestBothLengthsActuallyGate:
    """The owner asked for the baseline's length to be configurable, so these
    prove each setting CHANGES BEHAVIOUR rather than merely existing.

    A knob that is read but does not gate is the "control that reads as
    enforcement and is not" pattern this project has now found six times.
    """

    def history(self, db, session, city, counts, *, age_days=0):
        """One iteration holding `counts` J-airframes, optionally backdated."""
        from datetime import timedelta

        from surge_iw.db.database import iso, utcnow

        iteration = db.insert_iteration(session)
        query = db.enqueue_query(
            session_id=session, iteration_id=iteration,
            source_type="FLIGHT_LIVE", city_id=city,
            endpoint="/api/live/flight-positions/full", params={},
            dedup_key=f"f{iteration}")
        db.complete_query(query, 0)
        for i in range(counts):
            db.insert_signal(
                iteration_id=iteration, signal_type="FLIGHT", city_id=city,
                fr24_id=f"j{iteration}-{i}", flight_category="J",
                category_confidence="CONFIRMED", observed_at=ANCHOR)
        if age_days:
            db._exec("UPDATE iterations SET started_at = ? WHERE iteration_id = ?",
                     (iso(utcnow() - timedelta(days=age_days)), iteration))
        return iteration

    def baselines(self, db, session, city, cfg, **over):
        """Score as if a NEW iteration were running now.

        A real iteration row, because `_flight_baselines` logs against it — and
        because `before_iteration` is what keeps the run being scored out of
        its own baseline.
        """
        from surge_iw.agents.correlation import CorrelationAgent

        merged = {**cfg, **over}
        current = db.insert_iteration(session)
        agent = CorrelationAgent(db, {"correlation": merged})
        return agent._flight_baselines(current, city, merged)

    @pytest.fixture
    def city(self, db, session):
        return db.insert_city(session, "Atlanta", canonical="atlanta")

    def test_min_samples_gates_whether_a_baseline_forms(self, db, session,
                                                        city, corr_cfg):
        for _ in range(3):
            self.history(db, session, city, 10)
        cfg = {**corr_cfg, "flight_baseline_window_days": 3650}
        assert self.baselines(db, session, city, cfg, flight_baseline_min_samples=3)
        assert self.baselines(db, session, city, cfg, flight_baseline_min_samples=4) == {}

    def test_below_the_floor_the_absolute_count_stands(self, db, session, city,
                                                       corr_cfg):
        """The cold-start decision: fall back, and say so."""
        self.history(db, session, city, 10)
        cfg = {**corr_cfg, "flight_baseline_window_days": 3650,
               "flight_baseline_min_samples": 5}
        assert self.baselines(db, session, city, cfg) == {}
        result = score(jets(3), cfg)
        assert result.flight_baseline[KIND_FLIGHT_J]["state"] == "UNBASELINED"
        assert result.contributions[KIND_FLIGHT_J] > 0.0

    def test_window_days_excludes_older_samples(self, db, session, city,
                                                corr_cfg):
        """A sample beyond the window is not a sample."""
        self.history(db, session, city, 10, age_days=60)
        self.history(db, session, city, 10, age_days=60)
        cfg = {**corr_cfg, "flight_baseline_min_samples": 2}
        assert self.baselines(db, session, city, cfg, flight_baseline_window_days=30) == {}
        assert self.baselines(db, session, city, cfg, flight_baseline_window_days=90)

    def test_a_narrower_window_can_starve_a_baseline_that_a_wider_one_forms(
        self, db, session, city, corr_cfg
    ):
        self.history(db, session, city, 10, age_days=1)
        self.history(db, session, city, 12, age_days=20)
        cfg = {**corr_cfg, "flight_baseline_min_samples": 2}
        assert self.baselines(db, session, city, cfg, flight_baseline_window_days=5) == {}
        assert self.baselines(db, session, city, cfg,
                              flight_baseline_window_days=45)[KIND_FLIGHT_J] == 11

    def test_the_excess_scale_changes_what_counts_as_a_surge(self, corr_cfg):
        """The third length: how far above normal saturates the family."""
        strict = {**corr_cfg, "flight_excess_full_scale": 50.0}
        lax = {**corr_cfg, "flight_excess_full_scale": 200.0}
        baseline = {KIND_FLIGHT_J: 10.0}
        assert score(jets(15), strict, flight_baselines=baseline
                     ).contributions[KIND_FLIGHT_J] > \
            score(jets(15), lax, flight_baselines=baseline
                  ).contributions[KIND_FLIGHT_J]
