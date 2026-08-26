"""The operator's calendar of scheduled events — context, never input.

TriageAgent shows these events to the model as background; CorrelationAgent
records the ones that overlap a scored window. **Nothing here ever moves a
score or a band** — the calendar tells a reader what was supposed to happen,
and the arithmetic stays reproducible from the signals alone.

The loading discipline is `services/inputs.py`'s, verbatim: a YAML file in the
same `inputs.dir`, resolved by NAME over the API, every unresolvable city
refused together by name, unknown keys refused, all-or-nothing. Events are
append-only once stored (`db.insert_calendar_events`), because the triage
context block must be reconstructible byte-exact after later appends — see
`context_block`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from . import geo
from .inputs import InputError

#: Keys an event entry may carry. Mirrors `inputs._LOCATION_KEYS`' role: the
#: refusal names the key, and an unknown one is never silently dropped.
_EVENT_KEYS = frozenset({"name", "starts", "ends", "category", "note"})

#: Above this the load still succeeds but warns: every event rides in every
#: triage call's context block, and a calendar the size of a phone book is a
#: prompt-budget problem the operator should hear about at load time.
ADVISORY_EVENT_LIMIT = 50


@dataclass(frozen=True)
class LoadedCalendar:
    """A validated calendar file, ready for `db.insert_calendar_events`."""

    path: Path
    events: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]


def _instant(value: Any, *, end_of_day: bool, where: str) -> str:
    """Canonical ISO for a YAML date, datetime, or ISO string.

    A bare date means the whole day: 00:00Z as a start, end-of-day as an end.
    Everything is stored in one spelling because `calendar_events` is compared
    and ordered as TEXT — the same reasoning as `signals.observed_at` (9.12).
    """
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        moment = time(23, 59, 59) if end_of_day else time(0, 0, 0)
        return datetime.combine(value, moment, tzinfo=timezone.utc).isoformat()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InputError(f"{where}: {value!r} is not a date") from exc
        if parsed.tzinfo is None:
            if len(value.strip()) == 10:                    # bare date string
                return _instant(parsed.date(), end_of_day=end_of_day,
                                where=where)
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    raise InputError(f"{where}: {value!r} is not a date")


def load(path: Path, *, mission: Any = None) -> LoadedCalendar:
    """Parse and validate a calendar file, or raise `InputError`.

    All-or-nothing: every unresolvable city is collected and refused together
    by name, because an event attached to a city the engine cannot place would
    annotate nothing, silently.
    """
    if not path.is_file():
        raise InputError(f"No calendar file at {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise InputError(f"{path}: not valid YAML — {exc}") from exc
    if raw is None:
        raise InputError(
            f"{path} is empty. A calendar with every entry commented out "
            f"would annotate nothing while looking configured.")
    if not isinstance(raw, Mapping):
        raise InputError(
            f"{path} must be a mapping of 'City, ST' to a list of events")

    jurisdictions = getattr(mission, "jurisdictions", geo.NO_EQUIVALENTS)
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    unresolved: list[str] = []
    seen: set[tuple[str, str, str]] = set()

    for label, entries in raw.items():
        if not isinstance(label, str) or not label.strip():
            raise InputError(f"{path}: a city key is empty")
        canonical, _method = geo.resolve_city(label, jurisdictions)
        if canonical is None:
            unresolved.append(label)
            continue
        if not isinstance(entries, Sequence) or isinstance(entries, str) \
                or not entries:
            raise InputError(
                f"{path}: {label} must hold a non-empty list of events")
        for index, entry in enumerate(entries):
            where = f"{path}: {label}[{index}]"
            if not isinstance(entry, Mapping):
                raise InputError(f"{where} must be a mapping")
            unknown = sorted(set(entry) - _EVENT_KEYS)
            if unknown:
                raise InputError(
                    f"{where} has unknown key(s) {', '.join(unknown)}; "
                    f"expected {sorted(_EVENT_KEYS)}")
            name = str(entry.get("name") or "").strip()
            if not name:
                raise InputError(f"{where}: name is required")
            if "starts" not in entry:
                raise InputError(f"{where} ({name!r}): starts is required")
            starts = _instant(entry["starts"], end_of_day=False,
                              where=f"{where}.starts")
            ends = _instant(entry.get("ends", entry["starts"]),
                            end_of_day=True, where=f"{where}.ends")
            if ends < starts:
                raise InputError(
                    f"{where} ({name!r}): ends {ends} is before starts "
                    f"{starts}")
            key = (canonical, name.casefold(), starts)
            if key in seen:
                raise InputError(
                    f"{where}: {name!r} starting {starts} appears twice for "
                    f"{label}")
            seen.add(key)
            events.append({
                "name": name,
                "city_label": label.strip(),
                "city_canonical": canonical,
                "starts_at": starts,
                "ends_at": ends,
                "category": (str(entry["category"]).strip()
                             if entry.get("category") else None),
                "note": (str(entry["note"]).strip()
                         if entry.get("note") else None),
            })

    if unresolved:
        raise InputError(
            f"{path} names {len(unresolved)} city/cities the engine cannot "
            f"place: {', '.join(repr(c) for c in unresolved)}. Fix or remove "
            f"them — an event attached to a city the engine cannot place "
            f"would annotate nothing, silently.")
    if not events:
        raise InputError(f"{path} holds no events")
    if len(events) > ADVISORY_EVENT_LIMIT:
        warnings.append(
            f"{path.name}: {len(events)} events. Every event rides in every "
            f"triage call as context, so a large calendar is a real prompt "
            f"cost; consider trimming to what the sessions actually watch.")
    return LoadedCalendar(path=path, events=tuple(events),
                          warnings=tuple(warnings))


def context_block(events: Sequence[Mapping[str, Any]]) -> str:
    """The exact text TriageAgent appends to its user message.

    A PURE function of the event rows, shared byte-for-byte with
    `scripts/reconstruct_prompts.py` — that sharing is what makes a receipt's
    `prompt_user_hash` verifiable after the fact. No windowing, no city
    filtering, no clock: any of those would make the block depend on something
    that drifts (config, the session's city set, today's date), and a
    reconstruction against drifted inputs would refuse a byte-exact claim that
    was in fact true. Callers pass events with
    `added_at <= iteration.started_at`, ordered `(added_at, event_id)` — the
    database's insertion order.
    """
    if not events:
        return ""
    lines = [
        "",
        "Operator-provided calendar of scheduled events, for context only. "
        "These are known, planned events; a post consistent with one may "
        "still be relevant, and you may note the connection in your "
        "rationale. This context does not change what qualifies as relevant.",
    ]
    for event in events:
        category = event["category"] if "category" in event.keys() else None
        note = event["note"] if "note" in event.keys() else None
        detail = f" ({category})" if category else ""
        tail = f" — {note}" if note else ""
        lines.append(
            f"- {event['city_label']}: {event['name']!r}{detail} "
            f"{event['starts_at']} to {event['ends_at']}{tail}")
    return "\n".join(lines)
