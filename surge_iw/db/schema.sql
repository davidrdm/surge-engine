-- Surge I&W — SQLite schema v1
--
-- The database is the communication bus between agents. No agent calls another
-- directly; each reads its inputs from and writes its outputs to these tables.
--
-- Every analytical decision has a corresponding row. That includes decisions
-- NOT to act: refused queue entries land in queue_decisions, rejected social
-- posts land in triage_decisions. A silently dropped signal is a bug.
--
-- Note on foreign keys: signals.raw_id -> raw_results.query_id -> query_queue,
-- while query_queue.tipped_by_signal_id -> signals. That is a declarative
-- cycle, which SQLite permits because it resolves FK targets at DML time and
-- all three columns are nullable. At runtime there is no cycle: a social signal
-- always exists before the query it tips is enqueued.

PRAGMA foreign_keys = ON;

-- ===========================================================================
-- Session and geography
-- ===========================================================================

CREATE TABLE IF NOT EXISTS sessions (
    session_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT    NOT NULL,
    label            TEXT,
    -- 0 = search only the cities the user supplied; 1 = agents may admit more
    -- (subject to the corroboration gate in QueueAgent.admit_city).
    expand_cities    INTEGER NOT NULL DEFAULT 0 CHECK (expand_cities IN (0,1)),
    -- Which of the mission's tracks this session scores, as a CSV. No
    -- DEFAULT: the permitted values come from the loaded mission pack, so a
    -- default here would be one mission's vocabulary frozen into the schema.
    tracks           TEXT    NOT NULL,
    -- Which mission this session ran under. NULL means it predates missions,
    -- i.e. the vocabulary that used to be built in. Stored because a track
    -- name is
    -- meaningless without the definition that gave it meaning.
    mission          TEXT,
    -- Tunables frozen at init so a session stays reproducible even if the
    -- config file changes underneath it.
    config_json      TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'ACTIVE'
                         CHECK (status IN ('ACTIVE','CLOSED')),
    -- 8.6. The per-session lock, moved out of process memory.
    --
    -- One session runs one iteration at a time. That was enforced by a
    -- threading.Lock, which is only true inside ONE process — which is why
    -- `run.py serve` pinned uvicorn to workers=1 and called it a correctness
    -- requirement. Holding the claim here makes the guarantee survive a second
    -- worker, a second host, and the CLI running beside the API.
    --
    -- Claimed by a conditional UPDATE ... WHERE running_iteration_id IS NULL,
    -- so acquisition is atomic in SQLite rather than a read-then-write race.
    running_iteration_id INTEGER REFERENCES iterations(iteration_id),
    -- Which process holds it. A lock whose epoch is dead is STALE, not held —
    -- otherwise a crash would wedge the session permanently, and the operator's
    -- only recovery would be editing the database by hand.
    running_epoch_id INTEGER REFERENCES process_epochs(epoch_id)
);

CREATE TABLE IF NOT EXISTS cities (
    city_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER NOT NULL REFERENCES sessions(session_id),
    name             TEXT    NOT NULL,
    state            TEXT,
    canonical        TEXT    NOT NULL,   -- normalised match key
    is_seed          INTEGER NOT NULL DEFAULT 1 CHECK (is_seed IN (0,1)),
    admitted_by      TEXT    NOT NULL DEFAULT 'USER'
                         CHECK (admitted_by IN ('USER','TIP')),
    admitted_iteration INTEGER REFERENCES iterations(iteration_id),
    UNIQUE (session_id, canonical)
);

CREATE TABLE IF NOT EXISTS key_locations (
    location_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    city_id          INTEGER NOT NULL REFERENCES cities(city_id),
    name             TEXT    NOT NULL,
    address          TEXT,
    lat              REAL,
    lon              REAL,
    -- Deliberately unconstrained here. The permitted values are the loaded
    -- mission's `location_types`, which SQLite cannot know; validation is in
    -- Python at every write path, where the error can also name the mission.
    -- See db/enums.py and tests/test_database.py::TestMissionOwnedColumns.
    location_type    TEXT,
    UNIQUE (city_id, name)
);

-- Resolver caches: city -> airport codes, city -> car pickup point,
-- key location -> the fixed Staying listing set used for both windows.
CREATE TABLE IF NOT EXISTS geo_cache (
    cache_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT    NOT NULL CHECK (kind IN
                         ('AIRPORT','PICKUP_LOCATION','LISTING_SET','HOTEL_SET')),
    lookup_key       TEXT    NOT NULL,
    value_json       TEXT    NOT NULL,
    resolved_by      TEXT    NOT NULL DEFAULT 'TABLE' CHECK (resolved_by IN
                         ('TABLE','ALIAS','PREFIX','API','UNRESOLVED')),
    resolved_at      TEXT    NOT NULL,
    expires_at       TEXT,
    UNIQUE (kind, lookup_key)
);

-- The operator's calendar of scheduled events, per session. Context, never
-- input: TriageAgent shows these to the model as background and
-- CorrelationAgent records the ones that overlap a scored window — nothing
-- here ever moves a score or a band.
--
-- APPEND-ONLY, and rows are immutable once written. `added_at` is what makes
-- triage prompts reconstructible byte-exact after later appends: an
-- iteration's context block is exactly the events with
-- added_at <= iterations.started_at, so adding events tomorrow cannot change
-- what yesterday's receipt hashed. There is no delete; an event added in
-- error is corrected by a new session.
CREATE TABLE IF NOT EXISTS calendar_events (
    event_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER NOT NULL REFERENCES sessions(session_id),
    name             TEXT    NOT NULL,
    -- The city as the operator wrote it, and the canonical form it resolved
    -- to at load (refused by name if it could not). Matching is on canonical.
    city_label       TEXT    NOT NULL,
    city_canonical   TEXT    NOT NULL,
    -- Canonical ISO instants. A bare date becomes 00:00Z; an omitted end
    -- becomes the end of the start's day. ends_at >= starts_at, enforced at
    -- load rather than here so the refusal can name the event.
    starts_at        TEXT    NOT NULL,
    ends_at          TEXT    NOT NULL,
    -- The operator's own words; deliberately unconstrained. An engine CHECK
    -- here would make the engine the owner of a vocabulary that is neither
    -- its nor any mission's.
    category         TEXT,
    note             TEXT,
    source_name      TEXT,               -- which calendar file supplied it
    added_at         TEXT    NOT NULL,
    UNIQUE (session_id, city_canonical, name, starts_at)
);
CREATE INDEX IF NOT EXISTS idx_calendar_session
    ON calendar_events (session_id, city_canonical, starts_at);

-- ===========================================================================
-- Process instances
-- ===========================================================================

-- One row per process. This is what makes interruption a STRUCTURAL fact — an
-- iteration owned by an epoch that is not the current one and has not finished
-- — with no clock in the predicate and no staleness threshold to tune.
--
-- A heartbeat was rejected for exactly that reason. Legitimate silences here
-- are minutes long: Staying's /search was measured at 125 seconds and polls to
-- 420, and FR24 is paced at one request every six seconds. Any timeout that
-- cleared those would be too coarse to be useful, and a false positive costs a
-- duplicate collection pass in real money.
CREATE TABLE IF NOT EXISTS process_epochs (
    epoch_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT    NOT NULL,
    host             TEXT    NOT NULL,
    pid              INTEGER NOT NULL,
    entry_point      TEXT    NOT NULL,   -- serve | iterate | retry-triage |
                                         -- recover | cli | test
    -- CLEAN    shutdown drained; connectors and the database were closed.
    -- TIMEOUT  the bounded wait expired with work in flight.
    -- UNKNOWN  written by the NEXT epoch onto a predecessor it found open.
    --          ended_at stays NULL there: we do not know when it died, and
    --          inventing a timestamp that later reads as fact is worse than
    --          admitting the gap.
    ended_at         TEXT,
    shutdown_kind    TEXT CHECK (shutdown_kind IN ('CLEAN','TIMEOUT','UNKNOWN')),
    stranded_json    TEXT,               -- iteration ids still live at TIMEOUT
    closed_by_epoch  INTEGER REFERENCES process_epochs(epoch_id)
);
CREATE INDEX IF NOT EXISTS idx_epoch_open
    ON process_epochs (ended_at, epoch_id);

-- ===========================================================================
-- Iterations
-- ===========================================================================

CREATE TABLE IF NOT EXISTS iterations (
    iteration_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER NOT NULL REFERENCES sessions(session_id),
    seq              INTEGER NOT NULL,
    anchor_at        TEXT    NOT NULL,   -- correlation window anchor
    started_at       TEXT    NOT NULL,
    finished_at      TEXT,
    -- Which process instance owned the run. Interruption is derived from this
    -- and nothing else.
    owner_epoch_id   INTEGER REFERENCES process_epochs(epoch_id),
    -- Orthogonal to `outcome`, not a value of it. `outcome` means how an
    -- iteration ENDED, and an interrupted one has not ended — it can still be
    -- resumed. finish_iteration() also sets outcome, finished_at and a terminal
    -- stage as one indivisible pairing that every reader depends on.
    -- interrupted_at never clears, so the history survives a later resume.
    interrupted_at   TEXT,
    -- Persisted rather than re-derived, because start_agent_run's
    -- delete-then-insert destroys the agent_runs row it came from the moment
    -- the stage runs again.
    interrupted_stage TEXT,
    -- 8.8. Set when this iteration is a re-triage of an earlier one: it
    -- re-judges the posts that iteration's model calls failed on, inherits its
    -- anchor_at so the correlation window does not slide away from the evidence
    -- it exists to complete, and runs TIPPING onward without re-collecting.
    -- The parent is never edited; both records stand as what each run did.
    retry_of_iteration_id INTEGER REFERENCES iterations(iteration_id),
    stage            TEXT    NOT NULL DEFAULT 'SEEDING' CHECK (stage IN (
                         'SEEDING','COLLECTING_SOCIAL','TRIAGING','TIPPING',
                         'COLLECTING_TIPPED','CORRELATING','ALERTING',
                         'SCHEDULING','COMPLETE','FAILED')),
    -- PARTIAL: ran the whole sequence but an agent or a source degraded.
    outcome          TEXT CHECK (outcome IN ('COMPLETE','PARTIAL','FAILED')),
    budget_plan_json TEXT,               -- spend envelope computed at SEEDING
    degradations_json TEXT,              -- surfaced by GET /v1/iterations/{id}
    error_message    TEXT,
    UNIQUE (session_id, seq)
);

-- Per-agent run records. A failing agent fails ITS row, not the iteration: a
-- social-connector outage must never discard a real military-flight cluster.
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    iteration_id     INTEGER NOT NULL REFERENCES iterations(iteration_id),
    agent            TEXT    NOT NULL,
    stage            TEXT    NOT NULL,
    -- INTERRUPTED is not FAILED: the agent did not fail, it was stopped. It
    -- also un-hides the stage from StageInspector, which skips RUNNING rows —
    -- without it, rollback targets the stage BEFORE the interrupted one and
    -- orphans its partial output.
    status           TEXT    NOT NULL CHECK (status IN
                         ('RUNNING','COMPLETE','FAILED','INTERRUPTED')),
    started_at       TEXT    NOT NULL,
    finished_at      TEXT,
    error_message    TEXT,
    UNIQUE (iteration_id, agent, stage)
);

CREATE TABLE IF NOT EXISTS schema_version (
    version          INTEGER PRIMARY KEY,
    applied_at       TEXT NOT NULL
);

-- ===========================================================================
-- The query queue
-- ===========================================================================

CREATE TABLE IF NOT EXISTS query_queue (
    query_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER NOT NULL REFERENCES sessions(session_id),
    -- NULL means work scheduled for a FUTURE iteration; stage 1 claims it once
    -- not_before has passed and stamps the iteration_id then.
    iteration_id     INTEGER REFERENCES iterations(iteration_id),
    -- LODGING_PRICE is a hotel PRICE measurement, not availability. It is a
    -- separate source_type so it is budgeted, rate-limited and coverage-gapped
    -- on its own, but it maps to the LODGING scoring family (see
    -- base/scoring.SOURCE_TYPE_FAMILY) so it does not become a fifth family
    -- and silently move every band threshold.
    source_type      TEXT    NOT NULL CHECK (source_type IN
                         ('SOCIAL','FLIGHT_COUNT','FLIGHT_LIVE','FLIGHT_HISTORY',
                          'LODGING','LODGING_PRICE','CAR')),
    endpoint         TEXT    NOT NULL,
    params_json      TEXT    NOT NULL,
    -- Which of the mission's streams issued this query, for SOCIAL rows.
    -- NULL means the mission's single implicit stream (or a pre-v15 row).
    -- Deliberately unconstrained: stream ids are MISSION vocabulary, and a
    -- mission vocabulary in a CHECK is the mistake version 12 existed to
    -- remove. Validated in Python against the loaded pack.
    stream           TEXT,
    city_id          INTEGER REFERENCES cities(city_id),
    location_id      INTEGER REFERENCES key_locations(location_id),
    dedup_key        TEXT    NOT NULL,   -- canonical hash of endpoint + params
    priority         INTEGER NOT NULL DEFAULT 50,
    tip_depth        INTEGER NOT NULL DEFAULT 0,
    tipped_by_signal_id INTEGER REFERENCES signals(signal_id),
    rule_code        TEXT,               -- which rule enqueued this
    not_before       TEXT,
    -- INTERRUPTED is distinct from FAILED, which means "we asked the vendor and
    -- it went wrong" and sets executed_at, driving the cooldown. A query that
    -- was claimed and may never have been sent is a different fact, and
    -- conflating them corrupts the failure-rate view an operator uses to
    -- diagnose a broken key. It joins UNRELIABLE_QUERY_STATUSES, so an
    -- abandoned iteration's lost collection reaches data_completeness with no
    -- change to base/scoring.py.
    status           TEXT    NOT NULL DEFAULT 'PENDING' CHECK (status IN
                         ('PENDING','IN_PROGRESS','COMPLETE','FAILED',
                          'INTERRUPTED','SKIPPED_BUDGET','SKIPPED_NO_MAPPING')),
    skip_reason      TEXT CHECK (skip_reason IN
                         ('MONTHLY_QUOTA_EXHAUSTED','ITERATION_ALLOCATION_EXHAUSTED',
                          'HARD_STOP_PRIORITY','NO_AIRPORT_MAPPING',
                          'NO_PICKUP_MAPPING','NO_LISTING_SET',
                          'THIN_PAIRED_SAMPLE')),
    origin           TEXT NOT NULL DEFAULT 'TIP' CHECK (origin IN
                         ('SEED','TIP','SCHEDULED','CARRIED_FORWARD')),
    result_count     INTEGER,
    executed_at      TEXT,
    error_message    TEXT,
    created_at       TEXT,
    -- Which iteration WROTE this row, as opposed to which one owns it.
    -- The two differ for scheduled work: stage 8 writes rows with a NULL
    -- iteration_id so a future stage 1 can claim them, which otherwise leaves
    -- them unattributable — two iterations can each schedule an identical
    -- follow-on, because SQLite treats NULLs as distinct in idx_qq_dedup.
    -- Per-stage inspection and rollback both need to know which one is whose.
    created_iteration_id INTEGER REFERENCES iterations(iteration_id)
);

-- Structural guarantee that one iteration cannot hold two identical queries.
-- Enforced by the storage engine rather than by a check that could be bypassed.
CREATE UNIQUE INDEX IF NOT EXISTS idx_qq_dedup
    ON query_queue (session_id, iteration_id, dedup_key);
CREATE INDEX IF NOT EXISTS idx_qq_work
    ON query_queue (status, source_type, priority, not_before);
CREATE INDEX IF NOT EXISTS idx_qq_cooldown
    ON query_queue (dedup_key, executed_at);
CREATE INDEX IF NOT EXISTS idx_qq_city
    ON query_queue (iteration_id, city_id);

-- Every refusal is recorded, so enqueue decisions are auditable both ways.
CREATE TABLE IF NOT EXISTS queue_decisions (
    decision_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    iteration_id     INTEGER NOT NULL REFERENCES iterations(iteration_id),
    rule_code        TEXT    NOT NULL,
    outcome          TEXT    NOT NULL CHECK (outcome IN
                         ('ENQUEUED','DEDUPED','COOLDOWN','CAP_ITERATION','CAP_CITY',
                          'CAP_DEPTH','CITY_NOT_ADMITTED','BUDGET_EXHAUSTED',
                          'NO_MAPPING')),
    source_type      TEXT,
    -- Which stream a refusal belongs to, when the refused work was one
    -- stream's. NULL = not stream-scoped. Without it a per-stream refusal
    -- could not reach that stream's family as a coverage gap.
    stream           TEXT,
    city_name        TEXT,
    dedup_key        TEXT,
    signal_id        INTEGER REFERENCES signals(signal_id),
    detail           TEXT,
    decided_at       TEXT    NOT NULL,
    -- Which stage made the call. rule_code cannot answer this: the flight
    -- escalation in COLLECTING_TIPPED re-uses R4_LODGING and R5_CAR, so the
    -- same code is emitted by two stages. Per-stage inspection and rollback
    -- both need an unambiguous answer, and an operator reading the queue view
    -- wants to know which stage refused a query, not only which rule did.
    stage            TEXT
);
CREATE INDEX IF NOT EXISTS idx_qd_iter ON queue_decisions (iteration_id, outcome);

-- ===========================================================================
-- Raw payloads (retention-governed)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS raw_results (
    raw_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id         INTEGER NOT NULL REFERENCES query_queue(query_id),
    iteration_id     INTEGER NOT NULL REFERENCES iterations(iteration_id),
    source_type      TEXT    NOT NULL,
    provider         TEXT    NOT NULL CHECK (provider IN
                         ('APIDIRECT','FR24','STAYING','PRICELINE')),
    payload_json     TEXT    NOT NULL,
    retrieved_at     TEXT    NOT NULL,
    -- Provider-specific retention deadline. FR24's licence requires deletion
    -- 30 days after first receipt; services/retention.py enforces it.
    purge_after      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_purge ON raw_results (purge_after);
CREATE INDEX IF NOT EXISTS idx_raw_iter ON raw_results (iteration_id, source_type);

-- ===========================================================================
-- Normalised signals
-- ===========================================================================

CREATE TABLE IF NOT EXISTS signals (
    signal_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    iteration_id     INTEGER NOT NULL REFERENCES iterations(iteration_id),
    -- ON DELETE SET NULL is what makes retention possible. FR24's licence
    -- requires deleting the raw payload after 30 days, but the analytical
    -- record — that an aircraft of a given category was inbound, and that it
    -- contributed to an alert — is this system's own product and must survive.
    -- Without SET NULL, SQLite refuses the retention delete outright.
    raw_id           INTEGER REFERENCES raw_results(raw_id) ON DELETE SET NULL,
    signal_type      TEXT    NOT NULL CHECK (signal_type IN
                         ('SOCIAL','FLIGHT','LODGING','CAR')),
    city_id          INTEGER REFERENCES cities(city_id),
    location_id      INTEGER REFERENCES key_locations(location_id),
    -- Which of the mission's tracks this observation is attributed to, or
    -- UNKNOWN. UNKNOWN is ENGINE vocabulary, not the mission's: it means the
    -- source did not say who was acting, which is a fact about the evidence
    -- rather than about the mission, and it stays the default so a row that
    -- nobody attributed reads as unattributed rather than as a guess.
    track            TEXT NOT NULL DEFAULT 'UNKNOWN',
    observed_at      TEXT,               -- when the event/post happened
    quality          REAL NOT NULL DEFAULT 0.0
                         CHECK (quality BETWEEN 0.0 AND 1.0),

    -- CANDIDATE: recorded and visible, but does not score and cannot tip.
    -- CONFIRMED: scores, and may spend money on follow-on collection.
    -- The distinction Phase 7 added. Without it, one accepted post — even with
    -- a missing salience and no timestamp — produced a scoring signal for a
    -- seeded city and booked the full paid follow-on set.
    signal_state     TEXT NOT NULL DEFAULT 'CONFIRMED'
                         CHECK (signal_state IN ('CANDIDATE','CONFIRMED')),
    state_reason     TEXT,

    -- 9.4. How this reached us, as ONE value comparable across all four
    -- families. Per-family provenance already existed; a comparable one did
    -- not, so an analyst could not tell a vendor-cached lodging row from a
    -- freshly retrieved one.
    --
    -- Held on `signals` rather than only on `raw_results` because retention
    -- deletes the payload and sets `signals.raw_id` to NULL. The analytical
    -- record is this system's own product and must survive that; a provenance
    -- field that vanished with the payload would be missing exactly when the
    -- question is hardest to answer another way.
    --
    -- UNRECORDED is the DEFAULT so the ALTER can backfill an existing database
    -- without asserting anything about rows collected before the field
    -- existed. It means "no attestation", not "collected some other way".
    collection_class TEXT NOT NULL DEFAULT 'UNRECORDED'
                         CHECK (collection_class IN
                         ('DIRECT','INTERMEDIARY_LIVE','INTERMEDIARY_CACHED',
                          'UNRECORDED')),
    -- What the class was read from: the provider and endpoint, or the response
    -- field that said it was cached. A label without its attestation is a
    -- claim; with it, it is a record.
    collection_basis TEXT,
    -- social ----------------------------------------------------------------
    -- Which of the mission's streams produced this observation. NULL means the
    -- implicit stream (a no-streams mission, or a pre-v15 row). Mission
    -- vocabulary, so no CHECK; the scoring kind and the banding family are
    -- both derived from it in Python against the loaded pack.
    stream           TEXT,
    url              TEXT,
    author           TEXT,
    platform         TEXT,
    -- The RAW vendor value, preserved unchanged for audit and rights review.
    -- It is no longer what corroboration counts.
    source_domain    TEXT,
    -- Who told us, canonicalised. `www.apnews.com`, `apnews.com` and
    -- "Associated Press" are one publisher; before Phase 7 they were three.
    publisher_key    TEXT,
    publisher_method TEXT,      -- ALIAS | HOST | PLATFORM | UNKNOWN
    -- WHAT they told us. Two outlets reprinting one wire story are two
    -- publishers and ONE claim, and corroboration needs both to be plural.
    claim_key        TEXT,
    -- How a facility attribution was reached, or why it was refused.
    location_method  TEXT,
    snippet          TEXT,
    salience         REAL,
    activity_type    TEXT,
    imminence_hours  REAL,

    -- flight ----------------------------------------------------------------
    fr24_id          TEXT,
    callsign         TEXT,
    registration     TEXT,
    aircraft_type    TEXT,
    origin_iata      TEXT,
    dest_iata        TEXT,
    operating_as     TEXT,
    -- AMBIGUOUS: came from a live-positions response, which carries no
    -- category field. Never scored at military weight (see skills/scoring.py).
    flight_category  TEXT CHECK (flight_category IN ('M','J','T','H','AMBIGUOUS')),
    category_confidence TEXT CHECK (category_confidence IN
                         ('CONFIRMED','AMBIGUOUS')),
    flight_status    TEXT CHECK (flight_status IN ('landed','airborne_inbound')),
    eta              TEXT,

    -- lodging / car shared availability columns ------------------------------
    -- LODGING: one row per Staying listing; near/base_available = available
    --   nights in the window, near/base_total = nights offered.
    -- CAR: one row per (pickup counter x vehicle class); near/base_available =
    --   offers in that class per window, near/base_total = the search-level
    --   totalResultsAvailable, and `truncated` records a resultsCount
    --   shortfall so a paginated cut never reads as scarcity.
    provider_ref     TEXT,
    item_name        TEXT,
    near_available   INTEGER,
    near_total       INTEGER,
    base_available   INTEGER,
    base_total       INTEGER,
    drop_pct         REAL,
    price_near       REAL,
    price_baseline   REAL,
    discount_pct_near REAL,              -- savingsPercent collapse
    discount_pct_base REAL,
    distance_km      REAL,               -- Priceline distanceFromSearchLocation
    truncated        INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0,1)),

    -- car-specific (verified Priceline vehicles[] shape) ---------------------
    vehicle_class    TEXT,               -- code, e.g. 'ECAR'
    vehicle_class_name TEXT,             -- nameDisplay
    people_capacity  INTEGER,            -- drives capacity weighting
    bag_capacity     INTEGER,
    partner_code     TEXT,
    partner_name     TEXT,
    counter_type     TEXT,               -- e.g. 'ON_AIR_SHUTTLE'
    is_on_airport    INTEGER CHECK (is_on_airport IN (0,1)),
    is_peer_to_peer  INTEGER CHECK (is_peer_to_peer IN (0,1)),
    field_map_ver    TEXT                -- normaliser provenance
);
CREATE INDEX IF NOT EXISTS idx_sig_corr
    ON signals (city_id, signal_type, observed_at);
CREATE INDEX IF NOT EXISTS idx_sig_iter
    ON signals (iteration_id, signal_type);
-- Dedup within an iteration. COALESCE because each signal type populates a
-- different natural key, and SQLite treats NULLs as distinct in UNIQUE indexes.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sig_dedup ON signals (
    iteration_id,
    signal_type,
    -- WHERE the observation is about. An observation is distinct per place --
    -- a post announcing shows in Phoenix AND Chicago is evidence about each
    -- of them, and correlation scores each city over its own rows, so two
    -- rows can never meet in one score.
    --
    -- Omitting this was measured live: a tour announcement naming both
    -- session cities wrote one signal, and WHICH city kept it was decided by
    -- the order the model happened to list them in. The loser's evidence
    -- vanished with no queue decision, no skip row and no log line. -1
    -- because city_id is nullable and NULLs must keep colliding here.
    COALESCE(city_id, -1),
    -- Streams judge the same URL under different criteria, so one signal per
    -- (stream, URL) is legitimate. '' keeps pre-stream rows deduplicating
    -- exactly as before.
    COALESCE(stream, ''),
    COALESCE(url, ''),
    COALESCE(fr24_id, ''),
    COALESCE(provider_ref, ''),
    COALESCE(vehicle_class, '')
);

-- Every triaged post, accepted or rejected, with the reason.
-- 8.9. A collected post that never reached the model, and why.
--
-- Deliberately NOT a state in `triage_decisions`. Every state in that table is
-- about a model call that was made — including its three failure states. A post
-- recorded here was never asked about, so sharing the table would make it
-- answer two questions and force every reader to filter for which one; the
-- skips also outnumber the judgements, because the median collected post was
-- measured at 206 days old against a 168-hour cut.
--
-- Before this, `_gather()` had five drops and recorded one of them as a bare
-- count. The two whole-payload cases are the reason this table exists rather
-- than a per-post row for the age cut alone: a malformed vendor response
-- removed every post in it with no count, no degradation and no row, so an
-- absence of evidence produced by a parse failure was indistinguishable from an
-- absence of the thing being watched for.
CREATE TABLE IF NOT EXISTS triage_skips (
    skip_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    iteration_id     INTEGER NOT NULL REFERENCES iterations(iteration_id),
    -- Nullable for the same reason as triage_decisions.raw_id: the record that
    -- a post was skipped must outlive the payload it was skipped from.
    raw_id           INTEGER REFERENCES raw_results(raw_id) ON DELETE SET NULL,
    -- NULL for a whole-payload drop and for an item with no URL — which is one
    -- of the reasons. Never invented.
    url              TEXT,
    --   STALE              older than triage.max_post_age_hours
    --   PAYLOAD_UNPARSEABLE  the stored payload is not JSON       (whole payload)
    --   PAYLOAD_NOT_A_LIST   parsed, but not the expected array   (whole payload)
    --   ITEM_NOT_AN_OBJECT   an element that is not an object
    --   ITEM_NO_URL          no URL, so nothing to bind a judgement to
    -- DEDUPED is deliberately absent: the same article surfacing from three
    -- queries is judged once and that judgement is the record. It is not a drop.
    reason           TEXT    NOT NULL CHECK (reason IN (
                         'STALE','PAYLOAD_UNPARSEABLE','PAYLOAD_NOT_A_LIST',
                         'ITEM_NOT_AN_OBJECT','ITEM_NO_URL')),
    -- Which stream's gathering recorded the skip, so a rescan on resume can
    -- recognise its own refusals per stream. NULL = implicit / whole-payload.
    stream           TEXT,
    -- Enough to reproduce the decision from the row rather than recompute it
    -- against a clock that has moved. For STALE: when the post was observed,
    -- the cutoff in force, and the configured window. For a malformed payload:
    -- what failed. A record that says only "stale" leaves the reader doing the
    -- arithmetic that made it unreadable.
    observed_at      TEXT,
    cutoff_at        TEXT,
    max_post_age_hours REAL,
    detail           TEXT,
    -- How many posts a whole-payload drop cost, where that is knowable. NULL
    -- for a per-item skip and for an unparseable payload, where it is not.
    items_lost       INTEGER,
    skipped_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_triage_skips_iteration
    ON triage_skips (iteration_id, reason);

CREATE TABLE IF NOT EXISTS triage_decisions (
    triage_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    iteration_id     INTEGER NOT NULL REFERENCES iterations(iteration_id),
    -- Nullable for the same reason as signals.raw_id: the decision to accept or
    -- reject a post is an audit record that must outlive the post's payload.
    raw_id           INTEGER REFERENCES raw_results(raw_id) ON DELETE SET NULL,
    url              TEXT,
    -- Derived from `state` for compatibility. `state` is authoritative: this
    -- column cannot tell a considered rejection from an answer that never
    -- arrived, and before Phase 7 all four failure modes stored 0 here with the
    -- same rationale string — byte-identical rows for wholly different facts.
    relevant         INTEGER NOT NULL CHECK (relevant IN (0,1)),
    -- ACCEPTED        judged relevant
    -- REJECTED        judged not relevant — a real analytical conclusion
    -- UNDECIDED       requested, no judgement returned for this item
    -- INVALID_OUTPUT  a judgement arrived and failed the schema
    -- MODEL_ERROR     the call itself failed, or the response was not a list
    -- The last three are NON-COVERAGE, not negative results, and they feed
    -- data_completeness. Counting them as rejections is how a model outage
    -- would read as "we looked and found nothing".
    state            TEXT CHECK (state IN
                         ('ACCEPTED','REJECTED','UNDECIDED','INVALID_OUTPUT',
                          'MODEL_ERROR')),
    -- Why an item was unusable, when it was. Free text from the validator.
    fault_detail     TEXT,
    -- Which stream this judgement was made under. Load-bearing for resume:
    -- a URL judged under one stream and not yet under another must be
    -- distinguishable, and `raw_id` (the other route to the query's stream)
    -- is nulled by retention. NULL = implicit stream / pre-v15.
    stream           TEXT,
    track            TEXT,
    cities_json      TEXT,
    locations_json   TEXT,
    salience         REAL,
    imminence_hours  REAL,
    rationale        TEXT    NOT NULL,
    signal_id        INTEGER REFERENCES signals(signal_id),
    -- How this judgement was reached (8.1). Nullable: decisions written before
    -- receipts existed, and any decision no model call produced, have none.
    receipt_id       INTEGER REFERENCES receipts(receipt_id),
    model            TEXT    NOT NULL,
    -- The output contract in force when this was judged. Editing the accepted
    -- shape changes every future decision; without this, that change leaves no
    -- trace and old decisions cannot be segregated from new ones.
    schema_version   TEXT,
    decided_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_triage_state
    ON triage_decisions (iteration_id, state);
CREATE INDEX IF NOT EXISTS idx_triage_iter
    ON triage_decisions (iteration_id, relevant);

-- ===========================================================================
-- Idempotency (8.2)
-- ===========================================================================

-- Replay protection for the one endpoint that spends money.
--
-- A client that POSTs an iteration and loses the response to a network timeout
-- has no safe move: retrying may buy a second full collection pass, and not
-- retrying may mean no run at all. An Idempotency-Key makes the retry safe --
-- the second POST returns the FIRST one's response verbatim rather than
-- starting anything.
--
-- request_hash is what makes it honest: the same key with a DIFFERENT body is
-- a client bug, and answering it with the old response would silently ignore
-- what was asked for. It is refused instead.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER NOT NULL REFERENCES sessions(session_id),
    idempotency_key  TEXT    NOT NULL,
    request_hash     TEXT    NOT NULL,
    iteration_id     INTEGER REFERENCES iterations(iteration_id),
    status_code      INTEGER NOT NULL,
    response_json    TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    -- Keys expire so the table cannot grow without bound. Past expiry the same
    -- key is a NEW request, which is why the TTL is generous relative to how
    -- long a client could plausibly still be retrying.
    expires_at       TEXT    NOT NULL,
    UNIQUE (session_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_idem_expiry ON idempotency_keys (expires_at);

-- ===========================================================================
-- Classification receipts (8.1)
-- ===========================================================================

-- One row per model CALL, referenced by every decision or alert that call
-- produced. Not columns on the decision: a batch of ten posts shares one call,
-- and duplicating fourteen provenance columns ten times invites them to
-- disagree.
--
-- The prompt HASH is what makes decisions separable across a criteria change.
-- An edit to a prompt without bumping its version label still moves the hash,
-- so a careless label cannot make the record wrong.
--
-- Every provider-echo column is nullable because most OpenAI-compatible
-- endpoints omit most of them. An absent system_fingerprint is recorded as
-- absent, never defaulted into something later read as fact.
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    iteration_id     INTEGER REFERENCES iterations(iteration_id),
    kind             TEXT    NOT NULL CHECK (kind IN ('TRIAGE','ALERT')),
    -- what served the answer
    provider         TEXT,
    model_requested  TEXT    NOT NULL,   -- the config string we asked for
    model_served     TEXT,               -- response.model; differs when a
                                         -- vendor silently repoints an alias
    response_id      TEXT,
    system_fingerprint TEXT,
    tokens_in        INTEGER,
    tokens_out       INTEGER,
    -- >1 means a retry rewrote the prompt and the accepted answer came from
    -- the last variant, which was previously unrecorded.
    attempts         INTEGER NOT NULL DEFAULT 1,
    temperature      REAL,
    max_tokens       INTEGER,
    -- what was asked, and under which rules
    prompt_version   TEXT    NOT NULL,
    prompt_hash      TEXT    NOT NULL,
    -- Version 14. The USER message that was accepted, hashed. Distinct from
    -- prompt_hash, which covers the SYSTEM prompt: when `attempts > 1` the
    -- retry loop rewrote the user message with the parse error and the failed
    -- reply, so every other field here describes a request that was sent and
    -- refused rather than the one that produced the answer. NULL means the
    -- receipt predates this column — not that it could not be reconstructed.
    prompt_user_hash TEXT,
    schema_version   TEXT,
    rules_version    TEXT,
    normaliser_version TEXT,
    -- the world it ran in
    code_revision    TEXT,
    -- Version 12. Which mission pack supplied the prompt, and the digest of
    -- its bytes. `code_revision` alone stopped being sufficient the moment the
    -- prompt left the repository.
    mission_id       TEXT,
    mission_hash     TEXT,               -- NULL outside a git checkout
    package_version  TEXT,
    config_hash      TEXT    NOT NULL,   -- analytical config only
    -- what it was asked about
    batch_key        TEXT,
    input_hash       TEXT    NOT NULL,   -- hash of the BUILT payload, so the
                                         -- truncation window is covered too
    created_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipt_iter ON receipts (iteration_id, kind);

-- ===========================================================================
-- Correlation and alerts
-- ===========================================================================

CREATE TABLE IF NOT EXISTS correlations (
    correlation_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    iteration_id     INTEGER NOT NULL REFERENCES iterations(iteration_id),
    city_id          INTEGER NOT NULL REFERENCES cities(city_id),
    -- Unconstrained for the same reason as key_locations.location_type.
    track            TEXT    NOT NULL,
    score            REAL    NOT NULL CHECK (score BETWEEN 0.0 AND 1.0),
    band             TEXT    NOT NULL CHECK (band IN
                         ('LOW','MEDIUM','HIGH','NONE')),
    distinct_types   INTEGER NOT NULL,
    contributions_json TEXT  NOT NULL,   -- per-type weight x quality
    data_completeness REAL   NOT NULL
                         CHECK (data_completeness BETWEEN 0.0 AND 1.0),
    -- 9.11. TWO lists, because they answer two questions and conflating them
    -- scored a partly-collected family as absent:
    --   failed_sources    WHAT failed, as SOURCE_TYPE:endpoint. Reported.
    --   failed_families   families with NOTHING collected. Drives completeness.
    failed_sources   TEXT,               -- CSV of SOURCE_TYPE:endpoint
    failed_families  TEXT,               -- CSV of wholly uncollected families
    band_capped      INTEGER NOT NULL DEFAULT 0 CHECK (band_capped IN (0,1)),
    rule_trace       TEXT    NOT NULL,   -- which band rule fired, in words
    -- 9.6. What ELSE would produce this evidence, derived deterministically
    -- from which families contributed. Stored rather than computed on read so
    -- an alert can be re-read under the rules that produced its list; see
    -- services/hypotheses.py for why the model is not asked for these.
    alternatives_json TEXT,
    -- Operator-calendar events that overlapped this correlation's window,
    -- snapshotted verbatim at scoring time so the row stays self-contained
    -- after later appends. NULL = predates the feature or the session has no
    -- calendar; '[]' = a calendar exists and nothing matched. ANNOTATION
    -- ONLY: nothing in this column ever moves the score or the band.
    calendar_matches_json TEXT,
    -- The analytical configuration this score was computed under
    -- (`receipts.config_fingerprint`, same value the iteration's receipts
    -- carry). Correlation is the one judgement in this system made without a
    -- model, so it writes no receipt — and without this the tunables that
    -- produced a score were recorded NOWHERE. Measured: re-scoring a stored
    -- iteration produced different numbers, and nothing on the row could say
    -- whether the engine or the operator's config had moved. NULL on rows
    -- written before v16.
    config_hash      TEXT,
    -- 9.10. Per flight kind: whether it was scored against a baseline, what
    -- that baseline was, and what was observed. On the correlation rather than
    -- on each signal because the window spans iterations, so one signal can be
    -- baselined in one correlation and not in another.
    flight_baseline_json TEXT,
    -- 9.13. Whether this iteration contributed any evidence of its own, which
    -- iteration last did, and how old the contributing signals are. The window
    -- reads across iterations by design, so a correlation can rest entirely on
    -- collection paid for days ago — a correct current assessment, and one a
    -- reader must be able to tell from a new observation.
    evidence_freshness_json TEXT,
    computed_at      TEXT    NOT NULL,
    -- 8.7(b). What ALERTING decided about this correlation, and why.
    --   ALERTED      an alert row was written
    --   BELOW_FLOOR  scored under correlation.alert_min_score
    --   BAND_NONE    no band qualified, so there is nothing to report
    -- NULL means ALERTING has not run for this iteration yet, which is a
    -- different fact from any decision and must stay distinguishable.
    --
    -- A correlation that produces no alert is otherwise unreachable: every
    -- route into the evidence surface resolves alerts.correlation_id first, so
    -- the near misses -- exactly the set you calibrate the floors from -- were
    -- visible only by opening the database.
    alert_decision   TEXT CHECK (alert_decision IN
                         ('ALERTED','BELOW_FLOOR','BAND_NONE')),
    alert_decision_reason TEXT,
    UNIQUE (iteration_id, city_id, track)
);

CREATE TABLE IF NOT EXISTS correlation_signals (
    correlation_id   INTEGER NOT NULL REFERENCES correlations(correlation_id),
    signal_id        INTEGER NOT NULL REFERENCES signals(signal_id),
    contribution     REAL    NOT NULL,
    PRIMARY KEY (correlation_id, signal_id)
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id   INTEGER NOT NULL UNIQUE
                         REFERENCES correlations(correlation_id),
    session_id       INTEGER NOT NULL REFERENCES sessions(session_id),
    iteration_id     INTEGER NOT NULL REFERENCES iterations(iteration_id),
    city_id          INTEGER NOT NULL REFERENCES cities(city_id),
    track            TEXT    NOT NULL,
    -- Copied from correlations.score by AlertAgent, which must not alter it.
    -- A Phase 5 test asserts the two are identical.
    confidence_score REAL    NOT NULL,
    confidence_band  TEXT    NOT NULL CHECK (confidence_band IN
                         ('LOW','MEDIUM','HIGH')),
    summary          TEXT    NOT NULL,   -- 1-2 sentences, LLM-written
    caveat           TEXT,               -- data-gap disclosure, deterministic
    earliest_eta     TEXT,               -- drives urgency in the API payload
    -- NULL when the summary came from the deterministic fallback: there was no
    -- model call, so there is nothing to attribute and we invent nothing.
    receipt_id       INTEGER REFERENCES receipts(receipt_id),
    model            TEXT    NOT NULL,
    -- 8.2. A human gate before escalation or public use. UNREVIEWED is not a
    -- verdict, it is the absence of one: an alert nobody has looked at must not
    -- be indistinguishable from one an analyst has cleared for distribution.
    -- Scoring and evidence are unaffected -- this governs distribution only.
    review_state     TEXT    NOT NULL DEFAULT 'UNREVIEWED'
                         CHECK (review_state IN
                             ('UNREVIEWED','RELEASED','WITHHELD')),
    reviewed_at      TEXT,
    reviewed_by      TEXT,
    review_note      TEXT,
    created_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_read
    ON alerts (session_id, created_at, confidence_band);

-- ===========================================================================
-- Audit and quota
-- ===========================================================================

CREATE TABLE IF NOT EXISTS agent_log (
    log_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    iteration_id     INTEGER REFERENCES iterations(iteration_id),
    agent            TEXT    NOT NULL,
    level            TEXT    NOT NULL CHECK (level IN
                         ('DEBUG','INFO','WARNING','ERROR')),
    message          TEXT    NOT NULL,
    extra_json       TEXT,
    logged_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_iter ON agent_log (iteration_id, agent);

CREATE TABLE IF NOT EXISTS api_calls (
    call_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    iteration_id     INTEGER REFERENCES iterations(iteration_id),
    query_id         INTEGER REFERENCES query_queue(query_id),
    provider         TEXT    NOT NULL,
    endpoint         TEXT    NOT NULL,
    http_status      INTEGER,
    -- Billing unit differs per provider: requests for API Direct and Priceline,
    -- credits for Staying, and credits-per-RECORD-RETURNED for FR24. Must be
    -- computed after the response arrives, never estimated beforehand.
    units            REAL    NOT NULL DEFAULT 1.0,
    records_returned INTEGER,
    latency_ms       INTEGER,
    called_at        TEXT    NOT NULL,
    error_message    TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_budget
    ON api_calls (provider, endpoint, called_at);

CREATE TABLE IF NOT EXISTS api_budgets (
    budget_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    provider         TEXT    NOT NULL,
    endpoint         TEXT,               -- NULL = provider-wide
    period           TEXT    NOT NULL CHECK (period IN
                         ('MONTH','DAY','ITERATION')),
    limit_units      REAL    NOT NULL,
    UNIQUE (provider, endpoint, period)
);
