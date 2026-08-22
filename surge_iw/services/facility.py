"""Matching a model-named facility to a registered key location.

The old rule was a bidirectional substring test returning the first row in
`location_id` order — insertion order. Given `North Exhibition Center` and
`South Exhibition Center`, a model saying `"Exhibition Center"` attached it to
whichever the operator happened to register first. A registered `"City Hall"`
matched a model's `"the city hall annex parking structure downtown"`, and vice
versa.

This is the same bug `services/geo.py` was written to kill, and its docstring is
the post-mortem: *"which returns the first dict entry that shares a prefix in
either direction... The failure is silent: the caller gets a confident answer for
the wrong city."* So this is the same ladder, applied to facilities.

**Severity, stated accurately.** Today a social signal carries no
`distance_km`, and `scoring.spatially_anchored` treats a missing distance as
anchored — so a wrong `location_id` does *not* change the score. What it does is
tell an operator, in an alert and in the evidence drill-down, that people are
massing at a facility they are not massing at. It becomes a scoring error the
moment key-location coordinates are used for social signals, which is a
plausible next step and not one that should have to remember this.

**No match is a good outcome.** The signal still attaches to the city; it only
loses a facility attribution it was never entitled to. Returning nothing when
the answer is ambiguous is strictly better than returning one of two.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: Bumped when the rules below change. Stamped on the signal alongside the
#: method, so a match can be re-judged under the rules that made it.
RULES_VERSION = "facility/2"

#: A candidate shorter than this resolves only by exact match. Mirrors
#: geo.MIN_PREFIX_LEN, and stops a one-character candidate — which the old code
#: could produce from a bare string iterated as characters — matching anything.
MIN_CANDIDATE_LEN = 6

#: Words that carry no discriminating power in ANY facility name: articles and
#: prepositions, administrative-unit words, the generic nouns every building is
#: called, and compass directions. A candidate made only of these is generic,
#: and matching it to a specific facility is a guess.
#:
#: This is the STRUCTURAL core and stays in the engine. Which further words are
#: too common to discriminate is domain knowledge — a word in almost every
#: facility name in one domain is a discriminating one in another — so a mission ADDS to this set through
#: `facilities.yaml: tokens`. Adding rather than replacing, because every pack
#: restating "the" and "north" would be ceremony, and a pack that forgot to
#: would silently match on them.
GENERIC_TOKENS: frozenset[str] = frozenset({
    "the", "a", "an", "of", "at", "in", "on", "and", "for",
    "county", "city", "town", "state", "us", "usa", "united", "states",
    "center", "centre", "office", "building", "hall", "facility", "site",
    "department", "dept", "division", "annex", "branch", "location",
    "north", "south", "east", "west", "central", "main", "downtown",
})

#: Spelling variants, not aliases. An alias says two names mean the same place;
#: these say two spellings are the same word, and applying them per token is
#: what lets an EXACT match survive "Centre" against "Center".
#:
#: 9.7 / issue #6 extends the same table to abbreviations. `N. Exhibition
#: Center` returned NO_MATCH against a registered `North Exhibition Center`,
#: because
#: `north` is a generic token and `n` is not — so the containment rung compared
#: `{"n"}` against an empty set and correctly refused. Expanding the
#: abbreviation makes it an EXACT match instead, one rung above where the
#: problem was, and none of the uniqueness or specificity rules move.
#:
#: This is separate from a mission's ALIASES for a mechanical reason: an
#: abbreviation can appear at any position, while an alias matches the whole
#: normalised string. Aliasing `n. riverside center` would fix that one name
#: and nothing else.
#:
#: These are generic English and administrative abbreviations, so they stay in
#: the engine. A mission adds its own through `facilities.yaml: spellings`: an
#: abbreviation for the kind of facility a particular mission watches belongs
#: to that mission and to no other.
#:
#: Expansion is applied to the registered name as well as the candidate, so a
#: facility an operator registered *as* `N. Riverside Center` still matches
#: itself. What it cannot do is invent a distinction: two facilities that differ
#: only by an abbreviation collapse to one key and are refused as AMBIGUOUS,
#: which is the operator's data problem made visible rather than a guess.
_SPELLINGS: dict[str, str] = {
    "centre": "center",
    "dept": "department",
    "co": "county",
    "ctr": "center",
    "bldg": "building",
    "ofc": "office",
    # Directional. Single letters are safe because the expansion is applied to
    # both sides of every comparison — the risk would be inventing a match
    # between two different facilities, and any pair that collapses is refused.
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "ne": "northeast",
    "nw": "northwest",
    "se": "southeast",
    "sw": "southwest",
    "no": "north",
    "so": "south",
    # Institutional. Deliberately excludes two kinds. The genuinely ambiguous
    # — `comm` (commission/committee/community), `reg` (regional/registry),
    # `sec` (secretary/section) — because an expansion that guesses wrong is
    # worse than the NO_MATCH it replaces. And the domain-specific: an
    # abbreviation that is only common in one mission's facility names belongs
    # in that pack's `facilities.yaml: spellings`, which is merged on top of
    # this table rather than replacing it.
    "natl": "national",
    "nat": "national",
    "govt": "government",
    "gov": "government",
    "admin": "administration",
    "muni": "municipal",
    "twp": "township",
    "dist": "district",
    "hq": "headquarters",
    "hdqtrs": "headquarters",
}

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class FacilityMatch:
    """A resolution, and how it was reached."""

    location_id: int | None
    #: EXACT, ALIAS, CONTAINED, AMBIGUOUS, TOO_GENERIC, NO_MATCH or NONE_GIVEN.
    #: The refusals are named separately from the misses because an operator
    #: asking "why is this post not attached to a facility" deserves the reason.
    method: str

    @property
    def matched(self) -> bool:
        return self.location_id is not None


def normalise(name: Any, aliases: Mapping[str, str] | None = None,
              spellings: Mapping[str, str] | None = None) -> str:
    """Canonical match key: lowercase, punctuation-stripped, collapsed."""
    text = str(name or "").strip().lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    table = spellings if spellings is not None else _SPELLINGS
    text = " ".join(table.get(word, word) for word in text.split())
    return (aliases or {}).get(text, text)


def _significant(text: str, extra: frozenset[str] = frozenset()) -> set[str]:
    """The tokens that actually discriminate between facilities."""
    generic = GENERIC_TOKENS | extra
    return {word for word in text.split() if word not in generic}


def resolve(
    candidates: Sequence[Any] | None,
    registered: Sequence[Mapping[str, Any]],
    mission: Any = None,
) -> FacilityMatch:
    """Resolve the first candidate that resolves unambiguously.

    The ladder, most reliable first — and every rung requires *uniqueness*,
    which is the property the old code lacked:

      EXACT       normalised candidate equals a registered name
      ALIAS       equals a registered name after alias substitution
      CONTAINED   the candidate's significant words are a subset of exactly one
                  registered name's, and there is at least one of them

    Anything else returns no location with a reason. Results do not depend on
    the order facilities were registered: containment is evaluated against all
    of them before any answer is given.
    """
    if not candidates:
        return FacilityMatch(None, "NONE_GIVEN")
    if not registered:
        return FacilityMatch(None, "NO_MATCH")

    # Which spellings mean one place, and which words are too common to
    # discriminate, are the mission's. Absent one the ladder still runs: EXACT
    # and CONTAINED work on structure alone, and only the ALIAS rung goes
    # quiet — which costs a match, never invents one.
    aliases = getattr(mission, "facility_aliases", None) or {}
    extra = getattr(mission, "facility_tokens", None) or frozenset()
    spellings = {**_SPELLINGS,
                 **(getattr(mission, "facility_spellings", None) or {})}

    rows = [
        (int(row["location_id"]), normalise(row["name"], aliases, spellings))
        for row in registered
    ]
    outcome = FacilityMatch(None, "NO_MATCH")

    for raw in candidates:
        candidate = normalise(raw, aliases, spellings)
        if not candidate:
            continue

        exact = [lid for lid, name in rows if name == candidate]
        if len(exact) == 1:
            return FacilityMatch(exact[0], "EXACT")
        if len(exact) > 1:
            # Two facilities registered under one name. Refuse rather than
            # pick; the operator has a data problem worth seeing.
            outcome = FacilityMatch(None, "AMBIGUOUS")
            continue

        if len(candidate) < MIN_CANDIDATE_LEN:
            outcome = _weaker(outcome, FacilityMatch(None, "TOO_GENERIC"))
            continue

        wanted = _significant(candidate, extra)
        if not wanted:
            # A candidate whose every word is generic, so it
            # names a category rather than a place.
            outcome = _weaker(outcome, FacilityMatch(None, "TOO_GENERIC"))
            continue

        contained = [lid for lid, name in rows
                     if wanted and wanted <= _significant(name, extra)]
        if len(contained) == 1:
            return FacilityMatch(contained[0], "CONTAINED")
        if len(contained) > 1:
            outcome = _weaker(outcome, FacilityMatch(None, "AMBIGUOUS"))

    return outcome


def _weaker(current: FacilityMatch, proposed: FacilityMatch) -> FacilityMatch:
    """Keep the most informative refusal seen so far."""
    rank = {"NO_MATCH": 0, "TOO_GENERIC": 1, "AMBIGUOUS": 2}
    if rank.get(proposed.method, 0) > rank.get(current.method, 0):
        return proposed
    return current
