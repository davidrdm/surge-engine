"""A partly-collected family is degraded, not absent (9.11).

The PRICE sub-signal exists because Staying's calendar coverage is unreliable:
roughly one listing in fifteen returns calendar data, so the availability leg
regularly returns too few paired listings to score. Measured live in Houston —
availability paired fewer than 3 of 40 and was skipped, while `/price-compare`
paired 4 of 6 and produced signals.

The old rule then scored the whole LODGING family as a coverage gap, dropped
`data_completeness` to 0.75, and told the reader "lodging unavailable this
iteration" — about a family that had just been measured and had said there was
no pressure. A false negative dressed as caution, and it defeated the reason
the price signal was added.

Two things had to change, and they are separate:

  * `failed_sources` names the ENDPOINT, not just the family, so a reader can
    see WHICH leg failed.
  * `failed_families` — what drives completeness — counts only families with
    nothing collected at all.
"""
from __future__ import annotations

import pytest

from surge_iw.base.scoring import KIND_LODGING, correlate, lodging_quality
from conftest import AIRLIFT, ANCHOR, car, flight, lodging


def price_row(near, base, signal_id=1, **extra):
    """A lodging row from the PRICE leg: prices, no availability counts."""
    return {"signal_id": signal_id, "signal_type": "LODGING",
            "price_near": near, "price_baseline": base,
            "observed_at": ANCHOR.isoformat(), **extra}


def score(signals, cfg, **kw):
    return correlate(signals, track=AIRLIFT, anchor_at=ANCHOR, cfg=cfg, **kw)


class TestBothLegsCanCarryTheFamily:
    """The second requirement: coverage AND price must each be able to score."""

    def test_availability_alone_scores(self, corr_cfg):
        rows = [lodging(near_available=0, base_available=30, hours_ago=0.0)]
        assert lodging_quality(rows, corr_cfg) > 0.0

    def test_price_alone_scores(self, corr_cfg):
        """No availability counts at all — exactly what the price path writes
        when the calendar leg returned nothing usable."""
        rows = [price_row(near=300.0, base=200.0)]
        assert lodging_quality(rows, corr_cfg) > 0.0

    def test_the_stronger_of_the_two_carries_it(self, corr_cfg):
        """Not a mean. Averaging would let a family with no calendar coverage
        dilute a real price signal toward zero with a figure that is missing
        rather than reassuring."""
        weak_availability = lodging(near_available=28, base_available=30,
                                    hours_ago=0.0)
        strong_price = price_row(near=400.0, base=200.0, signal_id=2)
        both = lodging_quality([weak_availability, strong_price], corr_cfg)
        assert both == pytest.approx(
            max(lodging_quality([weak_availability], corr_cfg),
                lodging_quality([strong_price], corr_cfg)))

    def test_a_price_only_family_reaches_an_alert(self, corr_cfg):
        """End to end: the price leg alone contributes and, with a second
        family, clears the two-report floor."""
        result = score([price_row(near=400.0, base=200.0),
                        flight(category="M", fr24_id="m1", signal_id=2,
                               hours_ago=0.0)], corr_cfg)
        assert result.contributions.get(KIND_LODGING, 0.0) > 0.0
        assert result.is_alertable


class TestWhatFailedIsNamedPrecisely:
    def test_failed_sources_carries_the_endpoint(self, corr_cfg):
        result = score(
            [flight(fr24_id="a", signal_id=1, hours_ago=0.0)], corr_cfg,
            unreliable_source_types=["LODGING"],
            failed_endpoints={"LODGING": "/search"})
        assert result.failed_sources == ["LODGING:/search"]

    def test_an_unknown_endpoint_degrades_to_the_bare_type(self, corr_cfg):
        """An old row, or a refusal that never reached a query. Naming the type
        alone beats inventing an endpoint."""
        result = score(
            [flight(fr24_id="a", signal_id=1, hours_ago=0.0)], corr_cfg,
            unreliable_source_types=["CAR"])
        assert result.failed_sources == ["CAR"]


class TestOnlyAWhollyMissingFamilyIsAGap:
    def base(self, corr_cfg, **kw):
        return score([flight(fr24_id="a", signal_id=1, hours_ago=0.0)],
                     corr_cfg, **kw)

    def test_a_sibling_endpoint_saves_the_family(self, corr_cfg):
        """The live Houston case: availability failed, price succeeded."""
        result = self.base(
            corr_cfg,
            unreliable_source_types=["LODGING"],
            failed_endpoints={"LODGING": "/search"},
            collected_source_types=["LODGING_PRICE"])
        assert result.failed_sources == ["LODGING:/search"]
        assert result.failed_families == []
        assert result.data_completeness == 1.0
        assert "unavailable this iteration" not in (result.caveat() or "")
        assert "Partly degraded" in result.caveat()

    def test_both_legs_failing_is_still_a_gap(self, corr_cfg):
        """The property that must not weaken: a broken credential fails every
        endpoint in its family, and that is still absence."""
        result = self.base(
            corr_cfg,
            unreliable_source_types=["LODGING", "LODGING_PRICE"],
            failed_endpoints={"LODGING": "/search",
                              "LODGING_PRICE": "/price-compare"},
            collected_source_types=["FLIGHT_LIVE"])
        assert result.failed_families == ["LODGING"]
        assert result.data_completeness == 0.75
        assert "lodging unavailable this iteration" in result.caveat()

    def test_a_family_with_no_collection_at_all_is_a_gap(self, corr_cfg):
        result = self.base(corr_cfg, unreliable_source_types=["CAR"],
                           collected_source_types=["FLIGHT_LIVE"])
        assert result.failed_families == ["CAR"]

    def test_completeness_counts_families_not_endpoints(self, corr_cfg):
        """Two failed endpoints in one family is ONE gap, not two — the
        denominator is four families."""
        result = self.base(
            corr_cfg,
            unreliable_source_types=["LODGING", "LODGING_PRICE"],
            collected_source_types=[])
        assert result.data_completeness == 0.75


class TestCappingStaysStrict:
    def test_any_lost_endpoint_still_caps_high(self, corr_cfg):
        """9.11 made completeness more accurate. It must not make the top band
        easier to reach: a partial loss still caps HIGH."""
        rows = [flight(category="M", fr24_id=f"m{i}", signal_id=i,
                       hours_ago=0.0) for i in range(1, 4)]
        rows += [lodging(near_available=0, base_available=30, signal_id=9,
                         hours_ago=0.0),
                 car(near_available=0, base_available=20, signal_id=10,
                     hours_ago=0.0)]
        rows += [{"signal_id": 11, "signal_type": "SOCIAL",
                  "source_domain": "apnews.com", "platform": "news",
                  "url": "https://apnews.com/1", "salience": 1.0,
                  "observed_at": ANCHOR.isoformat()},
                 {"signal_id": 12, "signal_type": "SOCIAL",
                  "source_domain": "reuters.com", "platform": "news",
                  "url": "https://reuters.com/1", "salience": 1.0,
                  "observed_at": ANCHOR.isoformat()}]
        clean = score(rows, corr_cfg)
        assert clean.band == "HIGH", "fixture must reach HIGH before capping"

        capped = score(rows, corr_cfg,
                       unreliable_source_types=["LODGING"],
                       failed_endpoints={"LODGING": "/search"},
                       collected_source_types=["LODGING_PRICE"])
        assert capped.failed_families == [], "the family was still measured"
        assert capped.band_capped, "but a lost endpoint must still cap HIGH"
        assert capped.band == "MEDIUM"
        assert "LODGING:/search" in capped.rule_trace
