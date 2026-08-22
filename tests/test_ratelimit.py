"""Rate limiter pacing.

Tests inject a fake clock and sleep so the arithmetic can be proven without
spending wall-clock time. A test that actually waited 60 seconds to verify a
per-minute limit would simply not get written, and the limiter would go
unverified until it produced a 429 in production.
"""
from __future__ import annotations

import pytest

from surge_iw.config import load_config
from surge_iw.services.ratelimit import (
    DEFAULT_LIMITS,
    RateLimiter,
    TokenBucket,
    build_limiters,
)


class FakeClock:
    """Monotonic clock that only advances when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.slept)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


class TestTokenBucket:
    def test_burst_up_to_capacity_does_not_wait(self, clock):
        bucket = TokenBucket(5, 5, clock=clock.time, sleep=clock.sleep)
        for _ in range(5):
            assert bucket.acquire() == 0.0
        assert clock.total_slept == 0.0

    def test_exceeding_capacity_waits(self, clock):
        bucket = TokenBucket(5, 5, clock=clock.time, sleep=clock.sleep)
        for _ in range(5):
            bucket.acquire()
        waited = bucket.acquire()
        assert waited == pytest.approx(0.2)   # one token at 5/sec

    def test_tokens_refill_over_time(self, clock):
        bucket = TokenBucket(10, 10, clock=clock.time, sleep=clock.sleep)
        for _ in range(10):
            bucket.acquire()
        assert bucket.tokens == pytest.approx(0.0, abs=0.001)
        clock.now += 0.5
        assert bucket.tokens == pytest.approx(5.0, abs=0.001)

    def test_refill_is_capped_at_capacity(self, clock):
        bucket = TokenBucket(5, 5, clock=clock.time, sleep=clock.sleep)
        clock.now += 1000
        assert bucket.tokens == 5.0

    def test_acquiring_more_than_capacity_is_a_programming_error(self, clock):
        bucket = TokenBucket(5, 5, clock=clock.time, sleep=clock.sleep)
        with pytest.raises(ValueError):
            bucket.acquire(6)

    def test_zero_rate_is_rejected(self):
        with pytest.raises(ValueError):
            TokenBucket(0)


class TestRateLimiter:
    def test_a_twenty_query_burst_is_serialised_within_the_ceiling(self, clock):
        """The Phase 2 gate: three airports across five cities in one stage
        would otherwise trip FR24's per-minute limit."""
        limiter = RateLimiter(per_minute=30, clock=clock.time, sleep=clock.sleep)
        for _ in range(20):
            limiter.acquire()
        # 20 requests fit inside a 30/min burst allowance, so no waiting yet.
        assert clock.total_slept == 0.0
        # The next 20 must be paced: 10 more fit, then 10 wait 2s each.
        for _ in range(20):
            limiter.acquire()
        assert clock.total_slept == pytest.approx(20.0, abs=0.01)

    def test_sustained_rate_never_exceeds_the_limit(self, clock):
        """Over any window, requests issued must not outpace the stated rate."""
        limiter = RateLimiter(per_minute=30, clock=clock.time, sleep=clock.sleep)
        for _ in range(90):
            limiter.acquire()
        # 30 free from the initial burst, 60 paced at one per 2s.
        elapsed = clock.now
        assert elapsed == pytest.approx(120.0, abs=0.01)

    def test_both_buckets_must_be_satisfied(self, clock):
        """A provider stating both a per-second and a per-minute limit needs
        both enforced; the coarser one alone lets a burst breach the finer."""
        limiter = RateLimiter(per_second=2, per_minute=600,
                              clock=clock.time, sleep=clock.sleep)
        for _ in range(2):
            limiter.acquire()
        assert limiter.acquire() > 0.0   # per-second bucket binds

    def test_no_configured_limits_means_no_waiting(self, clock):
        limiter = RateLimiter(clock=clock.time, sleep=clock.sleep)
        assert limiter.unlimited
        for _ in range(1000):
            assert limiter.acquire() == 0.0


class TestBuildLimiters:
    def test_every_provider_gets_a_limiter(self):
        limiters = build_limiters(load_config(None))
        assert set(limiters) == {"FR24", "PRICELINE", "APIDIRECT", "STAYING"}
        assert not any(l.unlimited for l in limiters.values())

    def test_config_overrides_the_published_default(self, clock):
        config = load_config(None)
        config["flightradar"]["queries_per_minute"] = 90   # Advanced tier
        config["flightradar"]["burst"] = 90
        limiters = build_limiters(config, clock=clock.time, sleep=clock.sleep)
        for _ in range(90):
            limiters["FR24"].acquire()
        assert clock.total_slept == 0.0

    def test_fr24_paces_evenly_rather_than_bursting(self, clock):
        """FR24 returned 429 on the second of two calls 0.2s apart, so a
        burst allowance satisfies the documented limit and still trips the
        real one."""
        limiters = build_limiters(load_config(None),
                                  clock=clock.time, sleep=clock.sleep)
        fr24 = limiters["FR24"]
        assert fr24.acquire() == 0.0        # first is free
        assert fr24.acquire() > 0.0         # second must wait
        assert clock.total_slept == pytest.approx(6.0, abs=0.01)

    def test_defaults_match_the_published_tiers(self):
        # 10/min matches the observed 429 threshold on the live account
        # (Explorer tier), not the assumed Essential 30/min.
        assert DEFAULT_LIMITS["FR24"]["per_minute"] == 10.0
        # Measured burst ceiling of exactly one request.
        assert DEFAULT_LIMITS["FR24"]["burst"] == 1.0
        assert DEFAULT_LIMITS["PRICELINE"]["per_second"] == 5.0

    def test_limiter_delays_and_never_refuses(self, clock):
        """A limiter that refused would look like a data gap, and a data gap is
        scored as reduced coverage — misreporting local pacing as missing
        intelligence."""
        limiter = RateLimiter(per_second=1, clock=clock.time, sleep=clock.sleep)
        for _ in range(10):
            assert limiter.acquire() >= 0.0   # always returns, never raises
