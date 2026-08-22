"""Deterministic spatial/temporal correlation and confidence scoring.

No LLM is involved here, and none may be. The LLM's contribution to an alert is
the prose summary; the number is computed by this module and copied verbatim.

The model is the same shape for every mission: each scoring kind contributes
`weight x quality`, the contributions sum to a score, and the score plus the
count of distinct families that produced it selects a band. What differs per
mission is the numbers — the per-track weights in `TrackModel`, and the band
thresholds read from `cfg` — because what a military-coded airframe is worth,
and whether a track flies at all, is exactly the judgement a mission makes.
Neither the weights nor the thresholds live here any more; a mission pack
supplies both, and its own documentation is where their derivation belongs.

The scoring KINDS are engine vocabulary and do not move: they name the four
data families this system collects, with FLIGHT split by FR24 category because
a military-coded airframe and a business jet are different evidence.

A score is a starting hypothesis, not a calibrated model. There is no labelled
ground truth for "the thing being warned about was in fact imminent", every
input is persisted, and the number must not be presented to a reader as a
probability.

Two safety properties matter more than any tuning, and are the engine's rather
than the mission's:

  * A connector failure is never scored as absence of a signal. A failed query
    produces no signals row, but its source family is reported as unreliable,
    which lowers data_completeness and caps the band below HIGH.

  * A flight category is never assumed. FR24's live flight-positions response
    carries no `category` field — verified against its OpenAPI spec — so a live
    record cannot prove what it is. Such records score at the lowest weight
    reachable by the query that produced them, never the highest.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..db import enums
from ..services import provenance

# Scoring kinds. Distinct from signal_type because flights split by category.
KIND_SOCIAL = "social"
KIND_FLIGHT_M = "flight_M"
KIND_FLIGHT_J = "flight_J"
KIND_FLIGHT_AMBIGUOUS = "flight_AMBIGUOUS"
KIND_LODGING = "lodging"
KIND_CAR = "car"

# Which signal family each kind belongs to. "All four signals" in the original
# prompt means four families, so a city with both an M and a J flight still
# counts as one flight family rather than two.
KIND_FAMILY: dict[str, str] = {
    KIND_SOCIAL: "SOCIAL",
    KIND_FLIGHT_M: "FLIGHT",
    KIND_FLIGHT_J: "FLIGHT",
    KIND_FLIGHT_AMBIGUOUS: "FLIGHT",
    KIND_LODGING: "LODGING",
    KIND_CAR: "CAR",
}

FAMILIES: tuple[str, ...] = ("SOCIAL", "FLIGHT", "LODGING", "CAR")

# Map a query's source_type back to the family it was collecting for, so that a
# failed FLIGHT_COUNT and a failed FLIGHT_LIVE both register as one FLIGHT gap.
SOURCE_TYPE_FAMILY: dict[str, str] = {
    "SOCIAL": "SOCIAL",
    "FLIGHT_COUNT": "FLIGHT",
    "FLIGHT_LIVE": "FLIGHT",
    "FLIGHT_HISTORY": "FLIGHT",
    "LODGING": "LODGING",
    # Price and availability are two measurements of one thing, so a failed
    # price query is the SAME coverage gap as a failed availability query.
    "LODGING_PRICE": "LODGING",
    "CAR": "CAR",
}

#: Which source types a stage is responsible for producing (8.2).
#:
#: A stage that does not run is a coverage gap even when every query it DID
#: issue succeeded — and that case has no other trace. Measured live: a
#: cancelled iteration collected 12 social queries, skipped TRIAGING and
#: TIPPING, and reported no correlation at all. Every existing gap detector
#: missed it, each for a different and individually correct reason: the queries
#: were COMPLETE so `unreliable_source_types` saw nothing; TRIAGING wrote no
#: decisions so `triage_uncovered` counted zero; and TIPPING enqueued nothing,
#: so there was no refusal for `refused_source_types` to find. The run read as
#: a quiet city.
STAGE_SOURCE_TYPES: dict[str, tuple[str, ...]] = {
    "COLLECTING_SOCIAL": ("SOCIAL",),
    # Collected but never judged is not "nothing relevant was said".
    "TRIAGING": ("SOCIAL",),
    # Tipping is what enqueues the paid families at all, so skipping either it
    # or the collection it feeds loses all three.
    "TIPPING": ("FLIGHT_LIVE", "LODGING", "CAR"),
    "COLLECTING_TIPPED": ("FLIGHT_LIVE", "LODGING", "CAR"),
}


def source_types_for_skipped(stages: "Iterable[str]") -> list[str]:
    """Source types left uncollected by stages that did not run."""
    out: set[str] = set()
    for stage in stages:
        out.update(STAGE_SOURCE_TYPES.get(stage, ()))
    return sorted(out)


@dataclass(frozen=True)
class TrackModel:
    """One track's scoring parameters, as the mission defined them.

    These used to be a module-level `WEIGHTS` dict keyed by the two tracks the
    system was built for. They are per-mission: what a military-coded airframe
    is worth, and whether a track flies at all, is precisely the judgement a
    mission makes. Passing the numbers in rather than looking them up also
    means `correlate()` has no global to disagree with.
    """

    #: The track's name, as stored in `signals.track` and `correlations.track`.
    name: str
    #: Scoring kind -> weight. Every kind in `KIND_FAMILY` is present; the
    #: mission loader refuses a table that omits one, because an absent weight
    #: and an explicit 0.0 score the same and mean opposite things.
    weights: Mapping[str, float]
    #: The FR24 category codes this track's flight queries ask for. Determines
    #: what an AMBIGUOUS record could have been, and therefore what it earns.
    flight_categories: tuple[str, ...]

    @classmethod
    def from_mission(cls, mission: Any, name: str) -> "TrackModel":
        """Build the model for one of a loaded mission's tracks."""
        mission.track(name)
        return cls(
            name=name,
            weights=dict(mission.weights[name]),
            flight_categories=tuple(mission.flight_categories[name]),
        )


#: FR24 categories that score as flight_J. VENDOR vocabulary, not a mission's:
#: FR24 gives military and government their own code and lumps business jets,
#: general aviation and helicopters into three others, and this is the engine
#: collapsing those three into one scoring kind. Which of them a given track
#: asks for is the mission's choice and lives in `TrackModel.flight_categories`.
_FLIGHT_J_CATEGORIES = frozenset({"J", "T", "H"})


def ambiguous_flight_weight(track: TrackModel) -> float:
    """Weight for a live flight record whose category cannot be known.

    The live-positions endpoint accepts a `categories` filter but does not
    return the category, so all that is known is that the aircraft matched the
    filter. Credit the record at the LOWEST weight any category in that filter
    would have earned — never the highest.

    A track whose filter is M,J earns the business-jet weight for a record it
    cannot verify, rather than the military one. A track whose filter is J,T,H
    pays no penalty at all, because those three score identically: the
    ambiguity is real but analytically irrelevant.
    """
    weights = track.weights
    categories = track.flight_categories
    reachable = {
        KIND_FLIGHT_M if code == "M" else KIND_FLIGHT_J for code in categories
    }
    return min(weights[kind] for kind in reachable)


def kind_weight(track: TrackModel, kind: str) -> float:
    if kind == KIND_FLIGHT_AMBIGUOUS:
        return ambiguous_flight_weight(track)
    return track.weights.get(kind, 0.0)


def anchor_flight_kind(track: TrackModel) -> str:
    """The flight kind that counts as strong evidence on this track.

    Track-aware because a military-coded airframe can be the decisive
    indicator on one track and meaningless on another. The anchor is whichever
    flight kind carries the most weight on this track, which the mission
    decided when it wrote the weights.
    """
    weights = track.weights
    return max(
        (KIND_FLIGHT_M, KIND_FLIGHT_J), key=lambda kind: weights.get(kind, 0.0)
    )


def has_strong_anchor(track: TrackModel,
                      contributions: Mapping[str, float]) -> bool:
    """Whether the evidence includes something that names an actor.

    A strong anchor is social chatter, or a flight whose category was actually
    verified and matters on this track. Booking scarcity is deliberately not an
    anchor: hotel and rental-car availability collapse for conventions, holidays
    and home games, so on its own it cannot escalate a city.

    Note that AMBIGUOUS flights never anchor. A live-positions record cannot
    prove its own category, so it must not stand in for the verified airframe
    the band rules are built around.
    """
    if contributions.get(KIND_SOCIAL, 0.0) > 0.0:
        return True
    return contributions.get(anchor_flight_kind(track), 0.0) > 0.0


def scoring_kind(signal: Mapping[str, Any]) -> str | None:
    """Which scoring kind a signal row contributes to, or None if it cannot."""
    signal_type = signal.get("signal_type")
    if signal_type == "SOCIAL":
        return KIND_SOCIAL
    if signal_type == "LODGING":
        return KIND_LODGING
    if signal_type == "CAR":
        return KIND_CAR
    if signal_type != "FLIGHT":
        return None
    category = signal.get("flight_category")
    confidence = signal.get("category_confidence")
    if category == "AMBIGUOUS" or confidence == "AMBIGUOUS":
        return KIND_FLIGHT_AMBIGUOUS
    if category == "M":
        return KIND_FLIGHT_M
    if category in _FLIGHT_J_CATEGORIES:
        return KIND_FLIGHT_J
    return KIND_FLIGHT_AMBIGUOUS


# ---------------------------------------------------------------------------
# Temporal decay (9.5)
# ---------------------------------------------------------------------------

#: Key under which each eligible signal carries its age weight. Attached to a
#: shallow copy inside `correlate()`, never to the caller's row.
DECAY_KEY = "_decay"


def decay_weight(
    age_hours: float, window_hours: float, edge_weight: float
) -> float:
    """How much a signal of this age still counts, 0..1.

        weight(age) = edge_weight ** (age / window_hours)

    **The curve is a function of the window, and that is the whole design.**
    `window_hours` already states how far back evidence is considered relevant;
    a second, independent decay parameter could contradict it, and a curve
    chosen without reference to the window would silently re-narrow one that
    had been widened deliberately — which is exactly what happened when the
    window went from 48 to 168 hours because two corroborated reports scored
    nothing at five days old.

    Tying the two together means the operator sets ONE thing. The curve is
    self-similar under window scaling — the weight at a given *fraction* of the
    window is constant — so in absolute time it is steep for a short window and
    shallow for a long one:

        48h  tactical      24h old -> 0.32     (half-life 14.4h)
        168h situational   24h old -> 0.72     (half-life 50.6h)

    A 48-hour window is a claim that deployment is imminent, and a day-old
    booking surge is weak evidence for that. A 168-hour window is a claim about
    an established operation, and a day-old surge is most of the picture. Both
    fall out of the same expression.

    `edge_weight` is the weight at the window edge, which is a more useful knob
    than a half-life because it also sets the size of the discontinuity that
    remains. Decay does not remove the hard cutoff — the window is still what
    bounds the query — it shrinks the cliff from 1.0 to `edge_weight`. At 1.0
    there is no decay at all and the old step function is restored exactly,
    which is why there is no separate on/off flag: a boolean and a value that
    can disagree is one more thing to get wrong.
    """
    edge = min(1.0, max(_MIN_EDGE_WEIGHT, float(edge_weight)))
    if edge >= 1.0 or window_hours <= 0:
        return 1.0
    # Absolute, because `in_window` is symmetric: a lodging row is stamped at
    # collection time, which is minutes AFTER the anchor, and a signed age
    # would hand it a weight above 1.
    age = abs(float(age_hours))
    if age >= float(window_hours):
        return edge
    return float(edge ** (age / float(window_hours)))


#: Below this the curve is a step function in disguise — everything but a
#: brand-new signal rounds to nothing — and 0 would make `0 ** 0` the only
#: surviving weight.
_MIN_EDGE_WEIGHT = 0.001


def signal_decay(
    signal: Mapping[str, Any], anchor: datetime, window: timedelta,
    edge_weight: float,
) -> float:
    """The age weight for one signal, or 1.0 if it cannot be dated.

    An undated signal never reaches here — `in_window` already excludes it —
    so the fallback is unreachable in practice and exists so this function is
    total.
    """
    from ..db.database import parse_iso      # local import avoids a cycle

    observed = signal.get("observed_at")
    if isinstance(observed, str):
        observed = parse_iso(observed)
    if not isinstance(observed, datetime):
        return 1.0
    age_hours = abs((anchor - observed).total_seconds()) / 3600.0
    return decay_weight(age_hours, window.total_seconds() / 3600.0,
                        edge_weight)


def decay_of(row: Mapping[str, Any]) -> float:
    """The weight `correlate()` attached, or 1.0 for a row it did not touch.

    Defaulting to 1.0 keeps every quality function callable on its own — the
    tests do exactly that — and means a caller who forgets gets the previous
    behaviour rather than silent zeroes.
    """
    value = row.get(DECAY_KEY)
    return 1.0 if value is None else max(0.0, min(1.0, float(value)))


# ---------------------------------------------------------------------------
# Per-kind quality, 0..1
# ---------------------------------------------------------------------------


def corroboration_quality(
    count: float, full_scale: float, single_floor: float
) -> float:
    """Map a count of independent observations onto 0..1.

    A single observation must not score near zero. Intelligence practice treats
    one named source from an established outlet as STANDARD confidence and two
    or more independent sources as HIGHER — not as a third of a signal. A naive
    `count / full_scale` curve gives a lone credible report 0.33, which is low
    enough that the original prompt's "a single social signal is LOW confidence"
    case could never fire at all.

    So the curve starts at `single_floor` for one observation and reaches 1.0 at
    `full_scale`:

        n=1 -> 0.6,  n=2 -> 0.8,  n=3+ -> 1.0   (floor 0.6, full_scale 3)

    `count` became a FLOAT with 9.5: temporal decay counts a day-old aircraft
    as a fraction of an aircraft. Below one observation the floor is scaled
    down proportionally rather than applied whole — otherwise a signal the
    curve had already discounted to a tenth would be rescued back to 0.6 by the
    very rule that exists to stop ONE credible source reading as a third of a
    signal. The two meet exactly at count=1, so the curve stays continuous.
    """
    if count <= 0:
        return 0.0
    if count < 1.0:
        return max(0.0, min(1.0, single_floor * count))
    if full_scale <= 1.0:
        return 1.0
    extra = min(1.0, (count - 1) / (full_scale - 1.0))
    return max(0.0, min(1.0, single_floor + (1.0 - single_floor) * extra))


def social_quality(rows: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any]) -> float:
    """Corroboration breadth times peak salience.

    Peak rather than mean salience: one specific, credible post about a
    anchor facility is the signal, and averaging it against tangential
    mentions of the same city would dilute exactly the evidence that matters.

    Breadth is the LOWER of distinct publishers and distinct claims. Counting
    raw host strings made `www.apnews.com` and `apnews.com` two sources, and
    counting publishers alone would let one wire story syndicated to three
    outlets read as three independent observations. Forty reposts of one claim
    are one claim, and so are three mastheads carrying one dispatch.
    """
    if not rows:
        return 0.0
    # 9.5: each distinct publisher and claim counts at the weight of its
    # freshest observation. The BREADTH decays and the salience does not, and
    # applying it to only one of the two factors is the point.
    #
    # Decay measures how much an observation still counts as evidence, and the
    # breadth term is literally the count of observations — the same quantity
    # `flight_quality` decays, through the same function. Salience is a
    # property of what the post SAYS: a four-day-old report is exactly as
    # specific and as credible as it was when written, only less current, and
    # currency is what breadth is already carrying.
    #
    # Decaying both was the first implementation and it was wrong. Because
    # quality is their product, social decayed as the SQUARE of the weight
    # while flight, lodging and car decayed linearly — a signal at 0.12 of full
    # weight contributed 1.4% rather than 12%, so the one family that most
    # often stands alone was penalised for age roughly eight times harder than
    # the rest. `test_decay.py` now pins the linear invariant across all four.
    #
    # The ordering this was meant to buy — a fresh specific post ranking above
    # a stale strident one — belongs in the evidence drill-down, and lives
    # there: `correlate()` attributes per-signal shares by decay weight.
    publishers, claims = provenance.corroboration_weighted(rows, decay_of)
    breadth = corroboration_quality(
        min(publishers, claims),
        float(cfg["social_domains_full_scale"]),
        float(cfg["single_source_quality"]),
    )
    peak = max(float(r.get("salience") or 0.0) for r in rows)
    return max(0.0, min(1.0, breadth * peak))


#: 9.10. Kinds whose count is measured AGAINST A BASELINE rather than in
#: absolute terms. Military is deliberately absent: its baseline at a civilian
#: field is approximately zero, one military transport inbound is meaningful at
#: a count of one, and dividing by a near-zero normal would either destroy the
#: signal or explode it. AMBIGUOUS is included because an uncategorised
#: live-positions record is not military — it is exactly the general-aviation
#: background the baseline exists to subtract.
BASELINED_FLIGHT_KINDS: frozenset[str] = frozenset({
    KIND_FLIGHT_J, KIND_FLIGHT_AMBIGUOUS,
})


def median(values: Sequence[float]) -> float:
    """Middle value. Median rather than mean on the owner's decision: a mean
    lets one exceptional day drag the notion of normal with it, which is the
    contamination this baseline is built to resist."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (float(ordered[middle - 1]) + float(ordered[middle])) / 2.0


def flight_excess(observed: float, baseline: float, full_scale: float) -> float:
    """How far above normal this city's flight count is, 0..1 (9.10).

    Proportional, not absolute, and that is the point. `flight_quality` used a
    saturating count with `flight_full_scale` of 3, so three business jets at a
    quiet regional field and three at a major general-aviation hub scored
    identically — and the hub scored three every day of the year. Measured in
    live data: Atlanta returned 13, 13 and 14 distinct J-category airframes on
    three different days, all pinned at 1.0, a constant offset that says only
    that Atlanta has business-jet traffic.

    Against a baseline the same numbers say nothing happened, and a day with 26
    says something did. This is the lodging and car treatment applied to
    flights: a percentage change against a comparison window, saturating at a
    configured full scale.

    A baseline of zero falls back to nothing to divide by, so the caller keeps
    the absolute count — an airport with no normal traffic of a category makes
    any traffic of it excess.
    """
    if baseline <= 0 or full_scale <= 0:
        return 0.0
    excess_pct = max(0.0, (observed - baseline) / baseline * 100.0)
    return max(0.0, min(1.0, excess_pct / full_scale))


def flight_quality(rows: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any],
                   baseline: float | None = None) -> float:
    """Saturating count of distinct aircraft.

    Distinct by fr24_id or registration, so the same airframe seen by both the
    live and the historical query counts once. One military aircraft inbound is
    already meaningful, so the same single-observation floor applies.
    """
    if not rows:
        return 0.0
    # 9.5: each airframe counts at the weight of its freshest sighting, so
    # "three aircraft" that landed four days ago is not three aircraft now.
    # Freshest rather than summed for the same reason the identity is the
    # airframe: seeing one aircraft twice is one aircraft.
    seen: dict[str, float] = {}
    for index, row in enumerate(rows):
        key = (row.get("fr24_id") or row.get("registration")
               or row.get("callsign") or str(index))
        seen[key] = max(seen.get(key, 0.0), decay_of(row))
    count = sum(seen.values())
    # 9.10. With a usable baseline the family is scored as EXCESS over normal.
    # Without one — a cold start, or a category this city has never shown — the
    # absolute count stands, and the correlation records that it was not
    # baselined so a reader is never left to assume it was.
    if baseline is not None and baseline > 0:
        return flight_excess(count, baseline,
                             float(cfg.get("flight_excess_full_scale", 100.0)))
    return corroboration_quality(
        count,
        float(cfg["flight_full_scale"]),
        float(cfg["single_source_quality"]),
    )


def lodging_drop(rows: Sequence[Mapping[str, Any]]) -> float:
    """Percentage fall in available nights, near-term versus baseline.

    Aggregated over the whole listing set rather than averaged per listing: a
    single tiny listing going dark should not weigh as much as a large one, and
    summing available nights makes the denominator the thing actually being
    measured.
    """
    base = sum(int(r.get("base_available") or 0) for r in rows)
    near = sum(int(r.get("near_available") or 0) for r in rows)
    if base <= 0:
        return 0.0
    return max(0.0, (base - near) / base * 100.0)


def price_escalation(rows: Sequence[Mapping[str, Any]]) -> float:
    """Percentage rise in lodging price, near-term versus baseline.

    The second way of measuring the same demand pressure that `lodging_drop`
    measures. Aggregated over the property set rather than averaged per property,
    so one cheap room going up 300% does not outweigh a whole set holding steady.

    Only properties priced in BOTH windows count. A property priced in one window
    only says the property set moved, not that prices did — the same confound the
    pinned identity set exists to eliminate.
    """
    near_total = 0.0
    base_total = 0.0
    for row in rows:
        near = row.get("price_near")
        base = row.get("price_baseline")
        if near is None or base is None:
            continue
        try:
            near_value, base_value = float(near), float(base)
        except (TypeError, ValueError):
            continue
        if base_value <= 0:
            continue
        near_total += near_value
        base_total += base_value
    if base_total <= 0:
        return 0.0
    return max(0.0, (near_total - base_total) / base_total * 100.0)


def measurement_decay(
    rows: Sequence[Mapping[str, Any]],
    magnitude: "Callable[[Mapping[str, Any]], float]",
) -> float:
    """Age weight for a family whose quality is a RATIO, not a count (9.5).

    Lodging and car quality are not counts of observations — they are one
    measurement, "availability fell N%", taken at a moment. So the count-based
    rule does not apply: half a measurement is not a smaller drop, it is an
    older one. The measurement is decayed by its own age instead.

    Weighted by each row's share of the denominator the ratio is computed
    over, because `lodging_drop` and `car_drop` aggregate across the whole
    listing or fleet set. In the ordinary case — one iteration, one collection
    moment — every row carries the same weight and this returns exactly that.
    It only does real work when the correlation window spans two iterations,
    where it decays the stale half of a mixed measurement in proportion to how
    much of the measurement is stale.
    """
    total = 0.0
    weighted = 0.0
    for row in rows:
        size = max(0.0, float(magnitude(row)))
        total += size
        weighted += size * decay_of(row)
    if total <= 0:
        # No denominator, so the ratio is zero anyway and the weight cannot
        # matter. Freshest is the least surprising answer.
        return max((decay_of(r) for r in rows), default=1.0)
    return max(0.0, min(1.0, weighted / total))


def lodging_quality(
    rows: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any]
) -> float:
    """Quality for the lodging family: availability collapse OR price escalation.

    Two measurements of one thing, so the STRONGER is taken rather than a mean.
    Averaging would let a city with no calendar coverage — roughly fourteen
    listings in fifteen, measured live — dilute a real price signal toward zero
    with an availability figure that is missing rather than reassuring.

    Taking the max does mean either measurement alone can carry the family. That
    is intended: both are evidence of the same pressure, and requiring both would
    make the family unreachable wherever one source is thin.
    """
    availability = min(
        1.0, max(0.0, lodging_drop(rows) / float(cfg["lodging_drop_full_scale"]))
    )
    price_scale = float(cfg.get("price_escalation_full_scale", 40.0))
    price = min(1.0, max(0.0, price_escalation(rows) / price_scale)) \
        if price_scale > 0 else 0.0
    # 9.5. Each sub-measurement ages on its own denominator: availability on
    # nights offered, price on the baseline price. Decaying them separately
    # before the max matters when one is fresh and the other is not — taking
    # the stronger of two measurements should not let a stale one borrow a
    # fresh one's currency.
    availability *= measurement_decay(
        rows, lambda r: float(r.get("base_available") or 0.0))
    price *= measurement_decay(
        rows, lambda r: float(r.get("price_baseline") or 0.0))
    return max(availability, price)


def car_drop(rows: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any]) -> float:
    """Capacity-weighted fall in rental car availability, 0..100.

    A collapse in twelve-seat vans means something very different from a
    collapse in economy sedans, because moving people needs seats. Each vehicle
    class is weighted by peopleCapacity, and on-airport counters are weighted up
    because airport fleets book out before off-airport ones.

    Two exclusions, both to avoid manufacturing scarcity:
      * peer-to-peer listings — a private host going offline is not demand;
      * truncated responses — a pagination cut is not scarcity.
    """
    usable = [
        r for r in rows
        if not int(r.get("is_peer_to_peer") or 0)
        and not int(r.get("truncated") or 0)
    ]
    if not usable:
        return 0.0
    on_airport_weight = float(cfg["on_airport_weight"])

    def weight(row: Mapping[str, Any]) -> float:
        capacity = float(row.get("people_capacity") or 1)
        return capacity * (
            on_airport_weight if int(row.get("is_on_airport") or 0) else 1.0
        )

    base = sum(weight(r) * int(r.get("base_available") or 0) for r in usable)
    near = sum(weight(r) * int(r.get("near_available") or 0) for r in usable)
    if base <= 0:
        return 0.0
    return max(0.0, (base - near) / base * 100.0)


def kind_quality(
    kind: str, rows: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any],
    baseline: float | None = None,
) -> float:
    """Quality for a group of same-kind signals, before the spatial penalty."""
    if kind == KIND_SOCIAL:
        return social_quality(rows, cfg)
    if kind in (KIND_FLIGHT_M, KIND_FLIGHT_J, KIND_FLIGHT_AMBIGUOUS):
        # Only the non-military kinds are ever handed a baseline; see
        # BASELINED_FLIGHT_KINDS.
        return flight_quality(
            rows, cfg, baseline if kind in BASELINED_FLIGHT_KINDS else None)
    if kind == KIND_LODGING:
        return lodging_quality(rows, cfg)
    if kind == KIND_CAR:
        scale = float(cfg["car_drop_full_scale"])
        quality = max(0.0, min(1.0, car_drop(rows, cfg) / scale))
        # 9.5, on the same capacity-weighted denominator `car_drop` divides by,
        # so the ages that count are the ages of the seats that count.
        on_airport_weight = float(cfg["on_airport_weight"])

        def seats(row: Mapping[str, Any]) -> float:
            if int(row.get("is_peer_to_peer") or 0) or int(row.get("truncated") or 0):
                return 0.0
            capacity = float(row.get("people_capacity") or 1)
            multiplier = (on_airport_weight
                          if int(row.get("is_on_airport") or 0) else 1.0)
            return capacity * multiplier * int(row.get("base_available") or 0)

        return quality * measurement_decay(rows, seats)
    return 0.0


def independent_reports(
    grouped: Mapping[str, Sequence[Mapping[str, Any]]],
    contributions: Mapping[str, float],
) -> int:
    """How many INDEPENDENT reports this correlation rests on (9.8).

    The owner decision is that a single report cannot alert and two independent
    ones can — **whatever family they come from**. So independence is counted
    per family and summed, rather than being approximated by counting families:

      SOCIAL   the LOWER of distinct publishers and distinct claims. One wire
               story carried by three mastheads is three publishers and ONE
               claim, and is one report. Republication is not corroboration.
      FLIGHT   distinct airframes. Two aircraft inbound are two observations;
               seeing one aircraft twice is one.
      LODGING  one. However many listings it spans, it is a single availability
               measurement over one set, taken at one moment.
      CAR      one, for the same reason.

    Counting families instead was the first implementation and it was wrong in
    both directions: it refused eighteen distinct airframes over one airport as
    "a single report", and it would have accepted a lodging drop plus a car drop
    as two reports while calling two independent news outlets one.

    Only kinds that actually CONTRIBUTE are counted. A military flight carries
    no weight on a track the mission scored it zero for, and evidence that adds nothing to
    the score must not prop up the floor that decides whether the score alerts.
    """
    total = 0
    if contributions.get(KIND_SOCIAL, 0.0) > 0.0:
        publishers, claims = provenance.corroboration(grouped.get(KIND_SOCIAL, []))
        total += min(publishers, claims)

    airframes: set[str] = set()
    for kind in (KIND_FLIGHT_M, KIND_FLIGHT_J, KIND_FLIGHT_AMBIGUOUS):
        if contributions.get(kind, 0.0) <= 0.0:
            continue
        for index, row in enumerate(grouped.get(kind, [])):
            airframes.add(str(
                row.get("fr24_id") or row.get("registration")
                or row.get("callsign") or f"{kind}:{index}"))
    total += len(airframes)

    for kind in (KIND_LODGING, KIND_CAR):
        if contributions.get(kind, 0.0) > 0.0:
            total += 1
    return total


def spatially_anchored(
    rows: Sequence[Mapping[str, Any]], radius_km: float
) -> bool:
    """Whether any row sits within radius_km of a key location.

    A row with no distance recorded counts as anchored: social posts and flights
    are attributed to a city rather than measured against a facility, and
    penalising them for a distance that was never applicable would be wrong.
    Lodging and car rows do carry a distance, so an off-target booking cluster
    is correctly halved.
    """
    if not rows:
        return False
    for row in rows:
        distance = row.get("distance_km")
        if distance is None or float(distance) <= radius_km:
            return True
    return False


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class CorrelationResult:
    """The complete, auditable output of one city/track correlation."""

    track: str
    score: float
    band: str
    distinct_types: int
    contributions: dict[str, float]
    data_completeness: float
    failed_families: list[str]
    band_capped: bool
    rule_trace: str
    signal_contributions: dict[int, float] = field(default_factory=dict)
    earliest_eta: str | None = None
    #: 9.13. Whether this iteration contributed any evidence of its own, which
    #: iteration last did, and how old the contributing signals are. An alert
    #: that re-scores week-old collection is a legitimate current assessment,
    #: but a reader watching a queue must be able to tell it from a new
    #: observation without diffing two evidence surfaces.
    evidence_freshness: dict[str, Any] = field(default_factory=dict)
    #: 9.10. Per flight kind: whether it was scored against a baseline, what
    #: that baseline was, and what was observed. UNBASELINED means the absolute
    #: count stood — a cold start, or a category this city has never shown —
    #: and a reader must be able to tell the two apart.
    flight_baseline: dict[str, Any] = field(default_factory=dict)
    #: 9.11. Every failed collection as `SOURCE_TYPE:endpoint`, which is finer
    #: than `failed_families` and is what a reader actually needs: `LODGING`
    #: covers two endpoints that fail for different reasons at very different
    #: rates. Defaulted so the field can be added without disturbing the
    #: positional constructor every test uses.
    failed_sources: list[str] = field(default_factory=list)

    @property
    def is_alertable(self) -> bool:
        return self.band in enums.ALERT_BANDS

    def caveat(self) -> str | None:
        """Deterministic data-gap disclosure for the alert payload.

        Written here rather than by the LLM so that it cannot be softened,
        omitted, or paraphrased into something less alarming.
        """
        stale = staleness_note(self.evidence_freshness)
        if not (self.failed_sources or self.failed_families or stale):
            return None
        # 9.11. A family whose other endpoint succeeded is DEGRADED, not
        # absent. Saying "lodging unavailable" when the price measurement came
        # back is wrong in the direction that matters: it tells a reader
        # nothing is known about something that was in fact measured.
        partial = sorted(
            entry for entry in self.failed_sources
            if SOURCE_TYPE_FAMILY.get(entry.split(":", 1)[0])
            not in self.failed_families
        )
        text = ""
        if self.failed_families:
            sources = ", ".join(f.lower() for f in self.failed_families)
            text = (
                f"Collection incomplete: {sources} unavailable this iteration "
                f"(coverage {self.data_completeness:.0%}). Absence of those "
                f"indicators is not evidence of their absence."
            )
        if partial:
            text += (
                (" " if text else "")
                + f"Partly degraded: {', '.join(partial)} failed, but another "
                f"endpoint in that family was collected."
            )
        if self.band_capped:
            text += " Confidence capped below HIGH as a result."
        if stale:
            text += (" " if text else "") + stale
        return text or None


# ---------------------------------------------------------------------------
# The correlation itself
# ---------------------------------------------------------------------------


def staleness_note(freshness: Mapping[str, Any] | None) -> str:
    """The deterministic staleness disclosure, or "" when there is none (9.13).

    Written here beside the coverage caveat rather than left to the model, for
    the same reason: it must not be softened, paraphrased or dropped. And it is
    built from the stored record rather than recomputed, so an alert says what
    was true when it was written.

    Only fires when this iteration contributed NOTHING. A correlation that
    mixes new evidence with old is an ordinary current assessment and needs no
    disclosure; one that rests entirely on collection from previous runs is the
    same alert arriving again, and a reader watching a queue is entitled to be
    told which they are looking at.
    """
    if not freshness or freshness.get("new_this_iteration") is not False:
        return ""
    newest = freshness.get("newest_iteration")
    oldest_hours = freshness.get("oldest_hours")
    newest_hours = freshness.get("newest_hours")
    note = "No new evidence this iteration"
    if newest is not None:
        note += f"; nothing has contributed since iteration {newest}"
    if oldest_hours is not None and newest_hours is not None:
        span = (f"{newest_hours:g}h old" if oldest_hours == newest_hours
                else f"{newest_hours:g}-{oldest_hours:g}h old")
        note += f". The {freshness.get('signals', 0)} contributing signal(s) are {span}"
    return note + ". This is the same evidence re-scored, not a new observation."


def in_window(
    signal: Mapping[str, Any], anchor: datetime, window: timedelta
) -> bool:
    """Whether a signal falls inside the temporal correlation window.

    A signal with no observed_at is excluded. That is deliberate: an undated
    post cannot be placed relative to a 48-hour warning window, and treating it
    as current would let stale evidence inflate a live alert.
    """
    from ..db.database import parse_iso  # local import avoids a cycle

    observed = signal.get("observed_at")
    if isinstance(observed, str):
        observed = parse_iso(observed)
    if not isinstance(observed, datetime):
        return False
    return abs(observed - anchor) <= window


def correlate(
    signals: Iterable[Mapping[str, Any]],
    *,
    track: TrackModel,
    anchor_at: datetime,
    cfg: Mapping[str, Any],
    iteration_id: int | None = None,
    unreliable_source_types: Iterable[str] = (),
    failed_endpoints: Mapping[str, str] | None = None,
    collected_source_types: Iterable[str] = (),
    flight_baselines: Mapping[str, float] | None = None,
) -> CorrelationResult:
    """Score one city against one actor track.

    `signals` are rows for a single city. `unreliable_source_types` are the
    query source_types that failed or were skipped for that city, which is how
    a broken API key is prevented from reading as an absence of threat.
    """
    window = timedelta(hours=float(cfg["window_hours"]))
    radius_km = float(cfg["radius_km"])
    edge_weight = float(cfg.get("decay_edge_weight", 1.0))

    # Temporal gate, then track gate. UNKNOWN is admitted to every track: a
    # post that does not say which kind of activity it describes is still
    # evidence that there is one, and forcing the LLM to guess would be worse.
    #
    # 9.5: each surviving row is copied with its age weight attached. A COPY,
    # because these rows belong to the caller and a scoring pass must not
    # leave anything behind on them.
    eligible: list[Mapping[str, Any]] = [
        {**s, DECAY_KEY: signal_decay(s, anchor_at, window, edge_weight)}
        for s in signals
        if in_window(s, anchor_at, window)
        and (s.get("track") or enums.UNATTRIBUTED)
        in (track.name, enums.UNATTRIBUTED)
    ]

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for signal in eligible:
        kind = scoring_kind(signal)
        if kind is not None:
            grouped.setdefault(kind, []).append(signal)

    contributions: dict[str, float] = {}
    signal_contributions: dict[int, float] = {}
    for kind, rows in grouped.items():
        weight = kind_weight(track, kind)
        if weight <= 0.0:
            # This kind carries no weight on this track — the mission set it
            # to zero, meaning the track does not produce that signal at all.
            # Recorded as zero so the evidence stays visible in the audit
            # trail rather than being silently discarded.
            contributions[kind] = 0.0
            continue
        quality = kind_quality(kind, rows, cfg,
                               (flight_baselines or {}).get(kind))
        if not spatially_anchored(rows, radius_km):
            quality *= 0.5
        contribution = weight * quality
        contributions[kind] = round(contribution, 6)
        # Attribute the kind's contribution across its signals, purely so the
        # evidence drill-down can rank them. The total is what scores.
        #
        # 9.5: in proportion to each row's age weight rather than evenly, so a
        # reader opening the evidence sees the fresh observation at the top.
        # An even split would have ranked a five-day-old post level with one
        # from this morning while the score already knew better.
        if rows and contribution > 0:
            weights = [decay_of(row) for row in rows]
            total_weight = sum(weights)
            for row, row_weight in zip(rows, weights):
                signal_id = row.get("signal_id")
                if signal_id is None:
                    continue
                share = (contribution * row_weight / total_weight
                         if total_weight > 0 else contribution / len(rows))
                signal_contributions[int(signal_id)] = round(share, 6)

    score = round(min(1.0, sum(contributions.values())), 4)
    families = {
        KIND_FAMILY[k] for k, v in contributions.items() if v > 0.0
    }
    distinct_types = len(families)

    has_anchor = has_strong_anchor(track, contributions)

    # 9.8. How many independent reports the correlation rests on, across every
    # family. RAW counts, not decayed ones — "did two independent sources
    # report this" is a fact about the evidence, and letting age erode it would
    # conflate a structural gate with the score, which decay already moves.
    reports = independent_reports(grouped, contributions)

    # 9.13. How old the evidence is, and whether any of it is this run's.
    #
    # The correlation window reads ACROSS iterations by design — a flight seen
    # thirty minutes before this iteration started is still live evidence — so
    # a correlation can rest entirely on collection paid for days ago. Measured
    # live: one Atlanta alert re-scored 98 signals from two earlier iterations,
    # none from its own, and produced the fourth alert in a row from a single
    # day's collection. That is a correct current assessment; it is also
    # something a reader must not have to diff two evidence surfaces to notice.
    contributing = [
        row for kind, rows in grouped.items() if contributions.get(kind, 0.0) > 0
        for row in rows
    ]
    evidence_freshness: dict[str, Any] = {}
    if contributing:
        ages = []
        for row in contributing:
            observed = row.get("observed_at")
            if isinstance(observed, str):
                from ..db.database import parse_iso
                observed = parse_iso(observed)
            if isinstance(observed, datetime):
                ages.append(abs((anchor_at - observed).total_seconds()) / 3600.0)
        iterations = {int(row["iteration_id"]) for row in contributing
                      if row.get("iteration_id") is not None}
        newest = max(iterations) if iterations else None
        evidence_freshness = {
            "signals": len(contributing),
            "newest_iteration": newest,
            # None when the caller did not say which iteration is running, so
            # the claim is simply not made rather than guessed.
            "new_this_iteration": (None if iteration_id is None or newest is None
                                   else newest == int(iteration_id)),
            "oldest_hours": round(max(ages), 1) if ages else None,
            "newest_hours": round(min(ages), 1) if ages else None,
        }

    # 9.10. What the flight families were measured against, recorded per kind.
    # On the CORRELATION rather than on each signal, and deliberately: the
    # correlation window spans iterations, so one signal can be baselined in
    # one correlation and not in another. A per-signal flag would be claiming
    # something the signal's own collection never knew, and would contradict
    # itself between two readers. This is where the decision is made, so this
    # is where it is recorded.
    baseline_state: dict[str, dict[str, Any]] = {}
    for kind in sorted(set(grouped) & BASELINED_FLIGHT_KINDS):
        value = (flight_baselines or {}).get(kind)
        observed = sum(
            {row.get("fr24_id") or row.get("registration")
             or row.get("callsign") or f"{kind}:{i}": 1
             for i, row in enumerate(grouped[kind])}.values())
        baseline_state[kind] = {
            "state": "BASELINED" if value else "UNBASELINED",
            "baseline": round(float(value), 3) if value else None,
            "observed": observed,
        }

    # 9.11. Two different questions, and conflating them was the defect.
    #
    #   failed_sources    WHAT failed, as SOURCE_TYPE:endpoint. Reported.
    #   failed_families   which families were not measured AT ALL. Scored.
    #
    # A family is a coverage gap only when nothing in it was collected.
    # Measured live: Staying's calendar coverage is sparse enough that the
    # availability leg regularly returns too few paired listings to score while
    # the price leg succeeds — and the old rule then scored lodging as absent
    # and told the reader "lodging unavailable this iteration" when four
    # listings had been priced in both windows. A false negative dressed as
    # caution, and the PRICE signal exists precisely because the availability
    # endpoint is the unreliable one.
    #
    # The safety property survives intact: a broken credential fails EVERY
    # endpoint in its family, so the family is still a gap and still caps the
    # band. What no longer happens is a partial loss reading as a total one.
    failed = [st for st in unreliable_source_types if st in SOURCE_TYPE_FAMILY]
    endpoints = dict(failed_endpoints or {})
    failed_sources = sorted(
        f"{st}:{endpoints[st]}" if endpoints.get(st) else st for st in failed
    )
    collected_families = {
        SOURCE_TYPE_FAMILY[st] for st in collected_source_types
        if st in SOURCE_TYPE_FAMILY
    }
    failed_families = sorted(
        {SOURCE_TYPE_FAMILY[st] for st in failed} - collected_families
    )
    completeness = round(
        max(0.0, 1.0 - len(failed_families) / float(len(FAMILIES))), 4
    )

    band, trace = _band_for(score, distinct_types, has_anchor, cfg,
                            reports=reports)

    # 9.5. The curve is part of how the number was reached, so it belongs in
    # the trace beside the band rule. Without it a reader comparing two alerts
    # scored under different windows has no way to see that the older evidence
    # was weighted differently — and the whole point of tying the curve to the
    # window is that changing one changes the other.
    for kind in sorted(baseline_state):
        entry = baseline_state[kind]
        if entry["state"] == "BASELINED":
            trace += (f"; {kind} scored as excess over a baseline of "
                      f"{entry['baseline']:g} ({entry['observed']} observed)")
        else:
            trace += (f"; {kind} NOT baselined — too few prior samples, so the "
                      f"absolute count of {entry['observed']} stands")

    if edge_weight < 1.0:
        half_life = (window.total_seconds() / 3600.0) * (
            math.log(0.5) / math.log(max(_MIN_EDGE_WEIGHT,
                                         min(1.0, edge_weight))))
        trace += (
            f"; evidence aged on a {window.total_seconds() / 3600.0:g}-hour "
            f"window (half-life {half_life:.1f}h, "
            f"{edge_weight:g} at the edge)")

    band_capped = False
    # Keyed on failed_sources rather than failed_families deliberately: ANY
    # lost collection caps HIGH, even where a sibling endpoint covered the
    # family. 9.11 made completeness more accurate; it must not make the top
    # band easier to reach.
    if failed_sources and band == "HIGH":
        band = "MEDIUM"
        band_capped = True
        trace += (
            "; capped from HIGH because collection was incomplete "
            f"({','.join(failed_sources)})"
        )

    return CorrelationResult(
        track=track.name,
        score=score,
        band=band,
        distinct_types=distinct_types,
        contributions=contributions,
        data_completeness=completeness,
        failed_families=failed_families,
        failed_sources=failed_sources,
        flight_baseline=baseline_state,
        evidence_freshness=evidence_freshness,
        band_capped=band_capped,
        rule_trace=trace,
        signal_contributions=signal_contributions,
        earliest_eta=_earliest_eta(grouped),
    )


def _band_for(
    score: float, distinct_types: int, has_anchor: bool, cfg: Mapping[str, Any],
    reports: int = 0,
) -> tuple[str, str]:
    """Map a score to a band, returning the rule that fired in words.

    Transcribes the original prompt's clauses, with one deliberate departure:

      HIGH   all four signals present and consistent
      MEDIUM two or more signals, one of them social or a verified-category
             flight
      LOW    two or more INDEPENDENT REPORTS, from any family or families

    **A single report no longer produces any alert (9.8, issue #7).** The prompt
    granted LOW to a lone social post, and this system did too. Owner decision:
    it must not. One report is a lead, not a warning, and an instrument that
    escalates on one is an instrument that escalates on a rumour — which is
    exactly the `RUMOUR_AMPLIFICATION` alternative 9.6 records against
    social-only correlations.

    **The gate counts reports, not families**, and `independent_reports()`
    defines what one is. Two outlets making two distinct claims is two reports
    from one family and alerts; eighteen distinct airframes over one airport is
    eighteen reports and alerts; a lodging drop is one report however many
    listings it spans, and alone it does not. Counting families instead was the
    first implementation and it was wrong in both directions — see that
    function.

    A correlation that fails the gate is still computed, scored and stored —
    `GET /v1/iterations/{id}/correlations` shows it with the reason. Refusing to
    alert is not refusing to record.
    """
    if (
        score >= float(cfg["band_high_min_score"])
        and distinct_types >= int(cfg["band_high_min_types"])
    ):
        return "HIGH", (
            f"score {score:.2f} >= {cfg['band_high_min_score']} with "
            f"{distinct_types} distinct signal families"
        )
    if (
        score >= float(cfg["band_medium_min_score"])
        and distinct_types >= int(cfg["band_medium_min_types"])
        and has_anchor
    ):
        return "MEDIUM", (
            f"score {score:.2f} >= {cfg['band_medium_min_score']}, "
            f"{distinct_types} families, with social or verified-category flight"
        )
    min_reports = int(cfg.get("band_low_min_reports", 2))
    if score >= float(cfg["band_low_min_score"]) and reports >= min_reports:
        return "LOW", (
            f"score {score:.2f} >= {cfg['band_low_min_score']}, "
            f"{reports} independent reports across {distinct_types} signal "
            f"famil{'y' if distinct_types == 1 else 'ies'}"
        )
    if score >= float(cfg["band_low_min_score"]):
        return "NONE", (
            f"score {score:.2f} reaches the LOW threshold but rests on "
            f"{reports} independent report{'' if reports == 1 else 's'}, "
            f"below the {min_reports} required. One report is a lead, not a "
            f"warning"
        )
    return "NONE", f"score {score:.2f} below alerting threshold"


def _earliest_eta(grouped: Mapping[str, list[Mapping[str, Any]]]) -> str | None:
    """Soonest ETA among inbound aircraft.

    This is what makes a warning tactical rather than descriptive, and the
    previous implementation collected it and then discarded it at parse time
    because its dataclass had no field for it.
    """
    etas: list[str] = []
    for kind in (KIND_FLIGHT_M, KIND_FLIGHT_J, KIND_FLIGHT_AMBIGUOUS):
        for row in grouped.get(kind, []):
            if row.get("flight_status") == "airborne_inbound" and row.get("eta"):
                etas.append(str(row["eta"]))
    return min(etas) if etas else None


def haversine_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance in kilometres."""
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))
