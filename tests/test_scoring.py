"""Correlation and confidence scoring — the engine's arithmetic.

These tests are about `correlate()`, not about any mission. They therefore use
two FIXTURE tracks with weights pinned in this file rather than whatever the
loaded pack happens to say. The numbers below are inputs to the arithmetic
under test; they are not a claim about what any mission's weights should be,
and a mission that changed its weights must not break this suite.

The pinned values are the ones the system was originally built with, so the
expectation table below still reproduces each clause of the confidence rules
the predecessor's prose prompt stated — which is the property that table exists
to hold, independent of which mission is loaded.

The second class covers the two safety properties that matter more than the
tuning: a connector failure must never read as absence of a signal, and a
flight category must never be assumed from a live record.
"""
from __future__ import annotations

import pytest

from conftest import (AIRLIFT, ANCHOR, CHARTER, TRACKS, car, flight,
                      lodging, social)
from surge_iw.base import scoring

_BY_NAME = TRACKS


def score(signals, cfg, track=AIRLIFT, unreliable=()):
    if isinstance(track, str):
        track = _BY_NAME[track]
    return scoring.correlate(
        signals, track=track, anchor_at=ANCHOR, cfg=cfg,
        unreliable_source_types=unreliable,
    )


class TestPromptRulesReproduced:
    """Each case restates a clause of the predecessor's prose confidence rules.

    Rule text, verbatim:
      high   — all four signals are present and consistent
      medium — two or more signals are present where 1 is social media or a M
               coded flight
      low    — only one signal is present that is either social media or M coded
               flight, OR two or more signals that include J coded flight and
               hotel occupancy spike near a key location
    """

    def test_all_four_signals_is_high(self, corr_cfg):
        result = score(
            [
                social(domain="a.com", signal_id=1),
                social(domain="b.org", signal_id=2),
                social(domain="c.net", signal_id=3),
                flight(category="M", fr24_id="m1", signal_id=4),
                flight(category="M", fr24_id="m2", signal_id=5),
                flight(category="M", fr24_id="m3", signal_id=6),
                lodging(near_available=2, base_available=30, signal_id=7),
                car(near_available=1, base_available=20, signal_id=8),
            ],
            corr_cfg,
        )
        assert result.band == "HIGH"
        assert result.distinct_types == 4
        assert result.score >= corr_cfg["band_high_min_score"]

    def test_social_plus_military_flight_is_medium(self, corr_cfg):
        result = score(
            [
                social(domain="a.com", signal_id=1),
                social(domain="b.org", signal_id=2),
                social(domain="c.net", signal_id=3),
                flight(category="M", fr24_id="m1", signal_id=4),
                flight(category="M", fr24_id="m2", signal_id=5),
                flight(category="M", fr24_id="m3", signal_id=6),
            ],
            corr_cfg,
        )
        assert result.band == "MEDIUM"
        assert result.distinct_types == 2

    def test_one_social_report_no_longer_alerts(self, corr_cfg):
        """**The prompt granted this LOW and the owner overrode it (9.8).**

        One report is a lead, not a warning, and an instrument that escalates on
        one escalates on a rumour. Recorded here as a deliberate departure from
        the transcribed rules rather than as a weight that drifted.
        """
        result = score([social(domain="a.com", signal_id=1)], corr_cfg)
        assert result.band == "NONE"
        assert not result.is_alertable
        assert result.distinct_types == 1
        assert "One report is a lead, not a warning" in result.rule_trace
        assert result.score > 0.0, "still scored and still recorded, just not alerted"

    def test_two_independent_reports_in_one_family_do_alert(self, corr_cfg):
        """Owner decision, second half: two outlets making two DISTINCT claims
        is two reports, and intelligence practice treats two independent
        sources as raising confidence rather than as one-and-a-bit."""
        result = score(
            [social(domain="apnews.com", signal_id=1),
             social(domain="reuters.com", signal_id=2)],
            corr_cfg,
        )
        assert result.band == "LOW"
        assert result.distinct_types == 1
        assert "2 independent reports across 1 signal family" in result.rule_trace

    def test_one_wire_story_in_three_mastheads_is_still_one_report(self,
                                                                   corr_cfg):
        """The gate takes the LOWER of publishers and claims, so breadth of
        republication cannot manufacture corroboration."""
        rows = [social(domain="apnews.com", signal_id=1),
                social(domain="nytimes.com", signal_id=2),
                social(domain="wsj.com", signal_id=3)]
        for row in rows:
            row["claim_key"] = "one-wire-story"
        result = score(rows, corr_cfg)
        assert result.band == "NONE"
        assert not result.is_alertable

    def test_several_military_airframes_alone_still_alert(self, corr_cfg):
        """Independence is counted in EVERY family, not only social.

        Three distinct aircraft inbound are three observations, and the owner
        decision was about a single report — not about single-family evidence.
        An earlier reading of it counted families instead of reports and
        refused this; measured against real data, that same reading refused a
        correlation resting on eighteen distinct airframes.
        """
        result = score(
            [
                flight(category="M", fr24_id="m1", signal_id=1),
                flight(category="M", fr24_id="m2", signal_id=2),
                flight(category="M", fr24_id="m3", signal_id=3),
            ],
            corr_cfg,
        )
        assert result.band == "LOW"
        assert result.distinct_types == 1
        assert "3 independent reports" in result.rule_trace

    def test_one_airframe_alone_does_not(self, corr_cfg):
        """The floor still binds. One aircraft is one report."""
        result = score([flight(category="M", fr24_id="m1", signal_id=1)],
                       corr_cfg)
        assert result.band == "NONE"

    def test_seeing_one_airframe_twice_is_one_report(self, corr_cfg):
        """The same aircraft in both the live and the historical query must not
        corroborate itself, exactly as republication must not."""
        result = score(
            [flight(category="M", fr24_id="m1", signal_id=1),
             flight(category="M", fr24_id="m1", signal_id=2)],
            corr_cfg,
        )
        assert result.band == "NONE"

    def test_a_lodging_drop_is_one_report_however_large(self, corr_cfg):
        """It is a single availability measurement over a set, taken at one
        moment — not one report per listing."""
        rows = [lodging(near_available=0, base_available=30, signal_id=i,
                        provider_ref=f"L{i}", hours_ago=0.0)
                for i in range(1, 9)]
        result = score(rows, corr_cfg)
        assert result.band == "NONE"
        assert "rests on 1 independent report" in result.rule_trace

    def test_a_military_flight_with_any_second_family_alerts(self, corr_cfg):
        """The path that remains open to flight evidence: correlate it."""
        result = score(
            [flight(category="M", fr24_id="m1", signal_id=1),
             lodging(near_available=0, base_available=30, signal_id=2)],
            corr_cfg,
        )
        assert result.is_alertable
        assert result.distinct_types == 2

    def test_business_jet_plus_lodging_spike_is_low_not_medium(self, corr_cfg):
        """The prompt's second LOW clause, and the sharpest test of the weights.

        Two signal families are present, which would satisfy MEDIUM's count
        requirement, but neither is social nor a military flight. The prompt says
        LOW, so the weights must not let this reach MEDIUM.
        """
        result = score(
            [
                flight(category="J", fr24_id="j1", signal_id=1),
                flight(category="J", fr24_id="j2", signal_id=2),
                flight(category="J", fr24_id="j3", signal_id=3),
                lodging(near_available=0, base_available=30, signal_id=4),
            ],
            corr_cfg,
        )
        assert result.band == "LOW"
        assert result.distinct_types == 2

    def test_lodging_and_car_alone_cannot_reach_medium(self, corr_cfg):
        """Booking scarcity has many innocent causes: a convention, a holiday.

        Without an anchor naming an actor or a verified aircraft category, two
        booking signals must not escalate.
        """
        result = score(
            [
                lodging(near_available=0, base_available=30, signal_id=1),
                car(near_available=0, base_available=20, signal_id=2),
            ],
            corr_cfg,
        )
        assert result.band == "LOW"

    def test_lone_booking_anomaly_does_not_alert(self, corr_cfg):
        """The prompt grants LOW to a lone social or M signal, not to a lone
        booking anomaly. Hotel availability collapses for conventions, holidays
        and home games; on its own it is not a warning."""
        # hours_ago=0 so this tests the BAND RULE rather than the band rule
        # times temporal decay (9.5). At the helper's default of one hour the
        # outcome is identical — NONE, not alertable — but the score slips from
        # 0.150 to 0.148 and the "below alerting threshold" refusal fires first.
        # Both refusals are correct; which one fires is not what this test is
        # about. Decay has its own suite in tests/test_decay.py.
        result = score(
            [lodging(near_available=0, base_available=30, signal_id=1,
                     hours_ago=0.0)], corr_cfg
        )
        assert result.band == "NONE"
        assert not result.is_alertable
        assert "rests on 1 independent report" in result.rule_trace

    def test_no_signals_is_none_and_not_alertable(self, corr_cfg):
        result = score([], corr_cfg)
        assert result.band == "NONE"
        assert not result.is_alertable
        assert result.score == 0.0


class TestFailureIsNotAbsence:
    """The most important property in the suite.

    A broken API key, an expired token, or an exhausted quota must never be
    scored as "nothing found". The old connectors returned [] on any
    exception, which made exactly that mistake.
    """

    def _four_signal_set(self):
        return [
            social(domain="a.com", signal_id=1),
            social(domain="b.org", signal_id=2),
            social(domain="c.net", signal_id=3),
            flight(category="M", fr24_id="m1", signal_id=4),
            flight(category="M", fr24_id="m2", signal_id=5),
            flight(category="M", fr24_id="m3", signal_id=6),
            lodging(near_available=2, base_available=30, signal_id=7),
            car(near_available=1, base_available=20, signal_id=8),
        ]

    def test_high_is_capped_to_medium_when_a_source_failed(self, corr_cfg):
        clean = score(self._four_signal_set(), corr_cfg)
        assert clean.band == "HIGH"

        degraded = score(self._four_signal_set(), corr_cfg, unreliable=("CAR",))
        assert degraded.band == "MEDIUM"
        assert degraded.band_capped is True
        assert degraded.data_completeness < 1.0
        assert "CAR" in degraded.failed_families
        # The score itself is unchanged; only the claim made about it is.
        assert degraded.score == clean.score

    def test_caveat_names_the_missing_source(self, corr_cfg):
        result = score(self._four_signal_set(), corr_cfg, unreliable=("LODGING",))
        caveat = result.caveat()
        assert caveat is not None
        assert "lodging" in caveat
        assert "not evidence of their absence" in caveat

    def test_no_caveat_when_collection_was_complete(self, corr_cfg):
        assert score(self._four_signal_set(), corr_cfg).caveat() is None

    @pytest.mark.parametrize(
        "source_type,family",
        [
            ("FLIGHT_COUNT", "FLIGHT"),
            ("FLIGHT_LIVE", "FLIGHT"),
            ("FLIGHT_HISTORY", "FLIGHT"),
            ("SOCIAL", "SOCIAL"),
            ("LODGING", "LODGING"),
            ("CAR", "CAR"),
        ],
    )
    def test_every_source_type_maps_to_a_family(self, corr_cfg, source_type, family):
        result = score([], corr_cfg, unreliable=(source_type,))
        assert result.failed_families == [family]

    def test_three_flight_source_types_failing_counts_as_one_gap(self, corr_cfg):
        """Otherwise completeness would drop below zero on flights alone."""
        result = score(
            [], corr_cfg,
            unreliable=("FLIGHT_COUNT", "FLIGHT_LIVE", "FLIGHT_HISTORY"),
        )
        assert result.failed_families == ["FLIGHT"]
        assert result.data_completeness == 0.75


class TestMilitaryCategoryIsNeverAssumed:
    """FR24's live flight-positions response has no `category` field.

    Verified field-by-field against its OpenAPI spec: FlightPositionsFull returns
    22 fields and category is not one of them. The old code labelled such records
    "M/J" and let confidence rules that hinge on M act on them.
    """

    def test_ambiguous_flight_scores_below_confirmed_military(self, corr_cfg):
        confirmed = score(
            [flight(category="M", confidence="CONFIRMED", signal_id=1)], corr_cfg
        )
        ambiguous = score(
            [flight(category="AMBIGUOUS", confidence="AMBIGUOUS", signal_id=1)],
            corr_cfg,
        )
        assert ambiguous.score < confirmed.score

    def test_ambiguous_flight_scores_at_business_jet_weight_on_airshow(self):
        """The lowest weight reachable by the query's filter, never the highest."""
        weights = AIRLIFT.weights
        assert scoring.ambiguous_flight_weight(AIRLIFT) == weights["flight_J"]
        assert scoring.ambiguous_flight_weight(AIRLIFT) < weights["flight_M"]

    def test_ambiguity_costs_nothing_on_the_concert_tour_track(self):
        """That track filters J,T,H — all of which score identically.

        The ambiguity is real but analytically irrelevant, so penalising it would
        be false precision.
        """
        weights = CHARTER.weights
        assert scoring.ambiguous_flight_weight(CHARTER) == weights["flight_J"]

    def test_ambiguous_plus_social_cannot_reach_medium_on_airshow(self, corr_cfg):
        """Because an unverifiable record must not stand in for military airlift."""
        # hours_ago=0 for the same reason as above: 0.40 is a statement about
        # the KIND WEIGHTS, and aging the evidence would fold a second effect
        # into a number that exists to pin the first.
        result = score(
            [
                social(domain="a.com", signal_id=1, hours_ago=0.0),
                social(domain="b.org", signal_id=2, hours_ago=0.0),
                social(domain="c.net", signal_id=3, hours_ago=0.0),
                flight(category="AMBIGUOUS", confidence="AMBIGUOUS",
                       fr24_id="x1", signal_id=4, hours_ago=0.0),
                flight(category="AMBIGUOUS", confidence="AMBIGUOUS",
                       fr24_id="x2", signal_id=5, hours_ago=0.0),
                flight(category="AMBIGUOUS", confidence="AMBIGUOUS",
                       fr24_id="x3", signal_id=6, hours_ago=0.0),
            ],
            corr_cfg,
        )
        assert result.band == "LOW"
        assert result.score == pytest.approx(0.40, abs=0.001)

    def test_missing_category_is_treated_as_ambiguous(self, corr_cfg):
        row = flight(signal_id=1)
        row["flight_category"] = None
        row["category_confidence"] = None
        assert scoring.scoring_kind(row) == scoring.KIND_FLIGHT_AMBIGUOUS


class TestTracks:
    def test_military_flight_scores_zero_on_the_concert_tour_track(self, corr_cfg):
        """A touring band does not fly C-17s."""
        signals = [
            flight(category="M", fr24_id="m1", track="CONCERT_TOUR",
                   signal_id=1),
        ]
        result = score(signals, corr_cfg, track="CONCERT_TOUR")
        assert result.contributions.get("flight_M") == 0.0
        assert result.band == "NONE"

    def test_charter_weighs_more_on_the_concert_tour_track(self, corr_cfg):
        signals = [
            flight(category="J", fr24_id=f"j{i}", signal_id=i) for i in range(1, 4)
        ]
        airshow = score(signals, corr_cfg, track="AIRSHOW")
        concert = score(signals, corr_cfg, track="CONCERT_TOUR")
        assert concert.score > airshow.score

    def test_track_specific_signals_are_excluded_from_the_other_track(self, corr_cfg):
        signals = [social(domain="a.com", track="AIRSHOW", signal_id=1)]
        assert score(signals, corr_cfg, track="AIRSHOW").score > 0
        assert score(signals, corr_cfg, track="CONCERT_TOUR").score == 0.0

    def test_an_unattributed_signal_counts_for_every_track(self, corr_cfg):
        """A post that does not say what kind of gathering it is, is still
        evidence."""
        signals = [social(domain="a.com", track="UNKNOWN", signal_id=1)]
        assert score(signals, corr_cfg, track="AIRSHOW").score > 0
        assert score(signals, corr_cfg, track="CONCERT_TOUR").score > 0

    def test_every_track_weight_set_sums_to_one(self):
        """Keeps the score interpretable as a 0..1 saturation of the evidence."""
        for track, weights in ((t.name, t.weights) for t in (AIRLIFT, CHARTER)):
            assert sum(weights.values()) == pytest.approx(1.0), track


class TestTemporalWindow:
    """Written against the CONFIGURED window rather than a literal 48, so the
    property survives a change to `correlation.window_hours` — which moved to
    168 after New York produced two CONFIRMED reports that scored nothing,
    being five days old against a 48-hour horizon."""

    def test_signal_outside_the_window_is_excluded(self, corr_cfg):
        beyond = float(corr_cfg["window_hours"]) + 1
        assert score([social(hours_ago=beyond, signal_id=1)], corr_cfg).score == 0.0

    def test_signal_inside_the_window_counts(self, corr_cfg):
        within = float(corr_cfg["window_hours"]) - 1
        assert score([social(hours_ago=within, signal_id=1)], corr_cfg).score > 0.0

    def test_the_window_is_the_configured_one(self, corr_cfg):
        """The boundary is a setting, and a test that hard-codes it silently
        stops testing the boundary the moment the setting moves."""
        w = float(corr_cfg["window_hours"])
        assert score([social(hours_ago=w - 1, signal_id=1)], corr_cfg).score > 0.0
        assert score([social(hours_ago=w + 1, signal_id=1)], corr_cfg).score == 0.0

    def test_undated_signal_is_excluded(self, corr_cfg):
        """An undated post cannot be placed against any warning window."""
        row = social(signal_id=1)
        row["observed_at"] = None
        assert score([row], corr_cfg).score == 0.0

    def test_window_is_symmetric_around_the_anchor(self, corr_cfg):
        """A flight ETA is in the future relative to the anchor."""
        assert score([social(hours_ago=-6, signal_id=1)], corr_cfg).score > 0.0


class TestSpatialAnchoring:
    def test_distant_booking_cluster_is_halved(self, corr_cfg):
        near = score([lodging(distance_km=3.0, signal_id=1)], corr_cfg)
        far = score([lodging(distance_km=99.0, signal_id=1)], corr_cfg)
        assert far.score == pytest.approx(near.score / 2, abs=0.001)

    def test_signal_without_a_distance_is_not_penalised(self, corr_cfg):
        """Social posts and flights attach to a city, not to a facility.

        Compared against an explicitly far row rather than a magic number, so the
        test states the property (no spatial penalty when no distance applies)
        rather than pinning a weight.
        """
        undated = social(signal_id=1)
        undated["distance_km"] = None
        far = social(signal_id=1)
        far["distance_km"] = 99.0
        assert score([undated], corr_cfg).score == \
            pytest.approx(score([far], corr_cfg).score * 2, abs=0.001)


class TestCarCapacityWeighting:
    """peopleCapacity is the most analytically valuable field Priceline returns.

    Moving people needs seats, so a collapse in twelve-seat vans is a stronger
    indicator than the same proportional collapse in economy sedans.
    """

    def test_high_capacity_scarcity_outweighs_low_capacity(self, corr_cfg):
        vans = scoring.car_drop(
            [car(people_capacity=12, near_available=0, base_available=10),
             car(people_capacity=4, near_available=10, base_available=10)],
            corr_cfg,
        )
        sedans = scoring.car_drop(
            [car(people_capacity=12, near_available=10, base_available=10),
             car(people_capacity=4, near_available=0, base_available=10)],
            corr_cfg,
        )
        assert vans > sedans

    def test_peer_to_peer_inventory_is_excluded(self, corr_cfg):
        """A private host going offline is not a demand signal."""
        drop = scoring.car_drop(
            [car(is_peer_to_peer=1, near_available=0, base_available=20)],
            corr_cfg,
        )
        assert drop == 0.0

    def test_truncated_response_is_excluded(self, corr_cfg):
        """A pagination cut is not scarcity."""
        drop = scoring.car_drop(
            [car(truncated=1, near_available=0, base_available=20)], corr_cfg
        )
        assert drop == 0.0

    def test_on_airport_counters_weigh_more(self, corr_cfg):
        on = scoring.car_drop(
            [car(is_on_airport=1, near_available=0, base_available=10),
             car(is_on_airport=0, near_available=10, base_available=10)],
            corr_cfg,
        )
        off = scoring.car_drop(
            [car(is_on_airport=1, near_available=10, base_available=10),
             car(is_on_airport=0, near_available=0, base_available=10)],
            corr_cfg,
        )
        assert on > off

    def test_zero_baseline_does_not_divide_by_zero(self, corr_cfg):
        assert scoring.car_drop(
            [car(near_available=0, base_available=0)], corr_cfg
        ) == 0.0

    def test_availability_increase_does_not_produce_a_negative_drop(self, corr_cfg):
        assert scoring.car_drop(
            [car(near_available=30, base_available=10)], corr_cfg
        ) == 0.0


class TestLodgingDelta:
    def test_drop_is_aggregated_over_the_listing_set(self):
        """Summed nights, not averaged percentages: a tiny listing going dark
        must not weigh as much as a large one."""
        rows = [
            lodging(near_available=0, base_available=1),
            lodging(near_available=9, base_available=9),
        ]
        assert scoring.lodging_drop(rows) == pytest.approx(10.0)

    def test_zero_baseline_is_safe(self):
        assert scoring.lodging_drop(
            [lodging(near_available=0, base_available=0)]
        ) == 0.0


class TestSocialQuality:
    def test_reposts_of_one_claim_count_once(self, corr_cfg):
        """Breadth is distinct domains, not post count."""
        same = [social(domain="a.com", signal_id=i) for i in range(1, 6)]
        spread = [
            social(domain="a.com", signal_id=1),
            social(domain="b.org", signal_id=2),
            social(domain="c.net", signal_id=3),
        ]
        assert scoring.social_quality(spread, corr_cfg) > \
            scoring.social_quality(same, corr_cfg)

    def test_peak_salience_is_used_not_mean(self, corr_cfg):
        """One specific credible post is the signal; averaging dilutes it."""
        rows = [
            social(domain="a.com", salience=1.0, signal_id=1),
            social(domain="b.org", salience=0.1, signal_id=2),
            social(domain="c.net", salience=0.1, signal_id=3),
        ]
        assert scoring.social_quality(rows, corr_cfg) == pytest.approx(1.0)


class TestEarliestEta:
    def test_earliest_inbound_eta_is_surfaced(self, corr_cfg):
        """The field the old pipeline collected and then dropped at parse time."""
        result = score(
            [
                flight(fr24_id="a", eta="2026-07-27T19:40:00Z", signal_id=1),
                flight(fr24_id="b", eta="2026-07-27T15:10:00Z", signal_id=2),
            ],
            corr_cfg,
        )
        assert result.earliest_eta == "2026-07-27T15:10:00Z"

    def test_landed_flights_do_not_contribute_an_eta(self, corr_cfg):
        result = score(
            [flight(status="landed", eta="2026-07-27T15:10:00Z", signal_id=1)],
            corr_cfg,
        )
        assert result.earliest_eta is None


class TestScoreBounds:
    def test_score_is_clamped_to_one(self, corr_cfg):
        signals = (
            [social(domain=f"{i}.com", signal_id=i) for i in range(1, 11)]
            + [flight(category="M", fr24_id=f"m{i}", signal_id=100 + i)
               for i in range(1, 11)]
            + [lodging(near_available=0, base_available=50, signal_id=200)]
            + [car(near_available=0, base_available=50, signal_id=300)]
        )
        result = score(signals, corr_cfg)
        assert result.score <= 1.0

    def test_contributions_sum_to_the_score(self, corr_cfg):
        result = score(
            [
                social(domain="a.com", signal_id=1),
                flight(category="M", fr24_id="m1", signal_id=2),
                lodging(signal_id=3),
            ],
            corr_cfg,
        )
        assert sum(result.contributions.values()) == pytest.approx(
            result.score, abs=0.0001
        )

    def test_rule_trace_is_always_populated(self, corr_cfg):
        """Every band assignment states which rule fired, for the audit trail."""
        all_four = [
            social(domain="a.com", signal_id=1),
            social(domain="b.org", signal_id=2),
            social(domain="c.net", signal_id=3),
            flight(category="M", fr24_id="m1", signal_id=4),
            flight(category="M", fr24_id="m2", signal_id=5),
            flight(category="M", fr24_id="m3", signal_id=6),
            lodging(near_available=0, base_available=30, signal_id=7),
            car(near_available=0, base_available=20, signal_id=8),
        ]
        for signals in ([], [social(signal_id=1)], all_four):
            result = score(signals, corr_cfg)
            assert result.rule_trace
            assert f"{result.score:.2f}" in result.rule_trace
