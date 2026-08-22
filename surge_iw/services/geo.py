"""Resolve city and county names to airport codes and car pickup points.

Derived from surge/utils/geo.py, with its resolution logic rewritten. The
original did:

    for k, v in _CITY_AIRPORTS.items():
        if key.startswith(k) or k.startswith(key):
            return v

which returns the first dict entry that shares a prefix in either direction.
"San" matches whichever of san antonio / san diego / san francisco iteration
reaches first, and "dc" matches nothing useful while "d" would match dallas.
The failure is silent: the caller gets a confident answer for the wrong city and
queries flights into the wrong airport.

Resolution here is explicit and ordered:

  1. exact match on the normalised name
  2. explicit alias table
  3. longest-prefix match, but only when the input is at least MIN_PREFIX_LEN
     characters AND exactly one table entry matches

Ambiguity resolves to nothing. A caller that gets an empty result marks its
query SKIPPED_NO_MAPPING, which correlation reads as an absent source rather
than an absent signal — the safe direction.

Also dropped: city_to_amadeus_code(). Amadeus is replaced by the Staying API,
which takes a free-text `location` string rather than an IATA city code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

MIN_PREFIX_LEN = 4

# ---------------------------------------------------------------------------
# City / county name -> serving airport IATA codes, primary first.
#
# Ordering matters: callers cap fan-out at the first N airports, so the busiest
# or most likely arrival field for chartered and government traffic goes first.
# ---------------------------------------------------------------------------
CITY_AIRPORTS: dict[str, list[str]] = {
    # Major metros
    "new york": ["JFK", "LGA", "EWR"],
    "los angeles": ["LAX", "BUR", "LGB", "ONT", "SNA"],
    "chicago": ["ORD", "MDW"],
    "houston": ["IAH", "HOU"],
    "phoenix": ["PHX"],
    "philadelphia": ["PHL"],
    "san antonio": ["SAT"],
    "san diego": ["SAN"],
    "dallas": ["DFW", "DAL"],
    "fort worth": ["DFW"],
    "san jose": ["SJC"],
    "austin": ["AUS"],
    "jacksonville": ["JAX"],
    "columbus": ["CMH"],
    "charlotte": ["CLT"],
    "indianapolis": ["IND"],
    "san francisco": ["SFO", "OAK"],
    "oakland": ["OAK"],
    "seattle": ["SEA"],
    "denver": ["DEN"],
    "nashville": ["BNA"],
    "oklahoma city": ["OKC"],
    "el paso": ["ELP"],
    "boston": ["BOS"],
    "portland": ["PDX"],
    "las vegas": ["LAS"],
    "memphis": ["MEM"],
    "louisville": ["SDF"],
    "baltimore": ["BWI"],
    "milwaukee": ["MKE"],
    "albuquerque": ["ABQ"],
    "tucson": ["TUS"],
    "fresno": ["FAT"],
    "mesa": ["PHX"],
    "kansas city": ["MCI"],
    "atlanta": ["ATL"],
    "miami": ["MIA", "FLL"],
    "fort lauderdale": ["FLL"],
    "minneapolis": ["MSP"],
    "saint paul": ["MSP"],
    "new orleans": ["MSY"],
    "cleveland": ["CLE"],
    "pittsburgh": ["PIT"],
    "saint louis": ["STL"],
    "cincinnati": ["CVG"],
    "raleigh": ["RDU"],
    "durham": ["RDU"],
    "richmond": ["RIC"],
    "salt lake city": ["SLC"],
    "hartford": ["BDL"],
    "buffalo": ["BUF"],
    "detroit": ["DTW"],
    "sacramento": ["SMF"],
    "tampa": ["TPA"],
    "orlando": ["MCO"],
    "washington": ["DCA", "IAD", "BWI"],
    "san juan": ["SJU"],
    "anchorage": ["ANC"],
    "honolulu": ["HNL"],
    "birmingham": ["BHM"],
    "norfolk": ["ORF"],
    "virginia beach": ["ORF"],
    "greensboro": ["GSO"],
    "charleston": ["CHS"],
    "columbia": ["CAE"],
    "jackson": ["JAN"],
    "baton rouge": ["BTR"],
    "shreveport": ["SHV"],
    "little rock": ["LIT"],
    "omaha": ["OMA"],
    "des moines": ["DSM"],
    "madison": ["MSN"],
    "green bay": ["GRB"],
    "grand rapids": ["GRR"],
    "dayton": ["DAY"],
    "toledo": ["TOL"],
    "akron": ["CAK"],
    "rochester": ["ROC"],
    "albany": ["ALB"],
    "syracuse": ["SYR"],
    "springfield": ["SPI"],
    "peoria": ["PIA"],
    "rockford": ["RFD"],
    "knoxville": ["TYS"],
    "chattanooga": ["CHA"],
    "lexington": ["LEX"],
    "lubbock": ["LBB"],
    "amarillo": ["AMA"],
    "corpus christi": ["CRP"],
    "harlingen": ["HRL"],
    "laredo": ["LRD"],
    "brownsville": ["BRO"],
    "mcallen": ["MFE"],
    "midland": ["MAF"],
    "odessa": ["MAF"],
    "waco": ["ACT"],
    "killeen": ["GRK"],
    "beaumont": ["BPT"],
    "bakersfield": ["BFL"],
    "stockton": ["SCK"],
    "modesto": ["MOD"],
    "visalia": ["VIS"],
    "oxnard": ["OXR"],
    "santa barbara": ["SBA"],
    "san luis obispo": ["SBP"],
    "reno": ["RNO"],
    "spokane": ["GEG"],
    "boise": ["BOI"],
    "billings": ["BIL"],
    "great falls": ["GTF"],
    "missoula": ["MSO"],
    "fargo": ["FAR"],
    "sioux falls": ["FSD"],
    "rapid city": ["RAP"],
    "cheyenne": ["CYS"],
    "casper": ["CPR"],
    "colorado springs": ["COS"],
    "pueblo": ["PUB"],
    "flagstaff": ["FLG"],
    "yuma": ["YUM"],
    "palm springs": ["PSP"],
    "west palm beach": ["PBI"],
    # County -> metro airports. A county is often the unit a mission works
    # in, so counties are first-class keys rather than something to strip down.
    "cook county": ["ORD", "MDW"],
    "harris county": ["IAH", "HOU"],
    "maricopa county": ["PHX"],
    "los angeles county": ["LAX", "BUR", "LGB", "ONT", "SNA"],
    "san diego county": ["SAN"],
    "orange county": ["SNA", "LAX"],
    "dallas county": ["DFW", "DAL"],
    "tarrant county": ["DFW"],
    "bexar county": ["SAT"],
    "travis county": ["AUS"],
    "el paso county": ["ELP"],
    "miami-dade county": ["MIA", "FLL"],
    "broward county": ["FLL", "MIA"],
    "palm beach county": ["PBI"],
    "hillsborough county": ["TPA"],
    "king county": ["SEA"],
    "wayne county": ["DTW"],
    "fulton county": ["ATL"],
    "clark county": ["LAS"],
    "multnomah county": ["PDX"],
    "salt lake county": ["SLC"],
    "fayette county": ["LEX"],
    "philadelphia county": ["PHL"],
    "allegheny county": ["PIT"],
    "cuyahoga county": ["CLE"],
    "milwaukee county": ["MKE"],
    "dane county": ["MSN"],
    "mecklenburg county": ["CLT"],
    "gwinnett county": ["ATL"],
    "pima county": ["TUS"],
    "washoe county": ["RNO"],
}

# ---------------------------------------------------------------------------
# Places that are ONE operational unit under two names (9.9).
#
# WHICH places those are is a mission's judgement, not the engine's, so the
# table lives in the mission pack (`geography.yaml: equivalents`) and arrives
# here as an argument. What the engine owns is the RULE: an equivalence is a
# statement that two names mean one unit, it is deliberately NOT transitive,
# and it never merges two units into one.
#
# Why it exists: a session named one place and a source reported the same
# activity under the containing administrative unit's name. Measured live — the
# article was collected, judged relevant at salience 0.85, and refused because
# the two strings did not match. The evidence the system exists to surface was
# paid for, judged, and dropped on a name.
#
# Why it must not be transitive or metro-shaped: two neighbouring units in one
# metro are still two units, and merging them would let a report about one
# admit to a session that named the other — manufacturing evidence rather than
# finding it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Equivalents:
    """Which place names a mission treats as one operational unit.

    Holds both directions, built once, so the two cannot disagree — a
    hand-written reverse index is a consistency requirement with nothing
    enforcing it.
    """

    #: unit key -> the other name for it.
    forward: Mapping[str, str] = field(default_factory=dict)
    #: The other name -> its unit key. Derived, never supplied.
    reverse: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def of(cls, mapping: Mapping[str, str] | None) -> "Equivalents":
        forward = dict(mapping or {})
        reverse = {other: unit for unit, other in forward.items()}
        if len(reverse) != len(forward):
            # Two units claiming one name makes "which unit is this?" a guess.
            duplicated = sorted(
                {other for other in forward.values()
                 if list(forward.values()).count(other) > 1})
            raise ValueError(
                f"equivalents: {', '.join(duplicated)} is claimed by more than "
                f"one unit, so the reverse lookup would be ambiguous")
        return cls(forward=forward, reverse=reverse)

    def unit_of(self, canonical: str) -> str:
        """The one key that names this unit.

        A unit is its own; its other name belongs to it; anything else stands
        alone. Used to tell an APPARENT ambiguity from a real one — refusing to
        choose between two names for one place helps nobody.
        """
        if canonical in self.forward:
            return canonical
        return self.reverse.get(canonical, canonical)

    def others(self, canonical: str) -> list[str]:
        """Other canonical keys naming the same unit."""
        out: list[str] = []
        other = self.forward.get(canonical)
        if other:
            out.append(other)
        unit = self.reverse.get(canonical)
        if unit:
            out.append(unit)
        return out


#: No equivalences at all. The engine's default: without a mission it makes no
#: claim that any two place names mean one thing, so an apparent ambiguity is
#: REFUSED rather than resolved. Refusing is the safe direction — it produces a
#: recorded UNRESOLVED rather than a confident answer for the wrong place.
NO_EQUIVALENTS = Equivalents()



# ---------------------------------------------------------------------------
# Explicit aliases. Everything here was previously handled by accidental prefix
# matching, or not at all. An alias is a deliberate statement that two strings
# name the same place; a prefix coincidence is not.
# ---------------------------------------------------------------------------
CITY_ALIASES: dict[str, str] = {
    "nyc": "new york",
    "new york city": "new york",
    "manhattan": "new york",
    "brooklyn": "new york",
    "queens": "new york",
    "bronx": "new york",
    "staten island": "new york",
    "newark": "new york",
    "la": "los angeles",
    "l.a.": "los angeles",
    "dc": "washington",
    "d.c.": "washington",
    "washington dc": "washington",
    "washington d.c.": "washington",
    "district of columbia": "washington",
    "st. louis": "saint louis",
    "st louis": "saint louis",
    "st. paul": "saint paul",
    "st paul": "saint paul",
    "ft. worth": "fort worth",
    "ft worth": "fort worth",
    "ft. lauderdale": "fort lauderdale",
    "ft lauderdale": "fort lauderdale",
    "dallas-fort worth": "dallas",
    "dallas fort worth": "dallas",
    "dfw": "dallas",
    "minneapolis-saint paul": "minneapolis",
    "twin cities": "minneapolis",
    "vegas": "las vegas",
    "philly": "philadelphia",
    "sf": "san francisco",
    "bay area": "san francisco",
    "atl": "atlanta",
    "nola": "new orleans",
    "phx": "phoenix",
    "slc": "salt lake city",
    "raleigh-durham": "raleigh",
    "winston-salem": "greensboro",
    "research triangle": "raleigh",
    "hampton roads": "norfolk",
    "miami dade county": "miami-dade county",
    "miami dade": "miami-dade county",
}

_SUFFIX_RE = re.compile(
    r"\s+(county|parish|borough|municipality)\s*$", re.IGNORECASE
)
_PUNCT_RE = re.compile(r"[^\w\s.\-]")
_WS_RE = re.compile(r"\s+")

# A trailing US state, comma-separated. Stripped for matching but returned
# separately so callers can keep it for display and disambiguation.
_STATE_RE = re.compile(r",\s*([A-Za-z]{2})\.?\s*$")


def normalise(name: str) -> str:
    """Canonical match key: lowercase, punctuation-stripped, whitespace-collapsed.

    County suffixes are NOT stripped, because CITY_AIRPORTS carries county keys
    in their own right. The old code stripped them, which turned
    "orange county" into "orange" and then prefix-matched it to "orlando".
    """
    text = name.strip().lower()
    text = _STATE_RE.sub("", text)
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def split_state(name: str) -> tuple[str, str | None]:
    """Split a trailing two-letter state off a name: 'Phoenix, AZ' -> ('Phoenix', 'AZ')."""
    match = _STATE_RE.search(name.strip())
    if not match:
        return name.strip(), None
    return name[: match.start()].strip(), match.group(1).upper()


def _candidates(key: str) -> list[str]:
    """Table keys for which `key` is a prefix, or which are a prefix of `key`.

    Both directions, because input may be more specific than the table
    ("phoenix metro") or less ("maricopa" for "maricopa county").
    """
    return [k for k in CITY_AIRPORTS
            if k.startswith(key) or key.startswith(k)]


def resolve_city(
    name: str, equivalents: Equivalents = NO_EQUIVALENTS
) -> tuple[str | None, str]:
    """Resolve a name to a canonical table key.

    Returns (canonical_key, method) where method is one of TABLE, ALIAS, PREFIX
    or UNRESOLVED, matching the geo_cache.resolved_by vocabulary so the decision
    is auditable.
    """
    key = normalise(name)
    if not key:
        return None, "UNRESOLVED"
    if key in CITY_AIRPORTS:
        return key, "TABLE"
    if key in CITY_ALIASES:
        target = CITY_ALIASES[key]
        return (target, "ALIAS") if target in CITY_AIRPORTS else (None, "UNRESOLVED")
    # Also strip the suffix and retry aliases/exact, so "Miami Dade Parish"
    # style input still lands.
    stripped = _SUFFIX_RE.sub("", key).strip()
    if stripped != key:
        if stripped in CITY_AIRPORTS:
            return stripped, "TABLE"
        if stripped in CITY_ALIASES and CITY_ALIASES[stripped] in CITY_AIRPORTS:
            return CITY_ALIASES[stripped], "ALIAS"
    if len(key) < MIN_PREFIX_LEN:
        return None, "UNRESOLVED"
    matches = _candidates(key)
    if len(matches) == 1:
        return matches[0], "PREFIX"
    if len(matches) > 1 and len(
            {equivalents.unit_of(m) for m in matches}) == 1:
        # 9.9. Every candidate names ONE unit, so the ambiguity is apparent
        # rather than real, and refusing to choose between two names for one
        # place is a refusal that helps nobody.
        #
        # The LONGER candidate wins — the more specific name, and the one the
        # input was reaching for. This does not loosen the real ambiguity rule:
        # a prefix matching four DIFFERENT units still resolves to nothing.
        #
        # With no mission loaded there are no equivalences, every candidate is
        # its own unit, and this branch cannot fire. That is deliberate: the
        # engine makes no claim that two names mean one place.
        return max(matches, key=len), "PREFIX"
    # Zero matches, or two or more naming different places. Ambiguity is not
    # resolved by guessing.
    return None, "UNRESOLVED"


def city_to_airports(name: str, limit: int | None = None) -> list[str]:
    """Serving airport IATA codes for a city or county. Empty if unresolvable."""
    key, _ = resolve_city(name)
    if key is None:
        return []
    codes = list(CITY_AIRPORTS[key])
    return codes[:limit] if limit else codes


def city_to_pickup_location(name: str) -> str | None:
    """Car rental pickup point for a city: its primary airport IATA code.

    Priceline's car search accepts coordinates, a location name, or an
    airport code, and echoes the code back as pickupLocation.airportCode — so
    the airport code is an exact round-trip and needs no autocomplete call.
    Airport fleets also book out before off-airport ones, which makes them the
    leading indicator rather than merely the convenient one.

    Returns None when the city has no airport mapping; the caller then falls
    back to the autocomplete endpoint and, failing that, marks the query
    SKIPPED_NO_MAPPING.
    """
    codes = city_to_airports(name, limit=1)
    return codes[0] if codes else None


def lodging_location_string(
    city_name: str, state: str | None = None, key_location: str | None = None
) -> str:
    """Build the free-text `location` for Staying's /search.

    Anchoring on the key location rather than the city centre is the point:
    convergence shows up as scarcity near the facility, not city-wide.
    """
    parts: list[str] = []
    if key_location:
        parts.append(key_location)
    parts.append(city_name)
    if state:
        parts.append(state)
    return ", ".join(parts)
