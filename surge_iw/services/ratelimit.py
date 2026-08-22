"""Per-provider rate limiting.

On paid plans, rate limits bind long before quotas do. FR24 allows 30
queries/minute on Essential and Priceline 3-10 requests/second depending on
tier; three airports across five cities in one collection stage will trip both.

A rate limiter DELAYS; a budget REFUSES. Conflating them would turn a throttle
into a data gap, and a data gap is scored as reduced coverage — which would
misreport a purely local pacing problem as missing intelligence. So nothing here
ever raises or returns "denied": acquire() blocks until a token is available.

Two buckets per provider because providers publish two different limits.
Priceline states requests per second; FR24 states queries per minute. Enforcing
only the coarser of the two lets a burst breach the finer one.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Mapping


class TokenBucket:
    """Classic token bucket. Thread-safe, monotonic, and injectable for tests.

    `sleep` and `clock` are parameters so tests can prove the pacing arithmetic
    without spending wall-clock time — a test that actually waited 60 seconds to
    verify a per-minute limit would simply not be written.
    """

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else rate)
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until `tokens` are available. Returns seconds spent waiting.

        The wait is computed and slept while holding the lock, which serialises
        callers. That is intentional: it makes the pacing deterministic and
        keeps a thundering herd from all computing the same short wait and then
        firing simultaneously.
        """
        if tokens > self.capacity:
            raise ValueError(
                f"cannot acquire {tokens} tokens from a bucket of capacity "
                f"{self.capacity}"
            )
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0
            deficit = tokens - self._tokens
            wait = deficit / self.rate
            self._sleep(wait)
            self._refill()
            # Deduct even if refill undershot by a rounding hair; the bucket is
            # allowed to go marginally negative rather than spin.
            self._tokens = max(0.0, self._tokens - tokens)
            return wait

    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens


class RateLimiter:
    """A per-second and a per-minute bucket, both of which must be satisfied."""

    def __init__(
        self,
        *,
        per_second: float | None = None,
        per_minute: float | None = None,
        burst: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """`burst` caps how many requests may be issued back to back.

        Defaults to the full period allowance, which suits a provider that
        genuinely permits a burst. Set it to 1 for a provider that does not:
        FR24 was measured returning 429 on the SECOND of two calls 0.2 seconds
        apart, despite a documented per-minute allowance in the tens. A limiter
        that hands out ten tokens at once satisfies the stated limit on paper and
        still trips the real one on the first stage of collection.
        """
        self._buckets: list[TokenBucket] = []
        if per_second:
            self._buckets.append(
                TokenBucket(
                    per_second, burst if burst is not None else per_second,
                    clock=clock, sleep=sleep,
                )
            )
        if per_minute:
            self._buckets.append(
                TokenBucket(
                    per_minute / 60.0,
                    burst if burst is not None else per_minute,
                    clock=clock, sleep=sleep,
                )
            )

    def acquire(self, tokens: float = 1.0) -> float:
        """Satisfy every bucket. Returns total seconds waited."""
        return sum(bucket.acquire(tokens) for bucket in self._buckets)

    @property
    def unlimited(self) -> bool:
        return not self._buckets


# Published limits, used when config does not override them.
DEFAULT_LIMITS: dict[str, dict[str, float]] = {
    # MEASURED, not documented. Two /api/usage calls 0.2s apart returned 429 on
    # the second, and back-to-back probing found a burst ceiling of exactly one
    # request — far stricter than any published tier (Explorer 10/min, Essential
    # 30, Advanced 90). burst=1 forces even pacing rather than a permitted flurry
    # followed by a wall.
    "FR24": {"per_minute": 10.0, "burst": 1.0},
    # ULTRA tier. PRO is 3/sec and MEGA is 10/sec.
    "PRICELINE": {"per_second": 5.0},
    # Neither publishes a rate limit. Modest pacing avoids tripping an
    # unpublished one and costs nothing at this volume.
    "APIDIRECT": {"per_second": 5.0},
    "STAYING": {"per_second": 5.0},
}


def build_limiters(
    config: Mapping[str, object],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, RateLimiter]:
    """One RateLimiter per provider, from config with published fallbacks."""
    sections = {
        "FR24": "flightradar",
        "PRICELINE": "priceline",
        "APIDIRECT": "apidirect",
        "STAYING": "staying",
    }
    limiters: dict[str, RateLimiter] = {}
    for provider, section in sections.items():
        settings = dict(config.get(section) or {})  # type: ignore[arg-type]
        defaults = DEFAULT_LIMITS.get(provider, {})
        per_second = settings.get(
            "requests_per_second", defaults.get("per_second")
        )
        per_minute = settings.get(
            "queries_per_minute", defaults.get("per_minute")
        )
        burst = settings.get("burst", defaults.get("burst"))
        limiters[provider] = RateLimiter(
            per_second=float(per_second) if per_second else None,
            per_minute=float(per_minute) if per_minute else None,
            burst=float(burst) if burst else None,
            clock=clock,
            sleep=sleep,
        )
    return limiters
