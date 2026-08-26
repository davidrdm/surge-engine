"""Per-session configuration overrides (9.2, issue #11).

`POST /v1/sessions` has always accepted a `tunables` object, stored it in
`sessions.config_json`, and **never read it**. Every stage ran on the
process-wide configuration. The documentation mismatch was not the problem: a
client could request narrower criteria or tighter spending controls, receive a
200 with its session, and have paid collection run under settings it did not
choose — with `receipts.config_hash` stamped from a configuration it never
asked for. That last part is the serious one, because the config hash is what
makes a judgement reconstructible, and it was recording the wrong answer.

Two rules make this safe to turn on.

**Unknown is refused, never ignored.** A misspelled `triage.max_post_age` is a
422 naming the field, not a silent no-op. Silence is what the defect was.

**Ceilings may only come down.** A handful of fields are spending controls —
the per-iteration and monthly caps, the query fan-out limits — and a client may
lower them, never raise them. They are validated against the server's value at
session creation so the client learns immediately, and clamped again at merge
time so an operator who *lowers* the server cap afterwards is still obeyed.
Validation is a courtesy; the clamp is the guarantee.

Everything outside the allowlist stays server-owned: credentials, provider
endpoints and rate limits, retention ceilings, the model and its parameters,
the database, the API's own deployment settings, and `dry_run` — a client that
could set `dry_run` would receive fixture data indistinguishable from
collection it had paid for.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..db import enums

#: Sections a client may never touch, listed so the refusal message can say
#: *why* rather than only *no*. A section absent from both this map and
#: ALLOWED is refused as unknown.
SERVER_OWNED: dict[str, str] = {
    "database": "the database location is a deployment setting",
    "llm": "the model, its parameters and its credential are server-owned",
    "alerting": "model call limits are server-owned",
    "api": "API host, port, token and worker settings are deployment settings",
    "apidirect": "provider endpoints, credentials, rate limits and retention "
                 "ceilings are server-owned",
    "flightradar": "provider endpoints, credentials, rate limits and retention "
                   "ceilings are server-owned",
    "staying": "provider endpoints, credentials, rate limits and retention "
               "ceilings are server-owned",
    "priceline": "provider endpoints, credentials, rate limits and retention "
                 "ceilings are server-owned",
    "inputs": "the input directory is a deployment setting",
    "mission": "the mission is what the instrument is FOR. A session that "
               "could switch it would change the tracks, the lexicon, the "
               "prompts and the weights at once, and every alert in the "
               "database would then be scored under a definition chosen per "
               "request rather than by the operator",
    "dry_run": "a session that could set dry_run would receive fixture data "
               "indistinguishable from collection it had paid for",
}


class TunableError(ValueError):
    """A rejected tunable. The message always names the field."""


@dataclass(frozen=True)
class Spec:
    """What one tunable accepts.

    `ceiling` marks a spending control: permitted to move down from the
    server's value and never up. Nothing else has that constraint, because
    nothing else costs money — a client that wants a lower correlation
    threshold is asking for a more sensitive instrument, not a more expensive
    one.
    """

    kind: type
    minimum: float | None = None
    maximum: float | None = None
    ceiling: bool = False


#: Per-provider spending maps. Validated by value rather than by key set, so a
#: client may tighten one provider without restating the others.
_PROVIDER_UNITS = Spec(float, minimum=0.0, ceiling=True)

#: The whole surface. Nested exactly as config.yaml is, because a tunable that
#: did not look like the setting it overrides would be one more thing to get
#: wrong.
ALLOWED: dict[str, dict[str, Spec]] = {
    "triage": {
        "batch_size": Spec(int, 1, 100),
        "require_nexus": Spec(bool),
        "min_salience": Spec(float, 0.0, 1.0),
        "max_post_age_hours": Spec(float, 0.0, 24 * 365),
    },
    "sensitivity": {
        "confirm_min_salience": Spec(float, 0.0, 1.0),
        "tip_min_salience": Spec(float, 0.0, 1.0),
        "tip_require_timestamp": Spec(bool),
        "tip_max_age_hours": Spec(float, 0.0, 24 * 365),
        "max_future_skew_hours": Spec(float, 0.0, 168.0),
    },
    "correlation": {
        "window_hours": Spec(int, 1, 24 * 30),
        "radius_km": Spec(float, 0.1, 500.0),
        "lodging_drop_full_scale": Spec(float, 0.1, 100.0),
        "car_drop_full_scale": Spec(float, 0.1, 100.0),
        "price_escalation_full_scale": Spec(float, 0.1, 1000.0),
        "flight_full_scale": Spec(float, 0.1, 1000.0),
        "social_domains_full_scale": Spec(float, 0.1, 1000.0),
        "single_source_quality": Spec(float, 0.0, 1.0),
        "on_airport_weight": Spec(float, 0.0, 10.0),
        "band_high_min_score": Spec(float, 0.0, 1.0),
        # 16, not 4: the engine ships four families, but a mission may
        # promote streams to families of their own, and a session must be able
        # to demand any number the loaded pack can actually reach. A value
        # above the pack's family count makes the band unreachable, which is a
        # legible outcome rather than an invalid setting.
        "band_high_min_types": Spec(int, 1, 16),
        "band_medium_min_score": Spec(float, 0.0, 1.0),
        "band_medium_min_types": Spec(int, 1, 16),
        "band_low_min_score": Spec(float, 0.0, 1.0),
        "band_low_min_reports": Spec(int, 1, 20),
        # 9.10. Analytical, so a session may sharpen or relax the flight
        # baseline without changing it for anyone else.
        "flight_excess_full_scale": Spec(float, 1.0, 1000.0),
        "flight_baseline_min_samples": Spec(int, 1, 100),
        "flight_baseline_window_days": Spec(int, 1, 365),
        "alert_min_score": Spec(float, 0.0, 1.0),
        # 9.5. Settable because it is analytical, and bounded below at 0.001
        # because the curve degenerates to a step function underneath that.
        "decay_edge_weight": Spec(float, 0.001, 1.0),
    },
    "windows": {
        "near_term_hours": Spec(int, 1, 24 * 30),
        # A list; element rules are applied in _coerce.
        "baseline_days": Spec(list),
    },
    "tipping": {
        "max_queries_per_iteration": Spec(int, 1, 5000, ceiling=True),
        "max_queries_per_city": Spec(int, 1, 500, ceiling=True),
        # The schema's own CHECK stops at 3, so this cannot be a client's way
        # to reach a fourth level of tipping.
        "max_tip_depth": Spec(int, 0, 3, ceiling=True),
        "cooldown_minutes": Spec(int, 0, 10080),
        "max_locations_per_city": Spec(int, 1, 20, ceiling=True),
        "min_independent_domains": Spec(int, 1, 10),
        "min_expansion_salience": Spec(float, 0.0, 1.0),
        "max_expanded_cities": Spec(int, 0, 50, ceiling=True),
    },
    "budget": {
        "iterations_per_month_planned": Spec(int, 1, 10000),
        "hard_stop_pct": Spec(float, 0.0, 1.0),
        "reserved_priority_ceiling": Spec(int, 0, 100),
        "per_iteration_cap": Spec(dict, ceiling=True),
        "monthly_limit": Spec(dict, ceiling=True),
    },
}

#: Every dotted path a client may send, for an error message that can say what
#: was available instead of only what was wrong.
def allowed_paths() -> list[str]:
    return sorted(f"{section}.{key}"
                  for section, keys in ALLOWED.items() for key in keys)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _number(path: str, value: Any, spec: Spec) -> Any:
    """Coerce and range-check one scalar.

    `bool` is checked before `int` deliberately: in Python `True` is an `int`,
    so a client sending `true` for `batch_size` would otherwise be silently
    accepted as 1.
    """
    if spec.kind is bool:
        if not isinstance(value, bool):
            raise TunableError(f"{path}: expected true or false, "
                               f"got {type(value).__name__}")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TunableError(f"{path}: expected a number, "
                           f"got {type(value).__name__}")
    value = int(value) if spec.kind is int else float(value)
    if spec.kind is int and value != float(value):
        raise TunableError(f"{path}: expected a whole number")
    if spec.minimum is not None and value < spec.minimum:
        raise TunableError(f"{path}: {value} is below the minimum "
                           f"{spec.minimum}")
    if spec.maximum is not None and value > spec.maximum:
        raise TunableError(f"{path}: {value} is above the maximum "
                           f"{spec.maximum}")
    return value


def _baseline_days(path: str, value: Any) -> list[int]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 4:
        raise TunableError(f"{path}: expected a list of 1 to 4 day offsets")
    days = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise TunableError(f"{path}: every offset must be a whole "
                               f"number of days")
        if not 1 <= item <= 90:
            raise TunableError(f"{path}: {item} is outside 1..90 days")
        days.append(item)
    if len(set(days)) != len(days):
        raise TunableError(f"{path}: duplicate offsets")
    return sorted(days)


def _provider_units(path: str, value: Any,
                    server: Mapping[str, Any] | None) -> dict[str, float]:
    """A provider -> units map, checked per provider against the server's."""
    if not isinstance(value, Mapping):
        raise TunableError(f"{path}: expected an object keyed by provider "
                           f"({', '.join(sorted(enums.PROVIDERS))})")
    out: dict[str, float] = {}
    for provider, units in value.items():
        name = str(provider).upper()
        if name not in enums.PROVIDERS:
            raise TunableError(
                f"{path}.{provider}: unknown provider; expected one of "
                f"{sorted(enums.PROVIDERS)}")
        out[name] = _number(f"{path}.{name}", units, _PROVIDER_UNITS)
        limit = (server or {}).get(name)
        if limit is not None and out[name] > float(limit):
            raise TunableError(
                f"{path}.{name}: {out[name]:g} exceeds the server's "
                f"{float(limit):g}. A session may lower a spending cap, never "
                f"raise one — the budget being protected is the operator's.")
    return out


def validate(tunables: Mapping[str, Any] | None,
             server_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the normalised tunables, or raise `TunableError`.

    Refusal is by name and on the first offending field. Reporting one at a
    time is deliberate: a client fixing a config sends it again anyway, and a
    list of complaints reads as advisory where a single named refusal reads as
    a rule.
    """
    if not tunables:
        return {}
    if not isinstance(tunables, Mapping):
        raise TunableError("tunables must be an object shaped like config.yaml")

    clean: dict[str, Any] = {}
    for section, values in tunables.items():
        if section in SERVER_OWNED:
            raise TunableError(
                f"{section}: not settable per session — {SERVER_OWNED[section]}.")
        if section not in ALLOWED:
            raise TunableError(
                f"{section}: unknown configuration section. Settable sections "
                f"are {', '.join(sorted(ALLOWED))}.")
        if not isinstance(values, Mapping):
            raise TunableError(f"{section}: expected an object of settings")

        server_section = (server_config or {}).get(section) or {}
        out: dict[str, Any] = {}
        for key, value in values.items():
            path = f"{section}.{key}"
            spec = ALLOWED[section].get(key)
            if spec is None:
                raise TunableError(
                    f"{path}: unknown setting. Settable in {section}: "
                    f"{', '.join(sorted(ALLOWED[section]))}.")
            if spec.kind is list:
                out[key] = _baseline_days(path, value)
            elif spec.kind is dict:
                out[key] = _provider_units(path, value,
                                           server_section.get(key))
            else:
                out[key] = _number(path, value, spec)
                if spec.ceiling:
                    limit = server_section.get(key)
                    if limit is not None and out[key] > limit:
                        raise TunableError(
                            f"{path}: {out[key]:g} exceeds the server's "
                            f"{limit:g}. A session may lower this limit, never "
                            f"raise it.")
        if out:
            clean[section] = out
    return clean


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def effective(base: Mapping[str, Any],
              tunables: Mapping[str, Any] | None) -> dict[str, Any]:
    """The configuration one session's work actually runs under.

    Built once per iteration and used for every stage, so a receipt's
    `config_hash` names the settings that produced the judgement rather than
    whatever the process happened to be started with.

    Anything not in the allowlist is dropped rather than merged. A stored
    tunable can predate the allowlist — sessions created before this existed
    have arbitrary objects in `config_json` — and honouring one now would apply
    a setting nobody validated. The clamp on ceilings is re-applied here for
    the same reason: the server's cap may have been lowered since.
    """
    from ..config import deep_merge

    merged = deep_merge({}, base)
    if not tunables:
        return merged
    for section, values in (tunables or {}).items():
        if section not in ALLOWED or not isinstance(values, Mapping):
            continue
        target = merged.setdefault(section, {})
        server_section = (base.get(section) or {})
        for key, value in values.items():
            spec = ALLOWED[section].get(key)
            if spec is None:
                continue
            if spec.kind is dict and isinstance(value, Mapping):
                caps = dict(server_section.get(key) or {})
                for provider, units in value.items():
                    limit = caps.get(provider)
                    caps[provider] = (min(float(units), float(limit))
                                      if limit is not None else float(units))
                target[key] = caps
            elif spec.ceiling:
                limit = server_section.get(key)
                target[key] = (min(value, limit) if limit is not None
                               else value)
            else:
                target[key] = value
    return merged


def describe(tunables: Mapping[str, Any] | None) -> list[str]:
    """One line per override, for the session response and the audit log."""
    lines: list[str] = []
    for section in sorted(tunables or {}):
        values = (tunables or {})[section]
        if isinstance(values, Mapping):
            for key in sorted(values):
                lines.append(f"{section}.{key} = {values[key]}")
    return lines


def unsupported(tunables: Mapping[str, Any] | None) -> Iterable[str]:
    """Dotted paths in a stored object that `effective()` will ignore.

    Only reachable for a session created before the allowlist existed. Reported
    rather than silently dropped, because "stored but not applied" is precisely
    the failure this module was written to end.
    """
    for section, values in (tunables or {}).items():
        if section not in ALLOWED:
            yield str(section)
            continue
        if not isinstance(values, Mapping):
            yield str(section)
            continue
        for key in values:
            if key not in ALLOWED[section]:
                yield f"{section}.{key}"
