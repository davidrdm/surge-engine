"""Lodging availability via the Staying API (api.stayingapi.com).

Replaces the Amadeus connector. Staying is an OTA aggregator over Airbnb,
Booking.com, Vrbo and Google Hotels — not a hotel inventory API — which changes
the methodology and improves it.

Amadeus counted how many offers a search returned near-term versus at a
baseline. That conflates "not returned" with "not available": search ranking,
result limits and pagination all move the count without anything being booked.

Staying's /availability returns per-date `available` booleans for a specified
set of listings, so the two-stage method measures the thing itself:

  1. /search once per key location to fix a stable listing set, cached so both
     windows are measured against identical listings.
  2. /availability for the near-term window and each baseline window, over that
     same set.

The denominator is then nights offered rather than offers returned, and a drop
means listings actually went unavailable.

Two provider behaviours the connector must handle:

  * Async responses. HTTP 202 returns `data.jobId` and the result is collected
    from GET /jobs/{jobId}, honouring the Retry-After header.
  * GET /account costs zero credits and reports the plan, the key's environment
    (sandbox keys are prefixed stay_test_, production stay_live_) and the credit
    balance. It is authoritative over the local ledger.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

from ..base.connector import (
    BaseConnector,
    PlatformUnavailableError,
    SchemaError,
    UpstreamError,
)

# Platform-leg statuses that mean "we did not search", as distinct from
# "we searched and found nothing".
NOT_SEARCHED_STATUSES = frozenset({"skipped", "error", "failed", "unavailable"})


@dataclass
class SearchResult:
    """Records plus the response metadata that says how they were obtained.

    The `meta` block is not decoration. It carries `platformResults`, which
    distinguishes a platform leg that was searched and returned nothing from one
    that was never searched at all, and `creditsCharged`, which is the real cost
    of the call rather than an assumed one. Discarding it — which this connector
    originally did — throws away both the coverage signal and the accounting.
    """

    records: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def warnings(self) -> list[dict[str, Any]]:
        value = self.meta.get("warnings")
        return [w for w in value if isinstance(w, Mapping)] if isinstance(value, list) else []

    @property
    def platform_results(self) -> dict[str, dict[str, Any]]:
        value = self.meta.get("platformResults")
        if not isinstance(value, list):
            return {}
        return {
            str(row.get("platform")): dict(row)
            for row in value if isinstance(row, Mapping) and row.get("platform")
        }

    @property
    def partial(self) -> bool:
        """The provider's own admission that the result set is incomplete."""
        return bool(self.meta.get("partial"))

    @property
    def cached(self) -> bool:
        """The provider's own statement that it served a stored copy (9.4).

        Not an inference and not detectable any other way: the charge is
        identical either way — measured at 30 credits per price-compare call
        whether `meta.cached` is true or false — so the ledger says nothing
        about freshness. Absent means the vendor did not say, which is reported
        as live rather than as unknown: it answered the request it was given.
        """
        return bool(self.meta.get("cached"))

    @property
    def credits_charged(self) -> float | None:
        value = self.meta.get("creditsCharged")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def not_searched(self) -> dict[str, str]:
        """Requested platforms that were not actually searched, with reasons."""
        return {
            platform: str(row.get("reason") or row.get("status") or "unknown")
            for platform, row in self.platform_results.items()
            if str(row.get("status", "")).lower() in NOT_SEARCHED_STATUSES
        }

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self):
        return iter(self.records)

EP_ACCOUNT = "/account"
EP_SEARCH = "/search"
EP_AVAILABILITY = "/availability"
EP_PRICE_COMPARE = "/price-compare"
EP_JOBS = "/jobs"

DEFAULT_POLL_INTERVAL_S = 2.0


class StayingConnector(BaseConnector):
    """Lodging availability, with async job handling."""

    provider = "STAYING"

    def __init__(
        self,
        api_key: str,
        *,
        # The /v1 is not optional. The published OpenAPI document lists paths as
        # /search, /availability and /account, but those are relative to a server
        # base of https://api.stayingapi.com/v1 — calling them at the bare host
        # returns 404 "Route GET /search was not found". Verified live.
        base_url: str = "https://api.stayingapi.com/v1",
        # Measured: a /search for one city took 125 seconds to complete, while
        # the response's own `estimatedSeconds` claimed 45. The estimate is not
        # trustworthy, so the ceiling is generous. This latency is why the
        # listing set is cached for days — /search is not something to run per
        # iteration.
        poll_max_s: float = 420.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(api_key, base_url=base_url, **kwargs)
        self.poll_max_s = poll_max_s

    @property
    def name(self) -> str:
        return "Staying"

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    @property
    def is_sandbox_key(self) -> bool:
        """Sandbox keys are self-describing, which makes misuse detectable."""
        return self._api_key.startswith("stay_test_")

    # ------------------------------------------------------------------
    # Async job handling
    # ------------------------------------------------------------------

    def _request_maybe_async(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        count_records,
        iteration_id: int | None = None,
        query_id: int | None = None,
    ) -> Any:
        """Issue a request, following the job protocol if the API defers.

        A 202 is a success at the HTTP layer but not yet an answer. The job id is
        polled until the job reaches a terminal state or poll_max_s elapses.
        Timing out raises rather than returning partial data — an incomplete
        lodging picture must register as a coverage gap, not as availability.
        """
        response = self._request(
            path, params=params, count_records=count_records,
            iteration_id=iteration_id, query_id=query_id,
        )
        if response.status_code != 202:
            return response.data

        job_id = _job_id(response.data)
        deadline = time.monotonic() + self.poll_max_s
        interval = _retry_after(response.headers) or DEFAULT_POLL_INTERVAL_S

        while True:
            if time.monotonic() + interval > deadline:
                raise UpstreamError(
                    f"Staying job {job_id} did not finish within "
                    f"{self.poll_max_s:.0f}s",
                    provider=self.provider, endpoint=path,
                )
            self._sleep(interval)
            poll = self._request(
                f"{EP_JOBS}/{job_id}",
                # Re-reading a job is not a fresh billable search; the units were
                # charged when the job was created.
                count_records=lambda data: 0,
                iteration_id=iteration_id, query_id=query_id,
            )
            status = _job_status(poll.data)
            if status in ("completed", "complete", "succeeded", "success"):
                return poll.data
            if status in ("failed", "error", "cancelled", "canceled"):
                raise UpstreamError(
                    f"Staying job {job_id} ended in state {status!r}",
                    provider=self.provider, endpoint=path,
                )
            interval = _retry_after(poll.headers) or interval

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def account(self) -> dict[str, Any]:
        """Plan, key environment and credit balance. Costs zero credits."""
        response = self._request(EP_ACCOUNT, count_records=lambda data: 0)
        payload = _unwrap(response.data)
        if not isinstance(payload, dict):
            raise SchemaError(
                "Staying /account did not return an object",
                provider=self.provider, endpoint=EP_ACCOUNT,
            )
        return payload

    def credits_available(self) -> float | None:
        """Remote credit balance, or None if the plan does not report one.

        Authoritative over the local ledger: a ledger that has drifted
        optimistic is how a system discovers exhaustion by 402 rather than by a
        graceful skip.
        """
        payload = self.account()
        credits = payload.get("credits")
        if isinstance(credits, Mapping):
            for key in ("available", "balance", "remaining"):
                if credits.get(key) is not None:
                    return float(credits[key])
        for key in ("creditsAvailable", "credits_available", "balance"):
            if payload.get(key) is not None:
                return float(payload[key])
        return None

    def search_listings(
        self,
        params: Mapping[str, Any],
        *,
        iteration_id: int | None = None,
        query_id: int | None = None,
    ) -> SearchResult:
        """Stage one: the listing set for a location, cached by the caller.

        Deliberately date-less. `/search` here is set DISCOVERY, and the fixed
        set is what makes the near and baseline windows comparable. Passing
        checkIn/checkOut would select only listings free on those dates, biasing
        the set toward whichever window was passed and systematically
        understating any drop — the listings that went unavailable would simply
        be absent from the set instead of counted as unavailable.

        The provider requires dates for its Google Hotels leg, so Google is
        structurally unavailable for discovery. That is an accepted limitation,
        not a defect: see the note on hotels in the module docstring.
        """
        return self._fetch(EP_SEARCH, params, _normalise_property,
                           iteration_id=iteration_id, query_id=query_id)

    def availability(
        self,
        params: Mapping[str, Any],
        *,
        iteration_id: int | None = None,
        query_id: int | None = None,
    ) -> SearchResult:
        """Stage two: per-date availability booleans for a fixed listing set."""
        return self._fetch(EP_AVAILABILITY, params, _normalise_availability,
                           iteration_id=iteration_id, query_id=query_id)

    def price_compare(
        self,
        params: Mapping[str, Any],
        *,
        iteration_id: int | None = None,
        query_id: int | None = None,
    ) -> SearchResult:
        """Priced quote for one property on specific dates. Google mode.

        The one call in this connector that legitimately takes dates. `/search`
        performs set DISCOVERY, so dates there filter the set and bias it toward
        whichever window was passed. This asks for a price on a **named
        property** — nothing is being selected out of a set, so there is no
        selection effect. The two calls differ in kind, not just in parameters.

        Google is the only platform here that reaches actual hotels rather than
        short-term rentals, and it requires `checkIn`/`checkOut`, which is why it
        is unavailable to discovery and available here.

        Returns a single-property result, so `records` holds at most one row.
        """
        return self._fetch(EP_PRICE_COMPARE, params, _normalise_price_compare,
                           iteration_id=iteration_id, query_id=query_id,
                           unwrap=_unwrap_offers)

    def _fetch(
        self,
        endpoint: str,
        params: Mapping[str, Any],
        normalise,
        *,
        iteration_id: int | None,
        query_id: int | None,
        unwrap=None,
    ) -> SearchResult:
        """One call, with the metadata surfaced rather than discarded.

        `unwrap` overrides how records are located in the body. `/search` and
        `/availability` return a list; `/price-compare` returns a single object
        whose `offers[]` are the records. That difference is why the price path
        raised `SchemaError` on its first live call — it was written against an
        assumed shape and never exercised.
        """
        payload = self._request_maybe_async(
            endpoint, params=params, count_records=_count_records,
            iteration_id=iteration_id, query_id=query_id,
        )
        result = SearchResult(
            records=[normalise(item)
                     for item in (unwrap or _unwrap_list)(payload)],
            meta=_extract_meta(payload),
        )
        self._charge_actual_credits(result, endpoint, iteration_id, query_id)
        if endpoint != EP_PRICE_COMPARE:
            self._assert_platforms_searched(result, endpoint, params)
        return result

    def _charge_actual_credits(
        self, result: SearchResult, endpoint: str,
        iteration_id: int | None, query_id: int | None,
    ) -> None:
        """Correct the ledger with the cost the provider actually reported.

        The generic accounting charges one unit per call, which is right for a
        per-request provider and badly wrong here: a multi-platform search was
        measured at 20 credits and an airbnb-only one at 10. Left uncorrected the
        ledger under-counts by an order of magnitude, and BudgetGuard would keep
        authorising spend against a balance that ran out long ago.

        Recorded as a delta because _request already charged the flat unit.
        """
        credits = result.credits_charged
        if credits is None or self._on_call is None:
            return
        delta = credits - 1.0        # the flat unit already booked
        if abs(delta) < 0.001:
            return
        self._on_call(
            provider=self.provider, endpoint=f"{endpoint}#credit-adjustment",
            http_status=None, records_returned=0, latency_ms=None,
            error_message=None, iteration_id=iteration_id, query_id=query_id,
            units=delta,
        )

    def _assert_platforms_searched(
        self, result: SearchResult, endpoint: str, params: Mapping[str, Any],
    ) -> None:
        """Fail loud when a platform we asked for was not actually searched.

        The provider fans a request out to several vendors and reports each leg
        separately. A leg can be skipped for reasons unrelated to availability —
        a missing parameter, a vendor outage — and the response still arrives as
        HTTP 200 with a shorter list. Reading that as "fewer listings" is exactly
        the mistake that turns a collection failure into an apparent absence of
        signal.

        Only platforms this call requested can raise. A skipped leg for a
        platform we did not ask for is informational; the caller logs it.

        **Not applied to `/price-compare`, and the reason is structural.**
        `platform_results` is keyed by platform, which assumes one leg per
        platform — true for `/search` and `/availability`. Direct-mode pricing
        fans out one leg per LISTING, so six airbnb listings arrive as six legs
        all named `airbnb` and the per-leg status collapses to whichever was
        written last. Measured live: six listings returned four offers with
        the platform reported as `failed`, which this guard read as a total
        outage and raised on — discarding four good prices.

        Coverage there is per-listing, not per-platform, so the equivalent
        protection lives where it can be expressed: `_collect_lodging_price`
        pairs only listings priced in BOTH windows and skips the query as a
        recorded coverage gap when nothing pairs. A window that priced nothing
        therefore still becomes a gap rather than a zero — the property this
        guard exists to preserve — without throwing away partial coverage.
        """
        skipped = result.not_searched()
        if not skipped:
            return
        requested = _requested_platforms(params)
        blocking = {p: r for p, r in skipped.items()
                    if not requested or p in requested}
        if not blocking:
            return
        detail = "; ".join(f"{p}: {reason}" for p, reason in sorted(blocking.items()))
        raise PlatformUnavailableError(
            f"Staying {endpoint}: requested platform(s) not searched — {detail}. "
            f"Treating as a collection gap rather than as an absence of listings.",
            provider=self.provider, endpoint=endpoint,
        )

    def health_check(self) -> dict[str, Any]:
        try:
            payload = self.account()
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            return {"provider": self.provider, "healthy": False,
                    "detail": str(exc)}
        return {
            "provider": self.provider,
            "healthy": True,
            "detail": "sandbox key" if self.is_sandbox_key else "live key",
            "credits": payload.get("credits"),
        }


# ---------------------------------------------------------------------------
# Envelope handling
# ---------------------------------------------------------------------------


def _unwrap(payload: Any) -> Any:
    """Return the `data` member if the response is enveloped."""
    if isinstance(payload, Mapping) and "data" in payload:
        return payload["data"]
    return payload


def _unwrap_offers(payload: Any) -> list[dict[str, Any]]:
    """The per-listing offers inside a direct-mode /price-compare body.

    The response is one object, not a list: `data.result.offers[]` after the
    async job completes, or `data.offers[]` on a synchronous cache hit.

    A completed job with NO offers is an empty list rather than an error. That
    happens for real — a 6-listing call returned 4 offers, and a call whose
    check-in was today returned none at all because no listing could be priced
    at that notice. Partial coverage is normal here and `price_signal` already
    pairs only the listings present in both windows.
    """
    data = _unwrap(payload)
    if not isinstance(data, Mapping):
        return []
    result = data.get("result") if isinstance(data.get("result"), Mapping) else data
    offers = result.get("offers")
    if not isinstance(offers, list):
        return []
    return [o for o in offers if isinstance(o, Mapping) and o.get("listingId")]


def _unwrap_list(payload: Any) -> list[dict[str, Any]]:
    data = _unwrap(payload)
    if data is None:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, Mapping)]
    if isinstance(data, Mapping):
        # `result` first: a completed job wraps its records as
        # {"data": {"jobId": ..., "status": "completed", "result": [...]}},
        # which is the shape almost every real response takes because /search
        # and /availability are asynchronous in practice.
        for key in ("result", "results", "properties", "listings", "availability"):
            if isinstance(data.get(key), list):
                return [i for i in data[key] if isinstance(i, Mapping)]
    raise SchemaError(
        f"Staying response body was {type(data).__name__}, expected a list",
        provider="STAYING",
    )


def _extract_meta(payload: Any) -> dict[str, Any]:
    """The response's `meta` block, or an empty mapping.

    Sits beside `data` rather than inside it, which is why the original
    `_unwrap`-only handling silently dropped it.
    """
    if isinstance(payload, Mapping) and isinstance(payload.get("meta"), Mapping):
        return dict(payload["meta"])
    return {}


def _requested_platforms(params: Mapping[str, Any]) -> set[str]:
    """Platforms this call explicitly asked for, lowercased.

    Empty means "the caller did not constrain the platforms", in which case
    every leg the provider chose to run counts as requested.
    """
    raw = params.get("platforms") or params.get("platform") or ""
    if isinstance(raw, (list, tuple, set)):
        values: Sequence[Any] = list(raw)
    else:
        values = str(raw).split(",")
    return {str(v).strip().lower() for v in values if str(v).strip()}


def _count_records(payload: Any) -> int:
    """Billable record count. Total by construction — accounting never raises.

    Called from inside the request layer, which happens before the async job
    envelope has been recognised, so this must tolerate a 202 body carrying only
    a jobId: no records have been returned yet, and the units were charged when
    the job was created. Shape validation belongs to the parse step that follows,
    where a SchemaError is meaningful and actionable.
    """
    data = _unwrap(payload)
    if isinstance(data, Mapping) and "jobId" in data:
        return 0
    try:
        return len(_unwrap_list(payload))
    except SchemaError:
        return 0


def _job_id(payload: Any) -> str:
    data = _unwrap(payload)
    if isinstance(data, Mapping) and data.get("jobId"):
        return str(data["jobId"])
    raise SchemaError(
        "Staying returned HTTP 202 without a data.jobId to poll",
        provider="STAYING",
    )


def _job_status(payload: Any) -> str:
    for candidate in (payload, _unwrap(payload)):
        if isinstance(candidate, Mapping) and candidate.get("status"):
            return str(candidate["status"]).lower()
    # No status field on a 200 means the job finished and this is the result.
    return "completed"


def _retry_after(headers: Mapping[str, str]) -> float | None:
    for key in ("Retry-After", "retry-after"):
        if key in headers:
            try:
                return max(0.5, float(headers[key]))
            except (TypeError, ValueError):
                return None
    return None


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _normalise_property(item: Mapping[str, Any]) -> dict[str, Any]:
    location = item.get("location") or {}
    price = item.get("price") or {}
    return {
        "listing_id": str(item.get("platformListingId") or item.get("id") or ""),
        "platform": item.get("platform", ""),
        "name": item.get("name", ""),
        "url": item.get("url", ""),
        "property_type": item.get("propertyType", ""),
        "lat": location.get("lat") if isinstance(location, Mapping) else None,
        "lon": location.get("lng") or location.get("lon")
        if isinstance(location, Mapping) else None,
        "bedrooms": item.get("bedrooms"),
        "price_total": price.get("total") if isinstance(price, Mapping) else None,
        "currency": price.get("currency") if isinstance(price, Mapping) else None,
    }


def _normalise_price_compare(item: Mapping[str, Any]) -> dict[str, Any]:
    """One OFFER from a direct-mode /price-compare -> a price row.

    One row per listing, not per call. Direct mode fans out across the listing
    ids it was given and returns `offers[]` with a `listingId` on each, so the
    row identity is the same `listing_id` the availability path pins — the two
    lodging sub-signals therefore measure the same properties by construction
    rather than by resolving independently and hoping they agree.

    This replaced a Google-mode parser that never ran against a live response.
    Measured live, Google mode resolved the same location string to a different
    property in each window — Omni in one, Marriott in the other, and one
    civic building to a rental in Puerto Vallarta — while echoing the query
    string back as `property` so the mismatch was concealed, and it never
    returned the `googleHotelId` the pinning depended on. Direct mode returns
    the listing id it was asked about, which is an identity rather than a guess.
    """
    fees = item.get("fees")
    fees = fees if isinstance(fees, Mapping) else {}
    return {
        # The pinned identity. Direct mode echoes back the id we supplied, so
        # this cannot silently become a different property between windows.
        "property_ref": str(item.get("listingId") or ""),
        "platform": str(item.get("platform") or item.get("ota") or ""),
        "name": str(item.get("listingId") or ""),
        # The total for the stay, which is what a booker pays. `nightlyPrice` is
        # carried because a window whose length changes would otherwise make two
        # totals incomparable.
        "price_min": _as_float(item.get("totalPrice")),
        "price_nightly": _as_float(item.get("nightlyPrice")),
        "nights": item.get("nights"),
        "taxes": _as_float(fees.get("taxes")),
        "currency": item.get("currency") or "",
        "url": item.get("url") or "",
    }


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def price_signal(
    near_rows: Iterable[Mapping[str, Any]],
    baseline_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Pair near-term and baseline prices for the SAME property.

    Only properties priced in both windows are emitted. A property present in one
    window only says the property set moved, not that its price did, and
    substituting a different hotel would measure the difference between two
    hotels rather than the change in one.

    Availability columns are left unset: this measures price, and populating
    near/base_available with a zero would read as total scarcity.
    """
    near_by_ref = {r["property_ref"]: r for r in near_rows if r.get("property_ref")}
    base_by_ref = {r["property_ref"]: r for r in baseline_rows if r.get("property_ref")}

    signals: list[dict[str, Any]] = []
    for ref in sorted(set(near_by_ref) & set(base_by_ref)):
        near, base = near_by_ref[ref], base_by_ref[ref]
        if near.get("price_min") is None or base.get("price_min") is None:
            continue
        signals.append({
            "provider_ref": ref,
            "item_name": near.get("name") or base.get("name") or ref,
            "price_near": near["price_min"],
            "price_baseline": base["price_min"],
        })
    return signals


def _normalise_availability(item: Mapping[str, Any]) -> dict[str, Any]:
    """One listing's per-date availability over the requested window."""
    dates = item.get("dates") or []
    nights = [d for d in dates if isinstance(d, Mapping)]
    available = sum(1 for d in nights if d.get("available"))
    return {
        "listing_id": str(item.get("listingId") or ""),
        "platform": item.get("platform", ""),
        "nights_offered": len(nights),
        "nights_available": available,
        "dates": [
            {"date": d.get("date"), "available": bool(d.get("available")),
             "bookable": d.get("bookable"), "min_nights": d.get("minNights")}
            for d in nights
        ],
    }


def window_dates(anchor: date, *, near_hours: int, baseline_days: int) -> tuple[
    tuple[date, date], tuple[date, date]
]:
    """Near-term and baseline windows, aligned to the same weekday.

    Weekday alignment is the point. Comparing a Friday-Saturday near window
    against a midweek baseline makes ordinary weekend demand look like a surge,
    which is the most obvious way this signal produces false positives. Offering
    baselines at +7 and +14 days keeps both on the same weekday as the near
    window, and disagreement between them is itself informative.
    """
    near_start = anchor
    near_end = anchor + timedelta(days=max(1, round(near_hours / 24)))
    span = near_end - near_start
    base_start = near_start + timedelta(days=baseline_days)
    return (near_start, near_end), (base_start, base_start + span)


def availability_signal(
    near_rows: Iterable[Mapping[str, Any]],
    baseline_rows: Iterable[Mapping[str, Any]],
    *,
    listing_names: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Pair near-term and baseline availability per listing.

    Only listings measured in BOTH windows are emitted. A listing that appears
    in one window and not the other says nothing about availability — it says the
    listing set moved, which is exactly the confound the fixed listing set exists
    to eliminate.
    """
    near_by_id = {row["listing_id"]: row for row in near_rows if row.get("listing_id")}
    base_by_id = {row["listing_id"]: row for row in baseline_rows if row.get("listing_id")}
    names = listing_names or {}

    signals: list[dict[str, Any]] = []
    for listing_id in sorted(set(near_by_id) & set(base_by_id)):
        near = near_by_id[listing_id]
        base = base_by_id[listing_id]
        near_available = int(near.get("nights_available") or 0)
        base_available = int(base.get("nights_available") or 0)
        drop = (
            (base_available - near_available) / base_available * 100.0
            if base_available > 0 else 0.0
        )
        signals.append({
            "provider_ref": listing_id,
            "item_name": names.get(listing_id, near.get("platform", "")),
            "near_available": near_available,
            "near_total": int(near.get("nights_offered") or 0),
            "base_available": base_available,
            "base_total": int(base.get("nights_offered") or 0),
            "drop_pct": round(max(0.0, drop), 1),
        })
    return signals
