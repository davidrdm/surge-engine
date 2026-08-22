"""BudgetGuard — API spend accounting and runaway protection.

All four providers are on paid plans, so this is not about rationing a free
tier. It does two narrower jobs:

  * runaway protection — a tipping bug must not spend a month's allowance in one
    iteration;
  * correct accounting — the providers do not share a billing unit.

| Provider  | Unit                                  |
|-----------|---------------------------------------|
| APIDIRECT | requests, metered per endpoint        |
| FR24      | credits per RECORD RETURNED           |
| STAYING   | credits per call                      |
| PRICELINE | requests, any endpoint                |

FR24 is the one that breaks naive accounting. Billing is on records returned,
not requests, so `limit` is not a cost control: live flight-positions/full costs
8 credits per returned flight, and even an empty response is charged a flat 1
credit. That is why units must be computed after a response arrives — see
fr24_units() — and why the FLIGHT_COUNT tripwire exists at all.

Rate limits are enforced separately by services/ratelimit.py. A budget refuses;
a rate limiter delays. Conflating them would turn a throttle into a data gap.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone
from typing import Any, Mapping

from ..db.database import SurgeDB, utcnow

# FR24 credit costs per record returned, from its published credit table.
# /count variants cost 15% of the corresponding full endpoint, rounded up.
FR24_CREDITS_PER_RECORD: dict[str, float] = {
    "/api/live/flight-positions/full": 8.0,
    "/api/live/flight-positions/light": 6.0,
    "/api/historic/flight-positions/full": 8.0,
    "/api/historic/flight-positions/light": 6.0,
    "/api/flight-summary/full": 3.0,     # historical <=30 days
    "/api/flight-summary/light": 2.0,
}
FR24_COUNT_MULTIPLIER = 0.15
# An empty response is still billed. Charging zero would make cheap empty
# queries look free and remove the incentive to narrow filters.
FR24_MINIMUM_CREDITS = 1.0

PROVIDER_FOR_ENDPOINT_PREFIX: tuple[tuple[str, str], ...] = (
    ("/api/", "FR24"),
    ("/v1/twitter", "APIDIRECT"),
    ("/v1/reddit", "APIDIRECT"),
    ("/v1/news", "APIDIRECT"),
    ("/v1/facebook", "APIDIRECT"),
    ("/v1/web", "APIDIRECT"),
    ("/v1/forums", "APIDIRECT"),
    # 8.5 wrapper move. The legacy paths stay mapped so an old query_queue or
    # api_calls row still resolves to a provider rather than raising.
    ("/cars/search", "PRICELINE"),
    ("/cars/auto-complete", "PRICELINE"),
    ("/cars/partners", "PRICELINE"),
    ("/search-rental-car", "PRICELINE"),
    ("/auto-complete-location", "PRICELINE"),
    ("/advance-auto-complete-location", "PRICELINE"),
    ("/search", "STAYING"),
    ("/availability", "STAYING"),
    ("/price-compare", "STAYING"),
    ("/price", "STAYING"),
    ("/listing", "STAYING"),
    ("/reviews", "STAYING"),
    ("/account", "STAYING"),
    ("/jobs", "STAYING"),
)


def provider_for_endpoint(endpoint: str) -> str:
    """Infer the billing provider from an endpoint path.

    Longest prefix wins, so /search-rental-car resolves to PRICELINE rather than
    matching STAYING's /search.
    """
    best: tuple[int, str] | None = None
    for prefix, provider in PROVIDER_FOR_ENDPOINT_PREFIX:
        if endpoint.startswith(prefix):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), provider)
    if best is None:
        raise ValueError(f"No provider mapping for endpoint {endpoint!r}")
    return best[1]


def fr24_units(endpoint: str, records_returned: int) -> float:
    """Credits actually consumed by an FR24 call.

    Must be called with the response in hand. Estimating beforehand is the
    mistake the old `limit=200` parameter invited: the limit bounds the records
    you receive but the bill follows what the server returned, and on
    flight-summary the limit is not even honoured.
    """
    if endpoint.endswith("/count"):
        base = endpoint[: -len("/count")] + "/full"
        per_record = FR24_CREDITS_PER_RECORD.get(base, 8.0) * FR24_COUNT_MULTIPLIER
        return max(FR24_MINIMUM_CREDITS, per_record)
    per_record = FR24_CREDITS_PER_RECORD.get(endpoint)
    if per_record is None:
        return max(FR24_MINIMUM_CREDITS, float(records_returned))
    return max(FR24_MINIMUM_CREDITS, per_record * max(0, records_returned))


#: Endpoints the vendor documents — and bills — as free. Charging them anyway
#: is the safe direction, but it is still wrong: Staying's /account runs on
#: every iteration start and is the call the remote reconciliation depends on,
#: so billing it makes the ledger disagree with the vendor by one per run.
FREE_ENDPOINTS: frozenset[str] = frozenset({"/account"})


def units_for(provider: str, endpoint: str, records_returned: int) -> float:
    """Billing units for one completed call, in that provider's unit."""
    if endpoint in FREE_ENDPOINTS:
        return 0.0
    if provider == "FR24":
        return fr24_units(endpoint, records_returned)
    # APIDIRECT, STAYING and PRICELINE all bill per call. Staying's true cost
    # is 10-20x that and is corrected from meta.creditsCharged by a
    # `#credit-adjustment` entry — verified live: an airbnb /search billed 1
    # up front and 79 on adjustment.
    return 1.0


def month_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


class BudgetGuard:
    """Reads api_budgets, sums api_calls, and decides whether to spend."""

    def __init__(self, db: SurgeDB, config: Mapping[str, Any]) -> None:
        self.db = db
        self.config = config
        self.cfg = config.get("budget", {})
        self._dry_run = bool(config.get("dry_run"))

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def seed_budgets(self) -> None:
        """Write configured monthly and per-iteration limits into api_budgets."""
        for provider, limit in (self.cfg.get("monthly_limit") or {}).items():
            self.db.set_budget(provider, None, "MONTH", float(limit))
        for provider, limit in (self.cfg.get("per_iteration_cap") or {}).items():
            self.db.set_budget(provider, None, "ITERATION", float(limit))

    def _limit(self, provider: str, period: str) -> float | None:
        row = self.db.one(
            "SELECT limit_units FROM api_budgets "
            "WHERE provider = ? AND endpoint IS NULL AND period = ?",
            (provider, period),
        )
        return float(row["limit_units"]) if row else None

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def plan_iteration(self, iteration_id: int) -> dict[str, float]:
        """Spend envelope for one iteration, per provider.

        The envelope is the lower of a hard per-iteration cap and a fair share of
        what remains this month, where the share is sized by how many iterations
        are still expected before the month rolls. On a paid plan the fair share
        rarely binds; the hard cap is what stops a runaway.
        """
        planned = int(self.cfg.get("iterations_per_month_planned", 60) or 60)
        now = utcnow()
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        remaining_days = days_in_month - now.day + 1
        remaining_iterations = max(
            1, round(planned * remaining_days / days_in_month)
        )

        plan: dict[str, float] = {}
        providers = set(self.cfg.get("monthly_limit") or {}) | set(
            self.cfg.get("per_iteration_cap") or {}
        )
        for provider in sorted(providers):
            monthly = self._limit(provider, "MONTH")
            hard = self._limit(provider, "ITERATION")
            if monthly is None:
                plan[provider] = hard if hard is not None else float("inf")
                continue
            used = self.db.units_used(provider, since=month_start(now))
            fair = max(0.0, monthly - used) / remaining_iterations
            plan[provider] = min(fair, hard) if hard is not None else fair

        self.db.set_budget_plan(iteration_id, plan)
        return plan

    # ------------------------------------------------------------------
    # Spend decisions
    # ------------------------------------------------------------------

    def can_afford(
        self,
        provider: str,
        endpoint: str,
        priority: int = 50,
        units: float = 1.0,
        *,
        iteration_id: int | None = None,
    ) -> tuple[bool, str | None]:
        """Whether to spend. Returns (allowed, skip_reason).

        skip_reason is a SKIP_REASONS member, so the caller can record precisely
        why collection did not happen. "We ran out of budget" and "the endpoint
        is broken" must never be indistinguishable downstream.
        """
        if self._dry_run:
            return True, None

        now = utcnow()
        monthly = self._limit(provider, "MONTH")
        if monthly is not None:
            used = self.db.units_used(provider, since=month_start(now))
            if used + units > monthly:
                return False, "MONTHLY_QUOTA_EXHAUSTED"
            # Near the cliff, reserve what is left for genuinely imminent work.
            hard_stop = float(self.cfg.get("hard_stop_pct", 0.9) or 0.9)
            ceiling = int(self.cfg.get("reserved_priority_ceiling", 20) or 20)
            if used >= monthly * hard_stop and priority > ceiling:
                return False, "HARD_STOP_PRIORITY"

        if iteration_id is not None:
            allocation = self._iteration_allocation(iteration_id, provider)
            if allocation is not None:
                spent = self.db.units_used(provider, iteration_id=iteration_id)
                if spent + units > allocation:
                    return False, "ITERATION_ALLOCATION_EXHAUSTED"

        return True, None

    def _iteration_allocation(
        self, iteration_id: int, provider: str
    ) -> float | None:
        """This iteration's envelope for a provider, from its stored plan."""
        import json

        row = self.db.get_iteration(iteration_id)
        if row is None or not row["budget_plan_json"]:
            return self._limit(provider, "ITERATION")
        plan = json.loads(row["budget_plan_json"])
        value = plan.get(provider)
        if value is None:
            return self._limit(provider, "ITERATION")
        return None if value == float("inf") else float(value)

    def record(
        self,
        *,
        provider: str,
        endpoint: str,
        records_returned: int = 0,
        http_status: int | None = None,
        latency_ms: int | None = None,
        iteration_id: int | None = None,
        query_id: int | None = None,
        error_message: str | None = None,
        units: float | None = None,
    ) -> float:
        """Record a completed call and return the units charged.

        Called for failures too: a 402 or a 429 still consumed a request against
        most plans, and pretending otherwise makes the ledger drift optimistic.
        """
        charged = 0.0 if self._dry_run else (
            units if units is not None
            else units_for(provider, endpoint, records_returned)
        )
        self.db.record_api_call(
            provider=provider,
            endpoint=endpoint,
            units=charged,
            records_returned=records_returned,
            http_status=http_status,
            latency_ms=latency_ms,
            iteration_id=iteration_id,
            query_id=query_id,
            error_message=error_message,
        )
        return charged

    def remaining(self, provider: str) -> dict[str, float]:
        """Units left this month, and this month's limit, for reporting."""
        monthly = self._limit(provider, "MONTH")
        used = self.db.units_used(provider, since=month_start())
        if monthly is None:
            return {"used": used, "limit": float("inf"), "remaining": float("inf")}
        return {
            "used": used,
            "limit": monthly,
            "remaining": max(0.0, monthly - used),
        }

    def reconcile_staying(self, remote_available: float) -> None:
        """Trust Staying's own credit balance over the local ledger.

        GET /account costs nothing and is authoritative. A local ledger that has
        drifted optimistic is how a system discovers exhaustion by 402 instead of
        by a graceful skip, so the remote number wins and the gap is logged.
        """
        current = self.remaining("STAYING")
        if current["remaining"] == float("inf"):
            self.db.set_budget("STAYING", None, "MONTH", remote_available)
            return
        if remote_available < current["remaining"]:
            drift = current["remaining"] - remote_available
            self.db.log(
                "BudgetGuard", "WARNING",
                "Staying credit balance below local ledger; trusting remote",
                remote_available=remote_available,
                local_remaining=current["remaining"],
                drift=drift,
            )
            self.db.set_budget(
                "STAYING", None, "MONTH", current["used"] + remote_available
            )
