# Surge I&W — Engine behaviours and scoring

*(c) David Blum, 2026, dmblum@gmail.com*

This document records **how Surge behaves and why**: the principles that
drive the architecture, the design decisions taken and rejected, the code review
that reshaped the model boundary, and the live measurements that changed the
design.

It is not a user guide — see [README.md](../README.md) for installation,
configuration and operation. It is not a changelog either; routine fixes are
omitted deliberately. What is here is what a maintainer needs in order to change
the system without undoing a decision that was made for a reason.

**What is here and what is not.** Surge is two things kept apart: a
mission-agnostic engine, and a mission pack that says what it is looking for
(see [missions.md](missions.md) for the pack format). This document is the
**engine's** — the mechanism, and the reasoning behind it, both true whatever
pack is loaded.

Every measurement below was taken while running a real mission, because there
is no other way to measure one. What that mission concluded from those runs —
which tracks it scores, what its weights are, where its relevance line sits,
and what a worked example of its scoring looks like — belongs to that pack and
is recorded in its own `docs/`. The division is the same one the code makes:
if an engineer would change it to fix a bug it is here, and if an analyst would
change it to ask a different question it is in the pack.

---

## Contents

- [Principles](#principles)
- [Architectural decisions](#architectural-decisions)
- [Writing a mission](missions.md)
- [The data sources](#the-data-sources)
- [Collection and triage](#collection-and-triage)
  - [Every post that never reached the model](#every-post-that-never-reached-the-model)
  - [Recovering judgements a model failure lost](#recovering-judgements-a-model-failure-lost)
- [Defining a session's geography](#defining-a-sessions-geography)
- [Tipping and queuing](#tipping-and-queuing)
- [Correlation and confidence](#correlation-and-confidence)
  - [A correlation that becomes no alert](#a-correlation-that-becomes-no-alert)
- [The model-output boundary](#the-model-output-boundary)
- [Evidence identity](#evidence-identity)
- [Candidate versus signal](#candidate-versus-signal)
- [Surviving a crash](#surviving-a-crash)
- [The per-session lock](#the-per-session-lock)
- [Contract hardening](#contract-hardening)
- [Classification receipts](#classification-receipts)
- [Provider governance](#provider-governance)
- [The Phase 6 code review](#the-phase-6-code-review)
- [Changes based on live runs](#changes-based-on-live-runs)
- [Testing strategy](#testing-strategy)
- [Operational limits](#operational-limits)
- [Open questions](#open-questions)

---

## Principles

The following principles drive the design.

### 1. Control flow lives in Python, not in the model

The tipping rules, the spatial and temporal correlation, and the confidence 
score are all deterministic. The LLM is used at exactly two points where language reasoning is genuinely required:

- judging whether a free-text social post is evidence of what the loaded
  mission is looking for, and extracting which city, which facility, which
  track, and how imminent;
- writing the one-or-two sentence summary attached to an alert.

**The LLM never computes or alters a confidence score.** A test asserts
`alerts.confidence_score` is byte-identical to the deterministic
`correlations.score`.

The search lexicon is a table rather than a model call for the same reason: an
someone asking "why did you search that" deserves a better answer than "the
model chose it".

### 2. A failure must never look like an absence of threat

The predecessor's connectors did this:

```python
except Exception as exc:
    logger.error(...)
    return []
```

An expired token, a 402 on exhausted credits, a rate limit, a DNS blip — every
one became *"no military flights inbound"*, indistinguishable from a genuine
empty result.

Every connector raises a typed `ConnectorError`. A failed query writes no
signal, but it *is* recorded as a coverage gap: `data_completeness` drops, the
band cannot reach HIGH, and the alert carries a caveat naming the missing
source. The single most important test in the suite asserts that if any
contributing query failed, no alert for that city can reach HIGH.

This principle generalises well beyond connector errors, and most of the defects
found during development were violations of it in some new disguise — a budget
refusal that left no queue row, a model outage that reported full coverage, a
cancelled stage whose skipped collection was invisible, a dead vendor feed that
read as a quiet market. Each is recorded in its own section below.

### 3. No analytical decision without a database record

Every decision has a row, **including decisions not to act**. Refused queue
entries land in `queue_decisions`; rejected social posts land in
`triage_decisions` with a durable state. A silently dropped signal is a bug, and
so is a silently refused query.

---

## Architectural decisions

**SQLite is the communication bus.** No agent calls another. Each reads its
inputs from the database and writes its outputs there; the orchestrator
sequences them by passing an `iteration_id` — the integer is the entire payload.
This is what makes the audit trail complete by construction rather than by
discipline.

**The database is file-backed, not in-memory.** Iterations are separate API
calls and scheduled follow-ons must survive between them. This implies

- **Data retention becomes the code's responsibility.** FR24's licence forbids
  retaining data beyond 30 days, so `services/retention.py` is a required
  component. `signals.raw_id` and `triage_decisions.raw_id` are
  `ON DELETE SET NULL` — the licensed payload is deleted while the analytical
  record survives with a nulled pointer. Without that, SQLite refuses the delete
  outright.
- **Interruption becomes recoverable** — but only because something reconciles
  it. See [Surviving a crash](#surviving-a-crash).

**Failure is isolated per agent, not per iteration.** An agent that raises marks
its own `agent_runs` row FAILED, appends to the iteration's degradation list,
and returns False; the driver continues. Only `SEEDING` can fail the whole run.
A social-connector outage must not discard a military-flight cluster another
stage already collected.

**Migrations are driven by the live table definition, not the version number.**
`CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a schema edit
reaches an older file only through an `ALTER`. `SurgeDB._migrate()` adds any
missing column at construction, which is idempotent and correct even for a file
whose `schema_version` row is wrong.

**Tracks share one collection pass.** A mission's tracks are scored with
different weights but collected together, so adding one costs no extra API
calls — only a second pass over data already paid for.

**The mission is data, not code.** Who we are looking for, what words find
them, what each observation is worth and what makes an item relevant are read
from a **mission pack** at startup — see [missions.md](missions.md). The engine
supplies no fallback for any of it: a default lexicon or a default prompt would
collect and screen against criteria nobody chose while looking exactly like
criteria somebody did.

Three consequences worth stating, because each was a decision:

- **The vocabularies left the schema.** `signals.track`,
  `correlations.track` and `key_locations.location_type` were CHECK-constrained
  on one mission's values. SQLite cannot know a pack's vocabulary, so the
  constraints were dropped in v12 and validation moved to Python at every write
  path — where the error can also name the mission it was checked against.
  Removing a CHECK removes a guarantee, so `tests/test_database.py` now asserts
  that the guarantee moved rather than evaporated.
- **The contract stopped declaring them as enums.** A deployment running a
  different pack accepts different values against the same contract, so no
  fixed enum could describe them. `GET /v1/capabilities` reports the live set
  instead. This is a real reduction in what the contract alone guarantees; the
  mitigation is only worth anything if a client can find it, which is why that
  field carries a description saying so.
- **Provenance had to grow.** `code_revision` was sufficient while the prompt
  WAS code. A pack can be edited with no commit at all, so receipts gained
  `mission_id` and `mission_hash`, and the pack is hashed as a whole — a file
  that is neither loaded nor hashed would be a change nothing records.

It also closed a live gap. The search lexicon lived in Python, so it was **not**
covered by `receipts.config_hash`: two runs with different search terms produced
receipts claiming identical configuration.

**No agent framework, no ORM, no vector store, no task queue.** The scheduler is
a SQLite table and the trigger is an HTTP call.

---

## The data sources

Per-provider terms, billing units, failure modes, rate limits and retention are
recorded in [`docs/api/PROVIDERS.md`](api/PROVIDERS.md), generated from
`services/governance.py`. Key design decisions:

**Vendor documentation is not trusted.** Both major vendor doc sites are
JavaScript-rendered, and the rental-car vendor's marketing page advertised two
endpoint paths that do not exist. Every connector was built against a
machine-readable specification or a captured live response, and where neither
was available the endpoint was probed.

**Billing units differ per provider and the ledger must respect that.** API
Direct and Priceline bill per request; Staying bills credits per call and
reports the true figure in `meta.creditsCharged`; FR24 bills **credits per
record returned**. A flat one-unit-per-call ledger under-counted a Staying
search eightyfold. `api_calls.units` is computed *after* the response arrives,
never estimated beforehand.

**The FR24 `/count` tripwire was designed and then abandoned.** `/full` costs 8
credits per returned flight while `/count` costs 15% of that, which would make
it the cheap way to ask "is anything inbound at all". It returns 403 on the
subscribed tier. The collection order does not depend on it.

**Military category cannot be proven from a live position.** FR24's
`FlightPositionsFull` has 22 fields and `category` is not among them —
`categories` is filter-only. Such records are stored `AMBIGUOUS` and scored at
the *lower* of the two flight weights; they are upgraded to `CONFIRMED` only
when the same aircraft appears in a `flight-summary` result, which does return
category. This is why the historical call runs alongside the live one rather
than as an afterthought.

**Lodging measures a fixed listing set across two windows.** `/search` performs
set *discovery*, so passing dates would filter the set and bias it toward
whichever window was passed — listings that went unavailable would vanish from
the set instead of being counted as unavailable, systematically understating a
drop. The set is fixed once and cached; `/availability` then measures real
per-date booleans against it. The hotel price signal (`/price-compare`) is safe
to date because it asks for a priced quote on a *named property*, so nothing is
being selected out of a set.

**A requested platform reported `skipped` must raise.** The lodging vendor
confirmed by email that its API had been normalising a validation failure into
an `ok`/0-results leg — a failure wearing the costume of a successful search.
That is principle 2's exact adversary, so `PlatformUnavailableError` exists for
it.

**Rental car offer identity must be date-free.** The vendor's own `itemKey`
encodes the pickup and dropoff datetimes, so the same car has two different keys
in the two windows. Identity is composed from
`(partner code, vehicle class, pickup location)` instead.

---

## Collection and triage

`CollectionAgent` — no LLM. It dequeues by source type, consults the budget
guard, calls the connector, stores the raw payload, and writes deterministic
signals for flight, lodging and car records. Three rules govern it:

- **A failed query fails only itself.** The stage continues.
- **A budget refusal is not a failure.** It marks the query `SKIPPED_BUDGET`
  with a specific reason, so "we could not afford to look" stays distinguishable
  from "we looked and the endpoint was broken" and from "we looked and found
  nothing".
- **Social payloads produce no signals here.** Deciding whether a free-text post
  is evidence of what the mission looks for is language reasoning, so triage
  does it in the
  next stage. Collection stores the payload and stops.

`TriageAgent` — LLM. It batches posts, requires strict JSON per post, writes a
`triage_decisions` row for **every** post including rejections, and a `signals`
row only for accepted ones. It never sets a confidence score.

**A recency cut runs before the model sees anything.** The first live iteration
found the median collected post was 206 days old and only 1% fell inside the
correlation window — the system was spending its entire model budget judging
content that could not correlate. `triage.max_post_age_hours` drops stale posts
before the batch is built.

**The body window is head + tail, not a prefix.** A retraction is appended by
convention, so for *any* head length there is a body long enough to hide one.
The model sees the opening and the ending with the elision marked, and the
prompt states that a correction in the ending governs. Widening a prefix window
was rejected: it buys a bigger number, not a guarantee.

### Every post that never reached the model

Gathering had five ways to drop a post and recorded one of them as a bare count.
The other four left no trace at all, and two of those discarded a **whole vendor
payload** — collected, paid for, and gone with no count, no degradation and no
row. An absence of evidence produced by a parse failure was indistinguishable
from an absence of the thing being watched for, in the one place nobody was
looking.

Each drop now writes a `triage_skips` row: `STALE`, `PAYLOAD_UNPARSEABLE`,
`PAYLOAD_NOT_A_LIST`, `ITEM_NOT_AN_OBJECT`, `ITEM_NO_URL`. Three decisions shape
it.

**A separate table, not a state on `triage_decisions`.** Every state in that
table describes a model call that was *made*, including its three failure states.
A skipped post was never asked about, so sharing the table would make it answer
two questions — and the skips outnumber the judgements, so every count over it
would move: the SOCIAL coverage gap, the re-triage candidate set, the API's state
counts.

**Stale is a decision; a malformed payload is a defect.** Only the two
whole-payload reasons append a degradation, so an iteration that lost a vendor
response cannot close `COMPLETE`. Degrading on staleness instead would cap the
band on every city in every ordinary run, since most collected posts are stale.
`PAYLOAD_LEVEL_SKIPS` is the single definition of which is which.

**The row carries the cutoff, not just the verdict.** The cut is
`now - max_post_age_hours` evaluated at gather time, so a reader recomputing it
later gets a different answer than the run did. A stale row stores the
observation time, the cutoff in force and the configured window, and is therefore
reproducible from itself. A deduplicated post is deliberately *not* a skip: the
same article from three queries is judged once, and that judgement is the record.

### Recovering judgements a model failure lost

A batch that overruns `llm.max_tokens` records **every** post in it as
`MODEL_ERROR` — 40 posts across four batches in one live run. The evidence was
collected and paid for; only the judgement is missing, and it may be the
judgement that would have tipped paid collection.

**A retry opens a new iteration and never edits the parent.** The parent stays as
it was, partial and with its gap named; the child carries
`retry_of_iteration_id`. Two records in the order they happened rather than one
rewritten after the fact — which is what a reviewer months later can reconstruct
without being told.

**The child inherits the parent's `anchor_at`.** The correlation window is
measured from it, so a fresh anchor would slide the window off the very evidence
the retry exists to complete. `started_at` still records when the child actually
ran, so the two facts stay separable.

**Nothing is copied, because correlation already reads across the session.**
Signals are selected by observation time within the window, not by iteration, so
the child scores the union of both runs' evidence for free. That property is what
makes a new iteration cheap enough to prefer over an in-place rewrite.

**The candidate set comes from decision rows, never from re-gathering.** It is
exactly `TRIAGE_UNCOVERED` — `ACCEPTED` and `REJECTED` are finished judgements
and a rejection is a conclusion, not a failure. Re-gathering would re-derive the
staleness cutoff against a later clock and silently process a different set than
the one that failed. A post dropped for age has no decision row, so it cannot be
reached; the requirement holds by construction rather than by a second filter
that could drift from the first.

**Truncation is recorded structurally, not parsed out of a message.** Truncation
and a provider outage both surface as `MODEL_ERROR`, but only one is fixed by
sending fewer items. The retry halves its batch on each truncation down to one
rather than re-sending a batch that already proved too large — a retry that fails
identically spends the quota and looks like a decision.

**The child runs `TIPPING` through `ALERTING`.** `SEEDING` and
`COLLECTING_SOCIAL` are *inherited*, not skipped, and deliberately not recorded
in `skipped_stages_json`: `COLLECTING_SOCIAL` maps to the SOCIAL family, so
recording it would tell correlation that SOCIAL is uncollected on the one run
whose purpose is to improve social coverage. `SCHEDULING` **is** recorded as
skipped, because there the choice is real — the parent already queued follow-ons
for this evidence, and a duplicate set would be adopted by the next ordinary run.

The cooldown is deliberately **not** relaxed for a retry. The parent's collected
evidence is already inside the window and will be scored regardless, so a tip
duplicating one the parent executed has nothing to add; the refusal is the
correct outcome. What the cooldown lets through is what should: tips the parent
never raised.

---

## Defining a session's geography

A session's cities and their key locations may be loaded from a file rather than
inlined in the request. Two decisions govern it.

**An unresolvable city refuses the whole file, by name.** A loader that returned
the cities it understood would create a session quietly missing a jurisdiction
the operator believes is covered — and every later report about that place would
be a true absence of evidence about somewhere nobody looked. That is principle 2
at setup time, where the correction is still cheap; discovering it mid-iteration
produces a `SKIPPED_NO_MAPPING` query hours later that reads as a data gap rather
than a configuration mistake. The response echoes the resolved geography either
way, so an operator sees what they created rather than trusting that a file still
says what they remember.

**The API takes a name, not a path.** `input_set` names a file inside a
configured directory; anything containing a separator or `..` is refused. A path
field on an authenticated endpoint is still a file-disclosure primitive —
authenticated is not the same as trusted with the filesystem, and the caller is a
front end rather than the operator at a shell. The CLI does accept a path,
because an operator running it already has the filesystem and refusing there
would be ceremony rather than a control.

Cities resolve through the geo ladder at load, so the canonical key and the
method that produced it are settled before any collection is planned. Key
locations stay free text: there is no geocoder here, the lodging search string is
built from the name and facility matching compares against it, so pinning
coordinates would record a resolution nobody performed.

---

## Tipping and queuing

`agents/queueing.py`. Every enqueue passes through one `enqueue()` chokepoint,
which is the only place the guards live. Four invariants hold, verified with
Hypothesis over randomised workloads:

1. **No duplicates within an iteration** — enforced by the `idx_qq_dedup`
   UNIQUE index at the storage layer, not by a check that could be bypassed.
2. **Bounded fan-out** — `max_queries_per_iteration` and
   `max_queries_per_city`.
3. **No infinite chaining** — `tip_depth ≤ max_tip_depth`.
4. **No repeat inside the cooldown** — counting failures too, since a broken
   endpoint should not be retried on a tight loop either.

Every refusal writes a `queue_decisions` row.

| Rule | Trigger | Enqueues |
|---|---|---|
| `R0_SEED` | Stage 1, per active city | `SOCIAL` per lexicon group × city |
| `R1_FLIGHT_LIVE` | Relevant social signal for city C | `FLIGHT_LIVE`, `limit`-capped |
| `R2_FLIGHT_HIST` | R1 returned ≥1 record | `FLIGHT_HISTORY` — resolves categories |
| `R4_LODGING` | Social signal, per key location | `LODGING` two-window |
| `R5_CAR` | Social signal, per **airport IATA** | `CAR` two-window |
| `R6_FLIGHT_ESCALATION` | A flight signal arrives with no bookings queued | `LODGING` + `CAR` |
| `R7_REVISIT` | Stage 8: alert at MEDIUM+ | Same set, `not_before` in the future |

`R6` exists because the surge signature can enter through any door — flights tip
lodging as readily as social tips flights. This is why `max_tip_depth` is 3.

**`R5_CAR` keys on the airport IATA code** rather than resolving a city name.
The response echoes the code back, so the join is exact, and airport rental
fleets book out before off-airport ones, making them the leading indicator.

**City expansion is gated by a rule, not by the model.** An LLM proposes
candidate cities; two independent publishers making two distinct claims are
required to admit one, so a single viral post cannot steer collection into a city
nobody is deploying to. With `expand_cities: false` the refusal is recorded
rather than silent.

**A geo lookup that is ambiguous returns nothing.** The predecessor's prefix
match resolved `"San"` to whichever of san antonio / san diego / san francisco
dict iteration reached first — a wrong-city flight query, silently. The ladder is
exact match → explicit alias table → longest prefix requiring both a minimum
length *and* exactly one candidate; otherwise the query is marked
`SKIPPED_NO_MAPPING`, so the family reports absent rather than wrong.

---

## Correlation and confidence

Deterministic, in `base/scoring.py`. The arithmetic is the engine's; the
weights are the **mission's**, and arrive as a `TrackModel` built from the
loaded pack's `scoring.yaml`. They used to be a `WEIGHTS` dict in that module,
keyed by the two tracks the system was built for.

What a given set of weights produces on a given body of evidence is therefore a
question about a pack, and each pack answers it in its own `docs/` with a
worked example. What follows is the arithmetic that turns any of them into a
score and a band.

Decisions embedded in the model:

- **Bands need an anchor.** Booking scarcity has innocent causes — a convention,
  a holiday, a home game — so two booking signals alone cannot escalate. An
  anchor is social chatter or a flight whose category was actually *verified*;
  `AMBIGUOUS` flights never anchor, and the CAR family is excluded from
  anchoring entirely.
- **A single credible source is not a third of a signal.** IC practice treats
  one named source from an established outlet as STANDARD confidence, so
  corroboration quality starts at 0.6 for one observation rather than 0.33.
- **Corroboration breadth is the lower of distinct publishers and distinct
  claims.** Forty reposts of one claim are one claim, and three mastheads
  carrying one wire dispatch are one claim.
- **Capacity-weighted car scarcity.** A collapse in twelve-seat vans means
  something different from a collapse in economy sedans, because moving people
  needs seats. On-airport counters are weighted higher; peer-to-peer inventory
  and truncated responses are excluded, since a private host going offline is not
  demand and a pagination cut is not scarcity.
- **Lodging takes the stronger of availability collapse and price escalation**
  rather than averaging them, so a city with hotel prices but no calendar
  coverage still produces a signal. Price is deliberately *not* a fifth scoring
  family — `distinct_types` feeds the band thresholds, and adding a family would
  silently move every band.
- **Baselines are weekday-aligned** (+7 and +14 days), so ordinary weekend
  demand does not read as a surge. Both are stored, because divergence between
  them is itself informative.
- **The weights are a starting hypothesis, not a calibrated model.** There is no
  labelled ground truth for "a surge was in fact imminent". Everything is
  config, every input is persisted to `correlations.contributions_json`. **Do
  not present the score to a decision-maker as a probability.**

### A correlation that becomes no alert

Everything above is written for every scored city and track, alerting or not.
Nothing about it was reachable unless an alert existed: every route into the
evidence surface resolved `alerts.correlation_id` first, the stage view reported
a row count, and the alerting log carried an aggregate that named none of them.
A sub-threshold correlation was visible only by opening the database.

Those rows are the near misses, and **the near misses are the calibration set**.
The floors in `sensitivity` and the band gates in `correlation` are explicitly
interim and meant to be set from evidence — and the evidence for moving them is
precisely the set of correlations that failed to clear them.

Two changes. `correlations.alert_decision` and `alert_decision_reason` record
what alerting concluded and why, written by the agent that decides at the moment
it decides, with the numbers that produced the outcome — a reader should not need
the configuration in hand to reconstruct it, and NULL (alerting has not run)
stays distinct from a decision against. And the evidence surface is assembled
from the **correlation**, which is what it always read; the alert route resolves
its correlation and delegates, so the two cannot drift, and a correlation with no
alert returns the identical shape with `alert_id`, `summary` and `receipt` null —
null because no alert and no model call exist, not omitted.

Both routes are operational rather than debug-gated. An analyst calibrating
floors on a deployment serving an operations team should not have to mount the
endpoint that deletes analytical records in order to do it.

---

## The model-output boundary

`TriageAgent` is the only place untrusted text becomes an analytical record.
Everything below it is deterministic and auditable, and that is worth much less
than it looks if the signals feeding it can be manufactured by a malformed
completion.

**What the boundary used to accept.** Decisions were bound to posts by URL with
a *positional fallback*: an unrecognised URL was silently rekeyed to
`batch[index]`, so a response that omitted one item mid-array and rewrote the
rest misattributed every subsequent judgement — one post's rationale, city,
facility, salience and imminence landing on a different post. A duplicate URL
discarded the first judgement and manufactured a spurious "undecided" record for
the post whose slot was taken. The coercion helpers were looser than they
looked:

| Input | `_clamp` (salience) | `_number` (imminence) |
|---|---|---|
| `NaN` | **1.0** | `None` |
| `Infinity` | **1.0** | `inf` |
| `5` (out of range) | `1.0` | `5.0` |
| `"0.9"` | `0.9` | `0.9` |

`_clamp(NaN) == 1.0` is **maximum salience**, because `min(1.0, nan)` returns
`1.0`, and it was reachable: `json.loads` accepts bare `NaN` and `Infinity`
tokens. A model emitting `NaN` produced the single highest-salience signal in
the iteration. Separately, `relevant` was only truth-tested, so the string
`"false"` was accepted as true; and `cities` was iterated without a type check,
so a bare string `"Phoenix"` iterated *characters* and proposed seven cities.

**What replaced it.** Every batch item carries an opaque per-call `item_id`
derived from `(iteration_id, raw_id, url)` — unrelated to position, not the URL
itself, deterministic for tests. Exactly one output per known id is required;
unknown and duplicate ids are rejected. **The positional fallback is deleted.**
Each item is validated against a strict versioned schema before any branching or
persistence — Pydantic in strict mode with `allow_inf_nan=False` and
`extra="forbid"` — and `json.loads` is replaced by a decoder that refuses bare
`NaN` and `Infinity` at the door, for every LLM agent. Invalid output is
preserved as a typed outcome and can never materialise a signal or a
city-admission candidate.

**Five durable decision states.** `ACCEPTED`, `REJECTED`, `UNDECIDED`,
`INVALID_OUTPUT`, `MODEL_ERROR`. Before this, a missing decision, a model
exception and a non-list response were all persisted as `relevant=0` with the
identical rationale string — byte-identical rows for wholly different facts. The
split that matters is between a **conclusion** and a **non-answer**: `REJECTED`
is an analytical result, and the last three are coverage gaps.

**Triage non-coverage feeds `data_completeness`.** This was the more serious
half. Triage failures reached nothing: if the model was down for an entire
iteration, every post was recorded undecided, zero signals were written,
completeness stayed **1.0**, and the band was not capped — and because
correlation skips a city with no signals and no unreliable sources, that city
got no correlation row at all, identical to a genuinely quiet city. The system
reported full coverage and total quiet on an iteration where the judgement layer
never ran. Unjudged posts now make SOCIAL a coverage gap for **every city in the
iteration**, because an unjudged post has no city — the model is what would have
told us — so the honest attribution is all of them.

**Output truncation is named, not guessed at.** A response cut off at the token
ceiling used to surface as "invalid JSON after 3 attempts", which sends an
operator hunting a model fault instead of a token budget. It is now a distinct
`TruncatedResponse` naming the knob to turn, and it is **not retried** — the same
prompt at the same ceiling truncates again, and each retry appends more context.

---

## Evidence identity

Two independence gates depend on knowing who published something, and both
counted raw `source_domain` strings, so `www.apnews.com` and `apnews.com` were
two independent publishers. For news the fallback key was `source_name`, so a
display name — `"associated press"` — was stored in the domain column and
counted as a domain.

Hostname normalisation alone is not enough: two hosts carrying the same wire
story are not two claims, and triage dedups by exact URL, so syndicated copies
at different URLs counted as independent corroboration.

`services/provenance.py` adds a versioned canonical publisher key over
registrable domains plus an explicit alias table, and a conservative
claim-cluster key from the canonical URL plus a content fingerprint. Admission
and corroboration are expressed in terms of independent **publishers** and
independent **claims**. The raw fields are unchanged for audit and rights
review.

**Unknown provenance is unknown, never automatically independent.**

`services/facility.py` applies the same ladder to facility matching. The
predecessor did bidirectional substring matching and returned the first row in
insertion order, so `"Exhibition Center"` attached to whichever of
`North Exhibition Center` / `South Exhibition Center` the operator happened
to register first. Uniqueness *and* a minimum specificity are now required, and a
short or generic candidate returns no location at all. Results do not depend on
insertion order.

---

## Candidate versus signal

The corroboration gate applies **only to admitting a new city** — city
admission returns early for a city that already exists, before every gate. So a
city admitted once was thereafter a free target: one accepted post produced a
signal, which tipped the full paid follow-on set, with no corroboration ever
again. With `min_salience: 0.0`, a post with a *missing* salience became a
signal, and an undated signal booked paid collection and was then excluded from
correlation by the window check — money spent on evidence that could not score.

`services/sensitivity.py` introduces **CANDIDATE** (recorded, reviewable,
neither scoring nor spending) as distinct from **CONFIRMED SIGNAL** (scores, and
may tip), with separately calibrated floors for signal creation and for paid
tipping. A valid normalised timestamp inside an approved window is required
before any paid tip; `tip_min_salience` is deliberately higher than
`confirm_min_salience`, because a weak signal that scores gives a reader a
LOW alert they can dismiss while a weak signal that *tips* spends credits that
bill per record and cannot be refunded.

**No document may describe the corroboration gate as protecting operational
signals. It protects city admission only.**

**The floors remain interim.** They were set from one adversarial matrix run
against one model, which is a measurement rather than a calibration.

---

## Surviving a crash

The database is file-backed *specifically* so an interrupted run survives. What
made that reachable was noticing that the orchestrator could already resume an
iteration and **nothing ever called it** — and that an interrupted run was
byte-for-byte identical in the database to one still executing: `outcome IS
NULL`, `finished_at IS NULL`, one `agent_runs` row `RUNNING`, some queue rows
`IN_PROGRESS`. There was no pid, host, heartbeat or epoch anywhere, so nothing
could tell them apart, and a client watching those fields never terminated.

**Interruption is detected structurally.** Each process writes one
`process_epochs` row and stamps `iterations.owner_epoch_id` on the runs it
starts. Interruption is then *owned by an epoch that is not the current one and
not finished* — no clock in the predicate and no threshold to tune.

A heartbeat was rejected. Legitimate silences here are minutes long — the
lodging search was measured at 125 seconds and polls to 420, and FR24 is paced
at one request every six seconds — so any threshold that cleared those would be
too coarse to catch a real crash promptly, and a false positive costs a
duplicate collection pass in real money. A marker file was rejected because its
lifetime is not the database's, so a stale file beside a different database gives
a confident wrong answer. The epoch row is also the only option whose
multi-process extension is *additive*, and it makes a crash **simulable
in-process** — open a second epoch against the same database — so the recovery
suite needs no subprocess, no signals and no clock control.

**Startup reconciles and stops.** Resuming automatically would let a crash loop
re-buy records on every restart, and the choice between re-collecting and
counting the loss belongs to an operator. The reconcile marks and reports; it
never resumes and never abandons.

**Abandon still correlates and alerts.** An iteration that crashed before
`CORRELATING` and is merely closed produces no alert at all for a city whose
evidence may be nearly complete — a real cluster reading as silence, which is
this system's worst available failure. Stranded queries become `INTERRUPTED`, a
status inside `UNRELIABLE_QUERY_STATUSES`, so they lower `data_completeness`,
cap the band and reach the caveat with no change to the scoring code.

**Resume does not re-buy banked work.** Collection stores `raw_results` per
vendor call but marks the query complete only after its handler returns, so a
crash can leave money spent and the payload on disk with the query still
claimed. The reconcile settles those by a declared three-way rule: a payload
with a signal completes the query; a payload *without* one is marked
`INTERRUPTED` rather than complete, because a completed query with no signal
reads as "we looked and found nothing"; no payload leaves the query claimed for
the operator's decision.

**A new iteration is refused while one is UNFINISHED**, and the predicate is
`finished_at IS NULL` — not "interrupted". Not tidiness either: the cooldown
guard is keyed on the query hash across *all* iterations, so an outstanding run's
recent executions would silently suppress the new run's queries and it would
under-collect with no visible cause.

The narrower predicate was a real defect, found by driving the API by hand.
`interrupted_at` is stamped in exactly one place — the reconcile, on detecting a
crashed epoch — so a manual walk stepped partway and left, or a cancellation
recorded against an iteration not on a worker, left it NULL. The API reported
that iteration as `PENDING` and the next trigger was accepted. The word
INTERRUPTED named two different things: the iteration lifecycle state, which
gated, and the stage and query statuses of the same name, which did not — and the
second is what an operator sees.

Both kinds are closed the same two ways, so the refusal names which kind it is
and carries both URLs; one status code with two remedies should not leave the
operator guessing. Recovery was widened in the same change — `resume` and
`abandon` had required a crash stamp, so blocking a merely-open iteration without
that would have left a session with no exit, and a refusal with no remedy is
worse than the hole it closes. `discard-last-stage` deliberately sets
`finished_at` back to NULL, so an iteration reopened for debugging blocks a new
run exactly as it should.

**Shutdown records, does not close, and hard-exits.** A graceful stop drains,
records `CLEAN`, and closes everything. A stop that times out with work still in
flight records `TIMEOUT` with the stranded ids, closes **nothing**, and exits 75
(`EX_TEMPFAIL`) so a supervisor can distinguish it. An unclosed connection in an
exiting process costs a file descriptor for microseconds and WAL is crash-safe
for committed writes; closing it under a live worker is a segfault that was
observed. The hard exit is needed because merely returning lets interpreter
teardown reach the same segfault by GC. Iterations are deliberately *not* marked
interrupted at shutdown — the worker may still finish — so marking happens in
exactly one place, the next startup, by which time the row says truthfully
whether it finished.

A crashed epoch is closed as `UNKNOWN` with `ended_at` left **NULL** on purpose:
we do not know when it died, and a made-up timestamp would later read as fact.
Anything keying on "has this epoch ended" must therefore test `shutdown_kind`,
not `ended_at`.

---

## The per-session lock

One session runs one iteration at a time. That was enforced by a
`threading.Lock`, which is only true inside one interpreter — which is why
`serve` pinned uvicorn to a single worker and called it a correctness
requirement. A second worker would bring its own lock, and two iterations of one
session could run at once, spending twice and racing each other's queue rows.

The claim now lives in `sessions.running_iteration_id` and is taken by a
conditional `UPDATE ... WHERE running_iteration_id IS NULL`, so acquisition is
atomic inside SQLite rather than a read-then-write race.

The hard part of moving a lock into storage is that it outlives its holder, so a
crash would wedge the session permanently and the only recovery would be editing
the database by hand. The startup reconcile frees slots whose epoch has been
closed and **reports which**, since a freed lock means an iteration stopped
without releasing it. Release is conditional on still owning the slot, so a late
release from a reclaimed iteration cannot clear it out from under the current
holder.

`workers=1` remains the default for a different and smaller reason: sibling
workers appear in each other's startup reconcile as live epochs they must refuse
to touch, so crash recovery cannot run while they are up.

---

## Contract hardening

**Idempotent iteration triggers.** A client that POSTs an iteration and loses
the response has no safe move: retrying may buy a second full collection pass,
and not retrying may mean no run at all. An `Idempotency-Key` makes the retry
safe — the second POST returns the first one's response verbatim and starts
nothing, flagged `Idempotent-Replay: true`. The same key with a *different* body
is refused 422 rather than answered with the old response, which would let the
caller believe it started a run with its new parameters. Keys expire, so the
table cannot grow without bound.

**`Retry-After` on retryable refusals, and deliberately absent otherwise.** A
busy-session 409 carries it, because an iteration runs for minutes and a client
told only "409" either gives up or hammers. The unrecovered-interruption 409
carries none: waiting will never clear it, and telling a client to retry buys it
a loop that can only fail.

**Cancellation is cooperative.** The request is recorded and honoured at the next
stage boundary; the iteration then runs `CORRELATING` and `ALERTING` and closes
`PARTIAL`. A hard stop would spend the collection budget and discard the
evidence it bought. Skipped stages are recorded, and the uncollected work flows
into `data_completeness`, so cancelling cannot launder a partial run into full
confidence.

**A human review state before escalation.** `UNREVIEWED` / `RELEASED` /
`WITHHELD` governs **distribution only** — score, band and evidence are
untouched, because an alert withheld for being operationally unhelpful must
remain in the record at its computed confidence, or the audit trail becomes a
record of what was published rather than of what was found. The unfiltered
listing returns everything, because an operator cannot review what the API
hides.

**A capability surface.** `GET /v1/capabilities` answers per jurisdiction before
a session exists, because "no alerts for this county" and "this county was never
collectable" are otherwise identical on the wire, and the second silently reads
as reassurance.

**The evidence surface is the normalised record, not the vendor payload.** Raw
payloads and query parameters are withheld by default and *described* — provider,
timestamps, retention deadline, content hash — rather than omitted, so a reader
who cannot see the payload still learns it exists and how to demand it. What may
be redistributed from a provider response is a rights question per provider, not
something the endpoint should assume; the parameters carry the search lexicon and
facility coordinates.

---

## Classification receipts

A `receipts` row now records one model **call** — provider, model requested and
model served, response id, fingerprint, tokens, attempts, prompt version and
hash, output-schema / rules / normaliser versions, code revision, package
version, analytical-config hash, and a hash of the built payload — and every
decision or alert that call produced references it. One row per call, not
fourteen columns per decision: a batch of ten posts shares one call, and
duplicated provenance invites disagreement.

This implies:

- **The hash is the guarantee; the version label is a note.** An edit to a
  prompt without a version bump still moves `prompt_hash`, so the two wordings
  stay separable. A record whose integrity depends on a human remembering is not
  a record.
- **The config hash covers analysis, not deployment.** Database path and API
  port are excluded, so two deployments reasoning identically compare equal while
  a moved threshold does not.

`input_hash` is taken from the **built** payload, so the truncation window is
inside it. A receipt is written for a *failed* call too: a `MODEL_ERROR` row is a
coverage gap, and which prompt version and configuration failed is exactly what a
reader of that row needs. An alert whose summary came from the deterministic
fallback gets a null receipt — no model call produced that sentence.

The evidence endpoint exposes the prompt **hash**, never the prompt text.

A live call populates `model_served`, `response_id` and token counts — but 
`model_served` returns the model *alias*, not an immutablesnapshot, and
`system_fingerprint` is absent. So the receipt records what is
offered and **cannot by itself detect a silently repointed alias**. Absent fields
are stored NULL rather than defaulted, so the record does not overclaim.

---

## Provider governance

`services/governance.py` holds a per-provider record — billing unit and what a
unit means, retention ceiling and its basis, accepted identifiers, provenance and
intermediary chain, failure modes, rate limits, fixtures, and a per-field
never-retain list. It is code rather than a document because a document drifts
from the rules it describes within a phase: `retention_days` is the authority
`retention.py` defers to, and `strip_for_storage` runs inside the write path.

**Two kinds of claim, never mixed.** MEASURED facts were observed against the
live API and can be observed again. ASSERTED facts come from vendor terms and
have not been independently verified.

**`rights_verified` is False on every provider, and a test keeps it that way.**
No API call can establish a downstream-use right — a 200 means the vendor served
the bytes. All four providers are intermediaries, so the content belongs to a
platform or publisher whose terms bind us whatever the aggregator's say. **Vendor
intermediation is not proof of downstream rights, and the development ceiling is
not spend authorisation.**

The largest open question is the social aggregator: alert evidence returns post
snippets because *the snippet is the evidence* — an alert its reader cannot check
is not an alert — and no term has been produced that grants redistribution.
`GET /v1/capabilities` returns these questions rather than burying them.

A contractual retention ceiling cannot be raised by configuration; a config file
must not be able to buy a licence term. Configuration may only shorten a window.

---

## The Phase 6 code review

A full code review after Phase 6 opened six issues against the model boundary
and the evidence layer. All six were addressed in Phase 7, with two items
carried into Phase 8 where they proved larger than first scoped. The review's
framing was correct and worth restating: **`TriageAgent` is the only place
untrusted text becomes an analytical record, and it coerced where it should have
validated.** It was sequenced before the API contract work, because an API frozen
over ambiguous triage states would have fossilised the very distinctions the
review asked for.

| # | Review item | How it was addressed |
|---|---|---|
| 1 | **Positional fallback and loose coercion at the model boundary.** URL binding with an index fallback misattributed judgements; `_clamp(NaN)` returned maximum salience; `"false"` was truthy; a bare string in `cities` iterated characters. | Opaque per-call `item_id` binding, positional fallback **deleted**, strict Pydantic validation with `allow_inf_nan=False` and `extra="forbid"`, and a JSON decoder that refuses bare `NaN`/`Infinity` for every LLM agent. `agents/triage_schema.py` holds the versioned contract. See [The model-output boundary](#the-model-output-boundary). |
| 2 | **Indistinguishable failure states, and triage failures invisible to coverage.** A missing decision, a model exception and a non-list response all wrote `relevant=0` with one rationale string; none of them lowered `data_completeness`, so a dead judgement layer reported full coverage and total quiet. | Five durable states on every decision; per-batch and per-iteration counts; triage non-coverage became a SOCIAL coverage gap for every city in the iteration, capping the band and reaching the caveat. |
| 3 | **Publisher identity and claim independence counted raw strings.** `www.apnews.com` and `apnews.com` were two publishers; a display name was stored in the domain column; syndicated copies at different URLs counted as independent. | `services/provenance.py` — versioned canonical publisher key over registrable domains plus an explicit alias table, and a conservative claim-cluster key. Breadth is the lower of distinct publishers and distinct claims. Unknown provenance is never automatically independent. |
| 4 | **Facility matching was order-dependent.** Bidirectional substring matching returned the first row in insertion order, so a generic candidate attached to whichever facility was registered first. | `services/facility.py` on the same resolution ladder: exact, then alias, then a contained match requiring uniqueness *and* minimum specificity; otherwise no location. Match method and rules version are recorded. |
| 5 | **Sensitivity was an accepted risk, not a designed policy.** The corroboration gate protected city admission only, so an established city was a free target; a missing salience became a signal; an undated signal booked paid collection it could never score. | `services/sensitivity.py` — CANDIDATE versus CONFIRMED, separate floors for signal creation and for paid tipping, and a timestamp requirement before any spend. Floors set from the adversarial matrix rather than intuition, and still labelled interim. |
| 6 | **No record of how a judgement was reached.** The model column held a config string; the prompt was an unversioned constant; the retry rewrote the prompt without recording which variant was accepted. | Carried into Phase 8 as classification receipts: one `receipts` row per model call, referenced by every decision and alert it produced, with the prompt hash as the guarantee. See [Classification receipts](#classification-receipts). |

The review also asked for an adversarial measurement rather than an assertion,
which became `tests/test_adversarial.py`: prompt injection and fabricated
entities; quotation, explicit denial, sarcasm, hypotheticals; historical
retrospectives, stale reposts, future speculation; missing, malformed,
timezone-less and future timestamps; truncation where a trailing retraction falls
outside the input window; one weak source, one credible named source, two
publisher aliases, one syndicated claim on two hosts, two genuinely independent
claims; seeded and unseeded city variants. Outcomes are asserted **separately**
for candidate, signal, city admission, paid tip and alert.

It runs offline against a fake client to assert the *machinery* — that whatever
the model says, a malformed or injected answer cannot create a signal, an undated
post cannot buy collection, and syndication cannot satisfy an independence gate.
With `--live-model` it measures the *model's judgement* against the same cases
and reports precision and recall rather than asserting a bar, because asserting a
specific accuracy would make the suite fail on an unrelated model change, which
is how a measurement gets deleted.

**What the live matrix measured** (gemini-3.5-flash, 16 cases): precision and
recall of **1.00** on the eleven cases with asserted ground truth. Both injection
attempts rejected; denial, quotation, sarcasm and hypothetical all rejected;
sixteen decisions for sixteen posts with nothing unjudged.

Pass/fail columns do not capture:

- **The model accepted a fabricated city** at salience 0.85 — and no signal was
  written, because the admission rule refused it. The model was fooled and the
  rule was not, which is the layered defence working as designed.
- **Undated and future-dated posts were accepted** and became CANDIDATE rows:
  visible and reviewable, neither scoring nor spending.
- **Truncation was a genuine miss.** A report whose retraction fell beyond the
  body window was accepted at 0.90 and CONFIRMED, because the model never saw the
  correction. Fixed by the head+tail window; re-measured, the same case is now
  rejected.

### The re-review of issue 8, and what it reopened

A later bounded pass against `b2ccf34` — the head immediately before the
engine/mission split — reported six gaps and held issue 8 open. Re-checked
after the split and the contract hardening, **all six still reproduced**: the
refactor moved the mission out of the engine and did not touch any of these
seams. Each was reproduced first as a failing test, then closed.

| Finding | What it cost | How it was closed |
|---|---|---|
| **A partial triage resume lost the rest of a payload.** `untriaged_raw_results` excluded a payload as soon as ANY decision referenced it, and one response carries many posts. | A crash after one persisted batch turned every remaining post in that response into apparent absence — no decision, no skip, no coverage gap — while the API described triage as re-entrant. | Resume is per POST. Everything collected is rescanned and filtered per URL against `triaged_urls`; `recorded_skips` keeps the rescan from reporting the same refusal twice. |
| **Syndicated copies at two real URLs satisfied the independence gate.** `claim_of` preferred the canonical URL, and triage refuses a post without one — so the syndication clause was unreachable for exactly the traffic it was written for. | Republication breadth read as independent reporting: two publishers running one wire paragraph, enough with `expand_cities` to admit a city. | Claim identity is content-first above a 12-word specificity floor, URL below it. `provenance/2`. |
| **A split batch attributed every decision to the last sub-call's receipt.** | Two of four decisions referenced a receipt whose `input_hash` and `batch_key` cover a request that did not contain them — the evidence API attributing a judgement to a call that never saw it. | `receipt_of` is per requested id and is merged per sub-call. |
| **A retried call was reported byte-exact.** `_call_llm_json` rewrites the user message between attempts; every other field on the receipt describes the first variant. | A reviewer could be told the accepted classification prompt had been reproduced exactly when it had not. | Schema 14 records `prompt_user_hash` — the request that was ACCEPTED — so the claim is checked rather than assumed. Receipts predating the column are refused when `attempts > 1`, in the exit code as well as the prose. |
| **Typed triage outcomes existed in SQLite and not on the API.** | A server-to-server client could not tell a REJECTED item from one collected, paid for and never judged, except by parsing free-text degradations. | `IterationOut.triage_states`, on the operational spec and in the captured poll exchange. |
| **A stale first copy suppressed a fresh duplicate.** A URL was marked seen before the freshness cut ran. | Provider ordering decided whether eligible evidence reached the model. | Every copy is collected, the representative is chosen deterministically — freshest, dated over undated, then first seen — and only then do the pre-model gates run. |

Nineteen regression tests pin these. The two that were closed on the earlier
pass and re-opened by this one — typed outcomes and claim independence — were
both closed correctly for the case they were tested against and wrong for the
production shape, which is worth stating plainly: the tests asserted wire
stories with no URL, and no real post reaches triage without one.

---

## Changes based on live runs

Live runs found defects no offline suite could, and they are the reason several
designs above exist. 

**Collected evidence was overwhelmingly unusable.** The median social post was
206 days old and 1% fell inside the correlation window; the entire model budget
was being spent judging content that could not correlate. Hence the recency cut.

**A per-city cap silently refused an entire actor track.** The cap was sized for
one city and the real fan-out is two tracks × four lexicon groups × three
platforms. The refusal was recorded but the track simply never collected.

**Budget starvation is order-dependent and was silent.** Across seven metros the
spend envelope came out at 73 against 168 seeded queries; three cities collected
in full, one collected a single query, three collected nothing — and which three
followed queue order, not anything analytical. Every city still received a
correlation row. Seeding now warns at the moment the shortfall becomes
inevitable, naming the envelope, the fan-out and the cities at risk. Budget
refusals also now name the city they were refused *for*, since a NULL city is
treated as applying to every city and three fully-collected cities were
inheriting the starved cities' gap.

**A stage that never runs is a coverage gap with no other trace.** A cancelled
iteration collected twelve social queries, skipped `TRIAGING` and `TIPPING`, and
wrote **no correlation at all** — no gap, no capped band. All three gap detectors
missed it, each correct by its own terms: the queries succeeded, the skipped
stage wrote no decisions to count, and it enqueued nothing to refuse. Fixed with
`iterations.skipped_stages_json` and a stage→source-family map.

**A vendor feed can die while reporting success.** The rental-car provider began
returning `success: true` with a total of zero for every airport and window. The
pairing logic emits rows only for classes present in both windows, so an all-zero
feed produced zero rows, zero signals, and a query still marked `COMPLETE` — full
coverage reported on nothing. A *baseline* window with zero total availability
establishes no normal level and gives no denominator, so it now raises rather than
recording a zero drop.

**Price points were being counted as inventory.** The same car is listed once per
rate plan — 510 rows for 348 distinct vendor ids, differing only in price. One van
at three prices read as three available vans, and trimming to a single price read
as a 66% availability collapse: scarcity manufactured from a pricing change, on
the one family whose whole job is measuring scarcity. Offers are now
de-duplicated on the vendor's own id.

**A unit conversion was wrong for three phases.** The counter distance field is
in **miles**, verified against the counters' own coordinates, and was being stored
in a column named `distance_km` and compared against a 15 **km** spatial gate — so
counters read as nearer than they are and the spatial penalty was under-applied.

**An over-tight output bound manufactured the gap it described.** Two real
multi-city roundups named 15 and 16 cities and were rejected whole as
`INVALID_OUTPUT`, discarding the `relevant` verdict and rationale along with the
list, and recording non-coverage that lowered `data_completeness`.

**A discarded stage's degradation note outlived its rows.** Found by reading a
real database. TRIAGING truncated at a low token ceiling and recorded "10 of 20
post(s) were not judged"; the operator discarded back to TRIAGING, raised the
ceiling and re-ran, and all twenty were judged. The note survived — and `_finish`
reads degradations to decide PARTIAL, so the iteration stayed degraded by a gap
that no longer existed, with nothing in the row to say so.

The rows a stage wrote and the notes it wrote about what it could *not* write are
one record; deleting the first while keeping the second is a lie by omission. So
each entry now carries the **source** that wrote it — a stage name, or `recovery`
for an operator action, or `collection-gaps` for the derived summary — and a
rollback retracts what that stage said about itself. A recovery note is not a
stage's and survives. The derived gap summary is *replaced* rather than appended,
so a resume that closes some gaps cannot leave the older and now wrong summary
standing beside the new one.

Two latent bugs went with it. `degradations_json` had **four** independent
read-modify-writers, and `finish_iteration` *overwrote* the column — so the three
failure paths that pass no notes silently erased everything the agents had
recorded. There is now one writer, in `SurgeDB`, and closing an iteration does
not touch the column at all.

**Every alert in that database was the deterministic fallback.** All five carried
`model/fallback` and a null receipt: `AlertAgent` passed a hardcoded 400-token
ceiling and the model overran it every time, so no alert prose anyone read was
ever the model's. The ceiling is now `alerting.max_tokens`, and its default
is 4096 for a summary of about forty words — because **on a model that reasons
before answering, the ceiling has to cover the reasoning, not the answer.**
Measured with the tightened prompt: one summary came back at 44 output tokens and
another still overran 1200. Overrunning loses the summary entirely rather than
shortening it, since the JSON object is the last thing emitted.

Raising the number was not the whole fix. The prompt now states a word target and
requires the JSON object as the first token, with no preamble or reasoning ahead
of it. And the truncation error itself was misdirecting: it said "lower
`triage.batch_size` or raise `llm.max_tokens`" on *every* call, including the
alert call where neither knob exists. Each caller now names the setting that
actually set its ceiling.

**Three new endpoints did not inherit the hardening.** Auditing the 8.2
guarantees against the endpoints added afterwards, rather than assuming a new
route arrives hardened, found each of them broken in a different way.

*The re-triage endpoint had no idempotency*, and it needs it more than the
trigger does: the parent's `MODEL_ERROR` rows are deliberately never edited, so
a successful retry leaves the candidate set unchanged and a client that lost the
response would create another child and spend again — indefinitely, not once.

*The correlation listing returned 500 on a band of `NONE`* — the most
sub-threshold case there is, and the one the route was built to expose. The
response model reused the **alert's** band type, where NONE is impossible by
construction. A second model now carries the wider vocabulary and the alert
contract keeps the narrow one; widening the shared type would have removed a
guarantee that holds for free. Every test missed it because they force
`BELOW_FLOOR` by raising the floor, which leaves a real band — the fixture
excluded the failing case by construction.

*Cancellation did not reach a retry child.* A retry issues paid tipped
collection, so it is exactly as cancellable as an ordinary run, and a contract
holding on one path but not its sibling is worse than one holding on neither.

**An abandoned iteration closed `COMPLETE`.** Twelve queries marked
`INTERRUPTED`, already counted as a SOCIAL coverage gap by the completeness
calculation — and the iteration's own outcome said the run finished cleanly.
Collection that never happened, reporting as a finished run, one layer above
where it was guarded. Two causes. The outcome rule hand-rolled its own list of
"statuses that produced no data" and omitted `INTERRUPTED`, while
`UNRELIABLE_QUERY_STATUSES` includes it and that enum's own comment warns that
omitting it makes an abandoned iteration report full coverage: two definitions of
one concept, and the second had drifted from the rule the first states. And
`abandon` recorded the operator's decision only in the log, which the outcome
rule never reads. The existing test had been passing for the wrong reason — its
fixture crashes the iteration first, so the reconcile's degradation note forced
`PARTIAL` before the gap count was ever consulted. The merely-open abandon path,
which the unfinished-iteration guard had just made reachable, had no such note.

**Designed behaviors confirmed live**, each the first time on real data: the
timestamp tip gate refused a years-old post and saved the paid follow-on set; the
free-endpoint exclusion kept a preflight call out of the ledger; FR24's
per-record billing matched its specification exactly (a flat credit for an empty
response, eight with a record); every live-position record was stored `AMBIGUOUS`
with only the history window resolving category; and social amplification was
neutralised — eight posts about one story from one platform counted as **one**
source while two posts from two publishers counted as two.

### Three decisions, recorded

Issue #7 left three questions open. All three were answered on 2026-08-18; two
were ratifications and one changed the alerting rule.

**The sensitivity floors are accepted as they stand, and they remain interim.**
Accepting a number is not measuring it, and the distinction stays on the record:
the strongest calibration attempt available produced a result that could not be
used, because the top-scoring metro was one labelled as having *concluded*
operations and its score traced to general-aviation airport density.

**A single report no longer produces any alert.** This one changed behaviour.
The prompt this scoring model was transcribed from granted LOW to a lone social
post, and the system did too. One report is a lead, not a warning, and an
instrument that escalates on one escalates on a rumour — the very
`RUMOUR_AMPLIFICATION` alternative the system now records against social-only
correlations.

LOW therefore requires two independent **reports**, from any family or
families — and the distinction between a report and a family is the whole of it.
Two outlets making two distinct claims is two reports from one family and
alerts. Eighteen distinct airframes over one airport is eighteen reports and
alerts. A lodging drop is one report however many listings it spans, and alone
it does not.

What none of them can do is manufacture breadth from repetition: social counts
the lower of distinct publishers and distinct claims, so one wire story in three
mastheads is one report; flights count distinct airframes, so the same aircraft
seen by both the live and the historical query is one.

Counting *families* instead was the first implementation of this decision, and
it was wrong in both directions — it refused a real correlation resting on
eighteen airframes as "a single report", while it would have accepted a lodging
drop plus a car drop as two while calling two independent news outlets one. The
owner caught it against live data.

The gate reads raw counts, not decayed ones. Whether two independent sources
reported something is a fact about the evidence; letting age erode it would
conflate a structural gate with the score, which decay already moves.

A refused correlation is still computed, scored and stored, with
`alert_decision: BAND_NONE` and a reason naming the failure. Refusing to alert
is not refusing to record.

---

## Closing the review issues

A second review, this time against the API contract and the provenance surface,
produced eleven issues. Four were verified as already satisfied. Six were fixed;
two remain, and neither is waiting on engineering.

The pattern from the previous round repeated once, and it is the one worth
naming again: **a control that reads as enforcement and is not.** A credential
redactor that was correct and never started. A tunables field that was validated,
stored, and read by nothing. Both looked like protection from every angle except
the one that mattered.

### A service that never started

`redact.install()` documented that it must run once at startup before any
connector is constructed, and no entry point called it. The exact-value layer —
the reliable one, which catches a key however it was embedded — protected nothing
outside the tests that called the installer by hand. Pattern matching still
caught recognised header and query-string shapes, so the failure was partial and
therefore invisible.

Installation now happens inside configuration loading. That is a side effect in a
function named `load_`, and it is deliberate: adding a call to each of eight
commands would have left a ninth to forget, and the configuration is the earliest
thing that knows which environment variables hold credentials. Ordering is the
whole fix, so it must not be a thing anyone chooses.

The regression drives the real startup path — a config file on disk, the command
line — rather than calling the installer. A test that called `install()` itself
would have passed throughout the entire period the defect existed, which is the
only useful thing to know about it. One of them monkeypatches the database
constructor to capture the registered-secret count **at the moment the database
is opened**, because installing eventually is not the same as installing first.

### Configuration a client asked for and did not get

`POST /v1/sessions` accepted a `tunables` object, stored it, and never read it.
Every stage ran on the process-wide configuration. The documentation mismatch was
not the problem: a client could request narrower criteria or tighter spending
controls, receive a successful session, and have paid collection run under
settings it did not choose — with each receipt stamped `config_hash` from a
configuration it never asked for. That last part is the serious one, because the
config hash is what makes a judgement reconstructible, and it was recording the
wrong answer.

Two changes, in this order.

**Refuse what cannot be applied.** An allowlist over the analytical and budget
sections, validated per key rather than per section, so a misspelled
`triage.max_post_age` is refused with a message naming `max_post_age_hours`. A
typo is the most likely way a client meets this, and silently doing nothing was
the whole defect. Server-owned sections are refused with a reason rather than a
bare no — credentials, provider endpoints, rate limits, retention ceilings, the
model and its parameters, and `dry_run`, which a client that could set it would
use to receive fixture data indistinguishable from collection it had paid for.

**Then apply it once.** The orchestrator builds the effective configuration at
every entry point and uses that one object for every stage, the budget guard,
retention and every receipt. It merges onto the server's configuration each time
rather than onto the previous session's result, so one session's overrides cannot
leak into the next run through the same object.

**Spending ceilings may only come down**, and the enforcement is in two places
for a reason. Validation at session creation tells a client immediately; the
clamp at merge time is the guarantee. An operator who lowers the server's cap
after a session was created must be obeyed, and validation alone would leave the
stored number in force.

### A contract that did not mention its own authentication

Runtime authentication was correct: every protected route failed closed on a
missing or invalid bearer token. The generated OpenAPI document said nothing
about it, so a client generated from the artifact omitted the header and failed
every operational request. The server was right and unusable.

The declaration is now derived from the enforcement — routes depend on a
dependency that takes its credential through FastAPI's bearer primitive — so it
cannot drift away from the behaviour the way a note beside the artifact would.

`/v1/healthz` is the one operation the framework cannot express, because it is
anonymous for liveness and authenticated for `?deep=true`. OpenAPI can say
"either"; FastAPI can only say "required" or say nothing, and both are wrong
here. "Required" would make a generated client send a token for a liveness probe;
silence would leave the deep check unreachable from generated code. The anonymity
of the cheap check is deliberate — an unauthenticated deep check would be a free
way to burn four vendors' rate limits — and it is now visible in the contract as
a property rather than as prose.

The staleness half had a mechanism worth removing rather than a symptom worth
fixing. A captured example stamped `code_revision`, which tracks git HEAD, so the
drift gate failed on **every commit** rather than on an API change. A gate that
cries wolf is one an operator learns to silence, and the next real drift goes
through with it. The field is excluded from captured examples now; an example is
a shape, not a snapshot of the machine that produced it.

### How directly a thing is known

Per-family provenance already existed: the geo resolution method, the publisher
resolution method, the facility match method, each provider's governance record.
None of it was **comparable**. An analyst reading a lodging row beside a flight
row could not tell how directly either was known, which is the first question to
ask of evidence that may cost money to act on.

One value now sits on every signal, in a vocabulary defined by what the
connectors can attest rather than by the general taxonomy the review proposed. A
field that guessed between "web scrape" and "third-party feed" because the code
cannot tell would be worse than no field.

| Value | What it says |
|---|---|
| `DIRECT` | From the party that generated the record. **Nothing here qualifies** — every provider is an intermediary over a platform or a publisher. The value exists so filtering for it returns the honest answer rather than requiring a reader to already know. |
| `INTERMEDIARY_LIVE` | The third party retrieved it for this request. |
| `INTERMEDIARY_CACHED` | The third party served a stored copy whose age it did not state, and billed it as a fresh call. Measured: the lodging price endpoint charges the same 30 credits either way, so the ledger is no guide to freshness and nothing else in the record would show it. |
| `UNRECORDED` | Collected before this existed. Not a claim about the row — the absence of one. Also the default, so a writer that forgets produces a visible absence rather than a plausible claim. |

It is held on the signal rather than only on the raw payload because retention
deletes the payload and nulls the link. A provenance field that vanished with the
payload would be missing exactly when the question is hardest to answer another
way.

A lodging signal is a *comparison* of two windows, so if either window came from
the vendor's store the pair is recorded as cached. Calling it live because one
half was would overstate freshness, and the conservative direction is the only
one that does not.

### What else would explain this

Nothing on an alert recorded that a lodging or car surge could be a convention, a
home game, a holiday weekend, a weather evacuation or a routine exercise. The
reader was shown a score and its working and left to supply the alternatives —
and the reader who most needs them is the one already persuaded.

One mitigation existed and is now stated rather than counted as a fix. Baselines
are weekday-aligned at +7 and +14 days precisely so ordinary weekly demand does
not read as a surge. That **suppresses** a confound in the arithmetic; it does
not **record** the hypothesis for a reader, and the two are different jobs — the
first protects the score, the second protects the judgement made from it.

Each correlation now carries a deterministic list, derived only from which
families contributed. A booking-only correlation admits ordinary-demand
explanations that a military-flight correlation does not, and that mapping is a
rule rather than an inference. Each entry has a stable code, the statement of the
competing explanation, and what in *this* correlation argues against it — empty
when nothing does, because an unanswered alternative serves a reader better than
a manufactured rebuttal.

The model is not asked to generate these. That would put a second uncontrolled
judgement on the alert surface, which is the one place this system has been
careful to keep the language model out of the number. For the same reason the
list sits on the evidence surface rather than on the alert: the alert carries one
sentence a model wrote and a number it never saw, and a second block of prose
there would compete with the summary for the same attention.

### An abbreviation the matcher would not read

`N. Exhibition Center` returned no match against a registered `North
Exhibition Center`. The conservative direction was right — no location beats a wrong one —
but an operator writing a name the ordinary way got silence.

The diagnosis was one rung lower than it looked. `north` is a generic token and
`n` is not, so the containment rung compared a single meaningless token against
an empty set and refused, correctly. Expanding the abbreviation makes it an exact
match *above* the rung that had the problem, so no uniqueness or specificity rule
had to move.

Abbreviations that are genuinely ambiguous in this domain are left out on
purpose — `comm` is commission, committee or community; `reg` is registrar or
regional — because an expansion that guesses wrong is worse than the refusal it
replaces. Expansion runs on both sides of every comparison, so a facility
registered *as* the abbreviated form still matches itself, and two registered
names that collapse to one key are refused as ambiguous rather than guessed
between: an operator data problem made visible.

### What remains, and why it is not code

**Temporal decay** is accepted as correct and deliberately not built. Evidence
inside the correlation window counts at full weight and evidence outside counts
not at all — a step function, not a decay, and a lodging surge 36 hours ago
should not weigh the same as one 3 hours ago.

It interacts with a decision already made. The window was widened from 48 to 168
hours because two corroborated reports scored nothing at five
and six days old: evidence collected, paid for, judged, and structurally unable
to reach a correlation. Adding decay changes what the score means for a third
time. The honest sequencing is to decide whether this instrument is a 48-hour
tactical warning or a week-long situational picture, and *then* choose the curve
that expresses it. A decay function chosen first would silently re-narrow the
window that was widened deliberately.

**Three owner decisions** are still unrecorded: whether the mission's stricter
relevance leg stays the default, whether the interim sensitivity floors are
accepted or must be measured, and whether one credible report may produce a LOW
alert. The first is a mission's to make; the engine's obligation is that the
choice is applied and recorded.

---

### Evidence that ages

Until 9.5 a signal inside the correlation window counted at full weight and one
outside counted at nothing. A step function, not a decay, and both of its edges
were wrong.

Live running showed each. An iteration over a city holding 125 signals
correlated **zero pairs**, because the newest was 74.2 hours old against a
72-hour window — two hours past the line, and evidence that had been collected,
paid for and judged counted for exactly nothing. And re-scoring that same
evidence at a 168-hour window, which is what the widening was for, produced
**0.776 and a HIGH band** from evidence three to eight days old: a
high-confidence warning of an imminent deployment, drawn from last week.

**The curve is a function of the window, and that is the design rather than a
convenience.** The window already states how far back evidence is relevant. A
second, independent decay parameter could contradict it, and a curve chosen on
its own would silently re-narrow a window that had been widened deliberately —
which is precisely what the widening from 48 to 168 hours existed to undo.

    weight(age) = decay_edge_weight ** (age / window_hours)

One setting therefore expresses both postures. The curve is self-similar under
window scaling, so in absolute time it is steep for a short window and shallow
for a long one:

| window | posture | half-life | 24-hour-old signal |
|---|---|---|---|
| 48 h | tactical warning of an imminent deployment | 14.4 h | 0.32 |
| 168 h | situational awareness of an established operation | 50.6 h | 0.72 |

A 48-hour window is a claim that something is about to happen, and a day-old
booking surge is weak evidence for that claim. A 168-hour window is a claim
about an operation already under way, and the same surge is most of the
picture. Both fall out of one expression, and an operator changes one number.

**There is no separate on/off flag.** An edge weight of 1.0 restores the old
step function exactly. A boolean beside a value that can disagree with it would
be one more control that reads as enforcement and is not — the pattern this
project has now found five times.

**Decay does not remove the cutoff.** The window is still what bounds the
query; decay shrinks the cliff at the boundary from 1.0 to the edge weight. The
74.2-hour case is fixed by choosing the wider window, and decay is what makes
choosing it safe.

#### Counts and measurements decay differently

Two rules, because the two quantities are different kinds of thing.

Flight and social quality are **counts of independent observations**, so the
count decays: three aircraft that landed four days ago are not three aircraft
now. Each airframe, publisher and claim counts at the weight of its *freshest*
sighting rather than the sum of its sightings — summing would let one outlet
running the same story eight times read as corroboration, the exact confound
`claim_key` exists to prevent.

Salience does **not** decay, and that distinction had to be found the hard way.
Social quality is breadth times peak salience, and the first implementation
decayed both — so social decayed as the *square* of the weight while every
other family decayed linearly. A signal at 0.12 of full weight contributed 1.4%
rather than 12%: the family that most often stands alone was penalised for age
about eight times harder than the rest. Salience is a property of what a post
says, and a four-day-old report is exactly as specific and as credible as it
was when written — only less current, and currency is what the breadth term
already carries. There is now an invariant test asserting that all four
families scale linearly with the same weight, which is the check that would
have caught it.

Lodging and car quality are **one ratio measured at a moment**, so the
measurement decays by its own age: half a measurement is not a smaller drop, it
is an older one. The weighting runs over the same denominator the ratio is
computed on, so in the ordinary case — one iteration, one collection moment —
every row carries the same weight, and it only does real work when the window
spans two iterations.

One consequence had to be handled explicitly. The single-source floor exists so
that one credible report is not scored as a third of a signal; left alone it
would also have restored a signal the curve had just discounted to a tenth. It
now scales down below one observation and meets the original curve exactly at
one.

#### What it costs to read

The score moves, and it should be possible to see why. The band rule trace now
names the curve — window, half-life, edge weight — so a reader comparing two
alerts scored under different windows can see that the older evidence was
weighted differently. Per-signal shares in the evidence drill-down are
attributed in proportion to age rather than evenly, so the freshest observation
sorts to the top; an even split had ranked a five-day-old post level with one
from this morning while the score already knew better.

**The edge weight is interim.** `0.1` is the default because it makes the
residual step at the window boundary 10% rather than 100% — a property, not a
measurement. What it does to real evidence is a spread, not a point, and the
whole spread is in `docs/config.md` so the choice is visible. Nothing here is
calibrated against ground truth, and it should not be described as though it
were.

---

### Two names, one operational unit

A session named one place and a source reported the same activity under the
containing administrative unit's name. Found live, and the failure was total:
the article was collected, judged relevant at salience 0.85, and refused with
`expand_cities=false; city not in user list` — because the containing unit's
name is not the string the session was created with. Collected, paid for,
judged, and dropped on a name; the iteration reported a quiet city.

Two independent faults sat behind it.

**The name would not resolve.** The reported name prefix-matched both the city
and the larger unit containing it, and the resolver refused to choose. That
refusal was correct under a rule that could not tell two names for one place
from two different places — so the rule now makes that distinction. A candidate
set naming one jurisdiction collapses to its longest member, which is always
the more specific name. Nothing else moved: a prefix matching four cities in
three states still resolves to nothing.

**Even resolved, it would not admit.** Two names are one jurisdiction for
admission only where the loaded mission says so, consulted after the exact
match fails and only against cities the session already named — so it can never
widen collection to a place nobody asked about.

**WHICH names are equivalent is the mission's judgement**, and the table lives
in the pack (`geography.yaml: equivalents`). What the engine owns is the rule,
and the rule is deliberately narrow: an equivalence states that two names mean
one unit, it is not transitive, and it never merges two units into one. That
narrowness is the safety property. The same live article also named a
*neighbouring* administrative unit — adjacent, but a separate jurisdiction with
its own administration — and it is still refused. Admitting it would manufacture
evidence rather than find it.

The match is written to the audit trail, because deciding that one place name
means another is an analytical decision and a reader seeing a signal for one
city would otherwise have no account of how a report about another produced it.
An exact match stays silent: a line for every ordinary match would bury the one
that needs reading.

---

### A family that was measured is not a family that is missing

The lodging family has two legs: availability, through Staying's calendar, and
price. The price leg exists **because** the calendar leg is unreliable — roughly
one listing in fifteen returns calendar data, so the availability measurement
regularly has too few paired listings to score.

Live in Houston, that happened: availability paired fewer than 3 listings of 40
and was skipped, while price paired 4 of 6 and produced signals. The old rule
mapped both to one family, scored lodging as a coverage gap, dropped
completeness to 0.75, and told the reader *"lodging unavailable this
iteration"* — about a family that had just been measured and reported no
pressure. Letting the unreliable leg condemn the reliable one inverted the whole
point of having two.

Two questions were being answered by one field, and they are now separate.
**What failed** is reported as `SOURCE_TYPE:endpoint` — `LODGING:/search`, not a
bare `LODGING` a reader would take as "no lodging data". **What is genuinely
unknown** is the set of families with nothing collected at all, and that is what
drives `data_completeness` and the caveat.

The safety property was the constraint on the design, not an afterthought. A
broken credential fails *every* endpoint in its family, so the family is still a
gap, still lowers completeness, and still caps the band. And capping stays keyed
on the detailed list rather than the family list: any lost endpoint still caps
HIGH. Completeness became more accurate; the top band did not become easier to
reach.

---

### Flights measured against normal

The flight family was the only signal scored as an absolute count. Lodging and
car are both ratios against a weekday-aligned comparison window; flights were a
saturating count of distinct airframes with a full scale of three. So three
business jets at a quiet regional field and three at a major general-aviation
hub scored identically, and the hub scored three every day of the year.

Measured: Atlanta returned 13, 13 and 14 distinct business-jet airframes on
three different days, every one pinned at maximum. A constant offset that says
nothing except that Atlanta has business-jet traffic — and the same defect the
failed calibration attempt found from the other side, where the top-scoring
metro's flight score traced to airport density rather than to surge activity.

Non-military flights are now scored as **excess over what the city normally
shows**, proportionally, so airports of different sizes are comparable. Against
Atlanta's own history the steady 17–19 background now scores 0.000 to 0.056
where it used to score 1.000.

**Military is deliberately excluded and stays an absolute count.** Its baseline
at a civilian field is approximately zero, one transport inbound is meaningful
at a count of one, and dividing by a near-zero normal would either destroy the
signal or explode it. Uncategorised records *are* baselined: an uncategorised
live-positions record is not military, it is exactly the background being
subtracted.

The samples cost nothing. Every flight-summary response the system already buys
is an observation of normal traffic, and the analytical record outlives the
payload's retention deadline — so the baseline is derived rather than collected.
An iteration that looked and found none of a category counts as an explicit
zero, because omitting it biases the median upward, which is the direction that
would hide a surge.

**The contamination filter starves the baseline it protects, and that had to be
handled rather than shipped.** Excluding iterations that alerted at MEDIUM or
above is right in principle — a sustained surge should not quietly become its
own normal — but on real data Atlanta's only two flight iterations both alerted,
so the filter excluded every sample it had. The rule is circular for exactly the
cities that need it: density inflates the score, and the inflated score
disqualifies the correction. The filter is therefore applied whenever it can be
afforded and relaxed when it cannot, with the relaxation recorded on the
correlation. A median already resists a sustained surge; the filter is a second
line of defence, not the only one.

Where a baseline cannot be formed at all, the absolute count stands and the
correlation records `UNBASELINED`. A cold start must never be indistinguishable
from a city whose normal genuinely is what was seen today.

---

### The same evidence, arriving again

The correlation window reads across iterations deliberately: a flight seen
thirty minutes before a run started is still live evidence, and scoping to the
current iteration would discard it purely because of when somebody pressed the
button. The cost is invisible repetition.

One Atlanta correlation linked 98 signals, none of them from its own iteration
— all from two runs six days earlier — and was the fourth alert in a row drawn
from a single day's collection. Its score fell across those four (0.405, 0.459,
0.459, 0.241) as decay did its work, but each arrived as a fresh alert with a
fresh summary describing flights that had landed days before. Nothing in the
alert said so.

Every correlation now records how many signals contributed, which iteration
last contributed one, whether this iteration did, and the age span of the
evidence. When this iteration contributed nothing, the alert caveat says so in
a deterministic sentence written beside the coverage disclosure and for the
same reason — the model must not be able to soften or drop it.

The choice was to record rather than suppress. A repeat alert is a correct
current assessment of evidence still inside the window; what the reader needs
is to know which kind they are looking at, not to have the second one hidden.

Two ages are kept apart on purpose. Whether the iteration contributed is a
question about *collection*; the hour span is about *observation*, since
`observed_at` is when the event happened rather than when it was fetched. A
post published six days ago and collected today is old evidence gathered in a
fresh run, and only the collection question gates the sentence.

---

### One spelling for a timestamp

`recent_signals_for_city` compares `observed_at` as a string. That is only
sound if every value has the same shape, and it did not: API Direct returns a
space between the date and the time, while the window threshold is written with
a `T`. A space is 0x20 and `T` is 0x54, so a space-separated stamp sorts
*before* any `T` stamp on the same date.

Live iteration 14 showed what that costs. Two Atlanta social signals, 158 hours
old against a 168-hour window, were dropped by the comparison — `in_window`,
which parses properly, admits them, but the SQL pre-filter never handed them
over. Both correlations scored with no social contribution at all and the alert
rested entirely on flight and car background. Across the database, twelve of
twenty-two social signals were stored that way and the window query admitted
none of the eight that belonged in it.

The failure is date-dependent, which is why it survived. The separator only
decides the comparison when the window boundary falls on the same calendar date
as the signal; on any other day the date digits settle it first and nothing
looks wrong.

Storage is now canonical: every string timestamp is parsed and re-emitted on
write, and rows written before the rule are repaired on open — a change of
spelling, never of instant, with anything unparseable left exactly as it
arrived. The invariant worth stating is the one whose absence allowed this:
**whatever the SQL filter admits must be what the scoring gate admits.** Two
predicates for one question, disagreeing silently, is how evidence disappears.

---

### What live running found this time

Six previous rounds of live running each found something the offline suite could
not. This one found three.

**Two more controls that read as enforcement and were not.** Checking that Phase
8's contract hardening survived Phase 9 turned up the same pattern twice more.
Request bodies silently ignored unknown fields, so `tunabels` returned a 201 and
a session running on the server's configuration — the very failure the tunables
work had just fixed, one level up, in the same request. And `Retry-After` was
sent on a busy-session 409 and never declared, so a generated client could not
tell that 409 from the one that never clears by waiting. Both are now enforced
and asserted.

**A correlation that scored nothing, correctly.** An iteration over a city with
125 stored signals correlated zero pairs, which looked like a defect and was not:
the configured window is 72 hours and the newest signal was 74.2 hours old. Two
hours past the line and evidence that was collected, paid for and judged counts
for exactly nothing. That is the step function in the wild, and it is the
clearest argument available for the temporal-decay work — which is why the
argument belongs to the owner decision above rather than to a curve chosen in
advance.

**The social sweep produced nothing to correlate, twice.** Across two live
cities, 250 posts reached the model and none were accepted. The paid families
were reachable only by driving the tip path directly. That is not a defect —
it is the ~2% yield already recorded as the first open question — but it is
worth stating that the end-to-end path from chatter to alert went unexercised
by real chatter on the day, and that the alert produced instead rests on flight
and rental-car movement alone.

---

## Testing strategy

`pytest`, no live network by default. HTTP mocked with `respx`. LLM calls stubbed
by injecting a fake client, so what is tested is what the system runs.

Invariants worth property-testing rather than example-testing, over randomised
workloads:

1. Queries per iteration never exceed the configured cap.
2. No two queue rows in one iteration share a dedup key.
3. No queue row exceeds the tip-depth cap.
4. Every correlation score is within 0..1.
5. **If any contributing query failed, no alert for that city/track is HIGH.**
   The single most important test in the suite — the property that stops a broken
   API key from reading as "nothing found".
6. Every signal traces to a raw result traces to a queue row: no analytical
   record without provenance.
7. The same property as (5) extended to interruption: after an abandon, no alert
   for an affected city/track is HIGH. A crash must not be able to launder
   incomplete collection into full confidence.
8. For any permutation, omission, duplication or rewriting of the identifiers in
   a model response, no decision is ever bound to a post other than the one it
   names.

Fixture provenance is recorded per file, because trust level differs: a real
captured response is evidence of what a vendor does, and a fixture synthesised
from a specification is evidence only of the parsing contract. Session-bearing
fields are retained in fixtures deliberately, so the storage-stripping tests are
meaningful rather than vacuous.

Two opt-in live suites, excluded by default: `-m live` hits real vendors and
costs money; `-m live_model` calls the configured model and costs tokens but no
vendor credit. Live captures are written back to `tests/fixtures/live/` scrubbed,
so a cost is paid once.

Schema and enum drift fails the suite: every frozenset must match the CHECK
clause it mirrors.

---

## Operational limits

- **Rate limits, not quotas, bind first on a paid plan.** A per-provider
  token-bucket limiter delays rather than refuses, because a 429 must be
  prevented rather than retried into. One provider's measured burst ceiling was
  exactly one request.
- **One provider's plans are hard-capped with zero overage at every tier**, so
  exhaustion cuts access off rather than billing. Exhaustion must degrade
  gracefully.
- **Per-iteration spend is the primary runaway guard.** The envelope is the lower
  of a hard per-provider cap and a fair share of the month's remainder. Past a
  configured fraction of a month's allowance, only high-priority queries may
  spend, reserving the tail for genuinely imminent tips over routine refreshes.
- **Exhaustion mid-iteration does not fail the iteration.** The query is marked
  skipped with a specific reason, correlation treats the missing source exactly
  as it treats a connector failure, and the next iteration re-enqueues the
  skipped fingerprints with raised priority.
- Alerts name specific facilities. Binding the API to a non-loopback address
  requires TLS and a real identity layer in front of it.

---

## Open questions

- **The social sweep's yield is about 2%.** Across live runs, a few hundred posts
  returned and only a handful fell inside a week. The recency cut stops the
  system paying to judge the rest, but it does not make the sweep productive.
  Whether the lexicon, the platform mix or the vendor must change is the question
  that most affects whether this system can warn at all.
- **The lodging signal may not be viable at current coverage.** One live run
  paired 1 listing in 40 against a floor of 3, for roughly 100 credits. The
  refusal is correct; the economics are the problem.
- **The flight family measures general-aviation airport density**, which is a
  property of airport mix rather than of surge activity. Normalising against a
  per-airport baseline is the obvious approach and changes what the flight score
  means.
- **The sensitivity floors are interim.** One run against one model is a
  measurement, not a calibration. Repeat across the models actually under
  consideration before describing any of them as calibrated.
- **A silently repointed model alias is undetectable** from what the provider
  returns. Closing that needs a capability probe or a vendor offering immutable
  snapshot ids.
- **The correlation window and the recency cut define what warning really means**,
  defining how recent evidence must be to contribute to the alert score, and how
  recent it must be to tip further collection (and spend).
  Scoring on older evidence and spending on it are different decisions. The
  window now also sets the decay curve, so it is one decision rather than two —
  but `decay_edge_weight` itself is unmeasured, and moving it from 0.1 to 0.5
  moves real evidence from LOW to MEDIUM.
- **A second signal for the same post is refused by the dedup index** and the
  refusal leaves no record. Defensible behavior — one post is one piece of
  evidence — but an unacceptable silence by principle 3.
- **A re-triage run past the cooldown re-buys what its parent already bought.**
  Inside the window the cooldown correctly suppresses duplicate tips; past it the
  purchase goes ahead. That is waste rather than incorrectness, and it argues for
  keying a retry's cooldown check on the parent's executions rather than on
  wall-clock age.
- **The flight family is the only signal scored as an absolute count**, not as a
  ratio against a baseline. Three business jets at a busy general-aviation
  field score the same as three at a quiet one, every day of the year. Lodging
  and car are both weekday-aligned comparisons; this is not, and the failed
  calibration attempt found the same defect from the other side — the
  top-scoring metro's flight score was airport density.
