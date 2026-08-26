"""Configuration loading and the LLM client factory.

Derived from iw/iw_agents/config.py: a DEFAULT_CONFIG dict, a deep merge over a
YAML file, and an OpenAI-compatible client built from a base_url plus a key read
from a NAMED environment variable.

The api_key_env convention is load-bearing and easy to get wrong. It holds the
*name* of an environment variable, never a key. iw/config.yaml has live API keys
pasted into these fields; the code then looks up an environment variable named
after the key, finds nothing, and falls back to a placeholder — so the keys
leaked into git without ever working. require_api_key() below refuses to accept
a value that looks like a key rather than a variable name, so that mistake fails
loudly at startup instead of silently at request time.
"""
from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import yaml

# 9.1. Imported at module level deliberately: redact has no dependency of its
# own beyond the standard library, so there is no cycle, and a lazy import
# inside load_config() would be one more place the installation could be
# skipped. The whole defect this fixes was that installation was somebody
# else's job.
from .services.redact import install as install_redaction

DEFAULT_CONFIG: dict[str, Any] = {
    "database": {
        # File-backed by default: iterations are separate API calls and
        # scheduled follow-ons must survive between them. Use ":memory:" only
        # for tests.
        "path": "surge_iw.db",
    },
    "llm": {
        # Verified against the provider's own model list, not assumed. The
        # previous default `gemini-3.1-pro` does not exist at this endpoint —
        # only `gemini-3.1-pro-preview` does — so a fresh install with no
        # config.yaml failed every model call with a 404, and the failure
        # surfaced as "triage degraded" rather than as a configuration error.
        "model": "gemini-3.5-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        # 8192, not 4096. Measured on a live two-city iteration: three of ten
        # triage calls overran a 4096 ceiling and were re-sent at half the
        # batch size, and a fourth answered ten items whose answers the
        # re-split then discarded — four of ten calls paid for and unusable.
        # The split is a safety net, not a plan. A reasoning model spends the
        # ceiling on reasoning before it writes a word of the answer, so the
        # ceiling has to cover both.
        #
        # The other lever is `triage.batch_size`, which a MISSION sets: fewer
        # items per call means a shorter answer. This one is the operator's,
        # because it is a property of the endpoint rather than of the mission.
        "max_tokens": 8192,
        "temperature": 0.2,
    },
    "api": {
        "host": "127.0.0.1",          # localhost only; alerts name real facilities
        "port": 8000,
        "token_env": "SURGE_API_TOKEN",
        "sync_timeout_s": 600,
        # 8.2. How long a client told 409-busy should wait before retrying.
        # Config rather than a constant: the honest number is "about as long as
        # an iteration takes", which varies with cities and vendors.
        "busy_retry_after_s": 60,
        # How long an Idempotency-Key can replay its response. Generous
        # relative to how long a client could plausibly still be retrying;
        # past it, the same key is a new request.
        "idempotency_ttl_hours": 24.0,
        # Whether the evidence endpoint may return raw vendor payloads and
        # query parameters. Off by default: the evidence surface is the
        # NORMALISED record, and what may be redistributed from a provider
        # payload is 8.3's question rather than this endpoint's assumption.
        "expose_raw_payloads": False,
        # Concurrent iterations across DIFFERENT sessions. One session is
        # limited to one iteration by a lock regardless of this.
        "max_workers": 4,
        # uvicorn processes. 1 by default — not because the session lock needs
        # it any more (8.6 moved that into the database), but because sibling
        # workers appear in each other's startup reconcile as live epochs it
        # must refuse to touch, so crash recovery cannot run while they are up.
        "uvicorn_workers": 1,
        # How long shutdown waits for a running iteration. Closing the SQLite
        # connection under a worker that is mid-statement segfaults the
        # process, and an iteration that has already paid for collection
        # should finish and record what it bought.
        "shutdown_timeout_s": 30,
        # Mounts /v1/iterations/{id}/step, /stages and /discard-last-stage.
        # On by default because a PoC is developed by stepping; a deployment
        # serving an operations team should set it false. The routes are not
        # mounted at
        # all when this is off, so a disabled discard endpoint cannot be one
        # config read away from deleting analytical records.
        "debug_endpoints": True,
    },
    # Connector credentials, again as variable NAMES.
    "apidirect": {
        "api_key_env": "APIDIRECT_API_KEY",
        "base_url": "https://apidirect.io",
        "platforms": ["twitter", "reddit", "news"],
        "twitter_pages": 2,
        "news_limit": 50,
        # 7d, not 1d. Measured live: all four news queries returned ZERO
        # articles at 1d while twitter and reddit returned 240 posts. This is
        # the only endpoint with a real server-side recency filter, and set
        # this tight it contributed nothing at all.
        "news_time_published": "7d",
        "get_sentiment": False,        # +$0.001/page for a non-indicator
        "retention_days": 90,
    },
    "flightradar": {
        "api_key_env": "FR24_API_KEY",
        "base_url": "https://fr24api.flightradar24.com",
        "sandbox": False,
        "max_airports_per_city": 3,
        "history_hours": 48,
        # MEASURED live, not estimated. FR24 bills per record returned:
        #   live/flight-positions/full   8 credits/record
        #   live/flight-positions/light  6 credits/record  (no category, no ETA)
        #   flight-summary/full          3 credits/record  (the only category source)
        # One 24h flight-summary query at LAX cost 60 credits for 20 records, so
        # history is the dominant expense and is now only run when there is
        # something to resolve.
        #
        # `limit` is supported on flight-positions endpoints and caps records
        # returned, which is the only real cost control available there.
        "live_limit": 20,
        # The /count endpoints would have been the cheap tripwire, but BOTH
        # return 403 "You are not permitted to access this endpoint" on this
        # subscription. Left configurable in case a higher tier enables them; the
        # collection order does not depend on it.
        "use_count_tripwire": False,
        "flight_count_threshold": 1,
        # FR24 licence: data must not be retained beyond 30 days from receipt.
        "retention_days": 30,
        # Observed 429 at roughly 10 requests/minute, which matches the Explorer
        # tier rather than the assumed Essential. Conservative by design: a 429
        # is a lost collection window, and the cost of pacing is only latency.
        "queries_per_minute": 10,
        # Measured burst ceiling of exactly one: two calls 0.2s apart returned
        # 429. Without this the limiter satisfies the documented per-minute rate
        # and still trips the real one on the first stage of collection.
        "burst": 1,
    },
    # One of the four MISSION-settable analytic sections — see the note above
    # `windows` below. The measurements cited here are about vendor data rather
    # than about any mission, which is why they stayed.
    "triage": {
        # Posts per LLM call. Larger batches cost fewer round trips but make a
        # single malformed response lose more judgements at once.
        "batch_size": 10,
        # Whether an item must satisfy the mission's NEXUS to be relevant.
        #
        # The mission supplies two relevance clauses and this chooses between
        # them: true runs `prompts/relevance-strict.md`, false runs
        # `prompts/relevance-broad.md`. What each says is the mission's
        # business — the engine only picks the leg and records which one ran,
        # in the prompt hash and the prompt version on every receipt.
        #
        # Which leg to run is an ANALYST'S decision. A broad leg is a
        # materially different and noisier instrument than a strict one, not a
        # slightly more sensitive version of it, so triage logs a WARNING on
        # every run where this is false rather than letting it pass quietly.
        #
        # Per-session tunable, so one session can widen the criteria without
        # changing the instrument for everyone.
        "require_nexus": True,
        # Posts below this salience are still recorded as triage decisions —
        # the judgement stays on the record — but produce no scored signal.
        "min_salience": 0.0,
        # Posts older than this are never sent to the model. Measured live: the
        # MEDIAN collected post was 206 days old and the oldest 2,165, while
        # only 1% fell inside the 48-hour correlation window. Judging that tail
        # exhausted the model quota before the recent posts were reached, and
        # the single signal it produced was a tweet from 2020.
        #
        # Wider than the 48h correlation window on purpose: a few days of
        # context is legitimately useful, and the cut only has to remove a tail
        # measured in years.
        "max_post_age_hours": 168.0,
    },
    "alerting": {
        # Output ceiling for the summary call, separate from `llm.max_tokens`
        # because the two calls want opposite things: triage returns a verdict
        # per post in a batch and needs room, an alert is two sentences.
        #
        # It was hardcoded at 400 and every live alert overran it — in a real
        # database, all five alerts across three iterations carry
        # `model/fallback` and a NULL receipt, so no alert prose in that run was
        # model-written at all. The ceiling is not the whole fix: the prompt was
        # tightened at the same time. This is the headroom, not the instruction.
        #
        # 4096 for a summary of about 40 words, because on a model that reasons
        # before answering the ceiling must cover the REASONING, not the answer.
        # Measured: with the tightened prompt one summary came back at 44 output
        # tokens and another still overran 1200 — the prose is tiny and the
        # variance is entirely in what the model does before emitting it. That
        # is also why the JSON has to be the first token: a truncated reply
        # loses the answer completely rather than shortening it.
        "max_tokens": 4096,
    },
    "staying": {
        "api_key_env": "STAYING_API_KEY",
        # /v1 verified live: the OpenAPI paths are relative to this base, and
        # the bare host 404s.
        "base_url": "https://api.stayingapi.com/v1",
        # airbnb ONLY, and this is not a preference. Measured against the live
        # API on the Starter plan:
        #   airbnb   per-date `available` varies correctly (near 0/3 vs
        #            baseline 2/3 on the same listing) — the signal works
        #   vrbo     every flag False on every date, even 30 days out, which is
        #            not credible and would silently produce drop_pct = 0
        #   booking  the availability job ends in state 'failed'
        # Including a platform that always reports False would not merely add
        # noise; it would dilute a real drop toward zero.
        "platforms": ["airbnb"],
        # Coverage is sparse: only about 1 in 15 airbnb listings returns calendar
        # data at all, so the listing set has to be wide to yield a usable
        # paired sample. 40 is the documented maximum for /search.
        "listing_set_size": 40,
        # A drop computed from one or two listings is arithmetic, not evidence.
        # CollectionAgent (Phase 3) drops the lodging signal below this many
        # listings paired across both windows.
        "min_paired_listings": 3,
        # /search is asynchronous and slow — one city measured at 125 seconds,
        # against a self-reported estimate of 45. Caching the listing set for a
        # fortnight keeps that cost off the iteration path entirely, and the
        # fixed set is what makes the two windows comparable in the first place.
        "listing_set_ttl_days": 14,
        "job_poll_max_s": 420,
        # Hotel PRICE signal via /price-compare in Google mode — the only
        # route to actual hotels rather than short-term rentals. Off until
        # the per-call credit cost is measured: the lodging path already
        # runs two windows per key location and Staying bills per leg.
        # 8.11. The price sub-signal, in DIRECT mode over the same pinned
        # listing set the availability path uses. Google mode was measured and
        # abandoned: it resolved one location string to a different property in
        # each window and never returned the id the pinning depended on.
        "enable_price_compare": False,
        # `/price-compare` takes 2-6 listing ids per call and — measured live —
        # charges 3 credits per CALL whether it carries 2 or 6. The batch size
        # is therefore a pure cost saving, and 6 is the documented maximum.
        "price_batch_size": 6,
        # How much of the pinned set to price. NOT the whole set: measured
        # live, 6 listings priced 4 in each window and all 4 paired, which
        # clears `min_paired_listings` with margin for 6 credits a location.
        # Pricing all 40 would cost 42 a location for a proportionally larger
        # sample of a measurement that already had one. Cost is linear in this
        # number, so raise it deliberately.
        #
        # Note the contrast with availability, where the set has to be wide
        # because only ~1 listing in 15 returns calendar data at all. A price
        # quote needs no calendar, so the yield is far higher — 4 in 6 here
        # against 1 in 40 there.
        "price_max_listings": 6,
        # Days to shift BOTH price windows forward. Measured live: no listing
        # in a 6-listing sample could be quoted for a check-in today or
        # tomorrow — the call fails and charges nothing — while +2 priced
        # normally. Shifting both windows equally keeps the weekday alignment
        # `windows.baseline_days` exists for; the cost is that the price
        # sub-signal describes a horizon this many days later than the
        # availability one.
        "price_lead_days": 2,
        "retention_days": 90,
    },
    "priceline": {
        "api_key_env": "PRICELINE_RAPIDAPI_KEY",
        # 8.5. priceline8 began returning zero inventory for every airport
        # and window; priceline-com2 serves the same Priceline upstream
        # unchanged. Same data, same rights, different reseller.
        "host": "priceline-com2.p.rapidapi.com",
        "pickup_time": "12:00",
        "dropoff_time": "12:00",
        "rental_days": 2,
        "requests_per_second": 5,      # ULTRA tier; PRO is 3, MEGA is 10
        "retention_days": 90,
    },
    # ------------------------------------------------------------------
    # ANALYTIC SETTINGS — placeholders, not calibrated values.
    #
    # These four sections are the MISSION's. A pack sets them in its
    # `thresholds:` block, and what is here is what a deployment falls back to
    # when a pack leaves one out: internally consistent, in range, and chosen
    # for nothing else.
    #
    # They used to be ONE mission's measured values, which meant a deployment
    # pointed at a different question inherited that mission's calibration —
    # silently. A pack's reasoning of record now lives with its numbers, in its
    # own `docs/thresholds.md`.
    #
    # The comments below say what each key DOES, because whoever writes a pack
    # needs to know that. They deliberately no longer say why a value is what
    # it is: an engine cannot know that.
    # ------------------------------------------------------------------
    "windows": {
        # How far ahead "imminent" reaches. Drives the tipping horizon.
        "near_term_hours": 48,
        # Weekday-aligned comparison points for the booking families, in days
        # before the anchor. Weekday alignment is what differences out ordinary
        # weekly demand, so offsets should stay multiples of 7.
        "baseline_days": [7, 14],
    },
    "tipping": {
        # 200 was sized for a 1-2 city session. Measured across seven metros:
        # seeding alone enqueues 24 per city = 168, leaving almost no headroom
        # before tipping, and a session of nine cities would be truncated by
        # this cap before a single paid follow-on was considered. Raised so the
        # BUDGET is what bounds a run, not an incidental fan-out counter — the
        # budget refuses per query with a recorded reason, this cap just stops.
        "max_queries_per_iteration": 500,
        # Sized to the natural fan-out, not below it. The fan-out is, per
        # city, the sum over the mission's streams of (lexicon groups across
        # all tracks) x (that stream's effective platforms) — one implicit
        # stream over every configured platform when the pack declares none —
        # plus the tipped follow-ons. A mission with a larger lexicon or more
        # streams needs a larger cap. Measured live at the previous value of 12: the
        # cap bound during NORMAL seeding and refused every query for one whole
        # track, so that track was never collected at all. A guard that fires
        # in the ordinary case is not a runaway guard.
        "max_queries_per_city": 36,
        "max_tip_depth": 3,            # social -> count -> full -> lodging
        "cooldown_minutes": 180,
        "max_locations_per_city": 4,
        # City expansion gate. Corroboration from two independent domains stops
        # one viral post from steering collection.
        "min_independent_domains": 2,
        "min_expansion_salience": 0.6,
        "max_expanded_cities": 5,
    },
    "sensitivity": {
        # Two gates on what a collected item may do, deliberately separate.
        #
        # `confirm_*` decides whether an item SCORES. Below it the judgement is
        # still recorded — the row exists, visible to a reviewer — but it does
        # not contribute and cannot tip.
        #
        # `tip_*` decides whether an item may SPEND, and is the stricter of the
        # two on purpose: FR24 bills per record returned and cannot refund a
        # query bought on a weak signal.
        "confirm_min_salience": 0.35,
        "tip_min_salience": 0.5,
        # An item with no usable timestamp cannot be placed in a window, so it
        # must not buy collection on the assumption that it is recent.
        "tip_require_timestamp": True,
        "tip_max_age_hours": 48.0,
        # A timestamp further ahead than this is treated as unusable rather
        # than as a forecast. Vendors do emit future-dated records.
        "max_future_skew_hours": 24.0,
    },
    "correlation": {
        # The scoring window. Evidence older than this does not contribute at
        # all; inside it, `decay_edge_weight` sets how fast it fades.
        #
        # Keep this and `triage.max_post_age_hours` aligned. If triage admits
        # items older than the window scores, they are judged, stored, and then
        # structurally unable to reach a correlation — evidence collected, paid
        # for, judged, and unusable. Widening this one alone changes what the
        # score MEANS, from a tactical horizon toward situational awareness.
        #
        # `sensitivity.tip_max_age_hours` is deliberately NOT tied to it:
        # scoring on older evidence and SPENDING on it are different decisions.
        "window_hours": 72,
        # How close a signal must be to a registered facility to count as
        # anchored there. An unanchored contribution is halved.
        "radius_km": 25.0,
        # Denominators mapping a percentage drop onto 0..1 quality: the drop at
        # which the sub-signal saturates.
        "lodging_drop_full_scale": 50.0,
        "car_drop_full_scale": 50.0,
        # Percentage price rise that saturates the price sub-signal.
        "price_escalation_full_scale": 50.0,
        # Aircraft count that saturates flight quality, and distinct publishers
        # that saturate social breadth.
        "flight_full_scale": 3.0,
        "social_domains_full_scale": 3.0,
        # Quality credited to a single independent observation. One named
        # source from an established outlet is STANDARD confidence in IC terms,
        # not a third of a signal — tradecraft rather than a mission judgement,
        # which is why a value sits here at all. See
        # scoring.corroboration_quality.
        "single_source_quality": 0.6,
        # Airport rental fleets book out before off-airport ones, so an
        # on-airport drop is the leading indicator. Also tradecraft.
        "on_airport_weight": 1.5,
        # The band ladder: a score floor and a minimum number of distinct
        # families for each rung. `*_min_types` cannot exceed 4, there being
        # four families. MEDIUM additionally requires a strong anchor.
        "band_high_min_score": 0.75,
        "band_high_min_types": 3,
        "band_medium_min_score": 0.50,
        "band_medium_min_types": 2,
        "band_low_min_score": 0.20,
        # LOW additionally requires this many INDEPENDENT reports, counted as
        # reports rather than families: one report can never alert, however
        # strong, and several reports within one family can.
        "band_low_min_reports": 2,
        # Below this a correlation is recorded and does not become an alert.
        "alert_min_score": 0.20,
        # Temporal decay: the weight an observation carries at the far edge of
        # the window. 1.0 disables decay. Expressed relative to the window, so
        # narrowing `window_hours` steepens the curve automatically and the two
        # cannot drift apart.
        "decay_edge_weight": 0.1,
        # Flight baselining: how far a category's count must exceed its rolling
        # median to saturate, how many prior samples make a usable baseline,
        # and how far back to look for them.
        "flight_excess_full_scale": 100.0,
        "flight_baseline_min_samples": 3,
        "flight_baseline_window_days": 30,
    },
    "budget": {
        # Divides the month's remaining allowance into per-iteration fair
        # shares, and `plan_iteration` takes the LOWER of that and the hard cap.
        #
        # 60 is right for a small session run daily. It is a trap for a large
        # one: measured across seven metros, the fair share came out at 73
        # against 168 planned queries, so three cities collected in full, one
        # collected a single query and three collected NOTHING — and which
        # three depended on queue order, not on anything analytical. Lower this
        # (or raise the monthly limit) whenever cities x 24 exceeds
        # monthly_limit / iterations_per_month_planned, or the run silently
        # measures whichever cities the queue reached first.
        "iterations_per_month_planned": 60,
        "hard_stop_pct": 0.9,
        "reserved_priority_ceiling": 20,
        "per_iteration_cap": {
            "APIDIRECT": 60.0,
            "FR24": 4000.0,
            "STAYING": 400.0,
            "PRICELINE": 200.0,
        },
        # Starting values only. BudgetGuard.reconcile_staying() overwrites the
        # Staying figure from GET /account, which costs nothing and is
        # authoritative — the live account was found on the free plan with 221
        # credits against a configured 20,000, and one /search costs 36.
        "monthly_limit": {
            "APIDIRECT": 3000.0,
            # FR24 Explorer: 30,000 credits/month, 10 queries/minute, 30 days of
            # history. The measured 10/min throttle matches this tier exactly,
            # and Explorer is also why the /count endpoints return 403.
            # At 8 credits per live record and 3 per historical one, 30,000
            # credits is roughly 40 full iterations over five cities — the
            # binding constraint on how often this system can run.
            "FR24": 30000.0,
            "STAYING": 20000.0,
            "PRICELINE": 100000.0,
        },
    },
    # 8.7(c). Where `POST /v1/sessions` looks for a named input set. The API
    # takes a NAME and resolves it inside this directory — never a path, so an
    # authenticated caller cannot read an arbitrary file. The CLI accepts a
    # path, because an operator running it already has the filesystem.
    "inputs": {
        "dir": "./inputs",
    },
    # Which mission this deployment is running, and where the packs live.
    #
    # The engine collects and correlates; the mission says what it is looking
    # for — the tracks, the search lexicon, the system prompts, the scoring
    # weights, and the analytic thresholds. A pack is read once at startup and
    # hashed into every receipt, so a judgement names the exact definition that
    # produced it.
    #
    # `name` is a bare directory name resolved inside `dir`, never a path, for
    # the same reason `inputs` works that way. The shipped `reference` pack is
    # synthetic and uncalibrated: it exists so the engine can be tested and its
    # contract generated with no real mission present. Point this at your own
    # pack before drawing any operational conclusion from a run.
    "mission": {
        "dir": "./missions",
        "name": "reference",
    },
    # dry_run swaps in fixture-backed connectors and records zero budget units.
    # This is what a front end develops against and what a demo runs on.
    "dry_run": False,
}

# Heuristic for "this looks like a secret, not a variable name". Environment
# variable names are upper snake case; API keys are long and mixed-case, often
# with a vendor prefix and dots or dashes.
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ConfigError(ValueError):
    """Raised for a configuration mistake that must not be worked around."""


#: Every key `config.yaml` may set is defined by `DEFAULT_CONFIG` above, and
#: anything else is REFUSED by name. See `_refuse_unknown()`.


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `override` onto `base`, returning a new dict.

    Values taken from `override` are deep-copied. Without that, a key present in
    `override` but absent from `base` is inserted by reference, so mutating
    config["tipping"]["max_tip_depth"] on the returned dict would reach back and
    modify DEFAULT_CONFIG for the whole process. That is how one caller's
    override silently becomes every later caller's default.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_config(path: str | Path | None = "config.yaml",
                *, mission: Any = None) -> dict[str, Any]:
    """Load configuration, merging a YAML file over the defaults.

    The result is always a fresh deep copy, so callers may mutate it freely.

    Loading also **installs credential redaction** (9.1). That is a side effect
    in a function named `load_`, and it is deliberate. `redact.install()`
    documented that it must run once at startup before any connector is built,
    and no entry point called it — so the exact-value layer, the one that
    catches a key however it was embedded, protected nothing in production. The
    fix is not another call site to remember but the removal of the choice:
    every path that reaches a credential goes through here first, because the
    config is what names the variables the credential lives in.

    Ordering is the point. This runs before `require_api_key` reads a token,
    before `SurgeDB` is opened and can log, and before any connector is
    constructed — those all take a config, and this is where the config comes
    from. Nothing is printed or returned but a count; the values are the thing
    being protected.
    """
    config = deep_merge({}, DEFAULT_CONFIG)
    if mission is not None:
        # Layer TWO: the mission's analytic thresholds. Above the engine's
        # illustrative defaults and below the operator's file, so a deployment
        # can still override a calibrated value — but never by accident, since
        # `mission_overrides()` names every key where that happened.
        config = deep_merge(config, mission.thresholds)
    if path is not None:
        file_path = Path(path)
        if file_path.exists():
            with open(file_path, encoding="utf-8") as handle:
                user_config = yaml.safe_load(handle) or {}
            if not isinstance(user_config, dict):
                raise ConfigError(f"{file_path} must contain a YAML mapping.")
            _refuse_unknown(file_path, user_config)
            config = deep_merge(config, user_config)
    # Defaults alone already name every credential variable, so this matters
    # even on the path=None branch a test or a bare install takes.
    install_redaction(config)
    return config


def _refuse_unknown(path: Path, user_config: Mapping[str, Any]) -> None:
    """Refuse a setting the engine does not read, naming it.

    A key the engine has no use for is the most dangerous kind of stale
    configuration, because it is still SYNTACTICALLY fine: `deep_merge`
    carries it through, nothing reads it, and the deployment runs on the
    default while the file says otherwise. Measured on a live config after a
    9.x rename — an operator had set the OLD spelling of a triage setting to
    `false`, and after the rename the run would have used the opposite
    behaviour, chosen by nobody, with the file still saying `false`.

    This refuses on the general rule rather than on a list of renames.
    A list has to be maintained by whoever does the renaming, names the very
    strings a rename exists to retire, and says nothing about a plain typo.
    `DEFAULT_CONFIG` already enumerates every key the engine reads, so a key
    absent from it is unread by construction — which is the same rule the
    per-session tunables and the mission loader apply, on the one boundary
    that was still missing it.
    """
    def walk(user: Mapping[str, Any], default: Mapping[str, Any],
             prefix: str = "") -> list[str]:
        found: list[str] = []
        for key, value in user.items():
            if key not in default:
                found.append(f"{prefix}{key}")
            elif isinstance(value, Mapping) and isinstance(default[key], Mapping):
                found += walk(value, default[key], f"{prefix}{key}.")
        return found

    unknown = sorted(walk(user_config, DEFAULT_CONFIG))
    if unknown:
        raise ConfigError(
            f"{path} sets {', '.join(unknown)}, which the engine does not "
            f"read. Refused rather than ignored: left in place it would be "
            f"carried through, read by nothing, and the deployment would run "
            f"on the default while this file said otherwise. Delete the "
            f"key, or correct its spelling — a setting renamed in an upgrade "
            f"arrives here as an unknown one.\n\n"
            f"Analytic thresholds belong to the MISSION PACK, not to this "
            f"file; see docs/missions.md.")


def load_with_mission(
    path: str | Path | None = "config.yaml",
) -> tuple[dict[str, Any], Any]:
    """Load configuration and the mission it names, layered correctly.

    Two passes, because the mission's location is itself configuration: the
    first finds `mission.dir` and `mission.name`, and the second rebuilds the
    merge with the pack's thresholds in the middle. Rebuilding rather than
    merging on top is the point — merging the mission over an already-merged
    config would put the pack ABOVE the operator's file, which is backwards.
    """
    from .services import mission as mission_service

    bootstrap = load_config(path)
    loaded = mission_service.load_configured(bootstrap)
    if loaded is None:
        return bootstrap, None
    return load_config(path, mission=loaded), loaded


def mission_overrides(
    config: Mapping[str, Any], mission: Any
) -> list[str]:
    """Dotted keys where the operator's file overrode the mission's value.

    Reported at startup and into the iteration's record. A mission's thresholds
    are calibrated judgements with reasoning behind them; replacing one locally
    is legitimate and silently replacing one is not, which is the difference
    this makes visible.
    """
    if mission is None:
        return []
    out: list[str] = []
    for section, values in (mission.thresholds or {}).items():
        live = (config.get(section) or {})
        for key, value in (values or {}).items():
            if key in live and live[key] != value:
                out.append(f"{section}.{key}: mission {value!r} -> "
                           f"configured {live[key]!r}")
    return sorted(out)


def require_api_key(config: dict[str, Any], section: str) -> str:
    """Read a credential via the variable named in config[section]['api_key_env'].

    Raises if the configured value looks like a secret rather than a variable
    name, or if the variable is unset. Both are startup failures by design: an
    unauthenticated connector that returns empty results would be scored as
    "no signal", which for a warning system is the dangerous direction.
    """
    settings = config.get(section) or {}
    var_name = settings.get("api_key_env")
    if not var_name:
        raise ConfigError(f"config[{section!r}]['api_key_env'] is not set.")
    if not _ENV_NAME_RE.match(var_name):
        raise ConfigError(
            f"config[{section!r}]['api_key_env'] = {var_name[:8]}... does not look "
            "like an environment variable name. This field holds the NAME of a "
            "variable (e.g. 'FR24_API_KEY'), never the key itself. If you pasted "
            "a credential here, rotate it — it may have been committed."
        )
    value = os.environ.get(var_name, "")
    if not value:
        raise ConfigError(
            f"Environment variable {var_name} is not set "
            f"(required by config[{section!r}])."
        )
    return value


def build_llm_client(config: dict[str, Any]):
    """Construct an OpenAI-compatible client.

    The OpenAI SDK is used for every provider so that production can point at a
    self-hosted open-weight model by changing base_url alone. Imported lazily so
    that Phase 1, which has no LLM dependency, runs without the openai package
    installed.
    """
    import openai  # noqa: PLC0415 — deliberately lazy

    llm = config["llm"]
    return openai.OpenAI(
        api_key=require_api_key(config, "llm"),
        base_url=llm.get("base_url") or None,
    )
