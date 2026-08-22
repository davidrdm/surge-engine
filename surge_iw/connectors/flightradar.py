"""Flight collection via the FlightRadar24 API.

Adapted from surge/connectors/flightradar.py. Three corrections, all verified
against FR24's OpenAPI specification.

1. `_get` caught every exception and returned []. An expired Bearer token was
   therefore indistinguishable from "no military aircraft inbound". Errors now
   raise typed ConnectorErrors, including 402 on credit exhaustion, which FR24
   returns and which the old code could not have surfaced.

2. Live position records were labelled `"M/J"` and then scored by rules that
   hinge on M. `FlightPositionsFull` returns exactly 22 fields and `category` is
   not among them — `categories` is a filter-only parameter. Live records are
   therefore emitted as AMBIGUOUS and can only be upgraded by corroboration from
   `/flight-summary/full`, which does return `category`.

3. `limit=200` on flight-summary implied a cost ceiling it does not provide.
   FR24 bills per RECORD RETURNED, not per request, and the limit parameter is
   documented as applying only to flight-positions endpoints. Cost is controlled
   by the /count tripwire instead: it costs 15% of the corresponding /full
   endpoint and answers "is there any qualifying traffic at all" for about one
   credit.

Sandbox note: sandbox and production share the base URL and paths, distinguished
only by the key. The sandbox returns static responses that IGNORE ALL QUERY
PARAMETERS. It proves parsing and auth; it cannot prove that `airports` or
`categories` are correct, because a malformed filter returns the same canned
payload. Parameter correctness needs live calls.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..base.connector import BaseConnector, SchemaError

EP_LIVE_FULL = "/api/live/flight-positions/full"
EP_LIVE_COUNT = "/api/live/flight-positions/count"
EP_SUMMARY_FULL = "/api/flight-summary/full"
EP_SUMMARY_COUNT = "/api/flight-summary/count"
EP_USAGE = "/api/usage"

# FR24 category codes, verified against the OpenAPI spec. Note T is GENERAL
# AVIATION and H is HELICOPTERS; the vendor's own MCP server README transposes
# these and is wrong.
CATEGORY_CODES: dict[str, str] = {
    "P": "PASSENGER",
    "C": "CARGO",
    "M": "MILITARY_AND_GOVERNMENT",
    "J": "BUSINESS_JETS",
    "T": "GENERAL_AVIATION",
    "H": "HELICOPTERS",
    "B": "LIGHTER_THAN_AIR",
    "G": "GLIDERS",
    "D": "DRONES",
    "V": "GROUND_VEHICLES",
    "O": "OTHER",
    "N": "NON_CATEGORIZED",
}
_NAME_TO_CODE = {name: code for code, name in CATEGORY_CODES.items()}
# The docs describe category as "e.g., Passenger, Cargo", so the field may carry
# a display name rather than a letter. Both are accepted.
_FRIENDLY_TO_CODE = {
    "passenger": "P", "cargo": "C", "military": "M",
    "military and government": "M", "government": "M",
    "business jet": "J", "business jets": "J",
    "general aviation": "T", "helicopter": "H", "helicopters": "H",
}

# Only these four are scored; anything else is real but irrelevant here.
SCORED_CATEGORIES = frozenset({"M", "J", "T", "H"})


class FlightRadarConnector(BaseConnector):
    """Live and historical flight collection."""

    provider = "FR24"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://fr24api.flightradar24.com",
        **kwargs: Any,
    ) -> None:
        super().__init__(api_key, base_url=base_url, **kwargs)

    @property
    def name(self) -> str:
        return "FlightRadar24"

    def auth_headers(self) -> dict[str, str]:
        # Accept-Version is required by the spec, not optional.
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept-Version": "v1",
        }

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    def count_live(
        self,
        params: Mapping[str, Any],
        *,
        iteration_id: int | None = None,
        query_id: int | None = None,
    ) -> int:
        """The tripwire. Returns how many aircraft match, for ~1 credit.

        Billed at 15% of the corresponding /full endpoint. Running this before
        buying full records is the difference between paying 8 credits per
        returned flight on a hunch and paying it on evidence.
        """
        response = self._request(
            EP_LIVE_COUNT, params=params,
            # A /count call is billed as one flat unit regardless of the number
            # it reports, so the record count for accounting is 1, not N.
            count_records=lambda data: 1,
            iteration_id=iteration_id, query_id=query_id,
        )
        return _record_count(response.data)

    def live_positions(
        self,
        params: Mapping[str, Any],
        *,
        iteration_id: int | None = None,
        query_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Aircraft currently airborne and inbound, with ETAs.

        The earliest available warning: an ETA is what makes this tactical
        rather than descriptive. Every record is AMBIGUOUS on category.
        """
        response = self._request(
            EP_LIVE_FULL, params=params,
            count_records=lambda data: len(_data_list(data)),
            iteration_id=iteration_id, query_id=query_id,
        )
        return [_normalise_live(record) for record in _data_list(response.data)]

    def flight_summary(
        self,
        params: Mapping[str, Any],
        *,
        iteration_id: int | None = None,
        query_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Historical flights, which is the only source of a real category.

        `categories` cannot be used alone on this endpoint; it must accompany
        another filter such as `airports`. QueueAgent always pairs them.
        """
        response = self._request(
            EP_SUMMARY_FULL, params=params,
            count_records=lambda data: len(_data_list(data)),
            iteration_id=iteration_id, query_id=query_id,
        )
        return [_normalise_summary(record) for record in _data_list(response.data)]

    def health_check(self) -> dict[str, Any]:
        """/api/usage is the cheapest call and reports credit consumption.

        `period` is required in practice: without it the endpoint does not
        return a usage breakdown.
        """
        from .. base.connector import RateLimitError  # noqa: E501 local import

        try:
            response = self._request(
                EP_USAGE, params={"period": "24h"}, count_records=lambda data: 1
            )
        except RateLimitError as exc:
            # A 429 means the request got past authentication and was throttled,
            # which proves the credential is valid — an invalid key returns 401.
            # /api/usage is throttled far harder than the data endpoints
            # (measured at roughly one request per minute), so treating a 429
            # here as unhealthy would report a working system as broken.
            return {"provider": self.provider, "healthy": True,
                    "detail": f"throttled but authenticated: {exc}"}
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            return {"provider": self.provider, "healthy": False,
                    "detail": str(exc)}
        return {"provider": self.provider, "healthy": True,
                "detail": "ok", "usage": response.data}


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------


def _data_list(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise SchemaError(
            f"FR24 returned {type(payload).__name__}, expected an object",
            provider="FR24",
        )
    records = payload.get("data")
    if records is None:
        return []
    if not isinstance(records, list):
        raise SchemaError(
            f"FR24 'data' was {type(records).__name__}, expected a list",
            provider="FR24",
        )
    return records


def _record_count(payload: Any) -> int:
    """Read the /count envelope, tolerating both documented shapes."""
    if isinstance(payload, dict):
        if "record_count" in payload:
            return int(payload["record_count"])
        inner = payload.get("data")
        if isinstance(inner, dict) and "record_count" in inner:
            return int(inner["record_count"])
        if isinstance(inner, list):
            return len(inner)
    raise SchemaError(
        f"FR24 count response had no record_count; got "
        f"{sorted(payload)[:8] if isinstance(payload, dict) else type(payload).__name__}",
        provider="FR24", endpoint=EP_LIVE_COUNT,
    )


def normalise_category(value: Any) -> str | None:
    """Map whatever `category` carries onto a letter code, or None.

    Accepts a letter, the spec's enum name, or a display name. Returns None when
    the value is absent or unrecognised, which the caller must then treat as
    AMBIGUOUS rather than guessing.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    upper = text.upper()
    if upper in CATEGORY_CODES:
        return upper
    if upper in _NAME_TO_CODE:
        return _NAME_TO_CODE[upper]
    return _FRIENDLY_TO_CODE.get(text.lower())


# Below these thresholds an aircraft is on the ground, not inbound. A live query
# for `inbound:LAX` legitimately returns aircraft already parked at LAX, and a
# verified capture showed several with alt=0, gspeed=0 and eta=null.
_GROUND_ALT_FT = 200
_GROUND_SPEED_KTS = 40


def _is_airborne(record: Mapping[str, Any]) -> bool:
    """Whether a live record describes an aircraft actually in the air.

    Matters because the ETA is what makes this signal tactical. Reporting a
    parked airframe as `airborne_inbound` would overstate imminence, and an
    imminence claim is exactly what a reader would act on.
    """
    altitude = record.get("alt")
    speed = record.get("gspeed")
    if altitude is None and speed is None:
        return True          # no telemetry to contradict the query's intent
    return (
        (altitude or 0) > _GROUND_ALT_FT or (speed or 0) > _GROUND_SPEED_KTS
    )


def _normalise_live(record: Mapping[str, Any]) -> dict[str, Any]:
    """A live position record.

    Always AMBIGUOUS: the response carries no category field at all, so the only
    thing known is that the aircraft matched the requested filter. Scoring
    credits such records at the lowest weight any category in that filter would
    have earned.
    """
    airborne = _is_airborne(record)
    return {
        "fr24_id": record.get("fr24_id", ""),
        "callsign": record.get("callsign", ""),
        "registration": record.get("reg", ""),
        "aircraft_type": record.get("type", ""),
        "origin_iata": record.get("orig_iata") or record.get("orig_icao") or "",
        "dest_iata": record.get("dest_iata") or record.get("dest_icao") or "",
        "operating_as": record.get("operating_as") or record.get("painted_as") or "",
        "flight_category": "AMBIGUOUS",
        "category_confidence": "AMBIGUOUS",
        "flight_status": "airborne_inbound" if airborne else "landed",
        # An ETA on a stationary aircraft is stale, and scoring reads
        # earliest_eta as the urgency of the warning.
        "eta": (record.get("eta") or None) if airborne else None,
        "observed_at": record.get("timestamp") or None,
        "lat": record.get("lat"),
        "lon": record.get("lon"),
        "altitude_ft": record.get("alt"),
        "ground_speed_kts": record.get("gspeed"),
        "source": record.get("source", ""),
    }


def _normalise_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    """A historical flight record, which does carry a category."""
    code = normalise_category(record.get("category"))
    known = code in SCORED_CATEGORIES if code else False
    ended = record.get("flight_ended")
    return {
        "fr24_id": record.get("fr24_id", ""),
        "callsign": record.get("callsign", ""),
        "registration": record.get("reg", ""),
        "aircraft_type": record.get("type", ""),
        "origin_iata": record.get("orig_iata") or record.get("orig_icao") or "",
        "dest_iata": (
            record.get("dest_iata_actual") or record.get("dest_iata")
            or record.get("dest_icao_actual") or record.get("dest_icao") or ""
        ),
        "operating_as": record.get("operating_as") or record.get("painted_as") or "",
        "flight_category": code if known else "AMBIGUOUS",
        "category_confidence": "CONFIRMED" if known else "AMBIGUOUS",
        "flight_status": "landed" if ended is not False else "airborne_inbound",
        "eta": None,
        "observed_at": (
            record.get("datetime_landed") or record.get("last_seen")
            or record.get("datetime_takeoff") or record.get("first_seen")
        ),
        "first_seen": record.get("first_seen"),
        "last_seen": record.get("last_seen"),
    }


def resolve_categories(
    live: Iterable[Mapping[str, Any]],
    summary: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Upgrade AMBIGUOUS live records using confirmed categories from summary.

    This is why the historical query runs alongside the live one rather than as
    an afterthought: flight-summary is the only endpoint that returns a category,
    so it is what makes a live record's category legible. Matching is by fr24_id
    first, then registration, then callsign.

    Records that find no match stay AMBIGUOUS. Nothing is guessed — an
    unmatched live record from a `categories=M,J` query could be either, and
    scoring must not assume the more alarming one.
    """
    by_id: dict[str, str] = {}
    by_reg: dict[str, str] = {}
    by_callsign: dict[str, str] = {}
    for record in summary:
        if record.get("category_confidence") != "CONFIRMED":
            continue
        category = record.get("flight_category")
        if not category:
            continue
        if record.get("fr24_id"):
            by_id[str(record["fr24_id"])] = category
        if record.get("registration"):
            by_reg[str(record["registration"]).upper()] = category
        if record.get("callsign"):
            by_callsign[str(record["callsign"]).upper()] = category

    resolved: list[dict[str, Any]] = []
    for record in live:
        updated = dict(record)
        if updated.get("category_confidence") == "AMBIGUOUS":
            category = (
                by_id.get(str(updated.get("fr24_id") or ""))
                or by_reg.get(str(updated.get("registration") or "").upper())
                or by_callsign.get(str(updated.get("callsign") or "").upper())
            )
            if category:
                updated["flight_category"] = category
                updated["category_confidence"] = "CONFIRMED"
        resolved.append(updated)
    return resolved
