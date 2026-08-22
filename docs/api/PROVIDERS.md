# Provider governance

Generated from `surge_iw/services/governance.py`. Do not edit — edit the record and regenerate, so what is reviewed here is what the system enforces.

Rules version: `governance/1`

## The distinction that matters

**MEASURED** claims were observed against the live API and can be observed again. **ASSERTED** claims come from the vendor's terms or documentation and have not been independently verified.

**No provider has verified downstream rights, and none can acquire them by being called.** A 200 means the vendor served the bytes, not that we may redistribute them. All four are intermediaries: the content belongs to a platform or a publisher whose terms bind us whatever the aggregator's say. Vendor intermediation is not proof of downstream rights, and the development ceiling is not spend authorisation.

## APIDIRECT

- **Families**: SOCIAL
- **Billing unit**: request — One unit per HTTP request. Twitter bills per PAGE, so a pages=2 call is two billable pages inside one request — the ledger counts requests and would under-count a multi-page call.
- **Retention**: 90 days (our choice, not a granted permission). No retention term published by the vendor, so the window is OURS to choose rather than one we were granted. The shipped config asks for 90 days — long enough that a 14-day baseline stays re-derivable.
- **Identifiers accepted**: free-text query, post url
- **Provenance**: Aggregator. The posts are X/Twitter's, Reddit's and news publishers' content; API Direct is an intermediary and its terms do not displace theirs.
- **Rate limits**: {'published': None, 'measured': 'no throttling observed at 48 queries/run'}

**Downstream rights — UNVERIFIED.** UNRESOLVED. Post text and author handles are third-party content redistributed through an aggregator. The system returns snippets in alert evidence because the snippet IS the evidence — an alert its reader cannot check is not an alert — but no term has been produced that grants redistribution. This is the largest open rights question in the system and it is not closed by the fact that the vendor served the bytes.

### Failure modes

- `200 with []` — a genuine absence of results, and the ONLY case that may be read as one
- `401` — bad or missing key — raises, never an empty list
- `429` — rate limited — not observed at our volume
- `5xx` — vendor fault — raises; the query is FAILED and becomes a coverage gap

### Measured

- **cost_per_page_usd**: 0.006
- **news_time_published**: 1d returned ZERO for this lexicon; 7d works
- **social_yield**: 407 posts returned, 10 inside a week (~2%)
- **twitter_time_filter**: none exists; sort_by=most_recent only

### Asserted (from vendor terms, not verified)

- **plan**: pay-per-request

**Fixtures**: `apidirect_twitter.json`, `apidirect_news_articles.json`

## FR24

- **Families**: FLIGHT
- **Billing unit**: credit — Credits per RECORD RETURNED, not per request. /live/flight-positions/full costs 8 per flight returned and a flat 1 for an empty response, so `limit` is not a cost control and units must be computed AFTER the response arrives.
- **Retention**: 30 days (contractual ceiling). Contractual. The vendor's storage rules require permanent deletion within 30 days of first receipt, uniformly across endpoints. This is a CEILING: config may shorten it and cannot extend it.
- **Identifiers accepted**: airport IATA/ICAO, ISO-3166 country code, fr24_id, registration
- **Provenance**: Primary collector. FR24 aggregates ADS-B from its own receiver network; the positions are its data, which is why it is the one provider with an explicit retention term we are bound by.
- **Rate limits**: {'published': '30/min Essential, 90/min Advanced', 'measured': 'Explorer: 10/min, burst ceiling of ONE — two calls 0.2s apart returned 429'}

**Downstream rights — UNVERIFIED.** Retention is explicit and enforced in code. Redistribution of positions in alert evidence is NOT explicitly granted; the system returns normalised flight facts (callsign, type, ETA) rather than raw position payloads, which narrows the exposure without resolving the question.

### Failure modes

- `401` — bad key — raises
- `402` — credit exhaustion — DISTINCT from 429 and the reason the two are handled separately
- `403` — endpoint not on this tier (the /count tripwire on Explorer)
- `429` — rate limit — prevented by the token bucket, not retried into

### Measured

- **category_on_live_positions**: ABSENT — 22 fields, none of them `category`, so M cannot be proven from a live position
- **count_endpoint_pct_of_full**: 15
- **count_on_explorer**: 403 not permitted
- **credits_per_record_full**: 8
- **empty_response_flat_charge**: 1

### Asserted (from vendor terms, not verified)

- **history_floor**: 2022-06-01
- **per_query_range_cap_days**: 14
- **retention_days**: 30

**Fixtures**: `fr24_live_positions.json`, `fr24_flight_summary.json`

## PRICELINE

- **Families**: CAR
- **Billing unit**: request — One unit per request, any endpoint. RapidAPI plans are limitType: hard with zero overage at every tier, so exhaustion cuts access off rather than billing.
- **Retention**: 90 days (our choice, not a granted permission). No retention term published, so the window is ours to choose. The shipped config asks for 90 days.
- **Identifiers accepted**: airport IATA code, lat,lon, location name
- **Provenance**: Aggregator over rental partners (Avis, Hertz, Alamo and others) via RapidAPI. Two intermediaries deep: RapidAPI resells Priceline, which aggregates the partners. As of 8.5 the reseller is `priceline-com2`; the upstream and therefore the rights position are unchanged.
- **Rate limits**: {'published': 'PRO 3/s, ULTRA 5/s, MEGA 10/s'}

**Downstream rights — UNVERIFIED.** UNRESOLVED. The system stores counts and class-level aggregates rather than bookable offers, and checkoutUrl/detailsKey are dropped before storage because they are booking capability, not evidence. Partner pricing is the partners' commercial data.

### Failure modes

- `200 with totalResultsAvailable 0` — INDISTINGUISHABLE from a sold-out market on one call, so it is judged on the BASELINE window: a baseline with zero total availability establishes no normal level and gives no denominator, and is raised as PlatformUnavailableError rather than recorded as a zero drop.
- `400` — Zod validation — carries an issues[] array naming the field
- `missing vehicles[] field` — SchemaError, never a substituted zero — a silent zero in an availability count reads as scarcity
- `quota exhausted` — hard cutoff, not an overage charge

### Measured

- **daily_price_fields**: dailyPrice and strikeDailyPrice were both 0 while totalPrice was populated — derive from totalPrice/numRentalDays
- **distance_units**: MILES, not kilometres. Measured against the counters' own coordinates: every PHX counter matched the great-circle distance in miles. Converted before it reaches `distance_km` and a 15 KM spatial gate.
- **health_check_endpoint**: the probe calls the CAR SEARCH path, not autocomplete. On priceline8 autocomplete returned 500 while search worked, so the cheap probe reported the family down while it was up; probing search also means the check would have caught the 8.5 outage, where search was what broke.
- **itemKey_stability**: encodes the pickup/dropoff datetimes, so it CANNOT join a near-term offer to its baseline; identity is (partner.code, code, pickupLocation.locationId)
- **offer_identity**: `id` (e.g. PFAR-R-HZ-PHX-HZ-PHX-CC) is date-free and stable across windows: 311 of 348/560 ids shared between the near and baseline windows. `itemKey` is NOT — only 7 shared, because it encodes the datetimes.
- **offers_per_search**: 272 offers -> 71 CAR signals in 2 requests (first live run, priceline8 wrapper)
- **rate_plan_duplication**: the SAME car is listed once per rate plan — identical `id`, differing only in `rate`/`groupId`/`score`. Measured: 510 rows for 348 distinct ids. Offers are de-duplicated on `id`, because counting rows made an extra price point look like extra inventory and a trimmed price list look like scarcity.
- **vehiclesFinal**: NOT a de-duplicated list, despite the name. It held 170 of 494 retail offers plus 16 opaque `express` deals that carry no partner. Keying on it undercounted availability by about two thirds and produced offer keys with an unknown supplier.
- **vehicles_fields**: 40
- **wrapper_swap_2026_08_10**: priceline-com2 serves the SAME upstream unchanged — 510 vehicles near / 758 baseline at PHX, with peopleCapacity, counterType and partner intact. The outage was wrapper-side, not upstream (8.5).
- **zero_inventory_2026_08_09**: the priceline8 wrapper returned success:true with totalResultsAvailable 0 for every PHX and JFK window at +1/+2/+7/+14 days. Before 8.3 that read as full coverage of a quiet market.

### Asserted (from vendor terms, not verified)

- **schema**: vehicles[] is declared nullable with NO schema; the 40-field shape is from a live response and the vendor may change it without notice

### Never retained

Dropped before the payload is written, at any nesting depth. Not secrets — material we have no analytical use for and no business keeping.

- `checkoutUrl`
- `detailsKey`

**Fixtures**: `priceline_autocomplete.json`, `priceline_cars.json`

## STAYING

- **Families**: LODGING
- **Billing unit**: credit — Credits per call, REPORTED not inferrable. meta.creditsCharged is authoritative and sits beside `data`, so an unwrap that reads only `data` loses it.
- **Retention**: 90 days (our choice, not a granted permission). No retention term published, so the window is ours to choose. The shipped config asks for 90 days.
- **Identifiers accepted**: platform:listingId, listing url, googleHotelId, free-text location
- **Provenance**: Aggregator over Airbnb, Booking.com, Vrbo and Google Hotels. Listings, prices and host details belong to those platforms and their hosts.
- **Rate limits**: {'published': 'per plan, readable from GET /account'}

**Downstream rights — UNVERIFIED.** UNRESOLVED, and the exposure is narrower than it looks: the system stores and returns availability COUNTS and price aggregates, not listing content, images or host identities. No term has been produced granting redistribution of the underlying platforms' listing data, and the aggregator's licence does not speak for Airbnb's.

### Failure modes

- `202` — asynchronous job — poll /jobs/{id} honouring Retry-After. Not an error.
- `401` — bad key — raises
- `platform status 'skipped'` — a requested platform did not run. MUST raise — the vendor confirmed by email that a Google validation failure had been normalised into an ok/0-results leg, which is a failure wearing the costume of a search.

### Measured

- **account_endpoint_cost**: 0
- **airbnb_search_credits**: 10
- **availability_credits**: 5
- **booking**: availability job ends in state 'failed'
- **calendar_coverage**: ~1 listing in 40 returned calendar data against a floor of 3 paired listings
- **search_latency_s**: 125
- **three_platform_search_credits**: 20
- **vrbo**: every availability flag False even 30 days out

### Asserted (from vendor terms, not verified)

- **credit_balance_source**: GET /account is authoritative over the local ledger

**Fixtures**: `staying_search.json`, `staying_availability.json`, `staying_202_job.json`, `staying_account.json`

