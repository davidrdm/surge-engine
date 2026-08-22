"""CollectionAgent — executes queued queries. No LLM.

Drains `query_queue` for a given set of source types, calls the connector, stores
the raw payload, and derives normalised signals. Runs twice per iteration under
different filters (SOCIAL first, then the tipped sources), which is legitimate
precisely because it reads its work from the database rather than from a caller.

Three rules govern everything here.

**A failed query fails only itself.** A connector raising does not abort the
stage; the query is marked FAILED with the reason and collection moves to the
next one. Correlation later reads that FAILED status as a coverage gap, which
caps confidence and adds a caveat — as opposed to the query silently producing
nothing, which would read as an absence of threat.

**A budget refusal is not a failure.** It marks the query SKIPPED_BUDGET with a
specific reason, so "we could not afford to look" stays distinguishable from
"we looked and the endpoint was broken" and from "we looked and found nothing".

**Social payloads produce no signals here.** Deciding whether a free-text post is
about relevance is language reasoning, so TriageAgent does it in the
next stage. CollectionAgent stores the raw payload and stops.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

from ..base.agent import BaseAgent
from ..base.connector import (BaseConnector, ConnectorError,
                              PlatformUnavailableError)
from ..connectors import flightradar as fr
from ..connectors import priceline as pl
from ..connectors import staying as st
from ..db.database import SurgeDB, utcnow
from ..services import governance
from ..services.budget import BudgetGuard, provider_for_endpoint
from ..services.retention import retention_days

# Which source types each collection pass drains.
SOCIAL_TYPES: tuple[str, ...] = ("SOCIAL",)
TIPPED_TYPES: tuple[str, ...] = (
    "FLIGHT_COUNT", "FLIGHT_LIVE", "FLIGHT_HISTORY",
    "LODGING", "LODGING_PRICE", "CAR",
)


class SkipQuery(Exception):
    """A handler decided this query yields nothing usable, for a stated reason.

    Distinct from an exception, which means the call failed, and from a normal
    return, which means it succeeded. Raised rather than returned so that a
    handler cannot set a terminal status and then have `_collect_one` overwrite
    it with COMPLETE — which is exactly the bug this replaced.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class CollectionAgent(BaseAgent):
    """Executes queued queries and writes raw payloads plus derived signals."""

    stage = "COLLECTING"

    def __init__(
        self,
        db: SurgeDB,
        config: Mapping[str, Any],
        connectors: Mapping[str, BaseConnector],
        budget: BudgetGuard | None = None,
    ) -> None:
        super().__init__(db, config)
        self.connectors = connectors
        self.budget = budget

    # ------------------------------------------------------------------
    # Drain loop
    # ------------------------------------------------------------------

    def _execute(
        self,
        iteration_id: int,
        *,
        source_types: Sequence[str] = SOCIAL_TYPES,
        max_queries: int = 500,
    ) -> None:
        counts = {"executed": 0, "failed": 0, "skipped": 0, "signals": 0}
        for _ in range(max_queries):
            query = self.db.claim_next_query(iteration_id, source_types)
            if query is None:
                break
            outcome, signal_count = self._collect_one(iteration_id, query)
            counts[outcome] += 1
            counts["signals"] += signal_count

        self._log(
            "INFO",
            f"Collection pass over {','.join(source_types)}: "
            f"{counts['executed']} executed, {counts['failed']} failed, "
            f"{counts['skipped']} skipped, {counts['signals']} signals",
            iteration_id=iteration_id, **counts,
        )

    def _collect_one(
        self, iteration_id: int, query: Mapping[str, Any]
    ) -> tuple[str, int]:
        """Execute one query. Returns (outcome, signals_written)."""
        import json

        query_id = int(query["query_id"])
        source_type = query["source_type"]
        endpoint = query["endpoint"]
        params = json.loads(query["params_json"])
        provider = provider_for_endpoint(endpoint)

        # Second budget check. The first was at enqueue time, and a long
        # iteration can cross the boundary between the two.
        if self.budget is not None:
            allowed, reason = self.budget.can_afford(
                provider, endpoint, int(query["priority"]),
                iteration_id=iteration_id,
            )
            if not allowed:
                self.db.skip_query(query_id, "SKIPPED_BUDGET", reason or
                                   "MONTHLY_QUOTA_EXHAUSTED")
                # Attribute the refusal to the city it was refused FOR.
                # `refused_source_types` treats a NULL city_name as applying to
                # every city — correct for a guard that fires before the city
                # exists, wrong here, where the queue row names it. Measured
                # across seven metros: three were collected 24/24 and still
                # reported a SOCIAL coverage gap, because the other four cities'
                # budget refusals were being broadcast to all of them.
                city = (self.db.get_city(query["city_id"])
                        if query["city_id"] is not None else None)
                self.db.record_queue_decision(
                    iteration_id, query["rule_code"] or "COLLECT",
                    "BUDGET_EXHAUSTED", source_type=source_type,
                    city_name=city["name"] if city else None,
                    detail=f"{provider}: {reason}",
                )
                self._log("WARNING",
                          f"Skipped {source_type} query {query_id}: {reason}",
                          iteration_id=iteration_id, provider=provider)
                return "skipped", 0

        connector = self.connectors.get(provider)
        if connector is None:
            self.db.fail_query(query_id, f"no connector configured for {provider}")
            return "failed", 0

        try:
            handler = getattr(self, f"_collect_{source_type.lower()}")
            payload, signal_count = handler(
                iteration_id, query_id, query, connector, params
            )
        except SkipQuery as skip:
            self.db.skip_query(query_id, "SKIPPED_NO_MAPPING", skip.reason,
                               skip.detail)
            self._log("WARNING",
                      f"{source_type} query {query_id} yielded nothing usable: "
                      f"{skip.detail or skip.reason}",
                      iteration_id=iteration_id, reason=skip.reason)
            return "skipped", 0
        except ConnectorError as exc:
            # The whole point of the fail-loud contract: record why, keep going.
            self.db.fail_query(query_id, str(exc))
            self._log("ERROR", f"{source_type} query {query_id} failed: {exc}",
                      iteration_id=iteration_id, provider=provider,
                      status_code=getattr(exc, "status_code", None))
            return "failed", 0
        except Exception as exc:  # noqa: BLE001
            self.db.fail_query(query_id, f"{type(exc).__name__}: {exc}")
            self._log("ERROR", f"{source_type} query {query_id} raised: {exc}",
                      iteration_id=iteration_id, exc_type=type(exc).__name__)
            return "failed", 0

        self.db.complete_query(query_id, result_count=payload)
        return "executed", signal_count

    # ------------------------------------------------------------------
    # Per-source handlers
    # ------------------------------------------------------------------

    def _store_raw(
        self, iteration_id: int, query_id: int, source_type: str,
        provider: str, payload: Any,
    ) -> int:
        return self.db.insert_raw_result(
            query_id=query_id, iteration_id=iteration_id,
            source_type=source_type, provider=provider, payload=payload,
            retention_days=retention_days(self.config, provider),
        )

    def _collect_social(
        self, iteration_id: int, query_id: int, query: Mapping[str, Any],
        connector: Any, params: Mapping[str, Any],
    ) -> tuple[int, int]:
        """Store posts. No signals — relevance is TriageAgent's judgement."""
        posts = connector.search(
            query["endpoint"], params,
            iteration_id=iteration_id, query_id=query_id,
        )
        self._store_raw(iteration_id, query_id, "SOCIAL", "APIDIRECT", posts)
        return len(posts), 0

    def _collect_flight_count(
        self, iteration_id: int, query_id: int, query: Mapping[str, Any],
        connector: Any, params: Mapping[str, Any],
    ) -> tuple[int, int]:
        """The tripwire, only reachable when use_count_tripwire is enabled.

        Both /count endpoints return 403 on the Explorer tier, so this is off by
        default. Retained because a higher tier enables them.
        """
        count = connector.count_live(
            params, iteration_id=iteration_id, query_id=query_id
        )
        self._store_raw(iteration_id, query_id, "FLIGHT_COUNT", "FR24",
                        {"record_count": count})
        return count, 0

    def _collect_flight_live(
        self, iteration_id: int, query_id: int, query: Mapping[str, Any],
        connector: Any, params: Mapping[str, Any],
    ) -> tuple[int, int]:
        """Live positions. Every record is AMBIGUOUS on category by construction."""
        flights = connector.live_positions(
            params, iteration_id=iteration_id, query_id=query_id
        )
        raw_id = self._store_raw(iteration_id, query_id, "FLIGHT_LIVE",
                                 "FR24", flights)
        written = self._write_flight_signals(
            iteration_id, raw_id, query["city_id"], flights,
            self._acquisition(query),
        )
        return len(flights), written

    def _collect_flight_history(
        self, iteration_id: int, query_id: int, query: Mapping[str, Any],
        connector: Any, params: Mapping[str, Any],
    ) -> tuple[int, int]:
        """Historical flights, then resolve the categories of live records.

        This is the only endpoint returning a real category, so without this
        step every live flight signal stays AMBIGUOUS and is scored at the
        lowest weight its filter could have earned. Resolution happens here, on
        arrival, rather than being deferred to correlation — the correlation
        layer should read facts, not derive them.
        """
        flights = connector.flight_summary(
            params, iteration_id=iteration_id, query_id=query_id
        )
        raw_id = self._store_raw(iteration_id, query_id, "FLIGHT_HISTORY",
                                 "FR24", flights)
        written = self._write_flight_signals(
            iteration_id, raw_id, query["city_id"], flights,
            self._acquisition(query),
        )
        upgraded = self._resolve_live_categories(
            iteration_id, query["city_id"], flights
        )
        if upgraded:
            self._log("INFO",
                      f"Confirmed the category of {upgraded} live flight record(s)",
                      iteration_id=iteration_id, upgraded=upgraded)
        return len(flights), written

    def _acquisition(
        self, query: Mapping[str, Any], *, cached: bool | None = None,
    ) -> dict[str, str]:
        """9.4. The one comparable acquisition value for this query's signals.

        Per-family provenance already existed — the geo method, the publisher
        method, the facility match method, each provider's governance record —
        and none of it was comparable. An analyst reading a lodging row beside
        a flight row could not tell how directly either was known, which is the
        first question to ask of evidence that may cost money to act on.

        `cached` is the vendor's assertion where a vendor makes one. Only
        Staying does, and only its price-compare path; passing None everywhere
        else says the provider was asked and answered, not that freshness is
        unknown.
        """
        provider = provider_for_endpoint(query["endpoint"])
        collection_class, basis = governance.collection_class(
            provider, query["endpoint"], cached=cached)
        return {"collection_class": collection_class,
                "collection_basis": basis}

    def _write_flight_signals(
        self, iteration_id: int, raw_id: int, city_id: Any,
        flights: Iterable[Mapping[str, Any]],
        acquisition: Mapping[str, str] | None = None,
    ) -> int:
        import sqlite3

        written = 0
        for flight in flights:
            try:
                self.db.insert_signal(
                    iteration_id=iteration_id, raw_id=raw_id,
                    signal_type="FLIGHT", city_id=city_id,
                    observed_at=flight.get("observed_at"),
                    quality=1.0,
                    fr24_id=flight.get("fr24_id") or None,
                    callsign=flight.get("callsign") or None,
                    registration=flight.get("registration") or None,
                    aircraft_type=flight.get("aircraft_type") or None,
                    origin_iata=flight.get("origin_iata") or None,
                    dest_iata=flight.get("dest_iata") or None,
                    operating_as=flight.get("operating_as") or None,
                    flight_category=flight.get("flight_category"),
                    category_confidence=flight.get("category_confidence"),
                    flight_status=flight.get("flight_status"),
                    eta=flight.get("eta"),
                    **(acquisition or {}),
                )
                written += 1
            except sqlite3.IntegrityError:
                # The same airframe seen by both the live and historical query.
                # Deduplicated by idx_sig_dedup; not an error.
                continue
        return written

    def _resolve_live_categories(
        self, iteration_id: int, city_id: Any, summary: Sequence[Mapping[str, Any]],
    ) -> int:
        """Upgrade AMBIGUOUS live signals using confirmed historical categories."""
        if city_id is None:
            return 0
        existing = [
            dict(row) for row in self.db.signals_for_city(iteration_id, city_id)
            if row["signal_type"] == "FLIGHT"
            and row["category_confidence"] == "AMBIGUOUS"
        ]
        if not existing:
            return 0
        resolved = fr.resolve_categories(existing, summary)
        upgraded = 0
        for before, after in zip(existing, resolved):
            if after["category_confidence"] == "CONFIRMED":
                self.db.update_signal_category(
                    int(before["signal_id"]), after["flight_category"],
                    "CONFIRMED",
                )
                upgraded += 1
        return upgraded

    # ------------------------------------------------------------------
    # Lodging: two-stage, two-window
    # ------------------------------------------------------------------

    def _collect_lodging(
        self, iteration_id: int, query_id: int, query: Mapping[str, Any],
        connector: Any, params: Mapping[str, Any],
    ) -> tuple[int, int]:
        """Fixed listing set, then availability for the near and baseline windows.

        The listing set is cached because /search is asynchronous and slow — one
        city measured at 125 seconds — and because a *fixed* set is what makes
        the two windows comparable at all. A set that shifted between windows
        would measure catalogue churn, not availability.
        """
        cfg = self.config.get("staying", {})
        windows_cfg = self.config.get("windows", {})
        platform = (cfg.get("platforms") or ["airbnb"])[0]
        location = params.get("location", "")

        cache_key = f"{platform}|{location}"
        listings = self.db.get_geo_cache("LISTING_SET", cache_key)
        if listings is None:
            search = connector.search_listings(
                {**params, "platforms": platform},
                iteration_id=iteration_id, query_id=query_id,
            )
            self._log_provider_warnings(iteration_id, "search", search)
            listings = search.records
            self.db.put_geo_cache(
                "LISTING_SET", cache_key, listings, resolved_by="API",
                ttl_days=int(cfg.get("listing_set_ttl_days", 14)),
            )

        listing_ids = [
            l["listing_id"] for l in listings
            if l.get("platform") == platform and l.get("listing_id")
        ]
        if not listing_ids:
            raise SkipQuery("NO_LISTING_SET",
                            f"no {platform} listings for {location!r}")

        names = {l["listing_id"]: l.get("name", "") for l in listings}
        anchor = utcnow().date()
        near_hours = int(windows_cfg.get("near_term_hours", 48))
        baseline_days = list(windows_cfg.get("baseline_days", [7, 14]))

        near_window, _ = st.window_dates(
            anchor, near_hours=near_hours, baseline_days=baseline_days[0]
        )
        near_rows, near_cached = self._availability(
            connector, iteration_id, query_id, platform, listing_ids, near_window
        )

        # The first baseline that yields a usable paired sample wins; both are
        # weekday-aligned with the near window, and divergence between them is
        # itself informative.
        signals: list[dict[str, Any]] = []
        for offset in baseline_days:
            _, base_window = st.window_dates(
                anchor, near_hours=near_hours, baseline_days=offset
            )
            base_rows, base_cached = self._availability(
                connector, iteration_id, query_id, platform, listing_ids,
                base_window,
            )
            signals = st.availability_signal(near_rows, base_rows,
                                             listing_names=names)
            if len(signals) >= int(cfg.get("min_paired_listings", 3)):
                break

        raw_id = self._store_raw(
            iteration_id, query_id, "LODGING", "STAYING",
            {"platform": platform, "listing_count": len(listing_ids),
             "paired": len(signals), "signals": signals},
        )

        minimum = int(cfg.get("min_paired_listings", 3))
        if len(signals) < minimum:
            # A drop computed from one or two listings is arithmetic, not
            # evidence. Measured coverage is roughly 1 in 15 listings returning
            # calendar data, so a thin sample is common and must not be scored.
            raise SkipQuery(
                "THIN_PAIRED_SAMPLE",
                f"only {len(signals)} listing(s) paired across both windows, "
                f"need {minimum}")

        written = 0
        # A lodging signal is a COMPARISON of two windows, so if either side
        # came from the vendor's store the comparison rests on a stored copy.
        # Calling the pair cached when only one half was is the conservative
        # direction, and the only one that does not overstate freshness.
        acquisition = self._acquisition(query,
                                        cached=near_cached or base_cached)
        for signal in signals:
            written += self._insert_booking_signal(
                iteration_id, raw_id, "LODGING", query, signal, acquisition
            )
        return len(signals), written

    def _collect_lodging_price(
        self, iteration_id: int, query_id: int, query: Mapping[str, Any],
        connector: Any, params: Mapping[str, Any],
    ) -> tuple[int, int]:
        """Prices for the near and baseline windows on the PINNED listing set.

        **The same set the availability path pins**, read from the same
        `LISTING_SET` cache key. That is the whole point of direct mode: the two
        lodging sub-signals then measure the same properties by construction,
        and a price change is about a listing whose availability is also known.
        Google mode resolved a location string independently per window and
        produced, measured live, a different hotel each time.

        **Both windows are shifted by `staying.price_lead_days`.** Measured
        live: no listing in a 6-listing sample could be priced for a check-in
        today or tomorrow — the call fails `all_actors_failed` and charges
        nothing — while +2 days onward priced normally. Shifting *both* windows
        by the same amount preserves the weekday alignment `baseline_days`
        exists for, at the cost of the price sub-signal describing a horizon two
        days later than the availability one. That difference is real and is
        recorded on the stored payload rather than hidden.

        Writes LODGING-family signals with the price columns populated and the
        availability columns left NULL — a zero there would read as total
        scarcity.
        """
        cfg = self.config.get("staying", {})
        windows_cfg = self.config.get("windows", {})
        location = params.get("location", "")
        platform = (cfg.get("platforms") or ["airbnb"])[0]

        lead = int(cfg.get("price_lead_days", 2))
        anchor = utcnow().date() + timedelta(days=lead)
        near_hours = int(windows_cfg.get("near_term_hours", 48))
        baseline_days = list(windows_cfg.get("baseline_days", [7, 14]))
        near_window, base_window = st.window_dates(
            anchor, near_hours=near_hours, baseline_days=baseline_days[0]
        )

        # The availability path's set, not a second one. If it has not been
        # resolved yet there is nothing to price: discovery is that path's job
        # and duplicating it here would pay for /search twice.
        listings = self.db.get_geo_cache("LISTING_SET", f"{platform}|{location}")
        pinned = [
            f"{item['platform']}:{item['listing_id']}"
            for item in (listings or [])
            if item.get("platform") == platform and item.get("listing_id")
        ]
        if len(pinned) < 2:
            raise SkipQuery(
                "THIN_PAIRED_SAMPLE",
                f"no pinned listing set for {location!r} (direct-mode pricing "
                f"needs at least 2 listings and the availability path had "
                f"{len(pinned)})")
        # Deterministic prefix, not a sample: the same listings must be priced
        # every iteration or the series compares different properties over time,
        # which is the confound this whole path is built to avoid.
        pinned = pinned[:max(2, int(cfg.get("price_max_listings", 6)))]

        near_rows, near_cached = self._price_window(
            connector, iteration_id, query_id, location, near_window, pinned
        )
        base_rows, base_cached = self._price_window(
            connector, iteration_id, query_id, location, base_window, pinned
        )
        signals = st.price_signal(near_rows, base_rows)

        raw_id = self._store_raw(
            iteration_id, query_id, "LODGING_PRICE", "STAYING",
            {"location": location, "pinned": pinned, "signals": signals,
             "near_window": [d.isoformat() for d in near_window],
             "baseline_window": [d.isoformat() for d in base_window],
             "price_lead_days": lead},
        )
        if not signals:
            raise SkipQuery(
                "THIN_PAIRED_SAMPLE",
                f"no listing was priced in BOTH windows ({len(near_rows)} near, "
                f"{len(base_rows)} baseline); comparing different properties "
                "would measure the gap between them, not a change",
            )

        written = 0
        acquisition = self._acquisition(query,
                                        cached=near_cached or base_cached)
        for signal in signals:
            written += self._insert_booking_signal(
                iteration_id, raw_id, "LODGING", query, signal, acquisition
            )
        return len(signals), written

    def _price_window(
        self, connector: Any, iteration_id: int, query_id: int, location: str,
        window: tuple[date, date], pinned: Sequence[str],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Price the pinned listing set for one window, in direct mode.

        Returns the rows and whether ANY batch came from the vendor's cache
        (9.4). One cached batch makes the window partly a stored copy, and the
        30-credit charge is identical either way — so nothing else in the
        record would show it.

        Batched because `/price-compare` takes 2–6 `platform:listingId` pairs
        per call. Measured live, the charge is **3 credits per call regardless
        of whether it carries 2 listings or 6**, so the batch size is a pure
        cost saving: a 40-listing set costs 7 calls rather than 20.

        A batch that returns nothing is not an error. `all_actors_failed` is
        charged 0 and happens for a whole batch when the platform cannot quote
        those dates; individual listings also drop out of an otherwise good
        response — 6 in, 4 offers back, measured. Both are partial coverage and
        `price_signal` pairs only what appears in both windows.
        """
        start, end = window
        size = max(2, min(6, int(
            (self.config.get("staying") or {}).get("price_batch_size", 6))))
        rows: list[dict[str, Any]] = []
        cached = False
        for index in range(0, len(pinned), size):
            batch = list(pinned[index:index + size])
            if len(batch) < 2:
                # The endpoint refuses a single listing with 400. A trailing
                # odd one is dropped rather than sent to fail.
                self._log(
                    "INFO",
                    f"price-compare {start}: dropped a trailing single listing; "
                    f"the endpoint requires at least 2",
                    iteration_id=iteration_id)
                break
            result = connector.price_compare(
                {"listings": ",".join(batch),
                 "checkIn": start.isoformat(), "checkOut": end.isoformat()},
                iteration_id=iteration_id, query_id=query_id,
            )
            self._log_provider_warnings(
                iteration_id, f"price-compare {start}", result
            )
            cached = cached or result.cached
            rows.extend(result.records)
        return rows, cached

    def _availability(
        self, connector: Any, iteration_id: int, query_id: int, platform: str,
        listing_ids: Sequence[str], window: tuple[date, date],
    ) -> list[dict[str, Any]]:
        start, end = window
        result = connector.availability(
            {
                "platform": platform,
                "listingIds": ",".join(listing_ids),
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
            },
            iteration_id=iteration_id, query_id=query_id,
        )
        self._log_provider_warnings(iteration_id, f"availability {start}", result)
        return result.records, result.cached

    def _log_provider_warnings(
        self, iteration_id: int, label: str, result: Any,
    ) -> None:
        """Record what the provider said about how it obtained the result.

        A platform the caller requested but that was not searched raises in the
        connector. This handles the rest: warnings about legs we did not ask for,
        and the provider's own `partial` flag. Both are informational, but they
        belong on the record — a result the provider itself calls incomplete
        should not look identical to one it calls complete.
        """
        warnings = getattr(result, "warnings", None) or []
        for warning in warnings:
            self._log(
                "WARNING",
                f"Staying {label}: {warning.get('code', 'warning')} — "
                f"{warning.get('message', '')}",
                iteration_id=iteration_id,
                platform=warning.get("platform"), param=warning.get("param"),
            )
        if getattr(result, "partial", False):
            self._log("WARNING",
                      f"Staying {label}: provider reported a PARTIAL result set",
                      iteration_id=iteration_id)

    # ------------------------------------------------------------------
    # Rental cars: two windows
    # ------------------------------------------------------------------

    def _collect_car(
        self, iteration_id: int, query_id: int, query: Mapping[str, Any],
        connector: Any, params: Mapping[str, Any],
    ) -> tuple[int, int]:
        """Near-term and baseline searches at the same pickup point."""
        cfg = self.config.get("priceline", {})
        windows_cfg = self.config.get("windows", {})
        pickup = params.get("pickUpLocation", "")

        near = connector.search_rental_cars(
            params, iteration_id=iteration_id, query_id=query_id
        )
        offset = int((windows_cfg.get("baseline_days") or [7])[0])
        rental_days = int(cfg.get("rental_days", 2))
        base_start = utcnow().date() + timedelta(days=offset)
        # The SAME six parameters with only the dates moved. The key names are
        # the vendor's camelCase (8.5): spelling them the old way here left the
        # originals in place and silently queried the near window twice, which
        # produced a perfect 0% drop from two identical windows.
        baseline_params = {
            **params,
            "pickUpDate": base_start.isoformat(),
            "dropOffDate": (base_start + timedelta(days=rental_days)).isoformat(),
        }
        baseline = connector.search_rental_cars(
            baseline_params, iteration_id=iteration_id, query_id=query_id
        )

        # A baseline exists to establish the normal level. Zero total
        # availability in it does not establish a low one — it establishes
        # nothing, and there is no denominator to compute a drop against.
        #
        # Measured live 2026-08-09 (8.3): every PHX and JFK window returned
        # `success: true` with `totalResultsAvailable: 0`, where an earlier run
        # had measured 272 offers. Left alone this is the fail-loud contract's
        # exact adversary — `car_signal_rows` emits only classes present in
        # BOTH windows, so an all-zero feed yields zero rows, zero signals, and
        # a query still marked COMPLETE. CAR would then report FULL coverage
        # having observed nothing, and a dead feed would read as a quiet market.
        #
        # Raised rather than returned empty, so the query is FAILED, CAR joins
        # the coverage gaps, the band is capped and the caveat names it.
        if not baseline.get("total_results_available"):
            raise PlatformUnavailableError(
                f"Priceline returned zero total availability for the {pickup} "
                f"BASELINE window ({baseline_params['pickUpDate']}). No "
                "denominator, so no drop can be computed; recording this as a "
                "zero drop would report a dead feed as a healthy market."
            )

        rows = pl.car_signal_rows(near, baseline, pickup=pickup)
        raw_id = self._store_raw(
            # Derived, not hard-coded: a literal here silently mis-attributes
            # the payload (and its retention deadline) the moment the CAR
            # family changes provider. Found while mapping the 8.5 swap.
            iteration_id, query_id, "CAR",
            provider_for_endpoint(query["endpoint"]),
            {"pickup": pickup,
             "near_total": near.get("total_results_available"),
             "base_total": baseline.get("total_results_available"),
             "near_truncated": near.get("truncated"),
             "rows": rows},
        )
        written = 0
        acquisition = self._acquisition(query)
        for row in rows:
            written += self._insert_booking_signal(
                iteration_id, raw_id, "CAR", query, row, acquisition
            )
        return len(rows), written

    def _insert_booking_signal(
        self, iteration_id: int, raw_id: int, signal_type: str,
        query: Mapping[str, Any], row: Mapping[str, Any],
        acquisition: Mapping[str, str] | None = None,
    ) -> int:
        """Write one LODGING or CAR signal row, tolerating duplicates."""
        import sqlite3

        fields = {
            k: row[k] for k in (
                "provider_ref", "item_name", "near_available", "near_total",
                "base_available", "base_total", "drop_pct", "price_near",
                "price_baseline", "discount_pct_near", "discount_pct_base",
                "distance_km", "truncated", "vehicle_class",
                "vehicle_class_name", "people_capacity", "bag_capacity",
                "partner_code", "partner_name", "counter_type",
                "is_on_airport", "is_peer_to_peer", "field_map_ver",
            ) if k in row
        }
        try:
            self.db.insert_signal(
                iteration_id=iteration_id, raw_id=raw_id,
                signal_type=signal_type, city_id=query["city_id"],
                location_id=query["location_id"],
                observed_at=utcnow(), quality=1.0,
                **(acquisition or {}), **fields,
            )
            return 1
        except sqlite3.IntegrityError:
            return 0
