# Surge I&W
A tipping-and-cueing engine for tactical indications and warning

*(c) David Blum, 2026, dmblum@gmail.com*

Surge I&W detects that something is **imminent** in a named city or county, from
four independent open sources: social media and news, flight movements,
short-term-rental availability, and rental-car availability. It sweeps social
media first, uses what it finds to **tip** paid searches against the other three,
correlates the results deterministically, and exposes banded alerts with their
underlying evidence over a small REST API.

**What it is looking for is not part of the engine.** The tracks, the search
lexicon, the two system prompts, the scoring weights and the analytic thresholds
all come from a **mission pack** — a directory of data files read at startup and
hashed into every receipt. The engine collects and correlates; the mission says
what any of it means. See §3.1.

Users define a session (cities and the facilities that matter), trigger
iterations, and read alerts. To do this, Surge executes a limited version of the
intelligence cycle.

**Iterations: Surge's "Intelligence Cycle"**

Surge is operated in iterations. Each iteration consists of the following stages.

| # | Stage | Component | LLM | Purpose |
|---|---|---|---|---|
| 1 | `SEEDING` | QueueAgent (`seed`) | no | Social queries for every active city; adopt due follow-ons |
| 2 | `COLLECTING_SOCIAL` | CollectionAgent | no | Drain the SOCIAL queue via API Direct |
| 3 | `TRIAGING` | TriageAgent | **yes** | Judge relevance; extract city / facility / actor / imminence |
| 4 | `TIPPING` | QueueAgent (`tip`) | no | Apply tipping rules → enqueue FLIGHT / LODGING / CAR |
| 5 | `COLLECTING_TIPPED` | CollectionAgent | no | Drain the tipped queue via FR24, Staying, Priceline |
| 6 | `CORRELATING` | CorrelationAgent | no | Spatial + temporal correlation; weighted confidence |
| 7 | `ALERTING` | AlertAgent | **yes** | Write the summary; link evidence |
| 8 | `SCHEDULING` | QueueAgent (`schedule`) | no | Enqueue follow-ons for *future* iterations |

---

## 1. Install

Surge requires Python 3.10+ (the reference environment is the `surge` conda env at
3.10.18). No 3.11/3.12-only syntax is used — `timezone.utc`, not `datetime.UTC`.

```bash
cd /path/to/surge && pip install -r requirements.txt
```

```bash
cp .env.example .env && chmod 600 .env && $EDITOR .env
```

Load your credentials as described below.

---

## 2. Credentials

Every credential is read from an **environment variable**. The config file
holds the variable's *name*, never its value.

| Variable | Used for | Required |
|---|---|---|
| `SURGE_API_TOKEN` | Bearer token for the REST API | yes, to serve |
| `GEMINI_API_KEY` | Triage and alert wording | yes, or triage is skipped |
| `APIDIRECT_API_KEY` | Social and news collection | yes |
| `FR24_API_KEY` | Flight positions | optional |
| `STAYING_API_KEY` | Lodging availability | optional |
| `PRICELINE_RAPIDAPI_KEY` | Rental car availability | optional |

Put them in the `.env` file (gitignored) that you created in **1. Install** and load it before any command:

```bash
source scripts/load-secrets.sh
```

Check what is set without printing any values:

```bash
./scripts/check-secrets.sh
```

A missing optional key disables that signal family; the system records the
absence as a coverage gap rather than treating it as "nothing found".

---

## 3. Configure

```bash
cp config.example.yaml config.yaml
```

`config.yaml` is gitignored. `surge_iw/config.py` holds every default, so you
only override what you need — but note that the analytic defaults there are
**illustrative placeholders**, not calibrated values. The real ones come from
the mission pack (§3.1). The settings you will actually touch:

```yaml
database:
  path: "surge_iw.db"          # file-backed; follow-ons must outlive an iteration

api:
  host: "127.0.0.1"            # see the warning below before changing
  port: 8000
  token_env: "SURGE_API_TOKEN"
  debug_endpoints: true        # false for production — see §4

llm:
  model: "gemini-3.5-flash"
  base_url: "https://generativelanguage.googleapis.com/v1beta/openai/"
  api_key_env: "GEMINI_API_KEY"

mission:
  dir: "./missions"
  name: "reference"            # point this at your own pack — see §3.1

budget:
  iterations_per_month_planned: 60
  per_iteration_cap:
    APIDIRECT: 60.0            # raise this for multi-city sessions — see §10

dry_run: false                 # true serves fixtures; spends nothing
```

> **Binding to `0.0.0.0` requires TLS and a real identity layer in front of it.**
> Alerts name specific facilities. The default binds to localhost.

`config.example.yaml` deliberately omits `triage`, `windows`, `sensitivity` and
`correlation`. Those four belong to the mission (§3.1), and restating them here
would override your pack's calibrated values on every key. Set one only when
you mean to override deliberately, and only the keys you mean to change.

---

## 3.1 The mission

The engine is mission-agnostic. What it is looking for comes from a **mission
pack**: a directory of data files, read once at startup.

```yaml
mission:
  dir: "./missions"
  name: "reference"      # a bare directory name inside dir, never a path
```

A pack carries everything an analyst would change to point the instrument at a
different question, and nothing an engineer would change to fix a bug:

| File | What it decides |
|---|---|
| `mission.yaml` | The tracks, the location types, the analytic thresholds, and which files belong to the pack |
| `lexicon.yaml` | What to search for, per track |
| `scoring.yaml` | What each kind of observation is worth, per track; which flight categories each track asks for |
| `geography.yaml` | Which place names are one operational unit; which regional publishers to recognise |
| `facilities.yaml` | Which facility spellings mean the same place; which words are too generic to match on; domain abbreviations |
| `hypotheses.yaml` | What else would produce this evidence |
| `prompts/*.md` | The screening prompt, its two relevance clauses, and the alert prompt |
| `docs/`, `inputs/` | Carried alongside; not read by the loader |

**The pack is the audit unit.** `mission.yaml` declares its members. The loader
refuses a declared file that is missing *and* an undeclared file that is present,
then hashes every member into one digest. That digest is reported by
`GET /v1/capabilities` and stamped on every receipt, so a judgement names the
exact bytes that produced it. A file that is neither loaded nor hashed would be a
change nothing records.

**Unknown is refused, never ignored.** A lexicon whose track name is misspelled
is an error naming the key, not a silent no-op — a misspelled track would search
nothing, score nothing, and report a quiet city, which is indistinguishable from
a city where nothing is happening.

**Some tables ADD rather than replace.** The engine keeps a structural core —
generic English words, wire services and national outlets, ordinary
abbreviations — and a mission's `facilities.tokens`, `facilities.spellings` and
`geography.publishers` are merged on top. Every pack restating "the" and
"Reuters" would be ceremony, and a pack that forgot to would silently match on
them. The tables that are wholly the mission's — tracks, lexicon, weights,
prompts, facility aliases, jurisdiction equivalences, competing explanations —
have no engine-side default at all.

**Three layers, in order.** Engine defaults (illustrative) → the mission's
`thresholds` → your `config.yaml`. You can still override a calibrated value
locally; every key where you do is logged by name at startup, because a mission's
thresholds carry reasoning and replacing one silently is how that reasoning gets
lost.

**No mission, no iteration.** `init-db` and the contract need a schema, not a
mission. An iteration needs one, and there is no fallback: the tracks, lexicon,
prompts and weights have no engine-side default, and a default here would produce
numbers that look exactly like analysis.

**A session cannot switch it.** `mission` is server-owned, like credentials and
endpoints. A request that could change it would change the tracks, the lexicon,
the prompts and the weights at once.

### The packs in this repository

`missions/reference/` is **synthetic and uncalibrated** — a benign
crowd-convergence problem. It exists so the engine can be tested and its contract
generated with no real mission present, and it deliberately has three tracks
rather than two so that any surviving assumption about the old shape fails
loudly. Do not draw an operational conclusion from a run against it.

The pack you actually run is **yours**, and does not need to live in this
repository: point `mission.dir` wherever you keep it. The engine's test suite
and published contract build against `reference`, which is what demonstrates
that the engine no longer assumes your mission rather than merely asserting it.
Each pack documents its own reasoning — its tracks, its weights, where its
relevance line sits — in its own `docs/`, because none of that is the engine's
to explain.

Writing your own: copy `missions/reference/`, change `id`, and edit. The loader
names every refusal, so the fastest way to learn the format is to run
`python run.py --config config.yaml serve` and read what it says.


## 4. Production vs debugging

The difference is one setting.

| | Production | Debugging |
|---|---|---|
| `api.debug_endpoints` | `false` | `true` |
| Stage stepping, stage inspection, discard-last-stage | not mounted | available |
| Recovery routes (`/v1/recovery`, resume, abandon) | **always available** | always available |

With `debug_endpoints: false` the debug routes are *not mounted at all* rather
than merely refusing — a deployment serving an operations team cannot delete
analytical
records through the API.

For development without spending anything, set `dry_run: true`. Connectors
return recorded fixtures through the real parsers and the budget ledger records
zero.

---

## 5. Initialize and start

Create the database and serve the API:

```bash
source scripts/load-secrets.sh # If not previously run
python run.py --config config.yaml init-db
python run.py --config config.yaml serve
```

`init-db` is idempotent — run it after any upgrade to apply migrations.

Confirm it is up (no token needed, should be run from a new shell):

```bash
curl -s localhost:8000/v1/healthz | python -m json.tool
```

`serve` accepts `--host` and `--port`, and the global `--database FILE`
overrides `database.path`.

---

## 6. Normal operations from the terminal

After launching the Surge server in a shell, it will block other commands
until you terminate it with `Ctrl+C`. Therefore the `curl` commands listed below
should be run in a different shell.

Set up that second shell in this order — **credentials first, then `AUTH`**:

```bash
source scripts/load-secrets.sh
export SURGE=http://localhost:8000
export AUTH="Authorization: Bearer $SURGE_API_TOKEN"
```

**The order is the whole point.** `export AUTH=...` captures the token's value
at the moment it runs. In a shell where `SURGE_API_TOKEN` is not yet set, `AUTH`
becomes the string `Authorization: Bearer ` with nothing after it, and every
request then fails with:

```json
{"detail":"Bearer token required."}
```

That is a correct 401 for an empty credential, but it reads like a *wrong* token
rather than an unset variable, and re-sourcing `load-secrets.sh` afterwards does
not fix `AUTH` — it was already expanded. Check it without printing the token:

```bash
[ -n "${SURGE_API_TOKEN:-}" ] && [ "$AUTH" != "Authorization: Bearer " ] \
  && echo "AUTH ok" || echo "AUTH is empty — re-source load-secrets.sh, then re-export AUTH"
```

The three ways the API refuses, so you can tell them apart:

| Response | Meaning |
|---|---|
| `401 Bearer token required.` | No `Authorization` header, an empty bearer, or a non-bearer scheme. Usually the ordering mistake above. |
| `401 Invalid token.` | A token was sent and does not match. |
| `503 SURGE_API_TOKEN is not set…` | The **server** has no token configured. It refuses to serve rather than serving unauthenticated. Nothing you send will help; fix the server's environment. |

`GET /v1/healthz` is the exception and needs no token, so it is the one call that
works before any of this — use it to confirm the server is up. Its `?deep=true`
form probes all four vendors and **does** require the token, because an
unauthenticated deep check would be a free way to exhaust the rate-limit budget
this system depends on.

### Generating a client from the contract

`docs/api/openapi.json` declares the bearer scheme as `BearerToken`, and every
operation except `GET /v1/healthz` declares that it requires it, so a generated
client sends the header once you configure the token in whatever the generator
calls its security configuration. (Before this was declared, a generated client
omitted the header and failed every operational request with a 401 it had no way
to anticipate.) Two things worth knowing:

- **`GET /v1/healthz` declares authentication as optional** — `security: [{}, {BearerToken: []}]`.
  Anonymous liveness works; pass the token when you want `?deep=true`.
- **Unknown request fields are rejected, not ignored.** A body with a misspelled
  key returns `422` with `"type": "extra_forbidden"` naming it. A typo used to be
  silently dropped, which meant a request could succeed while doing something
  other than what was asked.
- **Use `openapi-operational.json`** if the deployment runs with
  `api.debug_endpoints: false`. It is the same document minus the four stage
  routes, which are not mounted there and would 404.

### Check what the system can collect, before creating anything

```bash
curl -s -H "$AUTH" "$SURGE/v1/capabilities?city=Chicago%2C+IL" | python -m json.tool
```

Reports which signal families each jurisdiction supports. A city that cannot be
resolved is reported as unsupported rather than silently returning nothing.

### Create a session

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  "$SURGE/v1/sessions" -d '{
    "label": "AZ display season",
    "expand_cities": false,
    "tracks": ["AIRSHOW"],
    "cities": [{
      "name": "Phoenix", "state": "AZ",
      "key_locations": [
        {"name": "Riverside Fairground", "location_type": "FAIRGROUND"}
      ]
    }]
  }' | python -m json.tool
```

Returns `201` with the `session_id`, the airports and rental pickup point each
city resolved to, and `warnings` naming any city with no mapping.

`tracks` and `location_type` come from the **loaded mission**, not from this
contract — `GET /v1/capabilities` reports the live set under `mission`, and an
unknown value is refused with a 422 naming it. The values above are the
reference pack's; a deployment running a different pack sends different ones.
`expand_cities: true` lets the system admit additional cities when two
independent sources corroborate them.

Add `tunables` to run this session under different settings from the server's —
shaped exactly like `config.yaml`:

```json
"tunables": {"correlation": {"window_hours": 48},
             "budget": {"per_iteration_cap": {"FR24": 500}}}
```

Settable sections are `triage`, `sensitivity`, `correlation`, `windows`,
`tipping` and `budget`; anything else is a `422` naming the field, and a
spending cap may be lowered but never raised. The response echoes what was
accepted plus `config_hash` — the same value every receipt this session produces
carries, so you can confirm a judgement was made under the settings you asked
for. See [Per-session overrides](docs/config.md#per-session-overrides).

### Create a session from a file instead

Editing an input set beats pasting geography into a request body
for anything past one or two cities:

```yaml
Chicago, IL:
  - name: Northside Exhibition Center
    location_type: VENUE
  - Lakeside Arena                      # just a name is fine too

Atlanta, GA:
  - name: Riverside Fairground
    location_type: FAIRGROUND
```

Comment an entry out to exclude it. Then either:

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  "$SURGE/v1/sessions" \
  -d '{"label":"AZ display season","input_set":"example"}' | python -m json.tool
```

```bash
python run.py --config config.yaml session create --from jurisdictions --dry-run
```

`--dry-run` prints the resolved geography — canonical key, airports, pickup
point, key-location count per city — and writes nothing. Drop it to create.

> **A city the geo table cannot place refuses the whole request**, naming it.
> It is not skipped. A jurisdiction missing from a session produces no query,
> no refusal and no warning, so its absence would be indistinguishable from
> having looked and found nothing there.

`input_set` is a **name**, resolved inside `inputs.dir` — not a path. Pass
`cities` or `input_set`, never both.

### Trigger an iteration

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  "$SURGE/v1/sessions/1/iterations" -d '{"mode":"auto"}' | python -m json.tool
```

Returns `202` with a `poll_url` and the spend envelope. **Send an
`Idempotency-Key`** — this endpoint spends money, and with a key a retry after
a lost response replays the first result instead of starting a second run. The
reply carries `Idempotent-Replay: true` on a replay.

`"mode": "manual"` creates the iteration without running it, for stepping.

Add `?wait=true` to block up to `api.sync_timeout_s`; the run is never
abandoned if the wait expires.

A `409` means the session is busy. It carries `Retry-After` when waiting will
help, and omits it when an operator has to act first (see §8).

### Poll it

```bash
curl -s -H "$AUTH" "$SURGE/v1/iterations/1" | python -m json.tool
```

`status` is `PENDING`, `RUNNING`, `INTERRUPTED` or `FINISHED`. Read `counts`,
`budget`, and `degradations` — the last names anything the run could not do.

### See summary of iteration, stage by stage

```bash
curl -s -H "$AUTH" "$SURGE/v1/iterations/1/stages" | python -m json.tool
```

### Read alerts

```bash
curl -s -H "$AUTH" "$SURGE/v1/sessions/1/alerts?min_confidence=MEDIUM" | python -m json.tool
```

Filters: `since` (ISO-8601), `min_confidence` (`LOW|MEDIUM|HIGH`), `city`,
`actor_track`, `iteration_id`, `review_state`, and `format=tuple` for
positional 4-arrays of (social, flights, lodging, cars).

### Drill into the evidence behind an alert

```bash
curl -s -H "$AUTH" "$SURGE/v1/alerts/12/evidence" | python -m json.tool
```

Every contributing signal, the query that produced it, the per-family
arithmetic, the band rule that fired, and the classification receipt.

Two fields are worth reading before you act on the number:

- **`alternatives`** — what else would produce this evidence, derived from which
  signal families contributed. A booking-only correlation admits a convention or
  a home game; a military-flight one does not. Each entry says what in *this*
  correlation argues against it, and an empty `weakened_by` means nothing does.
- **`collection`** — the contributing signals counted by how directly they are
  known. `INTERMEDIARY_LIVE` is the ordinary case. `INTERMEDIARY_CACHED` means a
  vendor served a stored copy of unknown age and charged for it as fresh. There
  is no `DIRECT`: every provider here is an intermediary over a platform or a
  publisher, and that is a standing fact about the system rather than a property
  of one alert.

### Review an alert before escalating

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  "$SURGE/v1/alerts/12/review" \
  -d '{"review_state":"RELEASED","reviewed_by":"duty analyst","note":"corroborated"}'
```

`RELEASED`, `WITHHELD` or `UNREVIEWED`. This governs distribution only — the
score and evidence do not change. A consumer distributing onward should ask
for `?review_state=RELEASED`; the unfiltered listing returns everything so an
analyst can review it.

### See what was refused and why

```bash
curl -s -H "$AUTH" "$SURGE/v1/sessions/1/queue" | python -m json.tool
```

Counts by status and by refusal outcome — deduped, cooldown, capped, budget.

### Stop a running iteration

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  "$SURGE/v1/iterations/1/cancel" -d '{"requested_by":"analyst","reason":"wrong city"}'
```

Cooperative: it stops at the next stage boundary, still scores and alerts on
what was already collected, and closes `PARTIAL`.

### Add cities between iterations

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  "$SURGE/v1/sessions/1/cities" -d '{"cities":[{"name":"Tucson","state":"AZ"}]}'
```

---

## 7. The CLI

Everything above has a terminal equivalent that does not need the server.

```bash
python run.py --config config.yaml session create --from jurisdictions --dry-run
python run.py --config config.yaml session create --from jurisdictions
python run.py --config config.yaml iterate 1          # run one iteration
python run.py --config config.yaml iterate 1 --step   # one stage at a time
python run.py --config config.yaml retry-triage 1 --dry-run
python run.py --config config.yaml alerts 1 --min-confidence MEDIUM
python run.py --config config.yaml alerts 1 --json
```

`session create` also takes `--label`, `--tracks` (comma-separated, from the
loaded mission) and
`--expand`. Unlike the API it accepts a path as well as a name, because an
operator at a shell already has the filesystem.

Global flags: `--config FILE`, `--database FILE`.

---

## 8. Finishing an iteration that did not close

**One session runs one iteration at a time, and an iteration you did not finish
still counts.** A new one is refused with `409` until the old one is resumed or
abandoned. This is not tidiness: the cooldown guard is keyed on the query hash
across *all* iterations, so an outstanding run's recent queries would silently
suppress the new one's and it would under-collect with no visible cause.

Two ways an iteration ends up outstanding, with the same two remedies:

| `kind` | How it happened |
|---|---|
| `INTERRUPTED` | The process died mid-run. Detected at the next start. |
| `OPEN` | Nothing crashed — you created it with `mode: manual` and stopped stepping, discarded a stage and left it, or cancelled one that was not running. |

The `409` names the blocking iteration, which kind it is, and the two URLs that
close it. To list them yourself:

```bash
curl -s -H "$AUTH" "$SURGE/v1/recovery" | python -m json.tool
```

`blocking` is everything that would refuse a new iteration. `interrupted` is the
narrower question of what *this* process found dead at startup, so an iteration
you simply left open appears only in `blocking`.

From the terminal:

```bash
python run.py --config config.yaml recover
```

Lists outstanding iterations and the stage each stopped at. `--check` exits `1`
if anything needs a decision, which suits a supervisor script. `--json` for
machine output.

**Inspect before deciding.** This is read-only and names every query that would
be re-collected and every payload already paid for:

```bash
python run.py --config config.yaml resume 7 --dry-run
```

**Then either resume:**

```bash
python run.py --config config.yaml resume 7 --confirm
```

`--confirm` is required when the plan would re-collect at a vendor.
`--from-stage STAGE` overrides the derived resume point.

**Or abandon**, which scores and alerts on what *was* collected and closes the
iteration `PARTIAL`:

```bash
python run.py --config config.yaml abandon 7 --reason "operator closed it" --confirm
```

Over the API the same three are `GET /v1/recovery`,
`GET /v1/iterations/{id}/recovery-plan`, `POST /v1/iterations/{id}/resume` and
`POST /v1/iterations/{id}/abandon`. All four work on either kind.

> The `409` carries no `Retry-After`. Waiting never clears it — only resuming or
> abandoning does.

### Recovering judgements a model failure lost

If a triage batch overruns `llm.max_tokens`, **every** post in it is recorded
`MODEL_ERROR`. The posts were collected and paid for; only the judgement is
missing, and the iteration reports the gap in its `degradations`.

```bash
python run.py --config config.yaml retry-triage 7 --dry-run
python run.py --config config.yaml retry-triage 7
```

```bash
curl -s -X POST -H "$AUTH" "$SURGE/v1/iterations/7/retry-triage?wait=true" \
  | python -m json.tool
```

This **creates a new iteration**, a child of the old one, and never edits the
parent. The child re-judges only the unanswered posts, inherits the parent's
`anchor_at` so the correlation window still covers the evidence, and runs
tipping through alerting without re-collecting anything. Correlation reads
signals across the session, so the child scores both runs' evidence together.

Only `UNDECIDED`, `INVALID_OUTPUT` and `MODEL_ERROR` are retried. `ACCEPTED` and
`REJECTED` are finished judgements — a rejection is a conclusion, not a failure.
A post skipped for being older than `triage.max_post_age_hours` never got a
decision row, so it cannot be reached.

The retry halves its batch on each truncation, down to one, rather than
re-sending a batch that already proved too large.

> Finish, resume or abandon the parent first: a retry is a new iteration, and a
> session runs one at a time (§8 above).

---

## 9. Choosing what counts as relevant

```yaml
triage:
  require_nexus: true
```

Every mission supplies two relevance clauses, and this chooses between them:

| | |
|---|---|
| `true` (default) | The mission's **strict** criteria — `prompts/relevance-strict.md`. |
| `false` | The mission's **broad** criteria — `prompts/relevance-broad.md`. |

What each clause says is the mission's business, not the engine's. What the
engine guarantees is that the choice is recorded: the prompt hash, the prompt
version and the pack digest on every receipt say which criteria produced a
judgement, and triage logs a `WARNING` on every run under the broad leg.

A broad leg is a materially different instrument from a strict one, not a
slightly more sensitive version of it — typically far noisier, tipping paid
collection on many more findings. It is off unless someone turns it on, and
it is a per-session tunable so one session can widen the criteria without
changing the instrument for everyone.

---

## 10. Budget

Two limits apply per iteration, and the **lower** wins:

- `budget.per_iteration_cap.<PROVIDER>` — a hard ceiling
- a fair share of the month's remainder, sized by
  `budget.iterations_per_month_planned`

Seeding enqueues about **24 social queries per city**. If your fan-out exceeds
the envelope, collection is refused per query with a recorded reason — but
*which* queries follow queue order, so whole cities can collect nothing while
others collect in full. Seeding logs a `WARNING` naming the shortfall when this
is about to happen.

For a multi-city session, either raise `per_iteration_cap.APIDIRECT` above
`cities × 24`, or lower `iterations_per_month_planned` so the fair share stops
binding.

Spend so far:

```bash
curl -s -H "$AUTH" "$SURGE/v1/iterations/1" | python -c \
  "import json,sys; print(json.load(sys.stdin)['budget'])"
```

---

## 11. Reading results from the database

The database is plain SQLite. Nothing is hidden from a direct query.

```bash
sqlite3 surge_iw.db
```

The tables you will want:

| Table | Holds |
|---|---|
| `alerts` | The output: score, band, summary, caveat, review state |
| `correlations` | Per city and track: score, band, `contributions_json`, `data_completeness` |
| `signals` | Normalised evidence — social, flight, lodging, car |
| `triage_decisions` | One row per post judged, **including rejections** |
| `query_queue` | Every query, its status and skip reason |
| `queue_decisions` | Every refusal, with the rule that caused it |
| `raw_results` | Vendor payloads, with a retention deadline |
| `api_calls` | Per-call spend ledger |
| `agent_log` | Structured run log |
| `receipts` | How each judgement was reached |

**Alerts with their city:**

```sql
SELECT a.alert_id, c.name, a.actor_track, a.confidence_score,
       a.confidence_band, a.review_state, a.summary
FROM alerts a JOIN cities c ON c.city_id = a.city_id
WHERE a.session_id = 1
ORDER BY a.confidence_score DESC;
```

**Why a city scored what it did:**

```sql
SELECT c.name, co.actor_track, co.score, co.band, co.distinct_types,
       co.data_completeness, co.failed_sources, co.contributions_json,
       co.rule_trace
FROM correlations co JOIN cities c ON c.city_id = co.city_id
WHERE co.iteration_id = 1
ORDER BY co.score DESC;
```

**Evidence behind one alert:**

```sql
SELECT s.signal_type, s.url, s.salience, s.observed_at, cs.contribution
FROM alerts a
JOIN correlation_signals cs ON cs.correlation_id = a.correlation_id
JOIN signals s ON s.signal_id = cs.signal_id
WHERE a.alert_id = 12;
```

**What collection did not happen:**

```sql
SELECT source_type, status, skip_reason, COUNT(*) n
FROM query_queue WHERE iteration_id = 1
GROUP BY source_type, status, skip_reason;
```

**Posts that were judged and rejected** (the record of what was considered):

```sql
SELECT state, COUNT(*) FROM triage_decisions
WHERE iteration_id = 1 GROUP BY state;
```

**Spend by provider:**

```sql
SELECT provider, SUM(units) units, COUNT(*) calls
FROM api_calls GROUP BY provider;
```

Export anything as CSV:

```bash
sqlite3 -header -csv surge_iw.db \
  "SELECT * FROM alerts WHERE session_id = 1;" > alerts.csv
```

---

## 12. Reading a result correctly

> A score is only as meaningful as the mission behind it. If you are
> running the shipped `reference` pack, the numbers are arithmetic over
> uncalibrated placeholders and mean nothing operationally — see §3.1.

- **`data_completeness < 1.0` means collection was incomplete.** The band is
  capped and the alert carries a caveat naming the missing source. A missing
  signal family is never scored as an absence of threat.
- **`confidence_score` is not a probability.** Do not present it to a reader
  as one.
- **Evidence is weighted by age, on a curve set by the window.** A signal's
  weight is `correlation.decay_edge_weight ** (age / correlation.window_hours)`,
  so a 48-hour window decays steeply — a day-old signal is worth 0.32 — and a
  168-hour window shallowly, where the same signal is worth 0.72. The window is
  the one thing you set; the curve follows it, so narrowing the window for a
  tactical posture sharpens the decay to match instead of leaving the two to
  disagree. `rule_trace` names the curve that was applied. Set
  `decay_edge_weight: 1.0` to score every in-window signal at full weight, as
  releases before this did.
- **`UNREVIEWED` is not a verdict.** It means nobody has looked yet.
- A `CANDIDATE` signal is recorded and reviewable but does not score.
- `raw_results` rows are deleted at their retention deadline. The analytical
  record survives with a nulled pointer, so old alerts stay readable after the
  licensed payload is gone.
- No analytical decision without a database record. That includes decisions 
  **not** to act. A refused queue entry writes a
- `queue_decisions` row with the reason; a rejected social post writes a
- `triage_decisions` row with the rationale. A silently dropped signal is a bug.

---

## 13. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `401` on every call | `SURGE_API_TOKEN` not exported, or the `Authorization` header is missing. |
| `409` on trigger, with `Retry-After` | Another iteration is running for that session. Wait and retry. |
| `409` on trigger, no `Retry-After` | An **unfinished** iteration is outstanding — crashed or simply never finished. `GET /v1/recovery` lists them under `blocking`; the message names which and how to close it. See §8. |
| `422` on trigger | Session has no cities, is closed, or an `Idempotency-Key` was reused with a different body. |
| Triage skipped, no signals | No LLM client — check `GEMINI_API_KEY`. The run still collects and correlates. |
| Whole cities collected nothing | Budget envelope smaller than the fan-out. See §10. |
| A batch failed with a truncation error | Lower `triage.batch_size` or raise `llm.max_tokens`, then `retry-triage` the iteration to recover the lost judgements. See §8. |
| Alerts empty but the run found things | The correlations scored below `correlation.alert_min_score`. `GET /v1/iterations/{id}/correlations` shows each with its `alert_decision_reason`. |
| Scores lower than a previous release | Evidence is now weighted by age. `rule_trace` names the curve. Raise `correlation.decay_edge_weight` to keep more of the window's tail, or set it to `1.0` for the old behaviour. |
| A whole city correlated nothing, but signals exist | They are outside `correlation.window_hours`. Measured live: signals 74.2 hours old against a 72-hour window scored nothing at all. Widen the window — decay is what makes a wide one safe. |
| Posts collected but not judged | `GET /v1/iterations/{id}/stages/TRIAGING` → `skips`. `STALE` is the recency cut; a `PAYLOAD_*` reason means a vendor response was unreadable and every post in it was lost. |
| `/healthz` shows a connector unhealthy | That family's key is missing or the vendor is failing. Collection continues; the gap is recorded. |

Full run log for one iteration:

```sql
SELECT logged_at, agent, level, message FROM agent_log
WHERE iteration_id = 1 ORDER BY log_id;
```

Run the offline suite — no network, no credentials needed:

```bash
python -m pytest tests/ -q
```

---

## 14. How is Surge structured

### Architecture

The SQLite database is the communication bus. No agent calls another; each reads
its inputs from the database and writes its outputs there. The orchestrator
sequences them by passing an `iteration_id` — the integer is the entire payload.

```
             POST /v1/sessions/{id}/iterations      ← the only trigger
                            │
                            ▼
              ┌──────────────────────────────┐
              │    IterationOrchestrator     │   no LLM
              │  stage machine; the ONLY     │
              │  component that instantiates │
              │  agents                      │
              └──────────────┬───────────────┘
                             │ iteration_id only — never data
                             ▼
     ┌────────────────────────────────────────────────────┐
     │           SQLite  (the communication bus)          │
     │  sessions · cities · key_locations · query_queue · │
     │  raw_results · signals · correlations · alerts ·   │
     │  agent_log · api_calls · queue_decisions           │
     └──┬────────┬────────┬────────┬────────┬─────────────┘
        ▼        ▼        ▼        ▼        ▼
   Collection  Triage   Queue  Correlation  Alert
     (py)      (LLM)     (py)      (py)     (LLM)
```

### Module layout

```
missions/
├── reference/              Synthetic pack: what tests and the contract build against
└── <your-mission>/         The pack you run. Yours; may live anywhere.

surge_iw/
├── config.py               Configuration + credential loading; the mission layer
├── models.py               Dataclasses for the API boundary; Alert.as_tuple()
├── db/
│   ├── schema.sql          Full DDL
│   ├── enums.py            Frozensets mirroring every CHECK constraint.
│   │                       Mission vocabularies are NOT here — see services/mission.py
│   └── database.py         SurgeDB — the bus. Typed wrappers only, no SQL in agents
├── base/
│   ├── agent.py            BaseAgent (no LLM) / LLMAgent (adds a model client)
│   ├── connector.py        HTTP foundation: pacing, typed errors, call accounting
│   └── scoring.py          Deterministic correlation and confidence   ← no LLM
├── connectors/
│   ├── apidirect.py        Social media and news
│   ├── flightradar.py      Live and historical flights
│   ├── staying.py          Lodging availability (async job protocol)
│   ├── priceline.py        Rental car availability
│   └── registry.py         Construction + fixture-backed dry-run mode
├── agents/
│   ├── orchestrator.py     IterationOrchestrator — the stage machine   ← no LLM
│   ├── queueing.py         QueueAgent — tipping, queuing, scheduling   ← no LLM
│   ├── collection.py       CollectionAgent — drains the queue          ← no LLM
│   ├── triage.py           TriageAgent — judges social posts           ← LLM
│   ├── correlation.py      CorrelationAgent — scores every city/track  ← no LLM
│   ├── alerting.py         AlertAgent — writes the summary             ← LLM
│   └── triage_schema.py    The contract for model output — strict, versioned
├── services/
│   ├── mission.py          Loads, validates and hashes a mission pack
│   ├── budget.py           Spend accounting and runaway protection
│   ├── stages.py           Per-stage inspection and rollback (see below)
│   ├── geo.py              Place name → airport codes and car pickup points
│   ├── governance.py       Everything Surge knows about its API providers
│   ├── ratelimit.py        Handle API rate limits
│   ├── receipts.py         "Receipts" are records of model calls stored in the db
│   ├── recovery.py         Crash detection, resume and abandon (see below)
│   ├── retention.py        FR24's 30-day deletion requirement
│   ├── provenance.py       Publisher identity and claim independence
│   ├── facility.py         Facility matching, on geo.py's ladder
│   ├── redact.py           Credential scrubbing for anything that is persisted
│   └── sensitivity.py      Candidate vs confirmed signal; what may spend money
└── api/
    ├── app.py              FastAPI factory; everything hangs off app.state
    ├── routes.py           The endpoints, operational and debug
    ├── contract.py         Contract hardening for clients that retry
    ├── schemas.py          Pydantic request/response models → OpenAPI
    ├── runner.py           Worker pool and the one-iteration-per-session lock
    └── security.py         Bearer-token dependency
```

---

## 15. Reference

- Interactive API docs: `http://localhost:8000/docs` while serving
- Generated OpenAPI spec and worked request/response examples: `docs/api/`
- Per-provider terms, limits and retention: `docs/api/PROVIDERS.md`
- Every configurable parameter, one line each: [`docs/config.md`](docs/config.md)
- **Writing or editing a mission pack** — every file, every key, and every
  refusal the loader can produce: [`docs/missions.md`](docs/missions.md)
- Exactly what was sent to the model for one iteration, rebuilt and checked
  against the receipts:

  ```bash
  python scripts/reconstruct_prompts.py surge_iw.db 4 --out iteration-4.md
  ```
- What each setting DOES, inline: `surge_iw/config.py`. Note that the analytic
  defaults there are illustrative placeholders; why a *mission's* value is what
  it is lives in that pack's own `docs/`
- Why Surge behaves as it does — design decisions, the code review that reshaped
  the model boundary, what live running changed, and the open questions:
  [`docs/behaviors_scoring.md`](docs/behaviors_scoring.md). Engine only: what a
  particular mission concluded from those same runs is in that pack's `docs/`
