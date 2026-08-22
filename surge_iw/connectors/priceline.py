"""Rental car availability from Priceline, via RapidAPI.

Three things about this provider need stating up front.

**Which wrapper (8.5).** The upstream is Priceline; the reseller in front of it
is `priceline-com2`, reached at `/cars/search`. It replaced `priceline8`
(`/search-rental-car`), which began returning `success: true` with
`totalResultsAvailable: 0` for every airport and every window. The data, the
rights position and the retention terms are unchanged — only the reseller
moved — so the billing provider is still PRICELINE. The old paths survive as
`LEGACY_EP_*` and stay mapped in `budget.provider_for_endpoint`, so an operator
reading a pre-8.5 `query_queue` or `api_calls` row can still resolve it.

**Documented paths are not to be trusted on this vendor.** Its marketing page
advertised `GET /cars` and `GET /autocomplete-location-search`; neither ever
existed. That is why 8.5 chose the replacement by probing six candidates live
rather than by reading their listings.

**The vehicle array is declared untyped in every wrapper's specification.** Its
shape is known only from captured live responses, so the vendor can change it
without notice. Every field this module reads is therefore either
required-and-checked or explicitly optional, and `signals.field_map_ver` stamps
which mapping produced a row so data collected under an older shape stays
identifiable.

The guiding rule for what raises and what degrades:

  * Raise when defaulting would FABRICATE EVIDENCE. `totalResultsAvailable`
    missing must not become 0, because 0 reads as total scarcity and scarcity is
    what this system alerts on.
  * Degrade when defaulting is CONSERVATIVE. `peopleCapacity` missing becomes
    None, which scoring weights as 1 — under-weighting the signal, which is the
    safe direction.

Two response fields make this signal better than counting offers:
`totalResultsAvailable` is a first-class count of matching vehicles, and
`resultsCount` reports how many were actually returned, so truncation is
detectable rather than silently scored as scarcity.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..base.connector import BaseConnector, SchemaError, require

# 8.5 — the wrapper moved, the upstream did not.
#
# `priceline8.p.rapidapi.com/search-rental-car` began returning `success: true`
# with `totalResultsAvailable: 0` for every airport and every window (measured
# 2026-08-09 at PHX and JFK, +1/+2/+7/+14 days), where it had previously
# returned 272 offers. Probing five alternative RapidAPI wrappers found the
# SAME Priceline upstream, unchanged, behind `priceline-com2`: 510 vehicles for
# the near window and 758 for the baseline, with `peopleCapacity`,
# `counterType`, `distanceFromSearchLocation`, `partner.code` and
# `pickupLocation.locationId` all intact.
#
# So this is a wrapper swap, not a provider swap. The billing provider stays
# PRICELINE because the data, the rights position and the retention terms are
# unchanged — only the reseller in front of it moved.
EP_CARS = "/cars/search"
EP_AUTOCOMPLETE = "/cars/auto-complete"
EP_PARTNERS = "/cars/partners"

#: Legacy paths on the priceline8 wrapper, kept so an operator reading an old
#: `query_queue` row or an old `api_calls` row can still resolve what was called.
LEGACY_EP_CARS = "/search-rental-car"
LEGACY_EP_AUTOCOMPLETE = "/auto-complete-location"

# Bumped whenever the vehicles[] mapping changes, and stamped onto every signal
# so rows produced by an older mapping remain identifiable after a vendor change.
FIELD_MAP_VERSION = "2026-08-10"

# Counter types that indicate an on-airport or airport-shuttle location. Airport
# fleets book out before off-airport ones, which makes them the leading
# indicator; scoring weights them higher.
_ON_AIRPORT_COUNTER_TYPES = frozenset({
    "ON_AIRPORT", "ON_AIR_SHUTTLE", "IN_TERMINAL", "TERMINAL", "AIRPORT",
})


class PricelineConnector(BaseConnector):
    """Rental car availability, keyed on airport IATA codes."""

    provider = "PRICELINE"

    def __init__(
        self,
        api_key: str,
        *,
        host: str = "priceline-com2.p.rapidapi.com",
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.host = host
        super().__init__(api_key, base_url=base_url or f"https://{host}", **kwargs)

    @property
    def name(self) -> str:
        return "Priceline (priceline-com2)"

    def auth_headers(self) -> dict[str, str]:
        # x-rapidapi-key is the RapidAPI platform standard. It is the one header
        # name not literally confirmed in the fetched listing HTML; the live
        # smoke test is what verifies it.
        return {
            "x-rapidapi-key": self._api_key,
            "x-rapidapi-host": self.host,
        }

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    def search_rental_cars(
        self,
        params: Mapping[str, Any],
        *,
        iteration_id: int | None = None,
        query_id: int | None = None,
    ) -> dict[str, Any]:
        """Search one pickup point and window.

        Returns the availability totals plus per-(class, counter) rows. Vehicles
        that fail to normalise are counted rather than silently dropped, and too
        many failures raise — a vendor schema change must surface as a loud
        FAILED query, never as thin inventory.
        """
        response = self._request(
            EP_CARS, params=params,
            count_records=lambda data: 1,   # billed per request, not per vehicle
            iteration_id=iteration_id, query_id=query_id,
        )
        return parse_rental_car_response(response.data)

    def autocomplete_location(
        self,
        location: str,
        *,
        iteration_id: int | None = None,
        query_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve a place name to coordinates and an airport code.

        Only needed for cities with no airport mapping in services/geo.py. The
        `id`/`cityID` values this returns are hotel/city identifiers and are NOT
        documented as accepted by the car search endpoint, so callers hand off the
        airport code or `lat,lon` instead.
        """
        response = self._request(
            EP_AUTOCOMPLETE, params={"location": location},
            count_records=lambda data: 1,
            iteration_id=iteration_id, query_id=query_id,
        )
        payload = response.data
        data = payload.get("data") if isinstance(payload, Mapping) else None
        items = data.get("locationData") if isinstance(data, Mapping) else None
        if not isinstance(items, list):
            raise SchemaError(
                "Priceline autocomplete returned no data.locationData list",
                provider=self.provider, endpoint=EP_AUTOCOMPLETE,
            )
        return [
            {
                "name": item.get("itemName", ""),
                "type": item.get("type", ""),
                "airport_code": item.get("airportCode") or "",
                "city_name": item.get("cityName", ""),
                "state_code": item.get("stateCode", ""),
                "lat": item.get("lat"),
                "lon": item.get("lon"),
            }
            for item in items if isinstance(item, Mapping)
        ]

    def health_check(self) -> dict[str, Any]:
        """Probe the endpoint collection actually depends on.

        Autocomplete was the cheaper probe and it measured the wrong thing. On
        the previous wrapper `/auto-complete-location` returned HTTP 500 while
        the search path returned 780 offers for the same airport on the same
        key, so `/healthz` reported the car family down while it was working —
        the cry-wolf failure that made `healthy` tri-state. Probing the search
        path also means the check would have caught the 8.5 outage, where the
        search path itself was the thing that broke.

        It costs one request against a 100,000/month plan, and it is only ever
        called from an authenticated `/healthz?deep=true`.
        """
        from datetime import timedelta

        from ..db.database import utcnow

        start = (utcnow() + timedelta(days=1)).date().isoformat()
        end = (utcnow() + timedelta(days=3)).date().isoformat()
        try:
            result = self.search_rental_cars({
                "pickUpLocation": "PHX", "dropOffLocation": "PHX",
                "pickUpDate": start, "dropOffDate": end,
                "pickUpTime": "12:00", "dropOffTime": "12:00",
            })
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            return {"provider": self.provider, "healthy": False,
                    "detail": str(exc)}
        return {
            "provider": self.provider, "healthy": True,
            "detail": f"{EP_CARS} returned "
                      f"{result.get('results_count', 0)} offer(s)",
            "field_map_ver": result.get("field_map_ver"),
        }


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_rental_car_response(payload: Any) -> dict[str, Any]:
    """Validate the response envelope and normalise every vehicle offer."""
    if not isinstance(payload, Mapping):
        raise SchemaError(
            f"Priceline returned {type(payload).__name__}, expected an object",
            provider="PRICELINE", endpoint=EP_CARS,
        )
    # Both wrappers report failure, in different words. `success: false` is the
    # priceline8 form; `status: false` is priceline-com2's.
    if payload.get("success") is False or payload.get("status") is False:
        raise SchemaError(
            f"Priceline reported failure: {payload.get('message', 'no message')}",
            provider="PRICELINE", endpoint=EP_CARS,
        )
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise SchemaError(
            "Priceline response has no 'data' object",
            provider="PRICELINE", endpoint=EP_CARS,
        )

    vehicles = data.get("vehicles")
    if not isinstance(vehicles, list):
        raise SchemaError(
            "Priceline 'vehicles' was not a list",
            provider="PRICELINE", endpoint=EP_CARS,
        )

    # One row per distinct OFFER, and the count that goes with it.
    #
    # `data.vehicles` carries the same car once per rate plan — measured live,
    # 510 rows for 348 distinct `id`s, differing only in `groupId`, `rate` and
    # `score`. Counting rows would inflate every availability figure, so the
    # list is de-duplicated on the vendor's own `id`, which is date-free and
    # therefore stable across the two windows.
    #
    # `data.vehiclesFinal` is NOT the de-duplicated list, despite looking like
    # one. Measured: it held 170 of the 494 retail offers plus the same 16
    # opaque "express" deals, and those carry no `partner`, so keying on it
    # both undercounted availability by about two thirds and produced offer
    # keys with an unknown supplier. It is deliberately not used.
    seen_ids: set[str] = set()
    distinct: list[Any] = []
    for vehicle in vehicles:
        if not isinstance(vehicle, Mapping):
            distinct.append(vehicle)          # let the normaliser reject it
            continue
        identifier = vehicle.get("id")
        if identifier:
            if identifier in seen_ids:
                continue
            seen_ids.add(str(identifier))
        distinct.append(vehicle)
    vehicles = distinct

    # The availability count, from whichever wrapper is answering.
    #
    # priceline8 published `totalResultsAvailable`/`resultsCount` on `data`, and
    # those are authoritative where present. priceline-com2's `meta.totalRecords`
    # counts `vehiclesFinal`, a different list from the one measured above, so it
    # is NOT used as the total — a total describing a set we did not count would
    # make `truncated` meaningless.
    #
    # Absent both, the total is the number of distinct offers actually parsed.
    # That is not a silent default: `vehicles` missing already raised above, and
    # an empty list is a genuine zero that the baseline guard in
    # CollectionAgent._collect_car turns into a coverage gap rather than a drop.
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    legacy_shape = "totalResultsAvailable" in data or "resultsCount" in data
    if legacy_shape:
        # Either both fields or neither. A payload carrying one of them is the
        # legacy shape with a field missing, and falling back to a counted
        # total there would silently restore the very default this guard
        # exists to forbid.
        total = int(require(
            data, "totalResultsAvailable", context="rental car search",
            provider="PRICELINE", endpoint=EP_CARS,
        ))
        returned = int(require(
            data, "resultsCount", context="rental car search",
            provider="PRICELINE", endpoint=EP_CARS,
        ))
    else:
        total = returned = len(vehicles)

    # A further page means the response was cut, and a cut must never read as
    # scarcity. priceline8 signalled that as resultsCount < total; this wrapper
    # paginates explicitly.
    paged = int(meta.get("totalPage") or 1) > int(meta.get("currentPage") or 1)

    offers: list[dict[str, Any]] = []
    skipped = 0
    for vehicle in vehicles:
        if not isinstance(vehicle, Mapping):
            skipped += 1
            continue
        try:
            offers.append(normalise_car_offer(vehicle))
        except SchemaError:
            skipped += 1

    # One malformed row is noise; a systematic failure is a schema change, and a
    # schema change must not present as an empty lot.
    if vehicles and skipped > max(1, len(vehicles) // 5):
        raise SchemaError(
            f"Priceline vehicles[]: {skipped} of {len(vehicles)} offers failed "
            f"to normalise. The vendor schema has probably changed; refusing to "
            f"report the remainder as availability.",
            provider="PRICELINE", endpoint=EP_CARS,
        )

    return {
        "total_results_available": total,
        "results_count": returned,
        # Truncation must be detectable, or a pagination cut reads as scarcity.
        "truncated": returned < total or paged,
        "skipped": skipped,
        "offers": offers,
        "field_map_ver": FIELD_MAP_VERSION,
    }


def normalise_car_offer(vehicle: Mapping[str, Any]) -> dict[str, Any]:
    """One `vehicles[]` element -> the fields the car signal needs.

    Required: a class code and a pickup location. Without them the offer cannot
    be attributed to a vehicle class or a counter, and an unattributable offer
    cannot be compared across windows.

    Everything else degrades to None, because under-weighting a real signal is
    safe and fabricating one is not.
    """
    code = require(
        vehicle, "code", context="vehicle offer",
        provider="PRICELINE", endpoint=EP_CARS,
    )
    pickup = vehicle.get("pickupLocation")
    if not isinstance(pickup, Mapping):
        raise SchemaError(
            "vehicle offer has no pickupLocation object",
            provider="PRICELINE", endpoint=EP_CARS,
        )

    features = vehicle.get("vehicleFeatures")
    features = features if isinstance(features, Mapping) else {}
    partner = vehicle.get("partner")
    partner = partner if isinstance(partner, Mapping) else {}

    counter_type = str(pickup.get("counterType") or "").upper()
    rate = _first_rate(vehicle.get("rate"))
    nights = vehicle.get("numRentalDays")

    total_price = _as_float(rate.get("totalPrice"))
    # dailyPrice came back as 0 in the captured live sample while totalPrice was
    # populated, so it is derived rather than trusted.
    daily_price = (
        total_price / float(nights)
        if total_price is not None and _as_float(nights) else None
    )

    return {
        # Stable across windows. itemKey is NOT usable as identity: it encodes
        # the pickup and dropoff datetimes, so the same car in the near and
        # baseline windows would have two different itemKeys.
        "offer_key": f"{partner.get('code') or '?'}|{code}|"
                     f"{pickup.get('locationId') or pickup.get('airportCode') or '?'}",
        "vehicle_class": str(code),
        # Both spellings: priceline8 used nameDisplay/exampleDisplay,
        # priceline-com2 uses name/example for the same values (8.5).
        "vehicle_class_name": (vehicle.get("nameDisplay")
                               or vehicle.get("name") or ""),
        "example_vehicle": (vehicle.get("exampleDisplay")
                            or vehicle.get("example") or ""),
        "people_capacity": _as_int(features.get("peopleCapacity")),
        "bag_capacity": _as_int(features.get("bagCapacity")),
        "partner_code": partner.get("code") or "",
        "partner_name": partner.get("name") or "",
        "pickup_location_id": pickup.get("locationId") or "",
        "airport_code": pickup.get("airportCode") or "",
        "counter_type": counter_type,
        "is_on_airport": counter_type in _ON_AIRPORT_COUNTER_TYPES,
        # Pre-computed by the provider, so no haversine is needed for cars.
        # MILES -> KM. Measured 2026-08-10 against the counters' own published
        # coordinates: every PHX counter's `distanceFromSearchLocation` matched
        # the great-circle distance in miles, not kilometres (Easirent at 2410 S
        # Central reported 4.1 for a true 5.9 km / 3.7 mi). The field has been
        # stored as `distance_km` since Phase 2, so scoring has been comparing
        # miles against a 15 KM gate and treating counters as nearer than they
        # are — `spatially_anchored` was under-applying its 0.5x penalty.
        "distance_km": _miles_to_km(
            _as_float(pickup.get("distanceFromSearchLocation"))),
        "lat": _as_float(pickup.get("lat")),
        "lon": _as_float(pickup.get("lng")),
        "is_peer_to_peer": bool(vehicle.get("isPeerToPeer")),
        "total_price": total_price,
        "daily_price": daily_price,
        "strike_total_price": _as_float(rate.get("strikeTotalPrice")),
        # Discount depth collapses as demand rises, which is a useful secondary
        # signal alongside the availability count.
        "savings_percent": _as_float(rate.get("savingsPercent")),
        "currency": rate.get("currencyCode") or "",
        "rental_days": _as_int(nights),
        "field_map_ver": FIELD_MAP_VERSION,
        # checkoutUrl and detailsKey are deliberately NOT carried: they embed a
        # booking refCode and session tokens. services/redact.py also strips them
        # from the stored raw payload.
    }


def car_signal_rows(
    near: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    pickup: str,
) -> list[dict[str, Any]]:
    """Pair near-term and baseline searches into per-class signal rows.

    Aggregated by (vehicle class, counter) rather than per offer, because the
    same class from the same counter appears repeatedly at different rates and
    counting rates would overstate inventory. Only classes present in BOTH
    windows are emitted; a class appearing in one window only says the catalogue
    moved, not that availability did.
    """
    near_groups = _group_offers(near.get("offers", []))
    base_groups = _group_offers(baseline.get("offers", []))
    truncated = bool(near.get("truncated") or baseline.get("truncated"))

    rows: list[dict[str, Any]] = []
    for key in sorted(set(near_groups) & set(base_groups)):
        near_group = near_groups[key]
        base_group = base_groups[key]
        near_count = near_group["count"]
        base_count = base_group["count"]
        drop = (
            (base_count - near_count) / base_count * 100.0
            if base_count > 0 else 0.0
        )
        rows.append({
            "provider_ref": pickup,
            "item_name": near_group["name"],
            "vehicle_class": near_group["vehicle_class"],
            "vehicle_class_name": near_group["name"],
            "people_capacity": near_group["people_capacity"],
            "bag_capacity": near_group["bag_capacity"],
            "partner_code": near_group["partner_code"],
            "partner_name": near_group["partner_name"],
            "counter_type": near_group["counter_type"],
            "is_on_airport": near_group["is_on_airport"],
            "is_peer_to_peer": near_group["is_peer_to_peer"],
            "near_available": near_count,
            "base_available": base_count,
            # Search-level totals, repeated per row so truncation is visible
            # wherever a row is examined.
            "near_total": int(near.get("total_results_available") or 0),
            "base_total": int(baseline.get("total_results_available") or 0),
            "drop_pct": round(max(0.0, drop), 1),
            "price_near": near_group["price"],
            "price_baseline": base_group["price"],
            "discount_pct_near": near_group["savings"],
            "discount_pct_base": base_group["savings"],
            "distance_km": near_group["distance_km"],
            "truncated": truncated,
            "field_map_ver": FIELD_MAP_VERSION,
        })
    return rows


def _group_offers(offers: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for offer in offers:
        key = offer.get("offer_key") or ""
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = {
                "count": 1,
                "vehicle_class": offer.get("vehicle_class", ""),
                "name": offer.get("vehicle_class_name", ""),
                "people_capacity": offer.get("people_capacity"),
                "bag_capacity": offer.get("bag_capacity"),
                "partner_code": offer.get("partner_code", ""),
                "partner_name": offer.get("partner_name", ""),
                "counter_type": offer.get("counter_type", ""),
                "is_on_airport": offer.get("is_on_airport", False),
                "is_peer_to_peer": offer.get("is_peer_to_peer", False),
                "price": offer.get("total_price"),
                "savings": offer.get("savings_percent"),
                "distance_km": offer.get("distance_km"),
            }
            continue
        entry["count"] += 1
        price = offer.get("total_price")
        if price is not None and (entry["price"] is None or price < entry["price"]):
            entry["price"] = price
    return grouped


#: The vendor reports counter distance in statute miles (measured, not
#: documented). Scoring compares against `correlation.radius_km`.
_KM_PER_MILE = 1.609344


def _miles_to_km(value: float | None) -> float | None:
    """Convert a reported counter distance to kilometres, or None."""
    return None if value is None else round(value * _KM_PER_MILE, 3)


def _first_rate(rate: Any) -> Mapping[str, Any]:
    """The first rate option. The captured sample had exactly one."""
    if isinstance(rate, Sequence) and not isinstance(rate, (str, bytes)):
        for item in rate:
            if isinstance(item, Mapping):
                return item
    if isinstance(rate, Mapping):
        return rate
    return {}


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
