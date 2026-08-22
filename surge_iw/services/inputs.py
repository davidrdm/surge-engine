"""Loading a session's geography from a file — 8.7(c).

Two files used to hold it — one listing cities, one listing their key locations
— and **nothing read either one**. Sessions had to be created by pasting the
whole geography into a JSON body; the files were input to a human, not to the
system.

They are now one file per input set (owner decision), because two files that
had to agree about a city's spelling were a consistency requirement with
nothing enforcing it: a city named in the first with no entry in the second
silently got no lodging anchor, and an entry in the second for a city that was
commented out of the first did nothing at all.

**An unresolvable city is refused by name, never skipped.** That is the whole
discipline of this module. A city dropped from a session is a coverage gap that
appears as nothing — no query, no refusal, no warning — which is the failure
this system is organised against. Refusing at load time also costs nothing to
correct, whereas discovering it mid-iteration produces a SKIPPED_NO_MAPPING
query hours later that reads as a data gap rather than a setup mistake.

Cities go through `geo.resolve_city` here rather than at query time (owner
decision), so `canonical` and the method that produced it are settled before any
collection is planned. Key locations stay free text: there is no geocoder in
this system, `lodging_location_string` builds a search string from the name, and
`services/facility.py` matches triage's extracted names against it. Inventing
coordinates would be a resolution nobody performed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import geo

#: Where `POST /v1/sessions` looks for a named input set.
DEFAULT_INPUT_DIR = "./inputs"

#: What the shipped file is called, and the default for the CLI.
#:
#: The CLI default and the API's refusal message both name this, so it has to
#: track whatever the engine actually ships. It did not: the previous value
#: named the input file that left with the mission pack, so `session create`
#: defaulted to a file that is not there and the 422 suggested the same
#: missing name back to the caller.
DEFAULT_INPUT_NAME = "example"

#: A name the API will accept. Deliberately not a path: the API takes a NAME
#: and resolves it inside the configured directory, so a request cannot reach
#: `../../etc/passwd` or any file the operator did not put there. The CLI does
#: accept a path, because an operator running it already has the filesystem.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Keys a location mapping may carry. `name` is required; the rest mirror
#: `KeyLocationIn` so a loaded set and a hand-written body produce the same row.
_LOCATION_KEYS = frozenset({"name", "address", "lat", "lon", "location_type"})


class InputError(ValueError):
    """A malformed or unusable input file. Always names what and where."""


@dataclass(frozen=True)
class LoadedCity:
    """One jurisdiction, resolved."""

    name: str
    state: str | None
    #: The geo table key this resolved to. Settled at load, not at query time.
    canonical: str
    #: TABLE, ALIAS or PREFIX — never UNRESOLVED, which is refused.
    resolved_by: str
    key_locations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}" if self.state else self.name

    def as_city_in(self) -> dict[str, Any]:
        """The `CityIn` shape, so a loaded set goes through the same validation
        and the same `_add_city` path a hand-written body does."""
        return {"name": self.name, "state": self.state,
                "key_locations": list(self.key_locations)}


@dataclass(frozen=True)
class InputSet:
    """What a file resolved to, and where it came from."""

    path: Path
    cities: list[LoadedCity]
    #: Cities that parsed and resolved but have no key locations. Not an error —
    #: a session can legitimately want flight and car coverage only — but the
    #: lodging family will be absent, and that should be said out loud.
    without_locations: list[str] = field(default_factory=list)

    def as_payload(self) -> list[dict[str, Any]]:
        return [city.as_city_in() for city in self.cities]

    def describe(self) -> list[str]:
        """One line per city, for a dry run."""
        lines = []
        for city in self.cities:
            airports = geo.city_to_airports(city.canonical) or ["—"]
            pickup = geo.city_to_pickup_location(city.canonical) or "—"
            lines.append(
                f"{city.label}  ->  {city.canonical} ({city.resolved_by}); "
                f"airports {','.join(airports)}; pickup {pickup}; "
                f"{len(city.key_locations)} key location(s)")
        return lines


def input_path(name: str, config: Mapping[str, Any] | None = None) -> Path:
    """Resolve a NAME to a file inside the configured input directory.

    Refuses anything that is not a bare name. A session is created over an
    authenticated API, but authenticated is not the same as trusted with the
    filesystem, and a field that reads a path is a file-disclosure primitive
    whatever the intent behind it.
    """
    if not _SAFE_NAME_RE.match(name or ""):
        raise InputError(
            f"{name!r} is not a valid input set name. Give the NAME of a file "
            f"in the input directory — {DEFAULT_INPUT_NAME!r} is the one "
            "shipped — not a path.")
    directory = Path(
        ((config or {}).get("inputs") or {}).get("dir", DEFAULT_INPUT_DIR))
    candidate = directory / name
    for path in (candidate, candidate.with_suffix(".yaml"),
                 candidate.with_suffix(".yml")):
        if path.is_file():
            return path
    available = sorted(
        p.stem for p in directory.glob("*.y*ml")) if directory.is_dir() else []
    raise InputError(
        f"No input set {name!r} in {directory}/. "
        + (f"Available: {', '.join(available)}." if available
           else "The directory holds no .yaml files."))


def load(path: str | Path, *, config: Mapping[str, Any] | None = None,
         mission: Any = None) -> InputSet:
    """Parse and resolve an input file. Raises rather than returning a partial set.

    The all-or-nothing rule is the point. A loader that returned the cities it
    understood would create a session quietly missing a jurisdiction the
    operator believes is covered — and every downstream signal about that city
    would then be a true absence of evidence about a place nobody looked at.

    `mission` supplies the permitted `location_type` values. Checked here
    rather than at insert so a typo is a load-time error naming the file and
    the city, instead of a 422 the operator has to trace back to a line.
    """
    jurisdictions = getattr(mission, "jurisdictions", geo.NO_EQUIVALENTS)
    path = Path(path)
    if not path.is_file():
        raise InputError(f"No input file at {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise InputError(f"{path} is not valid YAML: {exc}") from exc

    if raw is None:
        raise InputError(
            f"{path} is empty. A session needs at least one city; an input "
            "file with every entry commented out would otherwise create a "
            "session that collects nothing.")
    if not isinstance(raw, Mapping):
        raise InputError(
            f"{path} must be a mapping of 'City, ST' to a list of key "
            f"locations, not {type(raw).__name__}.")

    cities: list[LoadedCity] = []
    unresolved: list[str] = []
    without_locations: list[str] = []
    seen: dict[str, str] = {}

    for label, value in raw.items():
        if not isinstance(label, str) or not label.strip():
            raise InputError(f"{path}: a city key must be a non-empty string, "
                             f"got {label!r}")
        label = label.strip()
        canonical, method = geo.resolve_city(label, jurisdictions)
        if canonical is None:
            unresolved.append(label)
            continue
        if canonical in seen:
            raise InputError(
                f"{path}: {label!r} and {seen[canonical]!r} both resolve to "
                f"{canonical!r}. Two entries for one jurisdiction is ambiguous "
                "— the second's key locations would be silently ignored.")
        seen[canonical] = label

        name, state = geo.split_state(label)
        locations = _locations(path, label, value, mission)
        if not locations:
            without_locations.append(label)
        cities.append(LoadedCity(
            name=name, state=state, canonical=canonical, resolved_by=method,
            key_locations=locations,
        ))

    if unresolved:
        raise InputError(
            f"{path}: {len(unresolved)} city/cities cannot be resolved to an "
            f"airport or pickup mapping: {', '.join(repr(u) for u in unresolved)}. "
            "Fix or remove them — a city dropped from a session produces no "
            "query, no refusal and no warning, so its absence would be "
            "indistinguishable from finding nothing there.")
    if not cities:
        raise InputError(
            f"{path} resolved no cities. A session needs at least one.")
    return InputSet(path=path, cities=cities,
                    without_locations=without_locations)


def _locations(path: Path, label: str, value: Any,
               mission: Any = None) -> list[dict[str, Any]]:
    """Key locations for one city, from a string, a mapping, or a list of either.

    A bare string is the common case and keeps the file readable — it is the
    shape `inputs/locations.yaml` already used. The mapping form exists so a
    location can carry its `location_type`, which is what an analyst reading an
    alert wants to know about a facility.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise InputError(
            f"{path}: key locations for {label!r} must be a list, not "
            f"{type(value).__name__}. Use '- Name' per line.")

    locations: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in value:
        if isinstance(item, str):
            entry: dict[str, Any] = {"name": item.strip()}
        elif isinstance(item, Mapping):
            unknown = set(item) - _LOCATION_KEYS
            if unknown:
                raise InputError(
                    f"{path}: {label!r} has a key location with unknown "
                    f"field(s) {sorted(unknown)}. Known fields: "
                    f"{sorted(_LOCATION_KEYS)}.")
            entry = {k: item[k] for k in _LOCATION_KEYS if k in item}
            entry["name"] = str(entry.get("name") or "").strip()
        else:
            raise InputError(
                f"{path}: {label!r} has a key location that is neither a name "
                f"nor a mapping: {item!r}")

        if not entry["name"]:
            raise InputError(f"{path}: {label!r} has a key location with no name")
        if entry.get("location_type") is not None:
            # Validated here rather than at insert, so a typo is a load-time
            # error naming the file and the city instead of a 422 the operator
            # has to trace back to a line.
            try:
                if mission is None:
                    raise InputError(
                        "location_type cannot be checked: no mission is "
                        "loaded, and the permitted values are the mission's.")
                mission.location_type(str(entry["location_type"]).upper())
            except ValueError as exc:
                raise InputError(f"{path}: {label!r}: {exc}") from exc
            entry["location_type"] = str(entry["location_type"]).upper()
        key = entry["name"].casefold()
        if key in names:
            raise InputError(
                f"{path}: {label!r} lists {entry['name']!r} twice")
        names.add(key)
        locations.append(entry)
    return locations
