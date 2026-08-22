"""Temporal confidence decay, scaled to the correlation window (9.5, issue #2).

Evidence inside the window used to count at full weight and evidence outside at
nothing — a step function, not a decay. Measured live: an iteration over a city
with 125 stored signals correlated zero pairs because the newest was 74.2 hours
old against a 72-hour window. Two hours past the line, evidence that had been
collected, paid for and judged counted for exactly nothing.

**The curve is a function of the window, and the tests below are mostly about
that.** The window already states how far back evidence is relevant. A second,
independent decay parameter could contradict it, and a curve chosen on its own
would silently re-narrow a window that had been widened deliberately — which is
what the widening from 48 to 168 hours was for, after two corroborated reports
scored nothing at five days old.

    weight(age) = decay_edge_weight ** (age / window_hours)

One setting therefore expresses both postures. A 48-hour window is a claim that
the event is imminent, so a day-old booking surge is weak evidence for it. A
168-hour window is a claim about something already forming, so the same surge is
most of the picture.
"""
from __future__ import annotations

import math

import pytest

from surge_iw.base import scoring
from surge_iw.base.scoring import correlate, decay_weight
from surge_iw.services import provenance
from conftest import AIRLIFT, ANCHOR, flight, lodging, social, track_model


def cfg(base, **over):
    return {**base, **over}


def score(signals, config, track=AIRLIFT):
    return correlate(signals, track=track_model(track), anchor_at=ANCHOR, cfg=config)


class TestTheCurveFollowsTheWindow:
    """The requirement, stated as arithmetic."""

    def test_a_short_window_decays_steeply_and_a_long_one_shallowly(self):
        """Same evidence, same age, two postures. This is the whole point: an
        operator sets the window and the curve follows."""
        tactical = decay_weight(24, window_hours=48, edge_weight=0.1)
        situational = decay_weight(24, window_hours=168, edge_weight=0.1)
        assert tactical == pytest.approx(0.316, abs=0.001)
        assert situational == pytest.approx(0.720, abs=0.001)
        assert tactical < situational

    def test_fresh_evidence_is_never_discounted(self):
        for window in (48, 72, 168, 720):
            assert decay_weight(0, window, 0.1) == 1.0

    def test_the_edge_weight_is_exactly_what_it_says(self):
        for window in (48, 168):
            assert decay_weight(window, window, 0.1) == pytest.approx(0.1)

    def test_the_half_life_is_a_constant_fraction_of_the_window(self):
        """Self-similarity is what makes one setting express both postures."""
        expected = math.log(0.5) / math.log(0.1)          # 0.301
        for window in (48, 72, 168):
            half_life = window * expected
            assert decay_weight(half_life, window, 0.1) == pytest.approx(
                0.5, abs=0.001)

    def test_it_never_increases_with_age(self):
        previous = 1.0
        for age in range(0, 169, 3):
            weight = decay_weight(age, 168, 0.1)
            assert weight <= previous + 1e-12, age
            previous = weight

    def test_an_edge_weight_of_one_restores_the_step_function(self):
        """No separate on/off flag: a boolean and a value that can disagree is
        one more control that reads as enforcement and is not."""
        for age in (0, 12, 47.9, 48):
            assert decay_weight(age, 48, edge_weight=1.0) == 1.0

    def test_a_signal_stamped_after_the_anchor_is_not_worth_more_than_fresh(
        self,
    ):
        """`in_window` is symmetric and lodging rows are stamped at collection
        time, which is minutes AFTER the anchor. A signed age would hand them a
        weight above 1."""
        assert decay_weight(-6, 48, 0.1) == decay_weight(6, 48, 0.1)
        assert decay_weight(-6, 48, 0.1) <= 1.0

    def test_a_degenerate_edge_weight_is_clamped_not_obeyed(self):
        """At zero the curve is a step function in disguise, and `0 ** 0` would
        be the only surviving weight."""
        assert 0.0 < decay_weight(24, 48, edge_weight=0.0) < 1.0

    def test_beyond_the_window_it_holds_at_the_edge_rather_than_going_negative(
        self,
    ):
        """The window is still the hard bound — `in_window` excludes these — so
        this only has to be sane, not meaningful."""
        assert decay_weight(500, 48, 0.1) == pytest.approx(0.1)


class TestWhatItDoesToAScore:
    def test_the_same_evidence_scores_lower_as_it_ages(self, corr_cfg):
        config = cfg(corr_cfg, window_hours=48, decay_edge_weight=0.1)
        fresh = score([flight(hours_ago=1, signal_id=1)], config).score
        stale = score([flight(hours_ago=36, signal_id=1)], config).score
        assert fresh > stale > 0.0

    def test_a_narrow_window_punishes_age_harder_than_a_wide_one(self,
                                                                 corr_cfg):
        """The requirement, observed through a score rather than the curve."""
        day_old = [flight(hours_ago=24, signal_id=1)]
        tactical = score(day_old, cfg(corr_cfg, window_hours=48,
                                      decay_edge_weight=0.1)).score
        situational = score(day_old, cfg(corr_cfg, window_hours=168,
                                         decay_edge_weight=0.1)).score
        assert tactical < situational

    def test_turning_decay_off_reproduces_the_pre_9_5_number(self, corr_cfg):
        """A regression net for every score recorded before 9.5.

        Pinned against an explicit expected value rather than against the
        decayed run, so this fails if the weight model itself moves — which is
        the thing an operator comparing an old alert to a new one needs to be
        able to rule out.
        """
        rows = [social(domain="a.com", signal_id=1, hours_ago=30),
                flight(hours_ago=20, signal_id=2),
                lodging(near_available=2, base_available=30, signal_id=3,
                        hours_ago=10)]
        off = score(rows, cfg(corr_cfg, decay_edge_weight=1.0))
        on = score(rows, cfg(corr_cfg, decay_edge_weight=0.1))

        assert off.score == pytest.approx(0.5400, abs=0.0001)
        assert on.score < off.score, "aged evidence must score lower"
        assert "aged on a" not in off.rule_trace
        assert "aged on a" in on.rule_trace
        assert all(scoring.decay_weight(age, 168, 1.0) == 1.0
                   for age in (0, 10, 20, 30, 167))

    def test_the_trace_records_the_curve_that_was_applied(self, corr_cfg):
        """A reader comparing two alerts scored under different windows needs
        to see that the older evidence was weighted differently."""
        result = score([flight(hours_ago=6, signal_id=1)],
                       cfg(corr_cfg, window_hours=48, decay_edge_weight=0.1))
        assert "aged on a 48-hour window" in result.rule_trace
        assert "half-life 14.4h" in result.rule_trace
        assert "0.1 at the edge" in result.rule_trace

    def test_evidence_at_the_edge_still_counts_for_something(self, corr_cfg):
        """Decay does not remove the cutoff — the window still bounds the
        query — it shrinks the cliff from 1.0 to the edge weight. That is the
        answer to the live case where 74.2 hours against a 72-hour window
        scored nothing."""
        config = cfg(corr_cfg, window_hours=72, decay_edge_weight=0.1)
        assert score([flight(hours_ago=71, signal_id=1)], config).score > 0.0

    def test_a_borderline_score_can_cross_a_threshold_purely_on_age(self,
                                                                    corr_cfg):
        """Stated rather than hidden. A lone booking anomaly sits exactly on
        the alerting floor, so at any age at all it now falls under it — which
        is the intended consequence and the reason this default is a decision
        rather than a tweak."""
        config = cfg(corr_cfg, decay_edge_weight=0.1)
        rows = [lodging(near_available=0, base_available=30, signal_id=1,
                        hours_ago=1)]
        assert score(rows, config).score < score(
            rows, cfg(config, decay_edge_weight=1.0)).score


class TestCountsVersusMeasurements:
    """Two rules, because the two quantities are different kinds of thing.

    Flight and social quality are counts of independent observations, so the
    COUNT decays: three aircraft that landed four days ago are not three
    aircraft now. Lodging and car quality are one ratio measured at a moment,
    so the MEASUREMENT decays by its own age: half a measurement is not a
    smaller drop, it is an older one.
    """

    def test_an_aged_airframe_counts_as_a_fraction_of_an_airframe(self,
                                                                   corr_cfg):
        config = cfg(corr_cfg, window_hours=48, decay_edge_weight=0.1)
        fresh = scoring.flight_quality(
            [{**flight(fr24_id="a", hours_ago=0), scoring.DECAY_KEY: 1.0}],
            config)
        aged = scoring.flight_quality(
            [{**flight(fr24_id="a", hours_ago=24), scoring.DECAY_KEY: 0.316}],
            config)
        assert aged == pytest.approx(fresh * 0.316, abs=0.01)

    def test_seeing_one_airframe_twice_is_still_one_airframe(self, corr_cfg):
        """Decay must not let repetition accumulate weight, or an aircraft
        seen by both the live and the historical query would count twice."""
        config = cfg(corr_cfg, decay_edge_weight=0.1)
        once = scoring.flight_quality(
            [{**flight(fr24_id="a"), scoring.DECAY_KEY: 1.0}], config)
        twice = scoring.flight_quality(
            [{**flight(fr24_id="a"), scoring.DECAY_KEY: 1.0},
             {**flight(fr24_id="a"), scoring.DECAY_KEY: 0.5}], config)
        assert once == twice

    def test_the_single_source_floor_does_not_rescue_decayed_evidence(self,
                                                                      corr_cfg):
        """The floor exists so ONE credible source is not a third of a signal.
        It must not also restore a signal the curve has discounted to a tenth."""
        assert scoring.corroboration_quality(1.0, 3.0, 0.6) == pytest.approx(0.6)
        assert scoring.corroboration_quality(0.1, 3.0, 0.6) == pytest.approx(0.06)
        assert scoring.corroboration_quality(0.0, 3.0, 0.6) == 0.0

    def test_the_floor_is_continuous_where_the_two_rules_meet(self):
        below = scoring.corroboration_quality(0.999, 3.0, 0.6)
        at = scoring.corroboration_quality(1.0, 3.0, 0.6)
        assert below == pytest.approx(at, abs=0.001)

    def test_a_stale_measurement_is_an_older_drop_not_a_smaller_one(self,
                                                                    corr_cfg):
        """The lodging ratio itself is untouched; only its weight moves."""
        rows = [lodging(near_available=0, base_available=30)]
        assert scoring.lodging_drop(rows) == pytest.approx(100.0)
        aged = [{**rows[0], scoring.DECAY_KEY: 0.25}]
        assert scoring.lodging_drop(aged) == pytest.approx(100.0)
        assert scoring.lodging_quality(aged, corr_cfg) == pytest.approx(
            scoring.lodging_quality(
                [{**rows[0], scoring.DECAY_KEY: 1.0}], corr_cfg) * 0.25)

    def test_a_mixed_measurement_decays_in_proportion_to_what_is_stale(self,
                                                                       corr_cfg):
        """Only reachable when the window spans two iterations. The larger half
        of the measurement should carry the larger share of the weight."""
        mostly_fresh = [
            {**lodging(near_available=0, base_available=90),
             scoring.DECAY_KEY: 1.0},
            {**lodging(near_available=0, base_available=10, provider_ref="L2"),
             scoring.DECAY_KEY: 0.0},
        ]
        weight = scoring.measurement_decay(
            mostly_fresh, lambda r: float(r.get("base_available") or 0.0))
        assert weight == pytest.approx(0.9)

    def test_a_group_with_no_denominator_falls_back_to_the_freshest(self,
                                                                     corr_cfg):
        rows = [{**lodging(near_available=0, base_available=0),
                 scoring.DECAY_KEY: 0.3}]
        assert scoring.measurement_decay(
            rows, lambda r: float(r.get("base_available") or 0.0)) == 0.3


class TestCorroborationIsNotDoubleCounted:
    def test_weighted_corroboration_matches_the_plain_count_at_full_weight(
        self,
    ):
        """The two share a definition of "independent" and must not drift."""
        rows = [social(domain="apnews.com", signal_id=1),
                social(domain="reuters.com", signal_id=2),
                social(domain="apnews.com", signal_id=3)]
        assert provenance.corroboration_weighted(rows, lambda r: 1.0) == \
            tuple(float(v) for v in provenance.corroboration(rows))

    def test_a_publisher_counts_at_its_freshest_story(self):
        """Summing would let one outlet running the same claim eight times read
        as corroboration — the confound `claim_key` exists to prevent."""
        rows = [{**social(domain="apnews.com", signal_id=1),
                 scoring.DECAY_KEY: 0.2},
                {**social(domain="apnews.com", signal_id=2),
                 scoring.DECAY_KEY: 0.9}]
        publishers, _claims = provenance.corroboration_weighted(
            rows, scoring.decay_of)
        assert publishers == pytest.approx(0.9)

    def test_salience_itself_does_not_decay(self, corr_cfg):
        """Decay measures how much an observation still counts as EVIDENCE.
        Salience is a property of what the post says — a four-day-old report is
        exactly as specific as it was written, only less current, and currency
        is what the breadth term already carries.

        Applying it to both was the first implementation, and because quality
        is their product it made social decay as the SQUARE of the weight while
        every other family decayed linearly. See the invariant below.
        """
        config = cfg(corr_cfg, decay_edge_weight=0.1)
        strident_and_stale = [{**social(domain="a.com", salience=1.0,
                                        signal_id=1), scoring.DECAY_KEY: 0.1}]
        publishers, claims = provenance.corroboration_weighted(
            strident_and_stale, scoring.decay_of)
        breadth = scoring.corroboration_quality(
            min(publishers, claims), float(config["social_domains_full_scale"]),
            float(config["single_source_quality"]))
        assert scoring.social_quality(strident_and_stale, config) == \
            pytest.approx(breadth * 1.0)


class TestEveryFamilyAgesAtTheSameRate:
    """The invariant that would have caught the defect above.

    Decay says "this observation is worth W of a fresh one". A family's quality
    must therefore scale BY W — not by W squared because the family's formula
    happens to multiply two decayed terms together, and not by 1 because a
    family was missed. Without this, social was penalised for age roughly eight
    times harder than flight at the same weight, and the family that most often
    stands alone was the one being punished.
    """

    def rows_for(self, family, weight):
        if family == "SOCIAL":
            return [{**social(domain="a.com", salience=0.7, signal_id=1),
                     scoring.DECAY_KEY: weight}]
        if family == "FLIGHT":
            return [{**flight(fr24_id="a", signal_id=1),
                     scoring.DECAY_KEY: weight}]
        if family == "LODGING":
            return [{**lodging(near_available=0, base_available=30,
                               signal_id=1), scoring.DECAY_KEY: weight}]
        return [{"signal_type": "CAR", "signal_id": 1, "base_available": 10,
                 "near_available": 0, "people_capacity": 5,
                 "vehicle_class": "ECAR", scoring.DECAY_KEY: weight}]

    def quality(self, family, weight, config):
        rows = self.rows_for(family, weight)
        kind = {"SOCIAL": scoring.KIND_SOCIAL, "FLIGHT": scoring.KIND_FLIGHT_M,
                "LODGING": scoring.KIND_LODGING, "CAR": scoring.KIND_CAR}[family]
        return scoring.kind_quality(kind, rows, config)

    @pytest.mark.parametrize("family", ["SOCIAL", "FLIGHT", "LODGING", "CAR"])
    @pytest.mark.parametrize("weight", [1.0, 0.5, 0.25, 0.12])
    def test_quality_scales_linearly_with_the_weight(self, corr_cfg, family,
                                                     weight):
        full = self.quality(family, 1.0, corr_cfg)
        assert full > 0, f"{family} fixture produces no quality to scale"
        assert self.quality(family, weight, corr_cfg) == pytest.approx(
            full * weight, rel=0.01), (
            f"{family} does not age at the same rate as the others")

    def test_no_family_is_exempt(self, corr_cfg):
        """A family that ignored decay entirely would pass every test above
        that only checks its own internals."""
        for family in ("SOCIAL", "FLIGHT", "LODGING", "CAR"):
            assert self.quality(family, 0.5, corr_cfg) < \
                self.quality(family, 1.0, corr_cfg), family


class TestTheEvidenceTrailReflectsIt:
    def test_a_fresh_signal_outranks_a_stale_one_in_the_drill_down(self,
                                                                   corr_cfg):
        """An even split would have ranked a five-day-old post level with one
        from this morning while the score already knew better."""
        config = cfg(corr_cfg, window_hours=168, decay_edge_weight=0.1)
        result = score([flight(fr24_id="fresh", hours_ago=1, signal_id=1),
                        flight(fr24_id="stale", hours_ago=120, signal_id=2)],
                       config)
        assert result.signal_contributions[1] > result.signal_contributions[2]

    def test_the_shares_still_sum_to_the_contribution(self, corr_cfg):
        config = cfg(corr_cfg, window_hours=168, decay_edge_weight=0.1)
        result = score([flight(fr24_id="a", hours_ago=1, signal_id=1),
                        flight(fr24_id="b", hours_ago=100, signal_id=2)],
                       config)
        assert sum(result.signal_contributions.values()) == pytest.approx(
            sum(result.contributions.values()), abs=0.001)

    def test_scoring_does_not_write_the_weight_onto_the_caller_s_rows(self,
                                                                      corr_cfg):
        """These rows belong to CorrelationAgent, and a scoring pass must not
        leave anything behind on them."""
        rows = [flight(hours_ago=5, signal_id=1)]
        score(rows, cfg(corr_cfg, decay_edge_weight=0.1))
        assert scoring.DECAY_KEY not in rows[0]


class TestItGovernsARealRun:
    def test_the_session_tunable_reaches_the_curve(self, db, config):
        """`correlation.decay_edge_weight` is analytical, so a session may set
        it — and a session asking for a sharper instrument must get one."""
        from surge_iw.services import tunables

        clean = tunables.validate(
            {"correlation": {"decay_edge_weight": 0.02, "window_hours": 48}},
            config)
        effective = tunables.effective(config, clean)
        assert effective["correlation"]["decay_edge_weight"] == 0.02
        assert decay_weight(24, 48, 0.02) < decay_weight(24, 48, 0.1)

    def test_an_out_of_range_edge_weight_is_refused(self, config):
        from surge_iw.services import tunables

        with pytest.raises(tunables.TunableError, match="decay_edge_weight"):
            tunables.validate(
                {"correlation": {"decay_edge_weight": 0.0}}, config)
