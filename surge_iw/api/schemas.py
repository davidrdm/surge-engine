"""Request and response models.

Pydantic rather than hand-rolled validation, for two reasons that matter here:
the generated OpenAPI schema is what a front end builds against without reading
this source, and a malformed session-init body is caught before it can create a
half-populated session that an iteration would then run against.

Response models are deliberately loose where the underlying data is a free-form
record — `evidence`, `counts`, `budget` — and strict everywhere a client makes a
decision from the value.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Where the mission vocabularies are documented
# ---------------------------------------------------------------------------
#
# Dropping the `Literal` from these fields cost a client a control it could
# check statically, and `CapabilitiesOut.mission` is the whole mitigation. That
# only works if every field carrying a track SAYS so — a bare `string` in a
# response schema tells a reader nothing, and "look it up elsewhere" is not
# discoverable from the one place they are looking.
#
# One string per shape, shared by every model, so the six surfaces cannot drift
# into six different half-answers.

TRACK_FIELD = (
    "One of the loaded mission's tracks. Not an enum in this schema because "
    "the permitted values come from a mission pack read at startup; "
    "`GET /v1/capabilities` reports the live set as `mission.tracks`.")

TRACKS_FIELD = (
    "Tracks defined by the loaded mission. Not an enum in this schema because "
    "the permitted values come from a mission pack read at startup; "
    "`GET /v1/capabilities` reports the live set as `mission.tracks`.")


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class RequestModel(BaseModel):
    """Base for every request body: an unknown field is refused, not ignored.

    Found while verifying that Phase 8's contract hardening survived Phase 9,
    and it is issue #11 one level up. That issue was about `tunables` being
    accepted and never read; the field NAME had the same problem. A client
    sending `tunabels` — or `expandCities`, or `finalize` for `finalise` —
    received a 201 and a session running on settings it had not chosen, with no
    way to tell.

    Three real cases, each silent before this:

      * `tunabels` — the session runs on the server's configuration.
      * `confirm_respends` — the resume refuses and the client cannot see why.
      * `finalize` — `finalise: false` means "do not score", and the US
        spelling was dropped, so an operator asking to skip correlation and
        alerting got them anyway.

    The last is the safe direction and the first is not, which is exactly why
    the rule has to be uniform: a boundary that only refuses the dangerous
    typos is one that has to know in advance which those are.

    Matches `TriageItem`, where the same `extra="forbid"` was applied to the
    MODEL-output boundary in Phase 7. This is the client-input boundary, and
    untrusted is untrusted.
    """

    model_config = {"extra": "forbid"}


class KeyLocationIn(RequestModel):
    name: str = Field(min_length=1, max_length=200)
    address: str | None = None
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0)
    location_type: str | None = Field(
        default=None,
        description="One of the loaded mission's `location_types`, which "
                    "`GET /v1/capabilities` reports. Not an enum in this "
                    "schema because the permitted values come from a mission "
                    "pack read at startup; an unknown one is refused with a "
                    "422 naming the value and listing what was allowed.")


class CityIn(RequestModel):
    name: str = Field(min_length=1, max_length=120)
    state: str | None = Field(default=None, max_length=40)
    key_locations: list[KeyLocationIn] = Field(default_factory=list)


class SessionIn(RequestModel):
    """Initialisation. The key locations are what lodging queries anchor on.

    Give either `cities` inline or `input_set` — exactly one. The response
    echoes the resolved geography either way, so an operator can see what they
    created rather than trust that a file still says what they remember.
    """

    label: str | None = None
    cities: list[CityIn] = Field(
        default_factory=list,
        description="Inline jurisdictions. Omit when using `input_set`.")
    input_set: str | None = Field(
        default=None, max_length=120,
        description="NAME of a file in the configured input directory "
                    "(`inputs.dir`), without the extension — for example "
                    "'example'. Not a path: this field would otherwise "
                    "be a file-disclosure primitive, and an authenticated "
                    "caller is not thereby trusted with the filesystem. An "
                    "unresolvable city in the file is refused by name, never "
                    "silently dropped.")
    calendar_set: str | None = Field(
        default=None, max_length=120,
        description="NAME of a calendar file in the input directory "
                    "(`inputs.dir`), without the extension. Not a path, for "
                    "the same reason as `input_set`. Scheduled events the "
                    "operator already knows about: shown to the triage model "
                    "as context, recorded on any correlation whose window "
                    "they overlap, and NEVER an input to a score. More can "
                    "be appended between iterations via "
                    "`POST /v1/sessions/{id}/calendar`.")
    #: Whether the system may collect against a city the user did not name.
    #: False keeps collection strictly inside the listed jurisdictions.
    expand_cities: bool = False
    tracks: list[str] = Field(
        default_factory=list,
        description="Which of the loaded mission's tracks to score. Defaults "
                    "to all of them. `GET /v1/capabilities` reports the "
                    "permitted values, which are not an enum here because "
                    "they come from a mission pack read at startup.")
    #: Config overrides frozen onto the session (9.2). Shaped exactly like
    #: config.yaml — `{"correlation": {"window_hours": 48}}`. An unknown
    #: section or setting is refused with a 422 naming it, and so is a
    #: server-owned one: credentials, provider endpoints, retention ceilings
    #: and deployment controls are not a client's to set. Spending ceilings may
    #: be lowered and never raised.
    tunables: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-session configuration overrides, nested as in "
                    "config.yaml. Settable sections: triage, sensitivity, "
                    "correlation, windows, tipping, budget. Anything else is "
                    "a 422 naming the field — a setting that was accepted and "
                    "ignored is worse than one that was refused.")

    @model_validator(mode="after")
    def _one_source(self) -> "SessionIn":
        """Exactly one source of geography.

        Both would be two answers to one question, and merging them silently is
        how a session ends up with a city the operator did not think they had
        asked for. Neither is a session that collects nothing.
        """
        if self.cities and self.input_set:
            raise ValueError(
                "Give either `cities` or `input_set`, not both. Two sources "
                "of geography is two answers to one question.")
        if not self.cities and not self.input_set:
            raise ValueError(
                "A session needs at least one city: pass `cities`, or "
                "`input_set` naming a file in the input directory.")
        return self


class CalendarAppendIn(RequestModel):
    """Append events from a calendar file, between iterations."""

    calendar_set: str = Field(
        min_length=1, max_length=120,
        description="NAME of a calendar file in `inputs.dir` (not a path). "
                    "Loaded all-or-nothing; events already on the session "
                    "become warnings rather than errors, so re-loading a "
                    "grown file is a safe way to append.")


class CalendarEventOut(BaseModel):
    event_id: int
    name: str
    city: str = Field(description="The city as the operator wrote it.")
    city_canonical: str = Field(
        description="The resolved form correlation matching keys on.")
    starts_at: str = Field(description="Canonical ISO instant; a bare date "
                                       "in the file became 00:00Z.")
    ends_at: str = Field(description="Canonical ISO instant; an omitted end "
                                     "became the end of the start's day.")
    category: str | None = Field(
        default=None, description="The operator's own words; the engine "
                                  "constrains nothing here.")
    note: str | None = None
    source_name: str | None = Field(
        default=None, description="Which calendar file supplied it.")
    added_at: str = Field(
        description="When the event was appended. Load-bearing: an "
                    "iteration's triage context is exactly the events with "
                    "added_at <= its start, so later appends never change "
                    "what an earlier receipt hashed.")


class CalendarOut(BaseModel):
    session_id: int
    events: list[CalendarEventOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CitiesIn(RequestModel):
    cities: list[CityIn] = Field(min_length=1)


class IterationIn(RequestModel):
    """How to run the iteration being created.

    `manual` creates it and stops, for stepping through the stages one at a
    time. `auto` is the normal path and the default: agents must not solicit
    user interaction inside an iteration, so nothing between stage 1 and stage 8
    can ask for anything.
    """

    mode: Literal["auto", "manual"] = "auto"


class StepIn(RequestModel):
    #: The stage the caller believes is next. A guard, not a selector: if the
    #: iteration is elsewhere the request fails rather than running the wrong
    #: stage and spending money doing it.
    expect: str | None = None


class DiscardIn(RequestModel):
    expect: str | None = None
    #: Required for a stage that made paid calls. Re-running collection buys the
    #: same data again at the vendor, and nothing already spent is reclaimed.
    confirm: bool = False


class ResumeIn(RequestModel):
    """How to restart an interrupted iteration.

    Two fields and no third override. An *earlier* `from_stage` needs no extra
    ceremony — naming a stage is itself the explicit act — while a *later* one
    is refused outright, because skipping a stage that never ran is never what
    anyone wants. Collapsing two acknowledgements into one boolean is how
    confirmations stop meaning anything.
    """

    from_stage: str | None = Field(
        default=None,
        description="Override the derived resume point. Must not be later "
                    "than it. Use discard-last-stage to go further back.")
    confirm_respend: bool = Field(
        default=False,
        description="Required when the plan would collect again at a vendor. "
                    "Nothing already spent is reclaimed.")


class AbandonIn(RequestModel):
    reason: str = Field(min_length=1, max_length=500)
    confirm: bool = False
    #: Default true. Skipping the scoring closes the iteration with no
    #: correlation and no alert at all, for a city whose evidence may be almost
    #: complete — a real cluster reading as silence.
    finalise: bool = True


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class ErrorOut(BaseModel):
    """Every non-2xx response. FastAPI's HTTPException shape, made explicit.

    Declared so the generated schema documents the failure modes: a contract
    that lists only the success path tells a client nothing about what to
    handle, and 401, 409 and 503 are all reachable on routes whose happy path
    looks unremarkable.
    """

    detail: str


class CityOut(BaseModel):
    city_id: int
    name: str
    state: str | None = None
    is_seed: bool = True
    admitted_by: str = "USER"
    key_locations: list[str] = Field(default_factory=list)
    #: Resolved at init so a mapping gap is visible before an iteration runs.
    airports: list[str] = Field(default_factory=list)
    pickup_location: str | None = None


class SessionOut(BaseModel):
    session_id: int
    label: str | None = None
    status: str = "ACTIVE"
    created_at: str = ""
    expand_cities: bool = False
    tracks: list[str] = Field(default_factory=list,
                              description=TRACKS_FIELD)
    cities: list[CityOut] = Field(default_factory=list)
    calendar_events: int = Field(
        default=0,
        description="How many operator-calendar events this session holds. "
                    "`GET /v1/sessions/{id}/calendar` lists them; they are "
                    "context for triage and annotation on correlations, "
                    "never a scoring input.")
    #: Cities whose airport or pickup mapping could not be resolved. Surfaced
    #: here rather than discovered mid-iteration as a SKIPPED_NO_MAPPING query.
    warnings: list[str] = Field(default_factory=list)
    #: 9.2. The overrides as stored, echoed so a client can see that what it
    #: asked for is what governs its work.
    tunables: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-session configuration overrides in force, normalised. "
                    "Empty means every stage runs on the server's own "
                    "configuration.")
    #: The hash of the analytical configuration this session's iterations
    #: actually run under — the same value receipts carry, so a client can
    #: confirm a judgement was made under the settings it requested.
    config_hash: str = Field(
        default="",
        description="Fingerprint of the effective analytical configuration. "
                    "Matches `config_hash` on every receipt this session's "
                    "iterations produce.")


class RetryTriageIn(RequestModel):
    """How to re-judge the posts a model failure lost (8.8)."""

    batch_size: int | None = Field(
        default=None, ge=1, le=100,
        description="Posts per model call for the retry. Omit to use "
                    "`triage.batch_size` — the retry halves it on each "
                    "TruncatedResponse down to one anyway, so an override is "
                    "only needed to start smaller than the configured value.")


class IterationAccepted(BaseModel):
    iteration_id: int
    session_id: int
    seq: int = 0
    status: Literal["RUNNING", "PENDING", "FINISHED"]
    stage: str
    poll_url: str
    #: Present for mode=manual: the stage a step would run next.
    next_stage: str | None = None
    #: Set when this iteration is a re-triage of an earlier one (8.8). The
    #: parent is not modified; both records stand as what each run did.
    retry_of_iteration_id: int | None = None
    budget_plan: dict[str, float] = Field(default_factory=dict)


class IterationOut(BaseModel):
    iteration_id: int
    session_id: int
    seq: int
    stage: str = Field(
        description="One of the eight pipeline stages, or COMPLETE / FAILED.")
    outcome: Literal["COMPLETE", "PARTIAL", "FAILED"] | None = Field(
        default=None,
        description="Null while running. PARTIAL means the sequence finished "
                    "but something was lost — the alerts are real findings "
                    "made from incomplete evidence. FAILED means a "
                    "prerequisite stage never ran and the alerts should not "
                    "be read at all.")
    running: bool = False
    anchor_at: str = Field(
        default="", description="The instant the correlation window centres on.")
    started_at: str = ""
    finished_at: str | None = None
    next_stage: str | None = Field(
        default=None, description="What a debug step would run next, if any.")
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="Queries enqueued/executed/failed/skipped, and rows "
                    "written per analytical table. `triage_decisions` here is "
                    "a TOTAL; see `triage_states` for what those decisions "
                    "were.")
    triage_states: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Judgements by durable outcome: `ACCEPTED`, `REJECTED`, "
            "`UNDECIDED`, `INVALID_OUTPUT`, `MODEL_ERROR`. Absent keys are "
            "zero. The distinction is the point and it is not recoverable "
            "from the total: REJECTED is a judgement that the item is not "
            "relevant, while UNDECIDED, INVALID_OUTPUT and MODEL_ERROR are "
            "posts that were collected and paid for and never judged — a "
            "coverage gap, which caps the confidence band and reaches the "
            "alert caveat. Before this field a client could only infer that "
            "from free-text `degradations`."))
    budget: dict[str, dict[str, float]] = Field(
        default_factory=dict,
        description="Units billed this iteration, per provider. Always all "
                    "four providers, zero included.")
    status: Literal["PENDING", "RUNNING", "INTERRUPTED", "FINISHED"] = Field(
        default="PENDING",
        description="The lifecycle state, which `outcome` alone cannot give "
                    "you: an interrupted iteration has a null outcome and is "
                    "not running, and a client watching those two fields "
                    "would never stop. Stop on FINISHED or INTERRUPTED.")
    interrupted_at: str | None = Field(
        default=None,
        description="Set when a process ended without finishing this run. It "
                    "never clears, so the history survives a later resume.")
    interrupted_stage: str | None = Field(
        default=None, description="The stage that was in flight when it died.")
    retry_of_iteration_id: int | None = Field(
        default=None,
        description="Set when this iteration is a re-triage of an earlier one "
                    "(8.8). It re-judged that iteration's unanswered posts, "
                    "inherited its `anchor_at`, and did not re-collect. The "
                    "parent is unmodified — both records stand as what each run "
                    "did.")
    resumable: bool = Field(
        default=False,
        description="True while the iteration awaits a resume or an abandon — "
                    "for a crash-interrupted run and equally for one merely "
                    "left open. Either blocks a new iteration on this session, "
                    "so either needs closing.")
    #: What the iteration could not do. An empty list and a missing field mean
    #: different things, so this is always present.
    degradations: list[str] = Field(default_factory=list)
    error_message: str | None = None


class Confidence(BaseModel):
    """An ALERT's score and band. NONE is impossible here by construction."""

    score: float = Field(ge=0.0, le=1.0,
                         description="0..1. A weighted sum of per-family "
                                     "weight x quality, not a probability.")
    band: Literal["LOW", "MEDIUM", "HIGH"] = Field(
        description="Cannot be HIGH when any contributing source failed.")


class CorrelationConfidence(BaseModel):
    """A CORRELATION's score and band, which may be NONE.

    Separate from `Confidence` rather than widening it. NONE is a real computed
    outcome — "we looked and this did not reach a band" — and is stored so that
    finding nothing is on the record; but an alert is never written for one, so
    the alert contract keeps the narrower type that says so.

    Reusing the alert model here was a 500 on the very case the correlation
    routes exist to expose: a band-NONE correlation failed validation and the
    listing crashed. The tests missed it because their fixture raises
    `alert_min_score`, which produces a BELOW_FLOOR row with a real band —
    never the sub-threshold band itself.
    """

    score: float = Field(ge=0.0, le=1.0)
    band: Literal["NONE", "LOW", "MEDIUM", "HIGH"] = Field(
        description="NONE means no band rule fired at all, which is a result "
                    "rather than an absence of one.")


class CancelIn(RequestModel):
    """Why a run is being stopped. The reason is recorded, not just the act."""

    requested_by: str | None = None
    reason: str | None = Field(None, max_length=500)


class CancelOut(BaseModel):
    iteration_id: int
    status: str
    cancel_requested_at: str
    #: What the iteration will still do. Cancellation is cooperative: an
    #: iteration that already bought collection finishes scoring it, because
    #: stopping dead would spend the money and discard the evidence.
    will_still_run: list[str] = Field(default_factory=list)
    note: str = ""


class ReviewIn(RequestModel):
    """A human decision about DISTRIBUTION. Nothing analytical moves."""

    review_state: Literal["UNREVIEWED", "RELEASED", "WITHHELD"]
    reviewed_by: str | None = None
    note: str | None = Field(None, max_length=2000)


class ReviewOut(BaseModel):
    alert_id: int
    review_state: str
    reviewed_at: str | None = None
    reviewed_by: str | None = None
    note: str | None = None


class CapabilityCity(BaseModel):
    """What this deployment could actually collect for one jurisdiction."""

    name: str
    state: str | None = None
    #: Whether the name resolved at all, and by which rung of the geo ladder.
    resolved: bool
    resolved_by: str
    airports: list[str] = Field(default_factory=list)
    pickup_location: str | None = None
    #: Source families that would produce data here, and those that would not.
    supported_sources: list[str] = Field(default_factory=list)
    unsupported_sources: list[str] = Field(default_factory=list)
    #: Present when a family is unsupported: why, in words an operator can act
    #: on. An unsupported jurisdiction must report as unsupported rather than
    #: silently returning nothing.
    limitations: list[str] = Field(default_factory=list)


class CapabilitiesOut(BaseModel):
    """What the system can and cannot do, before anyone asks it to.

    Exists so that "no alerts for this county" can be distinguished from "this
    county was never collectable". Without it the two are identical on the
    wire, and the second one silently reads as reassurance.
    """

    mission: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Which mission definition this deployment loaded, and the "
            "vocabularies it supplies. **This is where the permitted `track` "
            "and `location_type` values come from.** They are reported here "
            "rather than declared as enums in this schema because they are "
            "read from a mission pack at startup, so no fixed enum could "
            "describe them — a deployment running a different pack accepts "
            "different values against this same contract. Carries `id`, "
            "`version`, `digest`, `tracks` and `location_types`; `digest` is "
            "the hash stamped on every receipt, so a client holding an alert "
            "can tell whether the definition has changed since it was "
            "written. When `configured` is false no mission is loaded: the "
            "database and this contract are available but no iteration can "
            "run, and `effect` says so."),
    )
    tracks: list[str] = Field(default_factory=list,
                              description=TRACKS_FIELD)
    signal_families: list[str] = Field(default_factory=list)
    #: Per provider: configured, and what it meters.
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: Coverage is US-only and airport-keyed; stated rather than implied.
    geography: dict[str, Any] = Field(default_factory=dict)
    retention: dict[str, Any] = Field(default_factory=dict)
    #: Populated when ?city= is given.
    cities: list[CapabilityCity] = Field(default_factory=list)
    #: Unresolved downstream-use questions, per provider (8.3). Never empty:
    #: no API call can establish a redistribution right, so these close by
    #: legal review or not at all.
    open_rights_questions: list[dict[str, str]] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class AlertOut(BaseModel):
    """One correlated finding with the evidence behind it."""

    #: Whether a human has cleared this for escalation (8.2). UNREVIEWED is the
    #: absence of a verdict, not a negative one — a consumer distributing
    #: onward should filter `?review_state=RELEASED` rather than assume.
    review_state: str = "UNREVIEWED"
    alert_id: int
    iteration_id: int
    city: str = Field(description="\"Name, ST\", or just the name if no state.")
    track: str = Field(description=TRACK_FIELD)
    confidence: Confidence = Field(
        description="Computed deterministically in Python. Not a probability, "
                    "and not to be presented to a decision-maker as one.")
    summary: str = Field(
        description="One or two sentences, written by a language model that is "
                    "never shown the score and cannot change it.")
    caveat: str | None = Field(
        default=None,
        description="Deterministic disclosure, written in Python and never by "
                    "the model, so it cannot be softened or dropped. Present "
                    "when a source could not be collected — naming the family "
                    "and, for a partial loss, the endpoint — and ALSO when "
                    "this iteration contributed no evidence of its own, in "
                    "which case it names the iteration that last did and how "
                    "old the contributing signals are. Either can appear "
                    "without the other.")
    earliest_eta: str | None = Field(
        default=None,
        description="Soonest arrival among the contributing flights. The most "
                    "actionable single field when it is present.")
    created_at: str
    evidence_url: str = Field(
        description="Drill-down to every contributing signal and the arithmetic.")
    social_posts: list[dict[str, Any]] = Field(
        default_factory=list, description="Contributing social signals.")
    flights: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Contributing flights. `category_confidence: AMBIGUOUS` "
                    "means the source could not determine the category, not "
                    "that it is unknown to the operator.")
    lodging: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Short-term-rental availability, and hotel price where the "
                    "price signal is enabled.")
    rental_cars: list[dict[str, Any]] = Field(
        default_factory=list, description="Rental-car availability by class.")


class AlertTupleOut(BaseModel):
    """`?format=tuple`: the four evidence groups as a positional array.

    The requirement asks for an array of tuples of (social posts, flights,
    lodging changes, car availability), each carrying a summary and a
    confidence score. JSON has no tuple type, so `evidence` is a fixed
    four-element array in that exact order and the summary and confidence stay
    addressable beside it rather than being appended as positions five and six.

    Mission streams do not change the shape: every social-feed observation —
    whatever stream found it, and whatever family that stream counts as in
    banding — lives in the first element, each entry carrying its `stream`.
    """

    alert_id: int
    city: str
    track: str = Field(description=TRACK_FIELD)
    summary: str
    confidence: Confidence
    caveat: str | None = None
    evidence: list[list[dict[str, Any]]]
    evidence_url: str


class CorrelationOut(BaseModel):
    """One scored city/track, whether or not it became an alert (8.7b)."""

    correlation_id: int
    city: str
    track: str = Field(description=TRACK_FIELD)
    confidence: CorrelationConfidence
    distinct_types: int = 0
    data_completeness: float = Field(
        default=1.0,
        description="Share of the mission's signal families collected at "
                    "all — the engine's four, plus any stream the mission "
                    "promotes to a family of its own. A "
                    "family counts as missing only when NOTHING in it was "
                    "collected — see `failed_families`.")
    band_capped: bool = False
    failed_sources: list[str] = Field(
        default_factory=list,
        description="Every failed collection as `SOURCE_TYPE:endpoint`, e.g. "
                    "`LODGING:/search`. A social loss attributable to one "
                    "mission stream appears as `SOCIAL(<stream>)`. Finer than "
                    "`failed_families`, and does NOT drive "
                    "`data_completeness`.")
    failed_families: list[str] = Field(
        default_factory=list,
        description="Signal families with NOTHING collected — what "
                    "`data_completeness` counts. A family in `failed_sources` "
                    "but absent here was degraded, not lost.")
    rule_trace: str = ""
    contributions: dict[str, float] = Field(default_factory=dict)
    alert_decision: Literal["ALERTED", "BELOW_FLOOR", "BAND_NONE"] | None = Field(
        default=None,
        description="What ALERTING concluded. BELOW_FLOOR: under "
                    "`correlation.alert_min_score`. BAND_NONE: no band "
                    "qualified. Null means ALERTING has not run for this "
                    "iteration yet, which is not the same as deciding against.")
    alert_decision_reason: str | None = Field(
        default=None,
        description="The decision in words, with the numbers that produced it. "
                    "Written by the agent that decided, so a reader does not "
                    "have to hold the configuration to reconstruct it.")
    alerted: bool = False
    computed_at: str = ""
    evidence_url: str = ""


class CorrelationsOut(BaseModel):
    """Everything an iteration scored. The sub-threshold rows are the point.

    A correlation that produced no alert is the near miss, and the near misses
    are what the interim floors in `correlation` and `sensitivity` are meant to
    be calibrated from. They have no alert row, and every other route into the
    evidence surface resolves `alerts.correlation_id` first.
    """

    iteration_id: int
    #: Rows per `alert_decision`, with NOT_DECIDED for correlations ALERTING
    #: has not reached.
    counts: dict[str, int] = Field(default_factory=dict)
    correlations: list[CorrelationOut] = Field(default_factory=list)


class EvidenceOut(BaseModel):
    """Full drill-down: every contributing signal back to its raw payload."""

    #: Null when this correlation produced no alert. Reached through
    #: `/v1/correlations/{id}/evidence` in that case.
    alert_id: int | None = None
    #: Always present: the evidence surface is assembled from the correlation,
    #: and the alert route resolves this and delegates.
    correlation_id: int = 0
    city: str
    track: str = Field(description=TRACK_FIELD)
    #: Read from the CORRELATION. AlertAgent copies score and band onto the
    #: alert unchanged — a Phase 5 test asserts they are identical — so this is
    #: the same answer from the row that computed it, and the only one a
    #: correlation without an alert has.
    #:
    #: The wider band type, because a correlation reached directly may be NONE.
    #: Reached through an alert it never is, and `AlertOut` keeps the narrow one.
    confidence: CorrelationConfidence
    #: Null without an alert: no model was asked to write one, and inventing a
    #: sentence here would present a summary nobody produced.
    summary: str | None = None
    caveat: str | None = None
    alert_decision: Literal["ALERTED", "BELOW_FLOOR", "BAND_NONE"] | None = None
    alert_decision_reason: str | None = None
    #: Which band rule fired, in words, copied from the correlation.
    rule_trace: str = ""
    #: Per-family weight × quality, summing to the score.
    contributions: dict[str, float] = Field(default_factory=dict)
    distinct_types: int = 0
    data_completeness: float = Field(
        default=1.0,
        description="Share of the mission's signal families collected at "
                    "all — the engine's four, plus any stream the mission "
                    "promotes to a family of its own; `GET /v1/capabilities` "
                    "reports the full list as `mission.families`. A family "
                    "counts as missing only when NOTHING in it was collected "
                    "(9.11): if the lodging availability endpoint failed and "
                    "the price endpoint succeeded, lodging was measured and "
                    "does not count against this. Compute it as "
                    "`1 - len(failed_families) / len(mission.families)`.")
    failed_sources: list[str] = Field(
        default_factory=list,
        description="Every failed collection as `SOURCE_TYPE:endpoint` — for "
                    "example `LODGING:/search`. The endpoint is the query's "
                    "ENTRY endpoint, so a lodging pairing shortfall that "
                    "happens at `/availability` still reports `/search`; the "
                    "query row's `skip_reason` carries the precise cause. "
                    "Where no endpoint was recorded the bare source type "
                    "appears, and a social loss attributable to one mission "
                    "stream appears as `SOCIAL(<stream>)`. This is finer than "
                    "`failed_families` and does NOT drive "
                    "`data_completeness`.")
    #: 9.11. The families with nothing collected at all — the subset that
    #: `data_completeness` is computed from. Exposed because a reader given the
    #: number and not its inputs cannot check it.
    failed_families: list[str] = Field(
        default_factory=list,
        description="Signal families with NOTHING collected. This is what "
                    "`data_completeness` counts and what the caveat names; a "
                    "family present in `failed_sources` but absent here was "
                    "degraded rather than lost.")
    band_capped: bool = False
    #: 9.6. What ELSE would produce this evidence, derived deterministically
    #: from which families contributed. Each entry carries a stable `code`, the
    #: `statement` of the competing explanation, and `weakened_by` — what in
    #: THIS correlation argues against it, empty when nothing does.
    #:
    #: On the evidence surface rather than on the alert, deliberately. The
    #: alert carries one sentence a model wrote and a number it never saw; a
    #: second block of prose there would compete with the summary for the same
    #: attention. This sits beside the arithmetic and the band rule, where a
    #: reader has already come to argue with the conclusion.
    #:
    #: Null on a correlation computed before these rules existed. An empty list
    #: is different: it means no family contributed, so there is nothing to
    #: explain away.
    alternatives: list[dict[str, str]] | None = Field(
        default=None,
        description=(
            "Competing explanations for this evidence, each with a stable "
            "`code`, a `statement`, and `weakened_by` — what in THIS "
            "correlation argues against it, empty when nothing does, because "
            "an unanswered alternative serves a reader better than a "
            "manufactured rebuttal. Deterministic, never model-written: the "
            "loaded mission supplies the explanations per signal family and "
            "the engine decides which of them this evidence admits, so a "
            "booking-only correlation admits ordinary-demand explanations "
            "that a multi-family one does not. `null` on a correlation "
            "computed before these rules existed; an empty list means no "
            "family contributed and there is nothing to explain away."))
    #: 9.13. Whether this iteration contributed evidence of its own, which
    #: iteration last did, and the age range of the contributing signals. The
    #: correlation window reads across iterations by design, so an alert can
    #: rest entirely on collection paid for days ago — a correct current
    #: assessment, and one a reader must be able to tell from a new
    #: observation. Null on a correlation computed before the check existed.
    evidence_freshness: dict[str, Any] | None = None
    config_hash: str | None = Field(
        default=None,
        description=(
            "Fingerprint of the analytical configuration this score was "
            "computed under — the same value the iteration's classification "
            "receipts carry, so a reader can confirm the arithmetic and the "
            "model judgements ran under one set of tunables. Correlation "
            "involves no model and therefore writes no receipt of its own; "
            "without this the settings behind a score lived only in a config "
            "file that anyone may edit afterwards, and re-scoring a stored "
            "iteration could silently produce different numbers. Compare it "
            "against `GET /v1/capabilities`; a mismatch means the tunables "
            "have moved since, not that the score is wrong. Null on a "
            "correlation computed before this was recorded."))
    #: 9.10. Per flight kind: whether it was scored as excess over this city's
    #: normal traffic, what that normal was, and what was observed.
    #: `UNBASELINED` means the absolute count stood because there were too few
    #: prior samples — a cold start, and a reader must be able to tell that
    #: from a city whose normal genuinely is what was seen today. Null on a
    #: correlation computed before baselining existed.
    flight_baseline: dict[str, Any] | None = None
    #: 9.4. How directly this correlation's evidence is known, counted by
    #: class, so a reader sees at a glance that (say) three of eight rows came
    #: from a vendor's cache rather than having to compare eight signal
    #: records. `INTERMEDIARY_LIVE` on everything is the ordinary case; the
    #: absence of `DIRECT` is the standing fact about this system, since every
    #: provider is an intermediary over a platform or publisher.
    collection: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Contributing signals counted by how directly they are known, so "
            "acquisition is one comparable value rather than four per-family "
            "provenance fields. `DIRECT` (retrieved from the party that "
            "holds the record), `INTERMEDIARY_LIVE` (a vendor's live call "
            "against a platform or publisher), `INTERMEDIARY_CACHED` (served "
            "from a vendor cache and billed as though fresh, so the ledger is "
            "no guide to freshness), `UNRECORDED` (the default, so a writer "
            "that forgets produces a visible absence rather than a plausible "
            "claim). Empty classes are omitted. The absence of `DIRECT` is "
            "the standing fact about this deployment: every provider here is "
            "an intermediary."))
    calendar_matches: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Operator-calendar events overlapping this correlation's window, "
            "snapshotted verbatim at scoring time (event_id, name, city, "
            "starts_at, ends_at, category, note, added_at) so the row stays "
            "self-contained after later appends. ANNOTATION ONLY: nothing "
            "here moved the score or the band — the SCHEDULED_EVENT entry in "
            "`alternatives` is where a reader is told a planned event would "
            "produce the same pattern. `null` on a correlation computed "
            "before the feature existed or for a session with no calendar; "
            "`[]` means a calendar exists and nothing matched."))
    signals: list[dict[str, Any]] = Field(default_factory=list)
    #: How the summary was produced (8.1) — provider, the model actually
    #: served, prompt and rules versions, and the hashes that make two
    #: judgements comparable. Null when the summary came from the
    #: deterministic fallback, because no model call produced it.
    #:
    #: The prompt HASH is here; the prompt TEXT is not. A reader needs to know
    #: the criteria were the same, not what they say.
    receipt: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Provenance for the model call behind `summary`, or null when the "
            "deterministic fallback wrote it. A loose object because the field "
            "set follows the provider echo, but three keys are the point of "
            "it: `mission_hash` — compare against `mission.digest` from "
            "`GET /v1/capabilities` to tell whether the definition has changed "
            "since this judgement was made; `config_hash` — compare against "
            "`SessionOut.config_hash` for the same question about settings; "
            "and `prompt_hash`, which says two judgements were made under the "
            "same criteria without disclosing what they are."))


class QueueOut(BaseModel):
    session_id: int
    iteration_id: int | None = None
    status_counts: dict[str, int] = Field(default_factory=dict)
    #: Every refusal, by outcome. A query that did not happen is as auditable
    #: as one that did.
    decision_counts: dict[str, int] = Field(default_factory=dict)
    scheduled_ahead: int = 0
    queries: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)


class StageOut(BaseModel):
    stage: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    error_message: str | None = None
    wrote: dict[str, int] = Field(default_factory=dict)
    decisions: dict[str, int] = Field(default_factory=dict)
    agents: list[dict[str, Any]] = Field(default_factory=list)
    api_calls: dict[str, dict[str, float]] = Field(default_factory=dict)
    log: list[dict[str, Any]] = Field(default_factory=list)
    #: CORRELATING only: what it scored, per city and actor track, including
    #: the rows that will not become alerts. Empty for every other stage.
    correlations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="What CORRELATING concluded. Each entry carries "
                    "`evidence_url` — the drill-down works for a correlation "
                    "with no alert, which is the case that had no route at all "
                    "before 8.7(b).")
    #: TRIAGING only: collected posts that never reached the model, by reason.
    skips: dict[str, int] = Field(
        default_factory=dict,
        description="Why collected posts did not reach the model (8.9). STALE "
                    "is a decision — older than `triage.max_post_age_hours`. "
                    "The PAYLOAD_* and ITEM_* reasons are defects: evidence "
                    "that was collected and paid for and then could not be "
                    "read, and a PAYLOAD_* reason costs every post in that "
                    "response. Empty for every other stage.")


class StagesOut(BaseModel):
    iteration_id: int
    stage: str
    outcome: str | None = None
    next_stage: str | None = None
    stages: list[StageOut] = Field(default_factory=list)


class StepOut(BaseModel):
    iteration_id: int
    stage: str
    ok: bool
    next_stage: str | None = None
    outcome: str | None = None
    #: The stage's own report, so a step and a verify are one round trip.
    report: StageOut


class DiscardOut(BaseModel):
    iteration_id: int
    stage: str
    deleted: dict[str, int] = Field(default_factory=dict)
    queries_reset: int = 0
    #: Already billed by this stage. Not reclaimed: re-running spends again and
    #: the ledger will show both.
    units_spent: dict[str, float] = Field(default_factory=dict)
    not_reverted: list[str] = Field(default_factory=list)
    degradations_retracted: int = Field(
        default=0,
        description="Notes this stage had recorded about what it could not do, "
                    "removed with its rows. A gap the re-run may close must not "
                    "outlive the evidence for it.")
    next_stage: str


class EpochOut(BaseModel):
    """One process instance."""

    epoch_id: int
    started_at: str
    host: str
    pid: int
    entry_point: str
    ended_at: str | None = None
    shutdown_kind: Literal["CLEAN", "TIMEOUT", "UNKNOWN"] | None = Field(
        default=None,
        description="UNKNOWN is written by a LATER process onto one it found "
                    "open, and leaves ended_at null — nothing knows when a "
                    "killed process died, and inventing a time that later "
                    "reads as fact is worse than admitting the gap.")
    stranded: list[int] = Field(default_factory=list)


class InterruptedOut(BaseModel):
    iteration_id: int
    session_id: int
    seq: int
    kind: Literal["INTERRUPTED", "OPEN"] = Field(
        default="INTERRUPTED",
        description="INTERRUPTED: a process died mid-run and the reconcile "
                    "stamped it. OPEN: never finished by any route — a manual "
                    "walk left partway, or a cancellation recorded against an "
                    "iteration that was not on a worker. Both block a new "
                    "iteration and both are closed the same two ways; the "
                    "distinction tells you what happened, not what to do.")
    interrupted_at: str | None = Field(
        default=None,
        description="Null for kind=OPEN. Only the crash reconcile stamps it.")
    interrupted_stage: str | None = None
    stage_pointer: str = ""
    started_at: str = ""
    resume_url: str = ""
    abandon_url: str = ""
    plan_url: str = ""


class RecoveryOut(BaseModel):
    """What the current process found when it started, and what still blocks."""

    epoch: EpochOut
    previous_epoch: EpochOut | None = None
    #: Crash-stamped only: what THIS process's reconcile found dead at startup.
    interrupted: list[InterruptedOut] = Field(default_factory=list)
    #: Every iteration with `finished_at IS NULL`, which is exactly what a new
    #: iteration is refused for. A superset of `interrupted`: an iteration left
    #: open without a crash is stamped by nothing and appears only here, and
    #: before 8.7(a) it appeared nowhere at all while still blocking nothing.
    blocking: list[InterruptedOut] = Field(default_factory=list)
    #: Epochs left alone because their process is demonstrably alive — two
    #: processes are sharing this database, which the per-process session lock
    #: cannot survive.
    refused_epochs: list[int] = Field(default_factory=list)


class RecoveryPlanOut(BaseModel):
    """What resuming would do, before it does any of it."""

    iteration_id: int
    session_id: int
    resume_from: str
    derived_by: Literal["RECONCILED", "IN_FLIGHT", "BETWEEN_STAGES",
                        "NOTHING_RAN"] = Field(
        description="How the resume point was decided. RECONCILED is the "
                    "durable one, read from the row the reconcile stamped.")
    stage_pointer: str
    interrupted_stage: str | None = None
    paid: bool = Field(
        description="True when resuming would collect again at a vendor. "
                    "`confirm_respend` is then required.")
    queries_to_recollect: list[dict[str, Any]] = Field(default_factory=list)
    already_banked: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Paid for and stored before the crash. Not re-bought, and "
                    "not counted as coverage either — a completed query with "
                    "no signal reads as 'we looked and found nothing'.")
    already_spent: dict[str, float] = Field(default_factory=dict)
    estimated_units_upper_bound: dict[str, float] = Field(
        default_factory=dict,
        description="An upper bound, NOT a price. FR24 bills per record "
                    "returned, so no pre-flight figure can be exact — judge "
                    "from queries_to_recollect.")


class AbandonOut(BaseModel):
    iteration_id: int
    outcome: str | None = None
    queries_marked_interrupted: int = 0
    coverage_gaps: dict[str, int] = Field(
        default_factory=dict,
        description="Source types now counted as missing collection, by count. "
                    "These lower data_completeness and cap the band.")
    correlations_written: int = 0
    alerts_written: int = 0
    scheduling_skipped: bool = Field(
        default=True,
        description="Abandon never schedules follow-ons: work queued by an "
                    "iteration nobody finished would arrive as a surprise.")


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    database: str
    schema_version: int
    dry_run: bool = False
    debug_endpoints: bool = False
    active_sessions: int = 0
    running_iterations: list[int] = Field(default_factory=list)
    epoch_id: int | None = None
    #: Reported `degraded` while any exists. A half-collected city sitting
    #: unrecovered for a week should not read as `ok`.
    interrupted_iterations: list[int] = Field(default_factory=list)
    budget: dict[str, dict[str, float]] = Field(default_factory=dict)
    #: Present only when ?deep=true, which requires authentication: probing four
    #: vendors is a rate-limited operation and must not be free to anyone who
    #: can reach the port.
    connectors: dict[str, Any] | None = None
    retention: dict[str, int] = Field(default_factory=dict)
