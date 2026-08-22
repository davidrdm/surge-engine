# Configuration reference

Every setting Surge reads, what it does, and its shipped default.

`surge_iw/config.py` holds the defaults as `DEFAULT_CONFIG`. `config.yaml` is
merged **over** them section by section, so you only write the keys you want to
change — an omitted key keeps its default, and an omitted section keeps all of
them. `config.example.yaml` is a commented starting point, not the full set;
several keys below appear only in `config.py`.

Reading the tables:

- **Default** is the value in `DEFAULT_CONFIG`. Some call sites carry a second
  fallback for direct-construction in tests; that fallback is unreachable
  through `load_config()` and is not what ships.
- Any key ending `_env` (and `api.token_env`) holds the **name of an
  environment variable**, never a credential. `require_api_key()` rejects a
  value that does not look like a variable name, so a pasted key fails at
  startup rather than silently at request time.
- Sections `database` and `api` are excluded from the classification
  receipt's `config_hash`; everything else is inside it, so a moved threshold is
  visible in the audit trail and a change of port is not.
- Six sections may be **overridden per session** through `tunables` on
  `POST /v1/sessions`. See [Per-session overrides](#per-session-overrides).

- Four sections — `triage`, `sensitivity`, `windows` and `correlation` — are
  the **mission's**, not the engine's. A mission pack sets them in its
  `thresholds:` block, layering above the defaults here and below your
  `config.yaml`. **The defaults shipped for those four are illustrative
  placeholders, not calibrated values**: internally consistent, in range, and
  chosen for nothing else. Their reasoning of record lives with the numbers, in
  the pack's own `docs/`. See [missions.md](missions.md).

For which settings you are likely to touch first, see [§3 of the
README](../README.md). For what a value DOES, `surge_iw/config.py` carries the
explanation inline; for why a mission's value is what it is, read that pack's
documentation.

---

## A key the engine does not read is refused

**`config.yaml` may only set keys that appear in this document.** Anything else
— a typo, a setting removed in an upgrade, a key renamed in one — is refused by
name at startup, and the process does not run:

```
config.yaml sets output, priceline.currency, which the engine does not read.
Refused rather than ignored: left in place it would be carried through, read by
nothing, and the deployment would run on the default while this file said
otherwise. Delete the key, or correct its spelling — a setting renamed in an
upgrade arrives here as an unknown one.
```

An unread key is the most dangerous kind of stale configuration precisely
because it is still *syntactically* fine. `deep_merge` carries it through,
nothing reads it, and the deployment runs on the default while the file says
otherwise. Measured on a live config after a rename: an operator had set the
old spelling of a triage setting to `false`, and after the rename the run would
have used the opposite behaviour — a materially different instrument, chosen by
nobody, with the file still saying `false`.

The rule is general rather than a list of known renames. A list has to be
maintained by whoever does the renaming, and it says nothing at all about a
plain typo. `DEFAULT_CONFIG` already enumerates every key the engine reads, so
a key absent from it is unread by construction.

**Upgrading?** Run the engine once against your existing `config.yaml` before
you rely on it. If a setting was removed or renamed, the refusal names it and
nothing starts until you edit the file — which is the intended outcome, and
better found on a console than in a quiet run.

Two boundaries apply the same rule for the same reason: `tunables` on
`POST /v1/sessions` (see [Per-session overrides](#per-session-overrides)) and
the mission loader (see [missions.md](missions.md)). Analytic thresholds belong
to a **mission pack**, not to this file; a pack setting a key the engine does
not read is refused the same way.

---

## Contents

[A key the engine does not read is refused](#a-key-the-engine-does-not-read-is-refused) ·
[Every window in one place](#every-window-in-one-place) ·
[`database`](#database) · [`llm`](#llm) · [`api`](#api) ·
[`triage`](#triage) · [`apidirect`](#apidirect) ·
[`flightradar`](#flightradar) · [`staying`](#staying) ·
[`priceline`](#priceline) · [`windows`](#windows) · [`tipping`](#tipping) ·
[`alerting`](#alerting) · [`sensitivity`](#sensitivity) · [`correlation`](#correlation) ·
[`budget`](#budget) · [`inputs`](#inputs) · [`mission`](#mission) · [`dry_run`](#dry_run) ·
[Per-session overrides](#per-session-overrides) ·


---

## Every window in one place

Twenty-two settings bound a span of time, spread across ten sections. This is
the index: what each one bounds, and where to find it.

**Four of them define what "warning" means, and they interact.** Widening one
alone opens a gap rather than closing one — evidence gets collected and judged
that can never score, or scores that can never buy follow-on collection. Both
have happened in live running:

| Setting | Default | Bounds |
|---|---|---|
| [`triage.max_post_age_hours`](#triage) | `168.0` | How old a collected post may be and still be **shown to the model**. Older ones are dropped before the call, with a `triage_skips` row each. |
| [`correlation.window_hours`](#correlation) | `168` | How old a signal may be and still **score**. Also sets the decay curve — see [Temporal decay](#temporal-decay-and-why-it-has-no-window-of-its-own). |
| [`sensitivity.tip_max_age_hours`](#sensitivity) | `48.0` | How recent a social signal must be to **buy paid collection**. Deliberately tighter than the scoring window: spending on old evidence and scoring it are different decisions. |
| [`sensitivity.max_future_skew_hours`](#sensitivity) | `6.0` | How far *ahead* of now a timestamp may be before the signal is held as CANDIDATE. Guards against a mis-parsed date reading as fresh. |

Set `max_post_age_hours` above `window_hours` and you pay to judge posts that
cannot reach a correlation. Set `window_hours` above `tip_max_age_hours` — the
shipped arrangement — and old evidence contributes to a score without being
able to tip, which is intended but worth knowing.

**The booking comparison.** Lodging and car signals are a near-term window
measured against a baseline window; these decide what is compared with what.

| Setting | Default | Bounds |
|---|---|---|
| [`windows.near_term_hours`](#windows) | `48` | Length of the near-term booking window, rounded to whole days. |
| [`windows.baseline_days`](#windows) | `[7, 14]` | How far forward the baseline windows sit. Both are the same weekday as the near window, so ordinary weekend demand is differenced out; the first to yield a usable paired sample wins. |
| [`staying.price_lead_days`](#staying) | `2` | Shifts **both** lodging windows forward. No listing quotes a check-in today or tomorrow. Shifting both equally preserves the weekday alignment. |
| [`priceline.rental_days`](#priceline) | `2` | Rental duration in the car query — the span priced, not a lookback. |
| [`flightradar.history_hours`](#flightradar) | `48` | How far back the flight-summary query looks. The only endpoint returning an aircraft category, and the most expensive call in the system. |

**Cooldown and provider-side filters.**

| Setting | Default | Bounds |
|---|---|---|
| [`tipping.cooldown_minutes`](#tipping) | `180` | How long before an identical query may run again. Keyed on the query hash across **all** iterations, so an unfinished run's executions suppress a new one's. |
| [`apidirect.news_time_published`](#apidirect) | `"7d"` | Recency filter applied by the **provider**, not by us. The only server-side one available; `1d` returned zero articles for this lexicon. |

**How long data is kept.** Ceilings, not preferences — `retention_days` is
capped by each provider's governance record and a config value can only shorten
it.

| Setting | Default | Bounds |
|---|---|---|
| [`flightradar.retention_days`](#flightradar) | `30` | Contractual. Values above 30 are clamped. |
| [`apidirect.retention_days`](#apidirect) | `90` | Raw payload retention. |
| [`staying.retention_days`](#staying) | `90` | Raw payload retention. |
| [`priceline.retention_days`](#priceline) | `90` | Raw payload retention. |
| [`staying.listing_set_ttl_days`](#staying) | `14` | How long the pinned listing set is reused. A set that shifted between windows would measure catalogue churn rather than availability. |
| [`api.idempotency_ttl_hours`](#api) | `24.0` | How long an `Idempotency-Key` replays its stored response. Past it, the same key is a new request. |

**How long the system waits.** Protocol and process timeouts; none of them
changes an analytical result.

| Setting | Default | Bounds |
|---|---|---|
| [`api.sync_timeout_s`](#api) | `600` | How long `?wait=true` blocks before falling back to the poll URL. The run is never cancelled. |
| [`api.busy_retry_after_s`](#api) | `60` | The `Retry-After` value sent with a busy-session `409`. |
| [`api.shutdown_timeout_s`](#api) | `30` | How long shutdown waits for an in-flight iteration before exiting hard. |
| [`staying.job_poll_max_s`](#staying) | `420` | How long to poll an asynchronous `/search` job. One city measured at 125 seconds. |

**The budget period.**

| Setting | Default | Bounds |
|---|---|---|
| [`budget.iterations_per_month_planned`](#budget) | `60` | How many iterations the month's allowance is divided across. Sets the fair-share half of the per-iteration envelope; the hard cap is the other half. |

---

## `database`

| Parameter | Default | What it does |
|---|---|---|
| `path` | `"surge_iw.db"` | SQLite file the whole system uses as its bus. `":memory:"` works but discards scheduled follow-ons, so it is for tests only. |

`run.py --database` overrides it for a single command.

---

## `llm`

| Parameter | Default | What it does |
|---|---|---|
| `model` | `"gemini-3.5-flash"` | Model id sent on every triage and alert call. |
| `base_url` | Google's OpenAI-compatible endpoint | API root. Any OpenAI-compatible endpoint works; pointing at a self-hosted model is a change to this line alone. |
| `api_key_env` | `"GEMINI_API_KEY"` | Name of the environment variable holding the model key. |
| `max_tokens` | `8192` | Output ceiling per call. |
| `temperature` | `0.2` | Sampling temperature. AlertAgent and TriageAgent both use it unless a call passes its own. |

**`max_tokens`.** A response that hits the ceiling raises `TruncatedResponse`
and is **never retried** — a truncated answer is not a transient fault, and
retrying it burns quota to fail identically. Its distinct error type exists
because the operator fix is theirs to make: lower `triage.batch_size` or raise
this. Conflated with malformed output, it once discarded 40 judged posts as
"invalid JSON".

**Why the default moved from 4096 to 8192.** Triage halves an over-long batch
and re-sends it rather than recording a coverage gap, so a ceiling that is too
low is a cost rather than a failure — which is exactly why it can sit there
unnoticed. Measured on a live two-city iteration: **three of ten triage calls
overran 4096**, and a fourth answered ten items whose answers the re-split then
discarded. Four of ten calls paid for and unusable. The split is a safety net,
not a plan.

---

## `api`

| Parameter | Default | What it does |
|---|---|---|
| `host` | `"127.0.0.1"` | Bind address. |
| `port` | `8000` | Bind port. |
| `token_env` | `"SURGE_API_TOKEN"` | Name of the environment variable holding the bearer token every request must present. |
| `sync_timeout_s` | `600` | How long `POST /iterations?wait=true` blocks before returning the poll URL instead. |
| `busy_retry_after_s` | `60` | The `Retry-After` value sent with a `409` from a session that already has an iteration running. |
| `idempotency_ttl_hours` | `24.0` | How long an `Idempotency-Key` replays its stored response. Past it, the same key is a new request. |
| `expose_raw_payloads` | `false` | Whether `GET /v1/alerts/{id}/evidence` may return raw vendor payloads and query parameters. |
| `max_workers` | `4` | Concurrent iterations across **different** sessions. |
| `uvicorn_workers` | `1` | uvicorn processes. Read only by `run.py serve`. |
| `shutdown_timeout_s` | `30` | How long shutdown waits for an in-flight iteration before giving up on it. |
| `debug_endpoints` | `true` | Mounts the stage-debugging routes. |

**`host`.** Alerts name specific facilities. Binding to `0.0.0.0` needs TLS and
a real identity layer in front of it.

**`sync_timeout_s`** bounds the *wait*, never the run. An iteration that has
already paid for collection always finishes; the client just stops holding the
connection open for it.

**`expose_raw_payloads`.** Off, the evidence surface is the normalised record.
What may be redistributed from a provider payload is an open question per
provider (`docs/api/PROVIDERS.md`), not something this endpoint should assume.

**`max_workers` is not the session lock.** One session runs one iteration at a
time regardless, enforced by a conditional `UPDATE` in the database.

**`uvicorn_workers`.** Since the session lock moved into the database, `>1` is
safe for correctness. Raise it deliberately anyway: sibling workers appear in
each other's startup reconcile as live process epochs it must refuse to touch,
so crash recovery cannot run while they are up.

**`shutdown_timeout_s`** is not politeness. Closing the SQLite connection under
a worker that is mid-statement segfaults the process.

**`debug_endpoints`.** Mounts `POST /v1/iterations/{id}/step`,
`GET /v1/iterations/{id}/stages[/{stage}]` and
`POST /v1/iterations/{id}/discard-last-stage`. When false the routes are **not
mounted at all** rather than refusing, so a discard that deletes analytical
records cannot be one config read away from firing. Set it false for a
deployment serving an operations team.

---

## `triage`

> **Mission-owned.** A pack sets this in its `thresholds:` block. The defaults
> below are illustrative placeholders — see [missions.md](missions.md).


| Parameter | Default | What it does |
|---|---|---|
| `batch_size` | `10` | Posts per model call. |
| `require_nexus` | `true` | Whether an item must satisfy a higher 
(more restrictive) standard to be judged relevant. |
| `min_salience` | `0.0` | Below this, the judgement is still recorded as a triage decision but no signal is written. |
| `max_post_age_hours` | `168.0` | Posts older than this are never sent to the model. |

**`batch_size`.** Larger batches cost fewer round trips but make one malformed
response lose more judgements at once, and push the reply toward
`llm.max_tokens`.

**`require_nexus`.** A switch between two instruments, not a
sensitivity dial. One is a more restrictive triage standard (i.e. the intersection of two
conditions). The other is a more relaxed triage standard.

| | |
|---|---|
| `true` | Used to apply a more restrictive triage standard.  |
| `false` | Broadens the standard to generate more tips. |

Under `false` volume rises, and Triage logs a
`WARNING` on every run in that mode, and each receipt's `prompt_version` and
`prompt_hash` record which criteria produced the judgement.

**`max_post_age_hours`** is a recency cut on collected posts, applied before the
model sees them. It exists because the median collected post measured 206 days
old and the oldest 2,165, while only 1% fell inside the correlation window;
judging that tail exhausted the model quota before the recent posts were
reached. It is deliberately the same width as `correlation.window_hours` —
see the note there.

---

## `alerting`

| Parameter | Default | What it does |
|---|---|---|
| `max_tokens` | `4096` | Output ceiling for the alert-summary call. Separate from `llm.max_tokens`. |

**Why 4096 for a 40-word summary.** On a model that reasons before answering,
the ceiling has to cover the *reasoning*, not the answer. Measured with the
current prompt: one summary came back at 44 output tokens and another still
overran a 1200 ceiling — the prose is tiny and the variance is entirely in what
the model does before emitting it.

**Overrunning it loses the summary completely**, rather than shortening it: the
JSON object is the last thing emitted, so a truncated reply has no `summary`
field at all and the alert falls back to a deterministic sentence with a null
receipt. Every alert in one real database was a fallback for exactly this
reason, at the previous hardcoded 400.

---

## `apidirect`

Social media and news. The only source seeded without a tip.

| Parameter | Default | What it does |
|---|---|---|
| `api_key_env` | `"APIDIRECT_API_KEY"` | Name of the environment variable holding the key. |
| `base_url` | `"https://apidirect.io"` | API root. |
| `platforms` | `["twitter", "reddit", "news"]` | Which platforms seeding queries. One query per platform, per lexicon group, per actor track. |
| `twitter_pages` | `2` | `pages` on the twitter and reddit post endpoints. |
| `news_limit` | `50` | `limit` on the news endpoint. |
| `news_time_published` | `"7d"` | Server-side recency filter, supported on the news endpoint only. |
| `get_sentiment` | `false` | Requests an eight-emotion vector alongside each post, on the twitter and reddit endpoints only. |
| `retention_days` | `90` | How long raw payloads are kept before purge. |

**`platforms` drives the fan-out.** Two actor tracks × four lexicon groups ×
three platforms is 24 social queries per city, which is what
`tipping.max_queries_per_city` and the budget envelope have to accommodate. A
name with no endpoint mapping is skipped silently, so a typo here reduces
coverage without any error — check the query count after changing it.

**`news_time_published`.** Measured live: `1d` returned **zero** articles for
this lexicon while twitter and reddit returned 240 posts. This is the only
endpoint with a real server-side recency filter, and set tight it contributed
nothing at all.

**`get_sentiment`** costs +$0.001/page for something that is not an I&W
indicator.

---

## `flightradar`

| Parameter | Default | What it does |
|---|---|---|
| `api_key_env` | `"FR24_API_KEY"` | Name of the environment variable holding the key. |
| `base_url` | `"https://fr24api.flightradar24.com"` | API root. |
| `sandbox` | `false` | Authenticates with `FR24_SANDBOX_KEY` instead, which returns static responses and costs no credits. |
| `max_airports_per_city` | `3` | How many airports a city resolves to, for both queuing and the geo lookup routes. |
| `history_hours` | `48` | Lookback span for a flight-summary query, sent as `flight_datetime_from`/`_to`. |
| `live_limit` | `20` | `limit` on the flight-positions endpoints. |
| `use_count_tripwire` | `false` | Runs a cheap `/count` query before committing to a full positions query. |
| `flight_count_threshold` | `1` | Records the tripwire must see before it escalates to the full query. |
| `retention_days` | `30` | Contractual deletion window. |
| `queries_per_minute` | `10` | Rate limiter, sustained. |
| `burst` | `1` | Rate limiter, back-to-back allowance. |

**`sandbox`** shares the production URLs and paths — only the key differs. A
sandbox response ignores query parameters, so it proves parsing and auth but
cannot prove a filter is correct.

**`live_limit` is the cost control.** FR24 bills per **record returned** (8
credits on `flight-positions/full`, 6 on `/light`, 3 on `flight-summary/full`),
so an unbounded query at a busy field is an unbounded charge. It is not sent on
flight-summary, where the parameter is inapplicable and `history_hours` bounds
cost instead.

**`use_count_tripwire` / `flight_count_threshold`.** Both `/count` endpoints
return `403 not permitted` on the Explorer tier, which is why the tripwire is
off. Left configurable for a higher plan; the collection order does not depend
on it.

**`retention_days` is a ceiling, not a preference.** FR24's licence forbids
retention beyond 30 days from receipt, and `services/governance.py` clamps any
larger configured value. A config file must not be able to buy a licence term.

**`queries_per_minute` / `burst`.** Both measured, not documented: the Explorer
tier's 10/min matches observation, and back-to-back probing found a burst
ceiling of exactly **one** — two calls 0.2s apart returned 429. `burst: 1`
forces even pacing rather than a permitted flurry followed by a wall. The same
two keys are read for every provider section, so either can be set on
`apidirect`, `staying` or `priceline` as well.

---

## `staying`

Lodging availability and, optionally, hotel prices. An OTA aggregator rather
than a hotel inventory API.

| Parameter | Default | What it does |
|---|---|---|
| `api_key_env` | `"STAYING_API_KEY"` | Name of the environment variable holding the key. |
| `base_url` | `"https://api.stayingapi.com/v1"` | API root. |
| `platforms` | `["airbnb"]` | Which OTA the availability path queries. **Only the first entry is used.** |
| `listing_set_size` | `40` | Listings `/search` pins per location. 40 is the documented maximum. |
| `min_paired_listings` | `3` | Listings that must appear in **both** windows or the lodging availability signal is dropped. |
| `listing_set_ttl_days` | `14` | How long a pinned listing set is cached before it is re-resolved. |
| `job_poll_max_s` | `420` | How long the connector polls an asynchronous job before giving up. |
| `enable_price_compare` | `false` | Enables the lodging **price** sub-signal via `/price-compare` in direct mode. |
| `price_batch_size` | `6` | Listing ids per call. The endpoint takes 2–6. |
| `price_max_listings` | `6` | How much of the pinned set to price. Cost is linear in this. |
| `price_lead_days` | `2` | Days both price windows are shifted forward. |
| `retention_days` | `90` | How long raw payloads are kept before purge. |

**`base_url`.** The `/v1` is not optional — the published OpenAPI paths are
relative to it and the bare host 404s on everything.

**`platforms`.** airbnb only, and this is measured rather than preferred: vrbo
returns every availability flag `False` on every date even 30 days out, and
booking's availability job ends in state `failed`. A platform that always
reports `False` would not merely add noise, it would dilute a real drop toward
zero.

**`listing_set_size` and its TTL.** Coverage is sparse — roughly 1 in 15 airbnb
listings returns calendar data at all — so the set has to be wide to yield a
usable paired sample. Pinning it is also what makes the two windows comparable:
a free-text re-resolve could return different listings per window, measuring the
gap between listings rather than a change in one. `/search` is asynchronous and
slow (one city measured at 125 seconds against a self-reported estimate of 45),
so the cache keeps that cost off the iteration path.

**`min_paired_listings`.** A drop computed from one or two listings is
arithmetic, not evidence.

**`enable_price_compare` — now sound, and still off by default.** It runs in
**direct mode** over the same pinned listing set the availability path uses, so
the two lodging sub-signals measure the same properties by construction. Google
mode was measured and abandoned: it resolved one location string to a different
property in each window — Omni in one, Marriott in the other, and one facility
to a rental in Puerto Vallarta — echoed the query string back as
`property` so the mismatch was concealed, and never returned the id the pinning
depended on. It also cost 30 credits a call, charged even on a cache hit.

Measured live in direct mode: **4 of 6 listings priced in each window and all 4
paired**, for **3 credits per call regardless of whether it carries 2 listings or
6** — 6 credits a location for both windows, against Google mode's 60. It stays
off by default because enabling it is a spend decision, not a correctness one.

**`price_lead_days`.** No listing in the sample could be quoted for a check-in
today or tomorrow — the call fails `all_actors_failed` and charges nothing —
while +2 days priced normally. Both windows are shifted equally so the weekday
alignment `windows.baseline_days` exists for is preserved; the cost is that the
price sub-signal describes a horizon two days later than the availability one.

**`price_max_listings` is not the whole set**, unlike the availability path. A
price quote needs no calendar data, so the yield is far higher — 4 in 6 here
against roughly 1 in 40 for availability — and 6 listings clear
`min_paired_listings` with margin. Raise it deliberately: cost is linear.

---

## `priceline`

Rental car availability.

| Parameter | Default | What it does |
|---|---|---|
| `api_key_env` | `"PRICELINE_RAPIDAPI_KEY"` | Name of the environment variable holding the RapidAPI key. |
| `host` | `"priceline-com2.p.rapidapi.com"` | RapidAPI host — both the hostname called and the `X-RapidAPI-Host` header. |
| `pickup_time` | `"12:00"` | `pickUpTime` sent with every search, 24-hour `HH:MM`. |
| `dropoff_time` | `"12:00"` | `dropOffTime` sent with every search. |
| `rental_days` | `2` | Rental length. Sets `dropOffDate` from the window's pickup date. |
| `requests_per_second` | `5` | Rate limiter. ULTRA tier; PRO is 3/sec and MEGA is 10/sec. |
| `retention_days` | `90` | How long raw payloads are kept before purge. |

**`host`.** The upstream is Priceline either way; this selects the reseller.
`priceline8` began returning zero inventory for every airport and window, and
`priceline-com2` serves the same data under the same rights.

**`requests_per_second`.** All RapidAPI tiers here are hard-capped with zero
overage, so exhaustion cuts you off rather than billing you.

---

## `windows`

> **Mission-owned.** A pack sets this in its `thresholds:` block. The defaults
> below are illustrative placeholders — see [missions.md](missions.md).


The two comparison windows every availability measurement is built from.

| Parameter | Default | What it does |
|---|---|---|
| `near_term_hours` | `48` | Length of the near-term window — the horizon "imminent" refers to. |
| `baseline_days` | `[7, 14]` | Day offsets at which baseline windows are measured. |

**`baseline_days`.** Both offsets are multiples of 7 so each baseline falls on
the same weekday as the near window, and a weekend or a convention does not read
as a surge. Divergence between the two is itself informative, and both are
stored. Lodging **availability** measures every offset in the list; lodging
**price** and **car** use the first entry only.

---

## `tipping`

Fan-out control for the queue. Every refusal here is written to
`queue_decisions` with its rule code, so a query that was not run is
distinguishable from one that found nothing.

| Parameter | Default | What it does |
|---|---|---|
| `max_queries_per_iteration` | `500` | Queries one iteration may hold before further enqueues are refused as `CAP_ITERATION`. |
| `max_queries_per_city` | `36` | Same, per city, refused as `CAP_CITY`. |
| `max_tip_depth` | `3` | How far a tip chain may extend before `CAP_DEPTH`. |
| `cooldown_minutes` | `180` | An identical query (same dedup key) run more recently than this is refused as `COOLDOWN`. |
| `max_locations_per_city` | `4` | Key locations per city that may generate tipped lodging and car queries. |
| `min_independent_domains` | `2` | Independent publishers a city needs before it can be admitted by expansion. |
| `min_expansion_salience` | `0.6` | Salience floor for the same. |
| `max_expanded_cities` | `5` | Ceiling on cities admitted by expansion in one session. |

**The two caps are runaway guards, not the budget.** They are sized *above* the
natural fan-out on purpose: at the earlier value of 12,
`max_queries_per_city` bound during ordinary seeding and — measured live —
refused every query belong to a specific track, so that track was never collected
at all. A guard that fires in the ordinary case is not a runaway guard. What should
bound a real run is [`budget`](#budget), which refuses per query with a recorded
reason rather than simply stopping.

**`max_tip_depth`.** The chain is social → flight count → flight full →
lodging. Nothing chains further, so 3 is the natural depth rather than a
throttle.

**The three expansion keys apply only when a session sets
`expand_cities: true`**, which is off by default. Requiring corroboration from
two independent publishers stops one viral post from steering collection. City
admission is the *only* thing this gate protects — it does not apply to
operational signals for a city that already exists.

---

## `sensitivity`

> **Mission-owned.** A pack sets this in its `thresholds:` block. The defaults
> below are illustrative placeholders — see [missions.md](missions.md).


The line between an observation worth recording and one worth acting on.

    CANDIDATE   recorded, visible in the evidence trail, reviewable.
                Does not score and cannot tip.
    CONFIRMED   scores, and may spend money.

| Parameter | Default | What it does |
|---|---|---|
| `confirm_min_salience` | `0.35` | Salience a signal must reach to be CONFIRMED and score. Below it, it is recorded as a CANDIDATE with the reason attached. |
| `tip_min_salience` | `0.5` | Salience a signal must reach to book **paid** follow-on collection. |
| `tip_require_timestamp` | `true` | Whether an undated signal may tip. |
| `tip_max_age_hours` | `48.0` | How old an observation may be and still justify buying collection. |
| `max_future_skew_hours` | `24.0` | How far into the future a claimed observation time may sit before it is treated as unusable rather than prescient. |

**These are interim values.** The decision was to build the distinction and the
adversarial measurement first, and set the floors from data rather than
intuition. Do not describe them as calibrated.

**`tip_min_salience` is deliberately higher than `confirm_min_salience`**,
because the failure modes differ in kind. A weak signal that *scores* gives an
reader a LOW alert they can dismiss. A weak signal that *tips* spends FR24
credits that bill per record returned and cannot be refunded.

**`tip_max_age_hours` is deliberately NOT aligned with
`correlation.window_hours`,** which is 168. Scoring on week-old evidence and
*spending* on it are different decisions: a stale report can still contribute to
a picture, but it must not buy fresh collection whose value depended on the
report being current. Widening this to match the correlation window is a
decision to let a six-day-old post book paid queries — make it deliberately or
not at all.

**`tip_require_timestamp`.** A signal with no usable timestamp cannot be placed
in the correlation window, so collection bought on it is money spent on evidence
that structurally cannot score. Turning this off is only coherent alongside a
change to how undated signals correlate.

---

## `correlation`

> **Mission-owned.** A pack sets this in its `thresholds:` block. The defaults
> below are illustrative placeholders — see [missions.md](missions.md).


The deterministic scoring model. No LLM touches any of this.

| Parameter | Default | What it does |
|---|---|---|
| `window_hours` | `72` | How long before the anchor a signal may be observed and still score. |
| `radius_km` | `25.0` | How far from a key location a signal may sit and still anchor a correlation spatially. |
| `lodging_drop_full_scale` | `50.0` | Percentage availability drop that saturates the lodging sub-signal at quality 1.0. |
| `car_drop_full_scale` | `50.0` | The same, for rental car availability. |
| `price_escalation_full_scale` | `50.0` | Percentage price rise that saturates the lodging price sub-signal. |
| `flight_full_scale` | `3.0` | Aircraft count that saturates flight quality. |
| `social_domains_full_scale` | `3.0` | Independent publishers that saturate social quality. |
| `single_source_quality` | `0.6` | Quality credited to a single independent observation. |
| `on_airport_weight` | `1.5` | Multiplier applied to on-airport car counters when computing the capacity-weighted drop. |
| `band_high_min_score` | `0.75` | Score floor for a HIGH band. |
| `band_high_min_types` | `3` | Distinct signal types required alongside it. |
| `band_medium_min_score` | `0.5` | Score floor for MEDIUM. |
| `band_medium_min_types` | `2` | Distinct signal types required alongside it. |
| `band_low_min_score` | `0.2` | Score floor for LOW, which also requires a spatial anchor or 2 distinct types. |
| `flight_excess_full_scale` | `100.0` | Percentage rise over a city's normal flight count that saturates the family. **INTERIM** — 100 means "double the normal", chosen to be legible rather than measured. |
| `flight_baseline_min_samples` | `3` | Prior iterations needed before a flight baseline is trusted. Below it the absolute count stands and the correlation records `UNBASELINED`. |
| `flight_baseline_window_days` | `30` | How far back baseline samples are drawn from. |
| `band_low_min_reports` | `2` | Independent **reports** LOW requires, from any family or families. **A single report cannot alert** (owner decision, 9.8). Reports, not families: social contributes the lower of distinct publishers and distinct claims (one wire story in three mastheads is one report); flights contribute distinct airframes; lodging and car contribute one each, being a single measurement over a set however large. |
| `alert_min_score` | `0.2` | Score below which AlertAgent writes no alert at all. |
| `decay_edge_weight` | `0.1` | Weight a signal still carries at the far edge of the window. `1.0` disables decay. See below. |

### Temporal decay, and why it has no window of its own

Evidence is weighted by age:

```
weight(age) = decay_edge_weight ** (age / window_hours)
```

**The curve is a function of `window_hours` on purpose.** The window already
states how far back evidence is relevant; a second, independent decay setting
could contradict it, and a curve chosen on its own would silently re-narrow a
window that had been widened deliberately. So you set one thing, and the shape
follows:

| `window_hours` | posture | half-life | a 24-hour-old signal is worth |
|---|---|---|---|
| `48` | tactical warning of an imminent deployment | 14.4 h | 0.32 |
| `72` | current default | 21.7 h | 0.46 |
| `168` | situational awareness of an established operation | 50.6 h | 0.72 |

The half-life is always `0.301 × window_hours`, so the curve is steep in
absolute time for a short window and shallow for a long one — which is the
distinction between "is something about to happen" and "what is going on".

`decay_edge_weight` is expressed as the weight at the edge rather than as a
half-life because it also sets the size of the discontinuity that remains.
Decay does not remove the hard cutoff: `window_hours` is still what bounds the
query. It shrinks the cliff at the boundary from 1.0 to this value, and `0.1`
is the default for that reason and not from any measurement — it makes the
residual step 10%.

**The value is interim, in the same sense as the `sensitivity` floors.** What it
does to a real correlation, measured against 125 stored signals aged 74–168
hours at a 168-hour window:

| `decay_edge_weight` | half-life | weight at 74 h | score, band |
|---|---|---|---|
| `1.0` (off) | — | 1.00 | 0.776 **HIGH** |
| `0.5` | 168 h | 0.74 | 0.558 MEDIUM |
| `0.3` | 97 h | 0.59 | 0.467 MEDIUM |
| `0.2` | 72 h | 0.49 | 0.401 LOW |
| `0.1` | 51 h | 0.36 | 0.278 LOW |

Read the top row as the argument for having decay at all: without it,
three-to-eight-day-old evidence produced a HIGH-confidence warning of an
imminent deployment. Read the spread as the reason the exact value is a
decision — raise it if a week-long window should keep more of its tail.

Settable per session, so one client can ask for a sharper instrument than the
server's default without changing it for anyone else.

**None of this is calibrated, and the score is not a probability.** There is no
labelled ground truth for "a surge was in fact imminent". The weights are a
starting hypothesis. Do not present the number to a decision-maker as a
likelihood.

**`window_hours` must move together with `triage.max_post_age_hours`.** At 48
against a 168-hour recency cut, posts were collected, judged and turned into
signals that could never reach a correlation — measured live in New York, where
two CONFIRMED reports of a mission activity scored nothing because they were five
and six days old. Aligning them changes what the score *means*: a week-old
report now counts toward "imminent", which is closer to situational awareness
over a week than to a 48-hour tactical horizon. Narrow **both** to get the
tactical instrument back; narrowing either alone re-opens the gap.

**`single_source_quality` is a floor, not a curve point.** A bare
`count / full_scale` would give a lone credible report 0.33, which understates
what one named source from an established outlet is worth — that is STANDARD
confidence in IC terms, not a third of a signal. With the floor at 0.6 and
`full_scale` at 3, the curve runs 1→0.6, 2→0.8, 3+→1.0.

**`on_airport_weight`.** Airport fleets book out first, so an on-airport
counter's inventory is the more sensitive indicator.

**The band gates are conjunctive.** A score alone never produces HIGH; it needs
`band_high_min_types` distinct signal types with it. This is what stops a single
loud family — social chatter, or a general-aviation airport's flight density —
from carrying a band on its own.

---

## `budget`

Spend accounting and runaway protection. Units are provider-native: FR24 counts
**credits**, the others count requests.

| Parameter | Default | What it does |
|---|---|---|
| `iterations_per_month_planned` | `60` | Divides the month's remaining allowance into per-iteration fair shares. |
| `hard_stop_pct` | `0.9` | Fraction of a monthly limit past which only high-priority queries may still spend. |
| `reserved_priority_ceiling` | `20` | Past the hard stop, only queries at or below this priority number may spend. Lower numbers are more urgent. |
| `per_iteration_cap.<PROVIDER>` | `APIDIRECT 60`, `FR24 4000`, `STAYING 400`, `PRICELINE 200` | Hard ceiling on one iteration's spend with that provider. |
| `monthly_limit.<PROVIDER>` | `APIDIRECT 3000`, `FR24 30000`, `STAYING 20000`, `PRICELINE 100000` | The month's total allowance. |

**Two limits apply per iteration and the lower wins:** the hard
`per_iteration_cap`, and a fair share of what remains this month, sized by
`iterations_per_month_planned` and the days left in the month.

**`iterations_per_month_planned` is a trap on large sessions.** Measured across
seven metros: the fair share came out at 73 against 168 planned queries, so
three cities collected in full, one collected a single query, and three
collected **nothing** — and which three depended on queue order, not on anything
analytical. Lower it (or raise the monthly limit) whenever
`cities × 24` exceeds `monthly_limit / iterations_per_month_planned`. Seeding
logs a `WARNING` naming the shortfall when this is about to happen.

**`monthly_limit.STAYING` is a starting value only.** `reconcile_staying()`
overwrites it from the free `GET /account`, which is authoritative — the live
account was found on the free plan with 221 credits against a configured 20,000,
and one `/search` costs 36.

**A refusal is recorded, never silent.** Each carries a reason
(`MONTHLY_QUOTA_EXHAUSTED`, `HARD_STOP_PRIORITY`,
`ITERATION_ALLOCATION_EXHAUSTED`), so "we ran out of budget" and "the endpoint
is broken" stay distinguishable downstream.

---

## `inputs`

| Parameter | Default | What it does |
|---|---|---|
| `dir` | `"./inputs"` | Directory `POST /v1/sessions` searches when a request names an `input_set`. |

**The API takes a name, not a path.** `{"input_set": "example"}` resolves
to `<dir>/example.yaml`; anything containing a separator or `..` is
refused. A path field on an authenticated endpoint is still a file-disclosure
primitive — authenticated is not the same as trusted with the filesystem, and
the caller is a front end rather than the operator at a shell. `run.py session
create --from` does accept a path, because an operator running it already has
the filesystem.

A file is loaded all-or-nothing: a city the geo table cannot place refuses the
whole request, naming it. See README §6.

---

## `mission`

| Parameter | Default | What it does |
|---|---|---|
| `dir` | `"./missions"` | Directory the mission packs live in. |
| `name` | `"reference"` | Which pack to load. A bare directory name inside `dir`, never a path. |

The engine collects and correlates. The **mission** says what it is looking
for, and it is read from a pack of files at startup rather than compiled in:
the tracks, the search lexicon, the two system prompts, the scoring weights,
the track-to-flight-category map, and the analytic thresholds.

**The pack is the audit unit.** `mission.yaml` declares its members; the loader
refuses a declared file that is missing *and* an undeclared file that is
present, then hashes every member into a single `digest`. That digest is
reported by `GET /v1/capabilities` and stamped on every receipt, so a judgement
names the exact bytes that produced it. A file that is neither loaded nor
hashed would be a change nothing records, which is what the digest exists to
prevent. Anything under the pack's `docs/` is exempt — a pack carries its own
prose, and requiring every note to be declared would make writing one a schema
change.

**Unknown is refused, never ignored** — the same rule as per-session tunables.
A lexicon whose track name is misspelled is an error naming the key, not a
silent no-op. The failure that prevents is specific: a misspelled track would
search nothing, score nothing, and report a quiet city, which is
indistinguishable from a city where nothing is happening.

**A mission cannot be set per session.** It is in `SERVER_OWNED`, because a
request that could switch it would change the tracks, the lexicon, the prompts
and the weights at once — and every alert in the database would then be scored
under a definition chosen per request rather than by the operator.

**The shipped `reference` pack is synthetic and uncalibrated.** It is a benign
crowd-convergence problem, and it exists so the engine can be tested and its
contract generated with no real mission present. It deliberately has three
tracks rather than two, so that any surviving assumption about the old
two-track shape fails loudly rather than passing by luck. Point `name` at your
own pack before drawing any operational conclusion from a run.

Configuring no mission at all (`name: ""`) is legitimate: the database opens
and the contract is served, but an iteration cannot run, because nothing in the
engine supplies a default for what a mission defines.

---

## `dry_run`

| Parameter | Default | What it does |
|---|---|---|
| `dry_run` | `false` | Top-level. Swaps the HTTP transport for one that serves recorded fixtures, and records zero budget units. |

The real connector classes, parsers and validators still run — only the network
is replaced — so a dry run exercises the same code paths production uses. No
credentials are read at all, so a demo works with an empty `.env`, and no rate
limiting is applied, so an iteration does not take minutes to read local files.
An unmapped path returns 404 rather than an empty success: a dry run that
answered everything with "nothing found" would mask a wiring mistake as a quiet
lack of intelligence.

---

## Per-session overrides

`POST /v1/sessions` accepts a `tunables` object shaped exactly like this file —
`{"correlation": {"window_hours": 48}}` — and every iteration of that session
runs under the result. The server's configuration is the base; the session's
overrides are merged onto it once per iteration and used by every stage, the
budget guard, retention and every receipt. `SessionOut` returns the merged
`config_hash`, which is the same value each receipt carries, so a client can
confirm after the fact that a judgement was made under the settings it asked for.

**Settable:** `triage`, `sensitivity`, `correlation`, `windows`, `tipping`,
`budget` — every key documented in those sections.

**Refused, with a 422 naming the field:**

| What | Why |
|---|---|
| `database`, `api`, `inputs` | Deployment settings. |
| `llm`, `alerting` | The model, its parameters and its credential are the server's. |
| `apidirect`, `flightradar`, `staying`, `priceline` | Provider endpoints, credentials, rate limits and retention ceilings. |
| `dry_run` | A session that could set it would receive fixture data indistinguishable from collection it had paid for. |
| Any unknown section or key | A misspelled `triage.max_post_age` is refused with a message naming `max_post_age_hours`. A setting accepted and then ignored is worse than one refused. |

**Ceilings may only come down.** `budget.per_iteration_cap`,
`budget.monthly_limit`, `tipping.max_queries_per_iteration`,
`tipping.max_queries_per_city`, `tipping.max_tip_depth`,
`tipping.max_locations_per_city` and `tipping.max_expanded_cities` are refused if
they exceed the server's value, and clamped again at merge time — so lowering a
cap in this file is obeyed by sessions created before the change.

---

## Nothing is undocumented

Every key in `DEFAULT_CONFIG` is read by something. Five that were not —
`priceline.currency`, `correlation.lodging_drop_min`, and the whole `output`
section (`log_dir`, `export_dir`, `log_level`) — were **removed** rather than
described, along with a stale doc entry for `staying.hotel_set_size`, a key that
no longer existed at all.

`correlation.lodging_drop_min` was the one that mattered: it was also in the
per-session `tunables` allowlist, so a client could set it, receive a 201, and
have it hashed into `receipts.config_hash` while nothing read it — the defect
class issue #11 was about. It is now refused by name like any other unknown
field.

`tests/test_tunables.py::TestNothingSettableIsInert` asserts the property
rather than the list: every key a client may set must be read somewhere in
`surge_iw/`, `scripts/` or `run.py`.
