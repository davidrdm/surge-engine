"""Enumerations mirroring every CHECK-constrained column in schema.sql.

SQLite enforces CHECK constraints, but it reports violations as an opaque
IntegrityError that names neither the column nor the offending value. Validating
in Python first buys a precise error message at the call site, which matters
when the value came from an LLM response rather than from code.

Keep these in lockstep with schema.sql. tests/test_schema.py asserts that every
frozenset here matches the CHECK clause it mirrors, so drift fails the suite.
"""
from __future__ import annotations

from typing import Iterable

# --- sessions ---------------------------------------------------------------
SESSION_STATUSES = frozenset({"ACTIVE", "CLOSED"})

#: What a signal nobody attributed carries in `signals.track`.
#:
#: ENGINE vocabulary, not a mission's. It means the source did not say who was
#: acting, which is a fact about the evidence rather than about the mission,
#: and every mission needs it for the same reason: forcing a model to name a
#: track it cannot see would turn a gap into a guess. `correlate()` admits an
#: unattributed signal to EVERY track for the same reason.
#:
#: A mission may not define a track with this name; `services/mission.py`
#: refuses one, so the two vocabularies cannot collide.
UNATTRIBUTED = "UNKNOWN"

# --- geography --------------------------------------------------------------
ADMITTED_BY = frozenset({"USER", "TIP"})
GEO_CACHE_KINDS = frozenset({
    "AIRPORT", "PICKUP_LOCATION", "LISTING_SET",
    # Hotel identities pinned once so both price windows measure the SAME
    # properties; a free-text re-resolve could return a different hotel.
    "HOTEL_SET",
})
GEO_RESOLVED_BY = frozenset({"TABLE", "ALIAS", "PREFIX", "API", "UNRESOLVED"})

# --- iterations -------------------------------------------------------------
# Ordered, because the orchestrator resumes by comparing positions. Anything
# that needs "is this stage before that one" must use STAGE_ORDER, not sorting.
STAGE_ORDER: tuple[str, ...] = (
    "SEEDING",
    "COLLECTING_SOCIAL",
    "TRIAGING",
    "TIPPING",
    "COLLECTING_TIPPED",
    "CORRELATING",
    "ALERTING",
    "SCHEDULING",
    "COMPLETE",
)
# FAILED is terminal and deliberately outside the ordering.
STAGES = frozenset(STAGE_ORDER) | {"FAILED"}
ITERATION_OUTCOMES = frozenset({"COMPLETE", "PARTIAL", "FAILED"})
# INTERRUPTED is not FAILED. The agent did not fail — it was stopped by a
# process exit, and the work may still be resumable. Keeping them apart is what
# lets a stage report be honest about the difference.
AGENT_RUN_STATUSES = frozenset({"RUNNING", "COMPLETE", "FAILED", "INTERRUPTED"})

# --- process instances ------------------------------------------------------
# How a process ended, as recorded on its own epoch row. UNKNOWN is written by
# the NEXT process onto a predecessor it found open — a crash leaves nothing
# behind to write its own epitaph.
SHUTDOWN_KINDS = frozenset({"CLEAN", "TIMEOUT", "UNKNOWN"})
ENTRY_POINTS = frozenset({
    "serve", "iterate", "retry-triage", "recover", "cli", "test",
})

# --- query queue ------------------------------------------------------------
SOURCE_TYPES = frozenset({
    "SOCIAL", "FLIGHT_COUNT", "FLIGHT_LIVE", "FLIGHT_HISTORY",
    # LODGING measures availability; LODGING_PRICE measures price. Separate
    # source types so each is budgeted and coverage-gapped on its own, but
    # both map to the LODGING scoring family.
    "LODGING", "LODGING_PRICE", "CAR",
})
QUERY_STATUSES = frozenset({
    "PENDING", "IN_PROGRESS", "COMPLETE", "FAILED",
    # Claimed, then the process died before it was executed or recorded.
    # Deliberately not FAILED: that means the vendor was asked and answered
    # badly, and it sets executed_at, which drives the cooldown.
    "INTERRUPTED",
    "SKIPPED_BUDGET", "SKIPPED_NO_MAPPING",
})
# Statuses that mean "we did not get data, and we know why". Correlation must
# treat these as a coverage gap, never as an absence of signal.
#
# INTERRUPTED belongs here for exactly that reason: an abandoned iteration whose
# stranded queries were invisible to this set would report FULL coverage on
# collection that never happened — the failure mode this whole system exists to
# prevent. IN_PROGRESS stays out, because a query in flight is not yet a gap.
UNRELIABLE_QUERY_STATUSES = frozenset({
    "FAILED", "INTERRUPTED", "SKIPPED_BUDGET", "SKIPPED_NO_MAPPING",
})
SKIP_REASONS = frozenset({
    "MONTHLY_QUOTA_EXHAUSTED", "ITERATION_ALLOCATION_EXHAUSTED",
    "HARD_STOP_PRIORITY", "NO_AIRPORT_MAPPING", "NO_PICKUP_MAPPING",
    # Nothing to collect against: the place resolved to no target at all.
    # Nothing was called and nothing was spent.
    "NO_LISTING_SET",
    # The target resolved and WAS called, and the vendor returned too little
    # to compare. Distinct from NO_LISTING_SET because the two differ in every
    # way that matters to a reader: one spent money and one did not, one has
    # an api_calls trail and one has none, and one should sit out its cooldown
    # while the other has nothing to repeat.
    #
    # Measured live on the reference run: a single Staying /search resolved a
    # listing set, made 13 calls, spent 100 credits, and was recorded as
    # NO_LISTING_SET with executed_at NULL — so the cooldown never started and
    # the same 100 credits would have been spent again next iteration.
    "THIN_PAIRED_SAMPLE",
})
QUERY_ORIGINS = frozenset({"SEED", "TIP", "SCHEDULED", "CARRIED_FORWARD"})

QUEUE_DECISION_OUTCOMES = frozenset({
    "ENQUEUED", "DEDUPED", "COOLDOWN", "CAP_ITERATION", "CAP_CITY",
    "CAP_DEPTH", "CITY_NOT_ADMITTED", "BUDGET_EXHAUSTED", "NO_MAPPING",
})
# Everything except ENQUEUED. Used by the fan-out property tests.
QUEUE_REFUSALS = QUEUE_DECISION_OUTCOMES - {"ENQUEUED"}

# --- triage -----------------------------------------------------------------
# What happened to one post at the model boundary. The split that matters is
# between a CONCLUSION and a NON-ANSWER: REJECTED is an analytical result, while
# UNDECIDED, INVALID_OUTPUT and MODEL_ERROR are coverage gaps. Before Phase 7 all
# four stored relevant=0 with the same rationale string, so a model outage was
# indistinguishable from an iteration in which nothing was relevant.
TRIAGE_STATES = frozenset({
    "ACCEPTED", "REJECTED", "UNDECIDED", "INVALID_OUTPUT", "MODEL_ERROR",
})
#: States that mean the post was never actually judged. These make SOCIAL a
#: coverage gap for the iteration.
TRIAGE_UNCOVERED = frozenset({"UNDECIDED", "INVALID_OUTPUT", "MODEL_ERROR"})

# 8.9. Why a collected post never reached the model. A separate vocabulary from
# TRIAGE_STATES on purpose: those describe a model call that was made, these
# describe one that was not.
#
# The split that matters to `data_completeness`:
#
#   STALE is a DECISION. Choosing not to judge a week-old post is the system
#   working as configured, and counting it as a gap would cap the band on every
#   city in every ordinary run — the median collected post was measured at 206
#   days old.
#
#   The other four are DEFECTS. A malformed payload removes evidence that was
#   collected and paid for, and the two whole-payload cases remove all of it at
#   once. Those are coverage gaps in the sense CorrelationAgent already models,
#   and `PAYLOAD_LEVEL_SKIPS` is what tells the two apart.
TRIAGE_SKIP_REASONS = frozenset({
    "STALE", "PAYLOAD_UNPARSEABLE", "PAYLOAD_NOT_A_LIST",
    "ITEM_NOT_AN_OBJECT", "ITEM_NO_URL",
})

#: Skips that cost a whole vendor response rather than one post. These get a
#: degradation as well as a row, so an iteration that lost a payload cannot
#: close COMPLETE.
PAYLOAD_LEVEL_SKIPS = frozenset({"PAYLOAD_UNPARSEABLE", "PAYLOAD_NOT_A_LIST"})

# Which kind of judgement a classification receipt covers (8.1). One row per
# model call; see services/receipts.py.
RECEIPT_KINDS = frozenset({"TRIAGE", "ALERT"})

# Human review before escalation or public use (8.2). UNREVIEWED is not a
# verdict — it is the absence of one, and an alert nobody has looked at must be
# distinguishable from one an analyst cleared for distribution. This governs
# DISTRIBUTION only; scoring, evidence and the audit trail are unaffected.
REVIEW_STATES = frozenset({"UNREVIEWED", "RELEASED", "WITHHELD"})

# --- providers and signals --------------------------------------------------
PROVIDERS = frozenset({"APIDIRECT", "FR24", "STAYING", "PRICELINE"})
SIGNAL_TYPES = frozenset({"SOCIAL", "FLIGHT", "LODGING", "CAR"})

# Whether an observation is merely recorded or is operational evidence.
# CANDIDATE rows are visible in the evidence trail and to a reviewer, but they
# do not score and cannot book paid collection. See services/sensitivity.py.
SIGNAL_STATES = frozenset({"CANDIDATE", "CONFIRMED"})

# 9.4 / issue #2 — how a signal reached us, as ONE value an analyst can filter
# on across all four families. Per-family provenance already existed (geo
# method, publisher method, facility method, per-provider governance); what did
# not was a comparable answer to "how directly do we know this".
#
# The review asked for Direct API / Cached API / Web scrape / OSINT feed /
# Third-party feed. That vocabulary is not the one this system can attest, and
# a field guessing between "web scrape" and "third-party feed" because the code
# cannot tell would be worse than no field. What the connectors CAN attest is
# whether the response came from the party that generated the record, and
# whether the intermediary served it live or from its own store:
#
#   DIRECT               from the party that generated the record. Nothing here
#                        qualifies today — declared so that filtering for it
#                        returns the honest answer (nothing) rather than
#                        requiring a reader to know that already.
#   INTERMEDIARY_LIVE    a third-party API retrieved it for this request.
#   INTERMEDIARY_CACHED  a third-party API served a stored copy whose age it did
#                        not state, and billed it as a fresh call. Staying's
#                        price-compare does this on a 1-hour cache; the 30
#                        credits are charged either way, so cost is no signal of
#                        freshness.
#   UNRECORDED           collected before this field existed. NOT a claim about
#                        the row — the absence of one.
COLLECTION_CLASSES = frozenset({
    "DIRECT", "INTERMEDIARY_LIVE", "INTERMEDIARY_CACHED", "UNRECORDED",
})

# FR24 category codes, verified against its OpenAPI spec. Only the four the
# system scores are permitted in the signals table; AMBIGUOUS means the record
# came from a live-positions response, which carries no category field at all.
FLIGHT_CATEGORIES = frozenset({"M", "J", "T", "H", "AMBIGUOUS"})
CATEGORY_CONFIDENCE = frozenset({"CONFIRMED", "AMBIGUOUS"})
FLIGHT_STATUSES = frozenset({"landed", "airborne_inbound"})

# Full FR24 category vocabulary, for building request filters. Not stored.
FR24_CATEGORY_NAMES: dict[str, str] = {
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

# --- correlation and alerts -------------------------------------------------
# NONE is a real computed outcome (below the alerting threshold) and is stored,
# so that "we looked and found nothing" is on the record. Alerts cannot be NONE.
BANDS = frozenset({"NONE", "LOW", "MEDIUM", "HIGH"})
ALERT_BANDS = frozenset({"LOW", "MEDIUM", "HIGH"})
BAND_ORDER: tuple[str, ...] = ("NONE", "LOW", "MEDIUM", "HIGH")

# 8.7(b). What ALERTING decided about a correlation. A correlation that becomes
# no alert is not a non-event: it is the near miss you calibrate the floors
# from, and the floors in `correlation` and `sensitivity` are explicitly interim
# and to be set from evidence. NULL is deliberately outside this set and means
# ALERTING has not run yet -- "not decided" and "decided against" are different
# facts, the same distinction TRIAGE_STATES exists to keep.
ALERT_DECISIONS = frozenset({"ALERTED", "BELOW_FLOOR", "BAND_NONE"})

# --- logging and budget -----------------------------------------------------
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
BUDGET_PERIODS = frozenset({"MONTH", "DAY", "ITERATION"})


class EnumViolation(ValueError):
    """Raised when a value is not in its column's permitted set."""


def validate(value: str, allowed: Iterable[str], field: str) -> str:
    """Return `value` if permitted, else raise with the full allowed set.

    Mirrors iw_database._validate. The allowed set is included in the message
    because the usual caller is parsing an LLM response and needs to know what
    it should have produced.
    """
    allowed = frozenset(allowed)
    if value not in allowed:
        raise EnumViolation(
            f"{field}={value!r} is not permitted; expected one of "
            f"{sorted(allowed)}"
        )
    return value


def validate_optional(
    value: str | None, allowed: Iterable[str], field: str
) -> str | None:
    """As validate(), but None passes through (for nullable columns)."""
    if value is None:
        return None
    return validate(value, allowed, field)


def stage_index(stage: str) -> int:
    """Position of a stage in the pipeline, for resume comparisons.

    FAILED and unknown values sort to -1 so that "resume from here" starts at
    the beginning rather than silently skipping the whole pipeline.
    """
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return -1


def band_index(band: str) -> int:
    """Position of a confidence band, for capping and comparison."""
    try:
        return BAND_ORDER.index(band)
    except ValueError:
        return 0


def cap_band(band: str, ceiling: str) -> str:
    """Return `band` limited to at most `ceiling`.

    Used to enforce that partial collection can never yield a HIGH alert.
    """
    return band if band_index(band) <= band_index(ceiling) else ceiling
