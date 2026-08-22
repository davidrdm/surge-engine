"""Dataclasses for the API boundary.

Derived from surge/models.py. Three deliberate changes:

  * FlightRecord carries `eta`, `status`, `category` and `category_confidence`.
    The old CharterFlight had none of them, so the pipeline collected an ETA,
    asked the model to return it, and then dropped it at parse time — destroying
    the "earliest possible warning" premise it was built on.

  * LodgingSignal and CarSignal replace HotelOccupancy, and both carry the price
    fields that the old code computed and then discarded.

  * Alert.as_tuple() returns the four-part evidence tuple the requirement asks
    for: (social posts, flights, lodging changes, car availability). JSON has no
    tuple type, so the REST layer serialises named arrays by default and strict
    positional arrays under ?format=tuple; this method is the canonical Python
    form both are built from.

These are a presentation layer over database rows, not a second source of truth.
Rows are authoritative; from_row() adapts.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence


def _get(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Read a key from a sqlite3.Row or dict, tolerating absence."""
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


@dataclass
class SocialPost:
    url: str = ""
    author: str = ""
    platform: str = ""
    source_domain: str = ""
    observed_at: str = ""
    snippet: str = ""
    salience: float = 0.0
    activity_type: str = ""
    imminence_hours: float | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "SocialPost":
        return cls(
            url=_get(row, "url", ""),
            author=_get(row, "author", ""),
            platform=_get(row, "platform", ""),
            source_domain=_get(row, "source_domain", ""),
            observed_at=_get(row, "observed_at", ""),
            snippet=_get(row, "snippet", ""),
            salience=float(_get(row, "salience", 0.0)),
            activity_type=_get(row, "activity_type", ""),
            imminence_hours=_get(row, "imminence_hours"),
        )


@dataclass
class FlightRecord:
    callsign: str = ""
    registration: str = ""
    aircraft_type: str = ""
    origin_iata: str = ""
    dest_iata: str = ""
    operating_as: str = ""
    # 'M', 'J', 'T', 'H', or 'AMBIGUOUS'. AMBIGUOUS means the record came from
    # the live-positions endpoint, whose response carries no category field at
    # all — so the category was never knowable, not merely unread.
    category: str = ""
    category_confidence: str = ""
    status: str = ""
    eta: str | None = None
    fr24_id: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "FlightRecord":
        return cls(
            callsign=_get(row, "callsign", ""),
            registration=_get(row, "registration", ""),
            aircraft_type=_get(row, "aircraft_type", ""),
            origin_iata=_get(row, "origin_iata", ""),
            dest_iata=_get(row, "dest_iata", ""),
            operating_as=_get(row, "operating_as", ""),
            category=_get(row, "flight_category", ""),
            category_confidence=_get(row, "category_confidence", ""),
            status=_get(row, "flight_status", ""),
            eta=_get(row, "eta"),
            fr24_id=_get(row, "fr24_id", ""),
        )


@dataclass
class LodgingSignal:
    """Availability change for one listing, near-term versus baseline.

    near_available / near_total are counts of available and offered nights, from
    Staying's /availability per-date booleans — a real availability ratio rather
    than the offer count the Amadeus implementation used as a proxy for one.
    """

    listing_ref: str = ""
    name: str = ""
    location_name: str = ""
    near_available: int = 0
    near_total: int = 0
    base_available: int = 0
    base_total: int = 0
    drop_pct: float = 0.0
    price_near: float | None = None
    price_baseline: float | None = None
    distance_km: float | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "LodgingSignal":
        return cls(
            listing_ref=_get(row, "provider_ref", ""),
            name=_get(row, "item_name", ""),
            near_available=int(_get(row, "near_available", 0)),
            near_total=int(_get(row, "near_total", 0)),
            base_available=int(_get(row, "base_available", 0)),
            base_total=int(_get(row, "base_total", 0)),
            drop_pct=float(_get(row, "drop_pct", 0.0)),
            price_near=_get(row, "price_near"),
            price_baseline=_get(row, "price_baseline"),
            distance_km=_get(row, "distance_km"),
        )


@dataclass
class CarSignal:
    """Rental availability for one pickup counter and vehicle class.

    people_capacity is the field that makes this signal worth more than a count:
    moving people needs seats, so scarcity in twelve-seat vans reads very
    differently from the same proportional scarcity in economy sedans.
    """

    pickup: str = ""
    vehicle_class: str = ""
    vehicle_class_name: str = ""
    people_capacity: int | None = None
    partner_name: str = ""
    counter_type: str = ""
    is_on_airport: bool = False
    near_available: int = 0
    near_total: int = 0
    base_available: int = 0
    base_total: int = 0
    drop_pct: float = 0.0
    price_near: float | None = None
    price_baseline: float | None = None
    distance_km: float | None = None
    truncated: bool = False

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CarSignal":
        return cls(
            pickup=_get(row, "provider_ref", ""),
            vehicle_class=_get(row, "vehicle_class", ""),
            vehicle_class_name=_get(row, "vehicle_class_name", ""),
            people_capacity=_get(row, "people_capacity"),
            partner_name=_get(row, "partner_name", ""),
            counter_type=_get(row, "counter_type", ""),
            is_on_airport=bool(_get(row, "is_on_airport", 0)),
            near_available=int(_get(row, "near_available", 0)),
            near_total=int(_get(row, "near_total", 0)),
            base_available=int(_get(row, "base_available", 0)),
            base_total=int(_get(row, "base_total", 0)),
            drop_pct=float(_get(row, "drop_pct", 0.0)),
            price_near=_get(row, "price_near"),
            price_baseline=_get(row, "price_baseline"),
            distance_km=_get(row, "distance_km"),
            truncated=bool(_get(row, "truncated", 0)),
        )


AlertTuple = tuple[
    list[SocialPost], list[FlightRecord], list[LodgingSignal], list[CarSignal]
]


@dataclass
class Alert:
    """One correlated finding, with its evidence and a confidence score."""

    alert_id: int
    city: str
    track: str
    confidence_score: float
    confidence_band: str
    summary: str
    created_at: str
    caveat: str | None = None
    earliest_eta: str | None = None
    social_posts: list[SocialPost] = field(default_factory=list)
    flights: list[FlightRecord] = field(default_factory=list)
    lodging: list[LodgingSignal] = field(default_factory=list)
    rental_cars: list[CarSignal] = field(default_factory=list)

    def as_tuple(self) -> AlertTuple:
        """The four-part evidence tuple, in the order the requirement specifies.

        (social media posts, military/charter flights, hotel availability
        changes, car availability). Mirrors SurgeResult.as_tuple() in the old
        models.py, extended from three parts to four.
        """
        return (self.social_posts, self.flights, self.lodging, self.rental_cars)

    def as_dict(self) -> dict[str, Any]:
        """Named-array form, for the default JSON response."""
        return asdict(self)

    def as_positional(self) -> list[list[dict[str, Any]]]:
        """Strict positional form, for ?format=tuple.

        Callers that asked for tuples get four arrays in a fixed order rather
        than an object whose keys they would have to trust.
        """
        return [
            [asdict(item) for item in group] for group in self.as_tuple()
        ]

    @classmethod
    def from_rows(
        cls,
        alert_row: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]],
    ) -> "Alert":
        """Build an Alert from its row plus the signals linked to its correlation."""
        city = _get(alert_row, "city_name", "")
        state = _get(alert_row, "city_state", "")
        alert = cls(
            alert_id=int(alert_row["alert_id"]),
            city=f"{city}, {state}" if state else city,
            track=_get(alert_row, "track", ""),
            confidence_score=float(_get(alert_row, "confidence_score", 0.0)),
            confidence_band=_get(alert_row, "confidence_band", ""),
            summary=_get(alert_row, "summary", ""),
            created_at=_get(alert_row, "created_at", ""),
            caveat=_get(alert_row, "caveat"),
            earliest_eta=_get(alert_row, "earliest_eta"),
        )
        for row in evidence:
            signal_type = _get(row, "signal_type", "")
            if signal_type == "SOCIAL":
                alert.social_posts.append(SocialPost.from_row(row))
            elif signal_type == "FLIGHT":
                alert.flights.append(FlightRecord.from_row(row))
            elif signal_type == "LODGING":
                alert.lodging.append(LodgingSignal.from_row(row))
            elif signal_type == "CAR":
                alert.rental_cars.append(CarSignal.from_row(row))
        return alert
