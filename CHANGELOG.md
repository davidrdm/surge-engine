# Changelog

All notable changes to Surge I&W are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) while the project is pre-1.0
(minor = features, patch = fixes).

Three version numbers exist on purpose, and only two move together:

- **Package version** (`surge_iw.__version__`) — the release. Stamped on every
  classification receipt as `package_version` and served as the API's
  `info.version`; the two are pinned equal by a test.
- **`SCHEMA_VERSION`** (`surge_iw/db/database.py`) — the database format.
  Deliberately separate: a database migrates when its shape changes, not when
  a release ships.
- **Per-mission versions** (`mission.yaml: version`, prompt version labels) —
  owned by each pack, stamped on receipts alongside the pack digest.

## [0.3.0] — 2026-08-26

### Added
- **`mission.yaml: collects`** — which of the engine's four data families a
  pack collects at all. A family absent from the list is never queried, never
  scored, never in the `data_completeness` denominator and never a coverage
  gap: nothing was attempted, so nothing failed. A social-only mission no
  longer has to buy flight, lodging and rental-car data in order to ignore it,
  or carry three vendor credentials to run a pack that will never call them.
  The declaration is the single statement of the fact — the loader refuses a
  weight row, a `flight_categories` block or a `hypotheses.yaml` entry left
  behind for a family the pack does not collect. Omitting the key collects all
  four, so every existing pack is unchanged. Switched-off families are named
  in the startup `describe()` and served as `mission.collects` on
  `GET /v1/capabilities`.
- **A third mission pack**, maintained under `missions/` and documented in its
  own README: a protective-intelligence mission warning a city that organised
  violence against a threatened minority population may be forming. It is the
  first shipped pack to use `collects` — social and news only — and the first
  to promote three stream families at once, so it exercises a banding shape
  the other two do not: seven watches over one feed, no family reachable by a
  paid connector, and a deliberate floor under which chatter alone cannot
  band however violent its language.

### Fixed
- `ambiguous_flight_weight` raised `ValueError` on a track that flies in no
  FR24 category — reachable by a legacy flight row inside the correlation
  window of a pack that collects no FLIGHT. Such a record is now worth 0.0 and
  stays visible in the evidence trail.
- The authoring guide's key-coverage pin matched a bare substring anywhere in
  the prose, so `collects` passed on the word "collects" in a sentence about
  what the engine does. It now requires the backticked name or the YAML key.

## [0.2.1] — 2026-08-26

Findings from the first live run of 0.2.0 (reference pack, two streams, an
operator calendar). The calendar, the streams, the per-stream receipts and the
byte-exact prompt reconstruction all behaved as designed; these are the three
defects the run exposed.

### Fixed
- **A post naming two cities produced one signal, not one per city.**
  `idx_sig_dedup` keyed on (iteration, type, stream, url) with no city, so the
  second city's evidence row was refused by the index — and WHICH city kept it
  was decided by the order the model happened to list them in. Measured live:
  a tour announcement naming both session cities became evidence for one of
  them, and the other lost a report. Schema v16 adds `COALESCE(city_id, -1)`
  to the index (a one-time index rebuild on first open, no data rewrite); the
  key only widens, so no existing row can conflict.
- **That refusal was silent.** `_write_signal` caught the `IntegrityError` and
  returned `None` with no log, no skip row and no queue decision — the one
  thing this system is not allowed to do with a lost judgement. A genuine
  duplicate is still refused, but now says so.
- **A session ran under a different mission version than it recorded, in
  silence.** A session created under `reference/1` was scored under
  `reference/2`, which had split the social weight row into two streams.
  Iterations now log a WARNING naming both versions. Not a refusal — packs are
  versioned precisely so they can move — but not silence either.

### Added
- `correlations.config_hash`, surfaced as `EvidenceOut.config_hash`: the
  analytical configuration each score was computed under, the same fingerprint
  the iteration's receipts carry. Correlation is the one judgement made
  without a model and so writes no receipt; re-scoring a stored iteration
  produced different numbers and nothing on the row could say whether the
  engine or the operator's config had moved. Now it can.

## [0.2.0] — 2026-08-25

### Added
- **Mission streams** (`streams.yaml`): a pack may split the social feed into
  named watches, each with its own platform subset, per-track lexicon, scoring
  weight, and optionally its own relevance criteria. Each stream scores as its
  own kind; its declared family decides whether it stays a sub-kind of SOCIAL
  or counts as a family of its own in banding and completeness. Triage batches
  are per stream under per-stream prompts; the same URL is judged once per
  stream; publishers and claims are pooled across streams so syndication
  cannot manufacture corroboration. A pack with no `streams.yaml` behaves
  byte-identically to v0.1 via one implicit stream.
- **Operator calendar** of scheduled events: a YAML in `inputs/`, named at
  session creation (`calendar_set`, CLI `--calendar`) and appendable between
  iterations (`POST /v1/sessions/{id}/calendar`, CLI `session add-calendar`;
  409 while an iteration runs). Annotation only, never scoring: triage shows
  the events to the model as context, each correlation stores verbatim
  snapshots of the events overlapping its window plus the engine-reserved
  competing explanation `SCHEDULED_EVENT` (packs may not claim that code), and
  score/band/contributions are pinned identical with and without a calendar.
  The prompt block is a pure function of rows filtered by
  `added_at <= iteration.started_at`, so receipts stay byte-exact
  reconstructible after later appends.
- Schema v15: `stream` columns on `query_queue`, `queue_decisions`, `signals`,
  `triage_decisions`, `triage_skips`; `idx_sig_dedup` rebuilt to include the
  stream (a one-time index rebuild on first open, no data rewrite);
  `calendar_events` table and `correlations.calendar_matches_json` (used by
  the operator calendar, above).

### Changed
- The API's `info.version` now reports the package version instead of an
  independent literal (`0.6.0` in 0.1.0). Clients pinning that string should
  read it as the release number from now on.
- **Reference pack version 2** is the streams exhibit: `lexicon.yaml` split
  into `streams.yaml` (`chatter` = twitter + reddit as a SOCIAL sub-kind;
  `local_news` = news promoted to its own LOCAL_NEWS family with its own
  strict relevance leg), per-stream weight rows summing to the v1 social
  budget, LOCAL_NEWS hypotheses, and a new pack digest. The published
  contract examples are regenerated against it. The other shipped pack is
  untouched — its unchanged digest and passing tests are the
  backward-compatibility proof, and a no-streams pack still behaves
  byte-identically to 0.1.0.

### Fixed
- Stale loader comment claiming the input-set name is recorded on the session
  (it never was; the session records the resolved geography).

## [0.1.0] — 2026-08-22

First shipped version.

- Mission-agnostic tipping-and-queuing engine over four data families —
  social/news, flight, lodging, rental car — with SQLite as the only
  communication bus, deterministic correlation and banding, per-session
  budget/dedup/cooldown/fan-out guards, crash recovery, and a classification
  receipt for every model call (prompt, accepted request, input, config,
  mission digest — all hashed).
- Mission packs: vocabulary, lexicon, scoring weights, prompts, geography
  equivalences, facility matching, and competing explanations live in a
  directory read once at startup, hashed into every receipt, validated with
  refuse-by-name. Two packs ship with the repository: `reference`, the
  synthetic fixture the engine's tests and published contract build against,
  and one operational pack, maintained under `missions/` and documented in its
  own README.
- REST API with a generated, drift-gated contract (`docs/api/`): OpenAPI
  specs, 45 captured exchanges, idempotent triggers, typed triage outcomes,
  evidence drill-down with receipts and competing explanations.
- Database schema v14.
