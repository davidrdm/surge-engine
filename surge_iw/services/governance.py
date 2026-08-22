"""Provider governance — what each vendor gives us, costs, and permits (8.3).

Everything the system knows about a provider that is not a request parameter:
what a billing unit means, what it does when it fails, what identifiers it
accepts, whose data it is actually serving, how long we may keep it, and which
fields may leave the building.

**The central distinction, and the reason this is code rather than a document.**
Two kinds of claim live here and they are never mixed:

  MEASURED   we observed it against the live API and can observe it again
  ASSERTED   it comes from the vendor's terms or documentation

`rights_verified` is False on every provider and always will be. No API call can
establish a downstream-use right — a 200 means the vendor served the bytes, not
that we may redistribute them. **Vendor intermediation is not proof of
downstream rights**: all four of these providers are intermediaries, so the
content is a platform's or a publisher's, and their terms bind us whatever the
aggregator's do. Similarly the development ceiling is not spend authorisation.

The per-field policy is enforced rather than described: `retention_days` is the
authority `retention.py` defers to, `strip_for_storage` runs before a payload is
written, and `withheld_from_evidence` is what the 8.2 evidence surface consults.
A record that only described the rules would drift from them within a phase.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

RULES_VERSION = "governance/1"

#: Retention when a provider states no limit of its own. Not a licence
#: permission — a default we chose, and the shortest that keeps a 14-day
#: baseline window usable.
DEFAULT_RETENTION_DAYS = 30


@dataclass(frozen=True)
class ProviderPolicy:
    """One provider's governance record."""

    provider: str
    families: tuple[str, ...]
    #: What one unit of `api_calls.units` means. These differ per provider and
    #: getting it wrong is how a ledger silently under-counts by 80x.
    unit: str
    unit_basis: str
    #: None means the vendor states no limit and DEFAULT_RETENTION_DAYS applies.
    #: A number here is a CEILING config cannot raise.
    retention_days: int | None
    retention_basis: str
    #: What the vendor accepts as a key for the thing we are asking about.
    identifiers: tuple[str, ...]
    #: Whose data this actually is, and through whom it reaches us.
    provenance: str
    #: HTTP status -> what it means here. Distinguishing these is a correctness
    #: requirement: a 402 is not a 429, and neither is "no results".
    failure_modes: dict[str, str] = field(default_factory=dict)
    rate_limits: dict[str, Any] = field(default_factory=dict)
    #: Field names never written to raw_results, at any nesting depth. These are
    #: not secrets — the redactor handles those — but material we have no
    #: analytical use for and no business storing.
    drop_before_storage: tuple[str, ...] = ()
    #: Stored for audit, never returned by the API.
    withhold_from_evidence: tuple[str, ...] = ()
    #: Observed against the live API. Re-measurable, and dated where it matters.
    measured: dict[str, Any] = field(default_factory=dict)
    #: Taken from the vendor's terms. NOT verified by anything we ran.
    asserted: dict[str, Any] = field(default_factory=dict)
    #: Always False. See the module docstring — this is not an oversight.
    rights_verified: bool = False
    #: The rights question, stated so it cannot be lost.
    downstream_rights: str = ""
    fixtures: tuple[str, ...] = ()


POLICIES: dict[str, ProviderPolicy] = {
    "APIDIRECT": ProviderPolicy(
        provider="APIDIRECT",
        families=("SOCIAL",),
        unit="request",
        unit_basis="One unit per HTTP request. Twitter bills per PAGE, so a "
                   "pages=2 call is two billable pages inside one request — "
                   "the ledger counts requests and would under-count a "
                   "multi-page call.",
        retention_days=None,
        retention_basis="No retention term published by the vendor, so the "
                        "window is OURS to choose rather than one we were "
                        "granted. The shipped config asks for 90 days — long "
                        "enough that a 14-day baseline stays re-derivable.",
        identifiers=("free-text query", "post url"),
        provenance="Aggregator. The posts are X/Twitter's, Reddit's and news "
                   "publishers' content; API Direct is an intermediary and "
                   "its terms do not displace theirs.",
        failure_modes={
            "401": "bad or missing key — raises, never an empty list",
            "429": "rate limited — not observed at our volume",
            "5xx": "vendor fault — raises; the query is FAILED and becomes a "
                   "coverage gap",
            "200 with []": "a genuine absence of results, and the ONLY case "
                           "that may be read as one",
        },
        rate_limits={"published": None,
                     "measured": "no throttling observed at 48 queries/run"},
        measured={
            "cost_per_page_usd": 0.006,
            "social_yield": "407 posts returned, 10 inside a week (~2%)",
            "news_time_published": "1d returned ZERO for this lexicon; 7d works",
            "twitter_time_filter": "none exists; sort_by=most_recent only",
        },
        asserted={"plan": "pay-per-request"},
        downstream_rights=(
            "UNRESOLVED. Post text and author handles are third-party content "
            "redistributed through an aggregator. The system returns snippets "
            "in alert evidence because the snippet IS the evidence — an alert "
            "its reader cannot check is not an alert — but no term has been "
            "produced that grants redistribution. This is the largest open "
            "rights question in the system and it is not closed by the fact "
            "that the vendor served the bytes."
        ),
        fixtures=("apidirect_twitter.json", "apidirect_news_articles.json"),
    ),
    "FR24": ProviderPolicy(
        provider="FR24",
        families=("FLIGHT",),
        unit="credit",
        unit_basis="Credits per RECORD RETURNED, not per request. "
                   "/live/flight-positions/full costs 8 per flight returned "
                   "and a flat 1 for an empty response, so `limit` is not a "
                   "cost control and units must be computed AFTER the "
                   "response arrives.",
        retention_days=30,
        retention_basis="Contractual. The vendor's storage rules require "
                        "permanent deletion within 30 days of first receipt, "
                        "uniformly across endpoints. This is a CEILING: "
                        "config may shorten it and cannot extend it.",
        identifiers=("airport IATA/ICAO", "ISO-3166 country code", "fr24_id",
                     "registration"),
        provenance="Primary collector. FR24 aggregates ADS-B from its own "
                   "receiver network; the positions are its data, which is "
                   "why it is the one provider with an explicit retention "
                   "term we are bound by.",
        failure_modes={
            "402": "credit exhaustion — DISTINCT from 429 and the reason the "
                   "two are handled separately",
            "429": "rate limit — prevented by the token bucket, not retried into",
            "403": "endpoint not on this tier (the /count tripwire on Explorer)",
            "401": "bad key — raises",
        },
        rate_limits={"published": "30/min Essential, 90/min Advanced",
                     "measured": "Explorer: 10/min, burst ceiling of ONE — "
                                 "two calls 0.2s apart returned 429"},
        measured={
            "credits_per_record_full": 8,
            "empty_response_flat_charge": 1,
            "count_endpoint_pct_of_full": 15,
            "count_on_explorer": "403 not permitted",
            "category_on_live_positions": "ABSENT — 22 fields, none of them "
                                          "`category`, so M cannot be proven "
                                          "from a live position",
        },
        asserted={"retention_days": 30,
                  "history_floor": "2022-06-01",
                  "per_query_range_cap_days": 14},
        downstream_rights=(
            "Retention is explicit and enforced in code. Redistribution of "
            "positions in alert evidence is NOT explicitly granted; the "
            "system returns normalised flight facts (callsign, type, ETA) "
            "rather than raw position payloads, which narrows the exposure "
            "without resolving the question."
        ),
        fixtures=("fr24_live_positions.json", "fr24_flight_summary.json"),
    ),
    "STAYING": ProviderPolicy(
        provider="STAYING",
        families=("LODGING",),
        unit="credit",
        unit_basis="Credits per call, REPORTED not inferrable. "
                   "meta.creditsCharged is authoritative and sits beside "
                   "`data`, so an unwrap that reads only `data` loses it.",
        retention_days=None,
        retention_basis="No retention term published, so the window is ours to "
                        "choose. The shipped config asks for 90 days.",
        identifiers=("platform:listingId", "listing url", "googleHotelId",
                     "free-text location"),
        provenance="Aggregator over Airbnb, Booking.com, Vrbo and Google "
                   "Hotels. Listings, prices and host details belong to those "
                   "platforms and their hosts.",
        failure_modes={
            "202": "asynchronous job — poll /jobs/{id} honouring Retry-After. "
                   "Not an error.",
            "platform status 'skipped'": "a requested platform did not run. "
                                         "MUST raise — the vendor confirmed "
                                         "by email that a Google validation "
                                         "failure had been normalised into an "
                                         "ok/0-results leg, which is a failure "
                                         "wearing the costume of a search.",
            "401": "bad key — raises",
        },
        rate_limits={"published": "per plan, readable from GET /account"},
        measured={
            "account_endpoint_cost": 0,
            "airbnb_search_credits": 10,
            "three_platform_search_credits": 20,
            "availability_credits": 5,
            "calendar_coverage": "~1 listing in 40 returned calendar data "
                                 "against a floor of 3 paired listings",
            "search_latency_s": 125,
            "vrbo": "every availability flag False even 30 days out",
            "booking": "availability job ends in state 'failed'",
        },
        asserted={"credit_balance_source": "GET /account is authoritative "
                                           "over the local ledger"},
        downstream_rights=(
            "UNRESOLVED, and the exposure is narrower than it looks: the "
            "system stores and returns availability COUNTS and price "
            "aggregates, not listing content, images or host identities. No "
            "term has been produced granting redistribution of the underlying "
            "platforms' listing data, and the aggregator's licence does not "
            "speak for Airbnb's."
        ),
        fixtures=("staying_search.json", "staying_availability.json",
                  "staying_202_job.json", "staying_account.json"),
    ),
    "PRICELINE": ProviderPolicy(
        provider="PRICELINE",
        families=("CAR",),
        unit="request",
        unit_basis="One unit per request, any endpoint. RapidAPI plans are "
                   "limitType: hard with zero overage at every tier, so "
                   "exhaustion cuts access off rather than billing.",
        retention_days=None,
        retention_basis="No retention term published, so the window is ours to "
                        "choose. The shipped config asks for 90 days.",
        identifiers=("airport IATA code", "lat,lon", "location name"),
        provenance="Aggregator over rental partners (Avis, Hertz, Alamo and "
                   "others) via RapidAPI. Two intermediaries deep: RapidAPI "
                   "resells Priceline, which aggregates the partners. As of "
                   "8.5 the reseller is `priceline-com2`; the upstream and "
                   "therefore the rights position are unchanged.",
        failure_modes={
            "400": "Zod validation — carries an issues[] array naming the field",
            "quota exhausted": "hard cutoff, not an overage charge",
            "missing vehicles[] field": "SchemaError, never a substituted zero "
                                        "— a silent zero in an availability "
                                        "count reads as scarcity",
            "200 with totalResultsAvailable 0": "INDISTINGUISHABLE from a "
                "sold-out market on one call, so it is judged on the BASELINE "
                "window: a baseline with zero total availability establishes "
                "no normal level and gives no denominator, and is raised as "
                "PlatformUnavailableError rather than recorded as a zero drop.",
        },
        rate_limits={"published": "PRO 3/s, ULTRA 5/s, MEGA 10/s"},
        # Session material. Not credentials the redactor would recognise as
        # such, but booking capability we have no analytical use for.
        drop_before_storage=("checkoutUrl", "detailsKey"),
        measured={
            "vehicles_fields": 40,
            "daily_price_fields": "dailyPrice and strikeDailyPrice were both "
                                  "0 while totalPrice was populated — derive "
                                  "from totalPrice/numRentalDays",
            "itemKey_stability": "encodes the pickup/dropoff datetimes, so it "
                                 "CANNOT join a near-term offer to its "
                                 "baseline; identity is "
                                 "(partner.code, code, pickupLocation.locationId)",
            "health_check_endpoint": "the probe calls the CAR SEARCH path, not "
                                     "autocomplete. On priceline8 autocomplete "
                                     "returned 500 while search worked, so the "
                                     "cheap probe reported the family down "
                                     "while it was up; probing search also "
                                     "means the check would have caught the "
                                     "8.5 outage, where search was what broke.",
            "offers_per_search": "272 offers -> 71 CAR signals in 2 requests "
                                 "(first live run, priceline8 wrapper)",
            "zero_inventory_2026_08_09": "the priceline8 wrapper returned "
                                         "success:true with "
                                         "totalResultsAvailable 0 for every "
                                         "PHX and JFK window at +1/+2/+7/+14 "
                                         "days. Before 8.3 that read as full "
                                         "coverage of a quiet market.",
            "wrapper_swap_2026_08_10": "priceline-com2 serves the SAME "
                                       "upstream unchanged — 510 vehicles "
                                       "near / 758 baseline at PHX, with "
                                       "peopleCapacity, counterType and "
                                       "partner intact. The outage was "
                                       "wrapper-side, not upstream (8.5).",
            "rate_plan_duplication": "the SAME car is listed once per rate "
                                     "plan — identical `id`, differing only in "
                                     "`rate`/`groupId`/`score`. Measured: 510 "
                                     "rows for 348 distinct ids. Offers are "
                                     "de-duplicated on `id`, because counting "
                                     "rows made an extra price point look like "
                                     "extra inventory and a trimmed price list "
                                     "look like scarcity.",
            "vehiclesFinal": "NOT a de-duplicated list, despite the name. It "
                             "held 170 of 494 retail offers plus 16 opaque "
                             "`express` deals that carry no partner. Keying on "
                             "it undercounted availability by about two thirds "
                             "and produced offer keys with an unknown supplier.",
            "offer_identity": "`id` (e.g. PFAR-R-HZ-PHX-HZ-PHX-CC) is "
                              "date-free and stable across windows: 311 of "
                              "348/560 ids shared between the near and "
                              "baseline windows. `itemKey` is NOT — only 7 "
                              "shared, because it encodes the datetimes.",
            "distance_units": "MILES, not kilometres. Measured against the "
                              "counters' own coordinates: every PHX counter "
                              "matched the great-circle distance in miles. "
                              "Converted before it reaches `distance_km` and "
                              "a 15 KM spatial gate.",
        },
        asserted={"schema": "vehicles[] is declared nullable with NO schema; "
                            "the 40-field shape is from a live response and "
                            "the vendor may change it without notice"},
        downstream_rights=(
            "UNRESOLVED. The system stores counts and class-level aggregates "
            "rather than bookable offers, and checkoutUrl/detailsKey are "
            "dropped before storage because they are booking capability, not "
            "evidence. Partner pricing is the partners' commercial data."
        ),
        fixtures=("priceline_autocomplete.json", "priceline_cars.json"),
    ),
}


def policy_for(provider: str) -> ProviderPolicy | None:
    return POLICIES.get(provider)


def shipped_retention_days(provider: str) -> int:
    """What the shipped configuration actually asks for, per provider.

    `DEFAULT_RETENTION_DAYS` is the fallback for a provider the config says
    nothing about — it is NOT what the system keeps. Measured during the 8.6
    soak: `config.DEFAULT_CONFIG` asks for 90 days for API Direct, Staying and
    Priceline, so `PROVIDERS.md` and the evidence surface were printing "30
    days (our default)" over payloads the system retains for 90. A governance
    artifact that documents a shorter window than the system keeps is the exact
    drift 8.3 exists to prevent, so the reporting path reads the real value.
    """
    from ..config import DEFAULT_CONFIG      # local: avoids an import cycle

    section = _CONFIG_SECTION.get(provider)
    if not section:
        return DEFAULT_RETENTION_DAYS
    return int((DEFAULT_CONFIG.get(section) or {}).get(
        "retention_days", DEFAULT_RETENTION_DAYS))


#: Provider -> the config section carrying its tunables.
_CONFIG_SECTION: dict[str, str] = {
    "FR24": "flightradar", "APIDIRECT": "apidirect",
    "STAYING": "staying", "PRICELINE": "priceline",
}


def retention_days(provider: str, configured: int | None = None) -> int:
    """Days a provider's payloads may be retained.

    The governance record is the authority on the CEILING. A configured value
    may shorten a window and can never extend one past a contractual limit — a
    config file must not be able to buy a licence term.

    With no `configured` value the shipped configuration is consulted rather
    than the bare default, so a caller that only knows the provider name still
    gets the number the system actually uses.
    """
    policy = POLICIES.get(provider)
    ceiling = (policy.retention_days if policy and policy.retention_days
               else None)
    value = (shipped_retention_days(provider) if configured is None
             else int(configured))
    value = max(1, value)
    return min(value, ceiling) if ceiling is not None else value


def strip_for_storage(provider: str, payload: Any) -> Any:
    """Remove fields this provider's policy says are never retained.

    Recursive and key-based, because the fields appear inside nested vendor
    structures. Distinct from `redact.py`, which removes SECRETS by pattern:
    this removes material that is not secret and simply has no business being
    kept — the two run together and neither replaces the other.
    """
    policy = POLICIES.get(provider)
    if policy is None or not policy.drop_before_storage:
        return payload
    drop = set(policy.drop_before_storage)

    def walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            return {k: walk(v) for k, v in node.items() if k not in drop}
        if isinstance(node, (list, tuple)):
            return [walk(v) for v in node]
        return node

    return walk(payload)


def evidence_note(provider: str) -> str:
    """One line for the evidence surface about this provider's constraints."""
    policy = POLICIES.get(provider)
    if policy is None:
        return "No governance record for this provider."
    days = retention_days(provider)
    basis = ("contractual" if policy.retention_days
             else "our choice, not a granted permission")
    return (f"{policy.provenance} Retained {days} days ({basis}). "
            f"Downstream redistribution rights are not verified.")


def open_rights_questions() -> list[dict[str, str]]:
    """Every unresolved rights question, for the operator-facing record.

    A list rather than a boolean because these do not resolve together: each
    provider's chain is different, and closing one says nothing about the rest.
    """
    return [
        {"provider": name,
         "provenance": p.provenance,
         "question": p.downstream_rights}
        for name, p in sorted(POLICIES.items())
        if not p.rights_verified
    ]


# ---------------------------------------------------------------------------
# Collection class (9.4, issue #2)
# ---------------------------------------------------------------------------


def collection_class(
    provider: str, endpoint: str | None = None, *, cached: bool | None = None,
) -> tuple[str, str]:
    """How a record reached us, as `(class, basis)`.

    Lives here because it is a statement about a vendor rather than about a
    signal: every provider in POLICIES is an intermediary, which is the single
    most important thing this field says and the thing an analyst reading a
    lodging row next to a flight row could not previously see.

    `cached` is the vendor's own assertion, not an inference. Staying's
    price-compare answers from a one-hour cache and charges the full 30 credits
    either way — so the ledger cannot distinguish a cached answer from a fresh
    one, and neither could a reader. Passing None means the vendor said nothing
    about it, which is reported as LIVE: the provider was asked and answered,
    and inventing a cache claim it did not make would be the same class of
    error in the other direction.

    Nothing returns DIRECT. That value exists so an analyst filtering for it
    gets the honest answer — no record in this system comes from the party that
    generated it — rather than having to already know.
    """
    policy = POLICIES.get(provider)
    where = f"{provider}{' ' + endpoint if endpoint else ''}"
    if policy is None:
        return "UNRECORDED", f"no governance record for {provider}"
    if cached:
        return ("INTERMEDIARY_CACHED",
                f"{where}: vendor served a stored copy (meta.cached), age not "
                f"stated, billed as a fresh call")
    return "INTERMEDIARY_LIVE", f"{where}: retrieved for this request"
