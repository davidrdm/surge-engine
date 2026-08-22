"""The contract for model output, and the only door it comes through.

`TriageAgent` is the one place untrusted text becomes an analytical record.
Everything below it — tipping, correlation, banding — is deterministic and
auditable, and that is worth much less than it looks if the signals feeding it
can be manufactured by a malformed completion.

The previous boundary coerced where it should have validated, and measurement
rather than inspection found how far that went:

    relevant: "false"   accepted as TRUE  (a non-empty string is truthy)
    salience: NaN       became 1.0, the highest salience in the iteration,
                        because min(1.0, nan) returns 1.0 — and `json.loads`
                        accepts a bare NaN token by default, so it was reachable
    cities: "Phoenix"   iterated as CHARACTERS, proposing seven cities
    salience: 5         silently clamped to 1.0, indistinguishable from a
                        model that said 1.0

Three rules replace all of that.

**Identity is opaque and explicit.** Every item carries an `item_id` derived
from `(iteration_id, raw_id, url)` — deterministic for tests, unrelated to
position, and not the URL itself. The old code matched on URL and *fell back to
list position* when it did not recognise one, with a comment conceding the
assumption ("harmless as long as order held") that nothing verified. A response
that dropped one item and rewrote the rest moved every later judgement onto the
wrong post: its city, its facility, its rationale, its salience.

**Exactly one output per requested id.** Unknown ids are rejected, duplicates
are rejected, and there is no positional fallback of any kind. A duplicate used
to be worse than tolerated — the second silently overwrote the first *and*
manufactured a spurious "undecided" record for the post whose slot it took.

**Validation precedes every branch.** Strict Pydantic, `extra="forbid"`, ranges
enforced rather than clamped. An item that fails is preserved as a typed
outcome and can never produce a signal or a city-admission candidate.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..db.enums import UNATTRIBUTED

#: Bumped whenever the shape below changes in a way that alters what the model
#: is asked for or what is accepted from it. Stamped on every decision so a
#: change of criteria is reconstructable rather than invisible.
SCHEMA_VERSION = "triage/1"

#: Bounds. Generous enough not to reject honest output, tight enough that a
#: runaway response cannot become a thousand city-admission candidates.
#:
#: 25, not 10. Measured live: two Reddit sighting roundups legitimately
#: named 15 and 16 cities and were rejected whole — losing the `relevant`
#: verdict and the rationale as well as the list, and counting as non-coverage
#: that lowered data_completeness. An over-tight bound manufactures the very
#: gap it is meant to describe. The anti-runaway guard that actually matters is
#: elsewhere: `expand_cities` is off by default, and `max_expanded_cities`
#: caps admission at five when it is on.
MAX_CITIES = 25
MAX_LOCATIONS = 25
MAX_RATIONALE = 2000
MAX_ACTIVITY = 100


def item_id(iteration_id: int, raw_id: int, url: str) -> str:
    """An opaque, stable handle for one post inside one call.

    Deliberately not the URL: the model rewrites URLs, and matching on a value
    it is free to alter is what made the positional fallback seem necessary.
    Deliberately not the position either — position is exactly what a dropped
    item corrupts. Deterministic so fixtures and `--check` stay byte-stable.
    """
    digest = hashlib.sha256(
        f"{iteration_id}:{raw_id}:{url}".encode("utf-8")
    ).hexdigest()
    return f"i{digest[:11]}"


class TriageItem(BaseModel):
    """One judgement. Strict: nothing is coerced, nothing is clamped.

    `strict=True` means `"false"` is not a bool and `"0.9"` is not a float.
    `extra="forbid"` means an unexpected nested structure is invalid rather
    than ignored. The ranges reject rather than clamp, so `salience: 5` is a
    malformed answer and not an enthusiastic one — and NaN and infinity fail
    the same comparison.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    item_id: str = Field(min_length=1, max_length=32)
    relevant: bool
    # One of the mission's tracks, or UNKNOWN. Deliberately NOT a Literal:
    # the permitted values come from the mission pack, and the prompt that
    # asked for them comes from the same pack. Checked against the loaded
    # mission where the judgement is stored, which is also where the error can
    # name the mission it was checked against.
    track: str = "UNKNOWN"
    cities: list[str] = Field(default_factory=list, max_length=MAX_CITIES)
    locations: list[str] = Field(default_factory=list, max_length=MAX_LOCATIONS)
    activity_type: str | None = Field(default=None, max_length=MAX_ACTIVITY)
    imminence_hours: float | None = Field(default=None, ge=0.0, le=8760.0)
    salience: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=MAX_RATIONALE)


@dataclass
class ItemFault:
    """One item that could not be used, and why."""

    item_id: str | None
    reason: str
    detail: str


@dataclass
class BatchOutcome:
    """What a single model call produced, item by item.

    `valid` and `faults` together account for every id that was requested —
    that is asserted, not assumed, so an item cannot be quietly lost between
    the model and the record.
    """

    valid: dict[str, TriageItem] = field(default_factory=dict)
    faults: list[ItemFault] = field(default_factory=list)
    #: Requested but not answered at all.
    missing: list[str] = field(default_factory=list)
    #: The whole response was unusable — not a list, or the call raised.
    batch_error: str | None = None
    #: True when the call died because the reply hit `llm.max_tokens` (8.8).
    #:
    #: Recorded rather than left to be parsed out of `batch_error`'s free text,
    #: because the two failures have different remedies: a truncation is fixed
    #: by sending fewer items, an outage by sending the same ones later. A
    #: re-triage that could not tell them apart would re-send an oversized batch
    #: and reproduce the failure exactly — spending the quota and looking like a
    #: decision. Same argument that produced the five triage states.
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.batch_error is None


#: Characters of body shown to the model, split head and tail (8.4).
#:
#: The live matrix measured the failure this shape exists to fix: a report whose
#: retraction fell past the window was accepted at 0.90 and CONFIRMED, because
#: the model never saw the correction. Widening a head-only window does not fix
#: that — a correction is appended by convention, so for ANY head length there
#: is a body long enough to hide one. Keeping both ends does fix it, and the
#: guarantee is then structural rather than a bet on typical length.
#:
#: The head carries the claim, the tail carries the correction, and the elision
#: is marked so the model is not shown a splice as though it were continuous
#: prose.
MAX_TEXT_HEAD = 900
MAX_TEXT_TAIL = 400
ELISION = "\n[...]\n"


def window_text(body: str) -> str:
    """The body as the model sees it: whole, or head + tail with the cut marked.

    Returns the body unchanged whenever it fits, so the common case is not a
    splice and the marker means what it says.
    """
    body = body or ""
    if len(body) <= MAX_TEXT_HEAD + MAX_TEXT_TAIL:
        return body
    return body[:MAX_TEXT_HEAD] + ELISION + body[-MAX_TEXT_TAIL:]


def build_request(
    posts: Sequence[Mapping[str, Any]], iteration_id: int
) -> tuple[list[dict[str, Any]], dict[str, Mapping[str, Any]]]:
    """The payload shown to the model, and the id → post map to bind replies.

    The URL is still sent — the model needs it to judge the item — but it is no
    longer the key. `item_id` is.
    """
    payload: list[dict[str, Any]] = []
    index: dict[str, Mapping[str, Any]] = {}
    for post in posts:
        identifier = item_id(iteration_id, int(post["raw_id"]), post["url"])
        index[identifier] = post
        payload.append({
            "item_id": identifier,
            "url": post["url"],
            "title": (post.get("title") or "")[:200],
            "platform": post.get("platform", ""),
            "author": (post.get("author") or "")[:80],
            "date": post.get("observed_at", ""),
            "text": window_text(post.get("snippet") or ""),
        })
    return payload, index


def parse_batch(result: Any, expected: Sequence[str],
                mission: Any = None) -> BatchOutcome:
    """Bind a model response to the ids that were requested.

    Every rejection is a recorded fault rather than a silent drop, because the
    difference between "the model rejected this post" and "the model's answer
    about this post was unusable" is exactly the difference this phase exists
    to preserve.

    `mission` supplies the permitted `track` values. `TriageItem.track` used to
    be a `Literal` of the two tracks the system was built for, so an invented
    one was a schema violation here; the vocabulary is now the mission's, which
    Pydantic cannot know. Checking it in this loop keeps the guarantee where it
    was — one bad item becomes ONE recorded fault, rather than escaping to the
    insert and taking the batch with it.
    """
    outcome = BatchOutcome()
    wanted = set(expected)

    if isinstance(result, Mapping):
        # Tolerate a wrapper object; some models add one despite the prompt.
        for key in ("items", "results", "decisions", "data"):
            if isinstance(result.get(key), list):
                result = result[key]
                break

    if not isinstance(result, list):
        outcome.batch_error = (
            f"expected a JSON array of judgements, got {type(result).__name__}"
        )
        outcome.missing = sorted(wanted)
        return outcome

    seen: set[str] = set()
    for entry in result:
        if not isinstance(entry, Mapping):
            outcome.faults.append(ItemFault(
                None, "NOT_AN_OBJECT",
                f"array element was {type(entry).__name__}, not an object"))
            continue

        identifier = entry.get("item_id")
        if not isinstance(identifier, str) or identifier not in wanted:
            # No positional fallback. Guessing which post this belongs to is
            # what moved one post's judgement onto another.
            outcome.faults.append(ItemFault(
                identifier if isinstance(identifier, str) else None,
                "UNKNOWN_ITEM_ID",
                f"item_id {identifier!r} was not one of the ids requested"))
            continue
        if identifier in seen:
            outcome.faults.append(ItemFault(
                identifier, "DUPLICATE_ITEM_ID",
                "a second judgement was returned for this item; both are "
                "discarded because there is no way to tell which is meant"))
            outcome.valid.pop(identifier, None)
            continue
        seen.add(identifier)

        try:
            item = TriageItem.model_validate(dict(entry))
        except ValidationError as exc:
            outcome.faults.append(ItemFault(
                identifier, "SCHEMA_VIOLATION", _first_error(exc)))
            continue

        if mission is not None and item.track != UNATTRIBUTED:
            try:
                mission.track(item.track)
            except ValueError as exc:
                outcome.faults.append(ItemFault(
                    identifier, "SCHEMA_VIOLATION", f"track: {exc}"))
                continue
        outcome.valid[identifier] = item

    outcome.missing = sorted(wanted - seen)
    return outcome


def _first_error(exc: ValidationError) -> str:
    """The most useful line of a Pydantic error, for the audit record."""
    errors = exc.errors()
    if not errors:
        return "failed validation"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "(root)"
    return f"{location}: {first.get('msg', 'invalid')} " \
           f"[{first.get('type', 'unknown')}]"
