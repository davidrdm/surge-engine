"""Phase 7.5: candidate versus signal, and what may spend money.

The gate this file defends is the one the review asked for explicitly. Before
it, a single accepted post — with a *missing* salience and no usable timestamp —
created a scoring signal for a seeded city and booked the full paid follow-on
set, and that signal was then excluded from correlation by the window check.
Money spent on evidence that could not score.

The thresholds asserted here are INTERIM. The owner's decision was to build the
distinction and the measurement first and set the floors from the adversarial
matrix afterwards. What these tests pin is the *shape* — that the two bars exist
and that the tipping one is higher — not the numbers.
"""
from __future__ import annotations

from datetime import timedelta

from conftest import ANCHOR
from surge_iw.agents.triage_schema import TriageItem
from surge_iw.db.database import iso
from surge_iw.services import sensitivity


def item(**overrides):
    base = {
        "item_id": "iX", "relevant": True, "track": "AIRSHOW",
        "cities": ["Phoenix"], "locations": [], "activity_type": "static display",
        "imminence_hours": 6.0, "salience": 0.9,
        "rationale": "names a place, a time and an actor",
    }
    base.update(overrides)
    return TriageItem.model_validate(base)


class TestClassification:
    def test_a_strong_recent_post_is_confirmed(self, config):
        state, _reason = sensitivity.classify(
            item(), iso(ANCHOR), config, now=ANCHOR)
        assert state == "CONFIRMED"

    def test_a_weak_post_is_recorded_but_does_not_score(self, config):
        state, reason = sensitivity.classify(
            item(salience=0.1), iso(ANCHOR), config, now=ANCHOR)
        assert state == "CANDIDATE"
        assert "below the confirmation floor" in reason

    def test_an_undated_post_cannot_be_placed_in_the_window(self, config):
        state, reason = sensitivity.classify(item(), None, config, now=ANCHOR)
        assert state == "CANDIDATE"
        assert "correlation window" in reason

    def test_an_unparseable_date_is_not_a_date(self, config):
        state, _reason = sensitivity.classify(
            item(), "sometime last week", config, now=ANCHOR)
        assert state == "CANDIDATE"

    def test_a_post_dated_in_the_future_is_not_prescient(self, config):
        state, reason = sensitivity.classify(
            item(), iso(ANCHOR + timedelta(days=3)), config, now=ANCHOR)
        assert state == "CANDIDATE"
        assert "future" in reason

    def test_the_reason_is_always_recorded(self, config):
        for observed in (iso(ANCHOR), None):
            _state, reason = sensitivity.classify(
                item(), observed, config, now=ANCHOR)
            assert reason, "an operator reading the evidence needs the why"


class TestTipping:
    def signal(self, **overrides):
        base = {
            "signal_state": "CONFIRMED", "salience": 0.9,
            "observed_at": iso(ANCHOR - timedelta(hours=2)),
        }
        base.update(overrides)
        return base

    def test_a_confirmed_recent_strong_signal_may_tip(self, config):
        assert sensitivity.may_tip(self.signal(), config, now=ANCHOR).allowed

    def test_a_candidate_may_never_tip(self, config):
        decision = sensitivity.may_tip(
            self.signal(signal_state="CANDIDATE"), config, now=ANCHOR)
        assert not decision.allowed
        assert "candidate" in decision.reason

    def test_the_tipping_bar_is_higher_than_the_scoring_bar(self, config):
        """A query is a purchase. A weak signal that scores produces a LOW
        alert a reader can dismiss; a weak signal that tips spends FR24
        credits that bill per record returned."""
        cfg = sensitivity.settings(config)
        assert cfg["tip_min_salience"] > cfg["confirm_min_salience"]

        middling = (cfg["confirm_min_salience"] + cfg["tip_min_salience"]) / 2
        state, _ = sensitivity.classify(
            item(salience=middling), iso(ANCHOR), config, now=ANCHOR)
        assert state == "CONFIRMED"
        assert not sensitivity.may_tip(
            self.signal(salience=middling), config, now=ANCHOR).allowed

    def test_an_undated_signal_may_not_buy_collection(self, config):
        """It would be excluded from correlation by the window check, so the
        money would buy evidence that cannot score."""
        decision = sensitivity.may_tip(
            self.signal(observed_at=None), config, now=ANCHOR)
        assert not decision.allowed
        assert "undated" in decision.reason or "usable observation" in decision.reason

    def test_a_stale_signal_may_not_buy_collection(self, config):
        decision = sensitivity.may_tip(
            self.signal(observed_at=iso(ANCHOR - timedelta(days=9))),
            config, now=ANCHOR)
        assert not decision.allowed
        assert "beyond" in decision.reason

    def test_a_missing_salience_is_zero_and_refused(self, config):
        """`_clamp` returned None, `or 0.0` made it zero, and `0.0 < 0.0` is
        false — so a post with NO salience passed the old gate."""
        assert not sensitivity.may_tip(
            self.signal(salience=None), config, now=ANCHOR).allowed

    def test_every_refusal_carries_a_reason(self, config):
        for override in ({"signal_state": "CANDIDATE"}, {"salience": 0.0},
                         {"observed_at": None}):
            decision = sensitivity.may_tip(
                self.signal(**override), config, now=ANCHOR)
            assert not decision.allowed and decision.reason


class TestConfigurable:
    def test_the_floors_are_config_not_constants(self, config):
        """They are interim values awaiting the adversarial measurements, so
        they must be movable without a code change."""
        config["sensitivity"] = {"confirm_min_salience": 0.9}
        state, _ = sensitivity.classify(
            item(salience=0.5), iso(ANCHOR), config, now=ANCHOR)
        assert state == "CANDIDATE"

    def test_the_timestamp_requirement_can_be_relaxed_deliberately(self, config):
        config["sensitivity"] = {"tip_require_timestamp": False}
        signal = {"signal_state": "CONFIRMED", "salience": 0.9,
                  "observed_at": None}
        assert sensitivity.may_tip(signal, config, now=ANCHOR).allowed
