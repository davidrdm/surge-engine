"""Competing explanations for a correlation (9.6, issue #2).

Nothing on an alert recorded that a lodging or car surge could be a convention,
a home game, a holiday weekend, a weather evacuation or a routine military
exercise. The reader was shown a score and its arithmetic, and left to supply
the alternatives themselves — which is exactly the point at which confirmation
bias operates, because the reader who most needs the alternatives is the one
already persuaded.

**One mitigation exists and is stated here rather than counted as a fix.**
Baselines are weekday-aligned at +7 and +14 days precisely so that ordinary
weekend demand does not read as a surge, and both are stored because divergence
between them is itself informative. That *suppresses* one confound in the
arithmetic. It does not *record* the hypothesis for a reader, and the two are
different jobs: the first protects the score, the second protects the judgement
made from it.

**Deterministic, and derived only from which families contributed.** A
booking-only correlation admits ordinary-demand explanations that a
military-flight correlation does not, and that mapping is a rule, not an
inference. Asking the model to generate alternatives would put a second
uncontrolled judgement on the alert surface — the one place this system has
been careful to keep the language model out of the number.

What this is not: a probability, a ranking, or a claim that any alternative is
true. It is the list of things that would also produce this evidence, with what
in *this* correlation argues against each, so a reader can rule them out
deliberately rather than never consider them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence

#: Bumped when the rules below change, so an alert can be re-read under the
#: rules that produced its list rather than under today's.
RULES_VERSION = "hypotheses/1"


@dataclass(frozen=True)
class Alternative:
    """One competing explanation for the same evidence."""

    #: Stable identifier, so a reviewer can filter or suppress one across
    #: alerts without matching on prose.
    code: str
    #: What else would produce this pattern.
    statement: str
    #: What in THIS correlation argues against it. Empty when nothing does —
    #: which is the honest answer more often than not, and a reader is better
    #: served by an unanswered alternative than by a manufactured rebuttal.
    weakened_by: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


#: When one of a mission's alternatives applies. A CLOSED, engine-owned
#: vocabulary: the mission writes the explanation, the engine decides whether
#: this correlation's evidence admits it, and these are the only conditions the
#: engine can actually evaluate.
#:
#:   ALWAYS                       whenever this family contributed.
#:   SOLE_FAMILY                  only when this family is ALL of the evidence.
#:                                A rumour hypothesis alongside a movement
#:                                signal has to explain the movement too, and
#:                                it cannot — so it is not offered.
#:   FLIGHT_CATEGORY_UNCONFIRMED  only when no flight record had its category
#:                                confirmed by the vendor.
CONDITIONS: frozenset[str] = frozenset({
    "ALWAYS", "SOLE_FAMILY", "FLIGHT_CATEGORY_UNCONFIRMED",
})


#: The order a reader meets them in: booking-market explanations first, because
#: they are the cheapest and the most often right, then movement, then chatter.
FAMILY_ORDER: tuple[str, ...] = ("LODGING", "CAR", "FLIGHT", "SOCIAL")


def _families(signals: Iterable[Mapping[str, Any]]) -> set[str]:
    return {str(row["signal_type"]) for row in signals
            if row["signal_type"] is not None}


#: Appended when more than one family contributed. Engine-owned because it is
#: a fact about the CORRELATION rather than about any mission: an explanation
#: for a two-family finding has to account for both, and one that explains only
#: the cheaper half is weaker for it.
#:
#: Weakened, not removed. "Does not explain all of it" is not "is false", and a
#: reader deciding whether to act is entitled to both.
CORROBORATION_NOTE = (
    "Evidence came from {n} independent families; an explanation has to "
    "account for all of them, not only this one.")


def for_correlation(
    signals: Sequence[Mapping[str, Any]],
    contributions: Mapping[str, float] | None = None,
    mission: Any = None,
) -> list[dict[str, str]]:
    """The competing explanations this correlation's evidence admits.

    The mission writes the explanations, per family. The engine decides which
    of them this evidence actually admits: which families contributed, whether
    more than one did, and whether any flight category was confirmed.

    An empty list means no signal family contributed, which happens for a
    correlation scored entirely out of a coverage gap — and a list of
    alternatives to nothing would be worse than silence.

    Without a mission the list is empty rather than invented. A competing
    explanation the engine made up would read exactly like one an analyst
    wrote.
    """
    families = _families(signals)
    catalogue = getattr(mission, "hypotheses", None) or {}
    if not families or not catalogue:
        return []

    corroborated = len(families) > 1
    confirmed = any(
        row["signal_type"] == "FLIGHT"
        and (row["category_confidence"] if "category_confidence"
             in row.keys() else None) == "CONFIRMED"
        for row in signals)

    out: list[Alternative] = []
    for family in FAMILY_ORDER:
        if family not in families:
            continue
        for entry in catalogue.get(family, ()):
            when = str(entry.get("when") or "ALWAYS").upper()
            if when == "SOLE_FAMILY" and families != {family}:
                continue
            if when == "FLIGHT_CATEGORY_UNCONFIRMED" and confirmed:
                continue
            weakened = str(entry.get("weakened_by") or "")
            if corroborated:
                note = CORROBORATION_NOTE.format(n=len(families))
                weakened = f"{weakened} {note}".strip()
            out.append(Alternative(str(entry["code"]),
                                   str(entry["statement"]), weakened))

    # Deduplicated by code: two families may offer the same explanation, and a
    # reader meeting it twice would read repetition as emphasis.
    seen: set[str] = set()
    unique = [item for item in out
              if not (item.code in seen or seen.add(item.code))]
    return [item.as_dict() for item in unique]


def describe(alternatives: Sequence[Mapping[str, str]]) -> str:
    """One line naming the codes, for a log or a caveat."""
    codes = [str(item.get("code")) for item in alternatives if item.get("code")]
    return ", ".join(codes)
