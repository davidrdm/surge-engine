"""QueueAgent — tipping, queuing, and scheduling. No LLM.

This is the heart of the system. Social media posts tip searches against the
other three sources, and the decision about what to search is made here by rule,
not by a model. The previous implementation handed four tools to an LLM and let
it decide how many paid API calls to make; spend and fan-out are decided by the
queue instead.

Three modes, sharing one set of guards:

  seed      stage 1 — social queries for every active city, plus any follow-on
                      from an earlier iteration whose not_before has passed
  tip       stage 4 — social signals tip FLIGHT / LODGING / CAR queries
  schedule  stage 8 — enqueue work for FUTURE iterations

Every enqueue goes through enqueue(), which is the only place the dedup,
cooldown, fan-out and budget guards live. Every refusal writes a queue_decisions
row, so a query that did not happen is as auditable as one that did.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from ..db import enums
from ..db.database import SurgeDB, utcnow
from ..services import geo, provenance, sensitivity
from ..services.budget import BudgetGuard, provider_for_endpoint

# Endpoint constants, verified against each provider's live specification.
EP_TWITTER = "/v1/twitter/posts"
EP_REDDIT = "/v1/reddit/posts"
EP_NEWS = "/v1/news/articles"          # NOT /v1/news, which the old code used
EP_FLIGHT_COUNT = "/api/live/flight-positions/count"
EP_FLIGHT_LIVE = "/api/live/flight-positions/full"
EP_FLIGHT_SUMMARY = "/api/flight-summary/full"
EP_LODGING_SEARCH = "/search"
EP_LODGING_AVAIL = "/availability"
EP_LODGING_PRICE = "/price-compare"
EP_CAR_SEARCH = "/cars/search"

SOCIAL_ENDPOINTS: dict[str, str] = {
    "twitter": EP_TWITTER,
    "reddit": EP_REDDIT,
    "news": EP_NEWS,
}

# Priorities: lower runs first, and lower survives the budget hard stop.
PRIO_FLIGHT = 10
PRIO_ESCALATION = 15
PRIO_FLIGHT_HISTORY = 20
PRIO_SCHEDULED = 25
PRIO_BOOKING = 30
PRIO_SEED = 40

def dedup_key(endpoint: str, params: Mapping[str, Any]) -> str:
    """Stable identity for a query: its endpoint plus its canonical parameters.

    Canonical JSON with sorted keys, so parameter ordering cannot produce two
    "different" queries that hit the same URL. Truncated to 32 hex characters,
    which is ample for collision avoidance at this volume and keeps the index
    narrow.
    """
    payload = json.dumps([endpoint, params], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class QueueAgent:
    """Deterministic tipping and scheduling over the query_queue table."""

    agent_name = "QueueAgent"

    def __init__(
        self,
        db: SurgeDB,
        config: Mapping[str, Any],
        budget: BudgetGuard | None = None,
        stage: str | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.cfg = config.get("tipping", {})
        self.budget = budget
        # Stamped onto every queue_decision so a refusal can be traced to the
        # stage that made it. Set once at construction rather than per mode: the
        # orchestrator builds one QueueAgent per stage, and a field reassigned
        # part-way through a run is a field some helper will reassign wrongly.
        self.stage = stage

    def _mission(self):
        """The loaded mission, or a refusal naming what is missing.

        Seeding without one would enqueue nothing and report a queue that ran
        cleanly — the exact shape of failure this system is organised against,
        so it has to raise rather than return an empty lexicon.
        """
        mission = getattr(self.db, "mission", None)
        if mission is None:
            raise RuntimeError(
                "No mission is loaded, so there is no search lexicon and no "
                "flight category filter. Configure `mission.name`.")
        return mission

    # ==================================================================
    # The single enqueue chokepoint
    # ==================================================================

    def enqueue(
        self,
        *,
        iteration_id: int,
        session_id: int,
        source_type: str,
        endpoint: str,
        params: Mapping[str, Any],
        rule_code: str,
        priority: int = 50,
        tip_depth: int = 0,
        origin: str = "TIP",
        city_id: int | None = None,
        city_name: str | None = None,
        location_id: int | None = None,
        signal_id: int | None = None,
        not_before: datetime | None = None,
        schedule_for_later: bool = False,
    ) -> int | None:
        """Enqueue one query, or refuse and record why. Returns query_id or None.

        Guard order is deliberate: the cheap local checks run before the ones
        that touch the budget ledger, and the storage-level uniqueness check runs
        last because it is the only one that cannot be evaluated without
        attempting the write.
        """
        key = dedup_key(endpoint, params)

        def refuse(outcome: str, detail: str) -> None:
            self.db.record_queue_decision(
                iteration_id, rule_code, outcome,
                source_type=source_type, city_name=city_name,
                dedup_key=key, signal_id=signal_id, detail=detail,
                stage=self.stage,
            )

        max_depth = int(self.cfg.get("max_tip_depth", 3))
        if tip_depth > max_depth:
            refuse("CAP_DEPTH", f"tip_depth {tip_depth} exceeds max {max_depth}")
            return None

        max_iter = int(self.cfg.get("max_queries_per_iteration", 200))
        if self.db.count_queued(iteration_id) >= max_iter:
            refuse("CAP_ITERATION", f"iteration already holds {max_iter} queries")
            return None

        if city_id is not None:
            max_city = int(self.cfg.get("max_queries_per_city", 12))
            if self.db.count_queued_for_city(iteration_id, city_id) >= max_city:
                refuse("CAP_CITY", f"city already holds {max_city} queries")
                return None

        cooldown = int(self.cfg.get("cooldown_minutes", 180))
        last = self.db.last_execution(key)
        if last is not None and (utcnow() - last) < timedelta(minutes=cooldown):
            refuse(
                "COOLDOWN",
                f"identical query ran at {last.isoformat()} "
                f"(cooldown {cooldown}m)",
            )
            return None

        if self.budget is not None:
            provider = provider_for_endpoint(endpoint)
            allowed, reason = self.budget.can_afford(
                provider, endpoint, priority, iteration_id=iteration_id
            )
            if not allowed:
                refuse("BUDGET_EXHAUSTED", f"{provider}: {reason}")
                return None

        try:
            query_id = self.db.enqueue_query(
                session_id=session_id,
                iteration_id=None if schedule_for_later else iteration_id,
                source_type=source_type,
                endpoint=endpoint,
                params=dict(params),
                dedup_key=key,
                priority=priority,
                tip_depth=tip_depth,
                origin=origin,
                city_id=city_id,
                location_id=location_id,
                tipped_by_signal_id=signal_id,
                rule_code=rule_code,
                not_before=not_before,
                # Scheduled work belongs to no iteration until stage 1 claims
                # it, but it was still written by this one.
                created_iteration_id=iteration_id,
            )
        except Exception as exc:  # sqlite3.IntegrityError on idx_qq_dedup
            if "UNIQUE" not in str(exc).upper():
                raise
            refuse("DEDUPED", "identical query already queued this iteration")
            return None

        self.db.record_queue_decision(
            iteration_id, rule_code, "ENQUEUED",
            source_type=source_type, city_name=city_name,
            dedup_key=key, signal_id=signal_id,
            detail=f"query_id={query_id} priority={priority} depth={tip_depth}",
            stage=self.stage,
        )
        return query_id

    # ==================================================================
    # City admission
    # ==================================================================

    def admit_city(
        self,
        *,
        iteration_id: int,
        session_id: int,
        name: str,
        signals: Sequence[Mapping[str, Any]],
        expand_cities: bool,
    ) -> int | None:
        """Thin delegate to the module-level rule. See admit_city() below."""
        return admit_city(
            self.db, self.cfg, iteration_id=iteration_id, session_id=session_id,
            name=name, signals=signals, expand_cities=expand_cities,
            stage=self.stage,
        )

    # ==================================================================
    # Mode: seed (stage 1)
    # ==================================================================

    def build_social_queries(
        self, city_name: str, state: str | None, tracks: Iterable[str]
    ) -> list[tuple[str, dict[str, Any]]]:
        """Social queries for one city: (endpoint, params) pairs.

        Deterministic template expansion over the mission's lexicon. Still a
        table rather than a model call, and now an auditable, hashed one: an
        someone asking "why did you search that" deserves a better answer than
        "the model chose it", and the pack digest on the receipt says which
        table was in force. The API Direct endpoints
        do not share a parameter vocabulary — twitter takes `pages` and
        `sort_by` with no time filter, news takes `limit` and `time_published` —
        so params are built per endpoint rather than one shape for all, which is
        what the old connector got wrong.
        """
        settings = self.config.get("apidirect", {})
        platforms = settings.get("platforms", ["twitter", "reddit", "news"])
        place = f"{city_name} {state}".strip() if state else city_name

        queries: list[tuple[str, dict[str, Any]]] = []
        for track in tracks:
            for group in self._mission().lexicon.get(track, ()):
                terms = " OR ".join(f'"{t}"' for t in group)
                query_text = f"{place} ({terms})"[:500]
                for platform in platforms:
                    endpoint = SOCIAL_ENDPOINTS.get(platform)
                    if endpoint is None:
                        continue
                    if endpoint == EP_NEWS:
                        params: dict[str, Any] = {
                            "query": query_text,
                            "limit": int(settings.get("news_limit", 50)),
                            "time_published": settings.get(
                                "news_time_published", "1d"
                            ),
                        }
                    else:
                        params = {
                            "query": query_text,
                            "pages": int(settings.get("twitter_pages", 2)),
                            "sort_by": "most_recent",
                        }
                        if settings.get("get_sentiment"):
                            params["get_sentiment"] = True
                    queries.append((endpoint, params))
        return queries

    def run_seed(self, iteration_id: int, session_id: int) -> dict[str, int]:
        """Stage 1: seed social queries and adopt due follow-ons."""
        tracks = self.db.session_tracks(session_id)
        counts = {"seeded": 0, "adopted": 0}

        for row in self.db.due_scheduled_queries(session_id):
            self.db.adopt_query(int(row["query_id"]), iteration_id)
            counts["adopted"] += 1

        for city in self.db.get_cities(session_id):
            for endpoint, params in self.build_social_queries(
                city["name"], city["state"], tracks
            ):
                query_id = self.enqueue(
                    iteration_id=iteration_id,
                    session_id=session_id,
                    source_type="SOCIAL",
                    endpoint=endpoint,
                    params=params,
                    rule_code="R0_SEED",
                    priority=PRIO_SEED,
                    tip_depth=0,
                    origin="SEED",
                    city_id=int(city["city_id"]),
                    city_name=city["name"],
                )
                if query_id is not None:
                    counts["seeded"] += 1

        self.db.log(
            self.agent_name, "INFO",
            f"Seeded {counts['seeded']} social queries, "
            f"adopted {counts['adopted']} follow-ons",
            iteration_id=iteration_id, **counts,
        )
        return counts

    # ==================================================================
    # Mode: tip (stage 4)
    # ==================================================================

    def _flight_params(
        self, iata: str, tracks: Sequence[str], *, history: bool = False
    ) -> dict[str, Any]:
        """FR24 query parameters for one airport.

        `airports=inbound:<IATA>` and the category letter codes are both
        verified against FR24's OpenAPI spec. Categories are the union across
        the session's active tracks, because one call serves all of them — and
        which codes each track wants is the mission's judgement, not the
        engine's.
        """
        mission = self._mission()
        categories: set[str] = set()
        for track in tracks:
            categories.update(mission.flight_categories.get(track, ()))
        fr_cfg = self.config.get("flightradar", {})
        params: dict[str, Any] = {
            "airports": f"inbound:{iata}",
            "categories": ",".join(sorted(categories)),
        }
        if history:
            hours = int(fr_cfg.get("history_hours", 48))
            now = utcnow()
            params["flight_datetime_from"] = (
                now - timedelta(hours=hours)
            ).strftime("%Y-%m-%dT%H:%M:%S")
            params["flight_datetime_to"] = now.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            # The only cost control available on flight-positions. Billing is
            # per record RETURNED, so an unbounded query at a busy field is an
            # unbounded charge. Not sent on flight-summary, where the parameter
            # is documented as inapplicable and the window bounds cost instead.
            params["limit"] = int(fr_cfg.get("live_limit", 20))
        return params

    def tip_from_social(
        self,
        *,
        iteration_id: int,
        session_id: int,
        city_id: int,
        city_name: str,
        state: str | None,
        signal_id: int | None,
        tracks: Sequence[str],
    ) -> dict[str, int]:
        """Rules R1-R5: a relevant social signal tips the other three sources."""
        enqueued: dict[str, int] = {}
        fr_cfg = self.config.get("flightradar", {})
        max_airports = int(fr_cfg.get("max_airports_per_city", 3))
        airports = geo.city_to_airports(city_name, limit=max_airports)

        common = {
            "iteration_id": iteration_id,
            "session_id": session_id,
            "city_id": city_id,
            "city_name": city_name,
            "signal_id": signal_id,
        }

        # R1: live positions, cost-capped by `limit`.
        #
        # This was originally a /count tripwire, on the reasoning that /count
        # costs ~15% of /full. Live testing killed that design: BOTH /count
        # endpoints return 403 "You are not permitted to access this endpoint"
        # on this subscription. Worse, the substitute tripwire would have been
        # counterproductive — /light costs 6 credits/record and lacks both the
        # category and the ETA, so it would have added cost without removing the
        # need for /full.
        #
        # `limit` IS supported on flight-positions endpoints and does cap records
        # returned, which is the real cost control: measured at 8 credits per
        # record, a limit of 20 bounds one airport query at 160 credits.
        use_tripwire = bool(fr_cfg.get("use_count_tripwire"))
        for iata in airports:
            if use_tripwire:
                ok = self.enqueue(
                    source_type="FLIGHT_COUNT", endpoint=EP_FLIGHT_COUNT,
                    params=self._flight_params(iata, tracks),
                    rule_code="R1_FLIGHT_COUNT", priority=PRIO_FLIGHT,
                    tip_depth=1, **common,
                )
                if ok:
                    enqueued["FLIGHT_COUNT"] = enqueued.get("FLIGHT_COUNT", 0) + 1
                continue
            if self.enqueue(
                source_type="FLIGHT_LIVE", endpoint=EP_FLIGHT_LIVE,
                params=self._flight_params(iata, tracks),
                rule_code="R1_FLIGHT_LIVE", priority=PRIO_FLIGHT,
                tip_depth=1, **common,
            ):
                enqueued["FLIGHT_LIVE"] = enqueued.get("FLIGHT_LIVE", 0) + 1

        # Historical context is NOT enqueued here. flight-summary is the only
        # endpoint returning a category, but it is also the most expensive call
        # in the system — one 24-hour query at LAX cost 60 credits for 20
        # records, and the window here is 48 hours. It only has value when there
        # is a live record whose category needs resolving, so it is enqueued by
        # escalate_to_history() once R1 comes back with something.

        if not airports:
            self.db.record_queue_decision(
                iteration_id, "R1_FLIGHT_LIVE", "NO_MAPPING",
                source_type="FLIGHT_LIVE", city_name=city_name,
                signal_id=signal_id,
                detail="no airport mapping; flight collection unavailable",
            )

        # R4: lodging near each key location. Anchoring on the facility rather
        # than the city centre is the point — a surge shows up as scarcity near
        # the anchor facility, not city-wide.
        locations = self.db.get_key_locations(city_id)
        if not locations:
            # A city admitted by tip has no operator-registered facility, so
            # lodging cannot be anchored to anything. Skipping is the safe
            # choice — a city-wide lodging search would carry no distance, and
            # scoring treats a distance-less row as spatially anchored, handing
            # it full quality for proximity it never demonstrated. Recorded so
            # the absence is visible rather than inferred from a missing row.
            self.db.record_queue_decision(
                iteration_id, "R4_LODGING", "NO_MAPPING",
                source_type="LODGING", city_name=city_name, signal_id=signal_id,
                detail="no key locations registered for this city; lodging "
                       "cannot be anchored to a facility",
            )
        max_locations = int(self.cfg.get("max_locations_per_city", 4))
        for location in locations[:max_locations]:
            params = {
                "location": geo.lodging_location_string(
                    city_name, state, location["name"]
                ),
                "limit": int(
                    self.config.get("staying", {}).get("listing_set_size", 25)
                ),
            }
            if self.enqueue(
                source_type="LODGING", endpoint=EP_LODGING_SEARCH, params=params,
                rule_code="R4_LODGING", priority=PRIO_BOOKING, tip_depth=1,
                location_id=int(location["location_id"]), **common,
            ):
                enqueued["LODGING"] = enqueued.get("LODGING", 0) + 1

        # R9: hotel prices near the same facilities. A second measurement of
        # the same demand pressure, and the only route to actual hotels rather
        # than short-term rentals — which matters because agencies book hotel
        # blocks. Off by default until the per-call credit cost is measured.
        if bool(self.config.get("staying", {}).get("enable_price_compare")):
            for location in locations[:max_locations]:
                price_params = {
                    "location": geo.lodging_location_string(
                        city_name, state, location["name"]
                    ),
                }
                if self.enqueue(
                    source_type="LODGING_PRICE", endpoint=EP_LODGING_PRICE,
                    params=price_params, rule_code="R9_LODGING_PRICE",
                    priority=PRIO_BOOKING, tip_depth=1,
                    location_id=int(location["location_id"]), **common,
                ):
                    enqueued["LODGING_PRICE"] = enqueued.get("LODGING_PRICE", 0) + 1

        # R5: rental cars, keyed on the airport IATA code. Priceline echoes the
        # code back as pickupLocation.airportCode, so no autocomplete round-trip
        # is needed, and airport fleets book out before off-airport ones.
        pickup = geo.city_to_pickup_location(city_name)
        if pickup:
            if self.enqueue(
                source_type="CAR", endpoint=EP_CAR_SEARCH,
                params=self._car_params(pickup),
                rule_code="R5_CAR", priority=PRIO_BOOKING, tip_depth=1, **common,
            ):
                enqueued["CAR"] = enqueued.get("CAR", 0) + 1
        else:
            self.db.record_queue_decision(
                iteration_id, "R5_CAR", "NO_MAPPING", source_type="CAR",
                city_name=city_name, signal_id=signal_id,
                detail="no airport mapping for car pickup point",
            )
        return enqueued

    def _car_params(self, pickup: str, offset_days: int = 0) -> dict[str, Any]:
        """Priceline /cars/search parameters for the near-term window.

        Dates are YYYY-MM-DD and times HH:MM, verified live against the
        priceline-com2 wrapper (8.5). All six parameters are required and there
        are no optional ones — notably no `currency`, which the previous
        wrapper accepted; the response carries `rate[].currencyCode` instead.

        `pickup` is an airport IATA code. The endpoint takes it directly, so
        unlike every other candidate evaluated in 8.5 there is no
        location-resolution call to pay for first.
        """
        cfg = self.config.get("priceline", {})
        start = utcnow() + timedelta(days=offset_days)
        end = start + timedelta(days=int(cfg.get("rental_days", 2)))
        return {
            "pickUpLocation": pickup,
            "dropOffLocation": pickup,
            "pickUpDate": start.strftime("%Y-%m-%d"),
            "pickUpTime": cfg.get("pickup_time", "12:00"),
            "dropOffDate": end.strftime("%Y-%m-%d"),
            "dropOffTime": cfg.get("dropoff_time", "12:00"),
        }

    def tip_from_flight(
        self,
        *,
        iteration_id: int,
        session_id: int,
        city_id: int,
        city_name: str,
        state: str | None,
        signal_id: int | None,
        tracks: Sequence[str],
    ) -> dict[str, int]:
        """Rule R6: a flight signal tips lodging and cars.

        The surge signature can enter through any door. If military or
        unverifiable-category traffic appears for a city and nothing has yet
        queried its lodging or vehicles, that gap gets filled — even though no
        social post mentioned the city.
        """
        if self.db.has_queued(iteration_id, city_id, ("LODGING", "CAR")):
            return {}
        booked = self.tip_from_social(
            iteration_id=iteration_id, session_id=session_id, city_id=city_id,
            city_name=city_name, state=state, signal_id=signal_id, tracks=tracks,
        )
        return {k: v for k, v in booked.items() if k in ("LODGING", "CAR")}

    def escalate_flight_count(
        self,
        *,
        iteration_id: int,
        session_id: int,
        city_id: int,
        city_name: str,
        iata: str,
        record_count: int,
        signal_id: int | None,
        tracks: Sequence[str],
    ) -> int | None:
        """Buy full records after a /count tripwire cleared.

        Only reachable when flightradar.use_count_tripwire is true, which it is
        not by default: both /count endpoints return 403 on this subscription.
        Retained because a higher tier enables them.
        """
        threshold = int(
            self.config.get("flightradar", {}).get("flight_count_threshold", 1)
        )
        if record_count < threshold:
            self.db.record_queue_decision(
                iteration_id, "R2_FLIGHT_LIVE", "CAP_DEPTH",
                source_type="FLIGHT_LIVE", city_name=city_name,
                signal_id=signal_id,
                detail=f"record_count {record_count} below threshold {threshold}; "
                       "full records not purchased",
                stage=self.stage,
            )
            return None
        return self.enqueue(
            iteration_id=iteration_id, session_id=session_id,
            source_type="FLIGHT_LIVE", endpoint=EP_FLIGHT_LIVE,
            params=self._flight_params(iata, tracks),
            rule_code="R2_FLIGHT_LIVE", priority=PRIO_FLIGHT, tip_depth=2,
            city_id=city_id, city_name=city_name, signal_id=signal_id,
        )

    def escalate_to_history(
        self,
        *,
        iteration_id: int,
        session_id: int,
        city_id: int,
        city_name: str,
        iata: str,
        live_record_count: int,
        signal_id: int | None,
        tracks: Sequence[str],
    ) -> int | None:
        """Rule R2: resolve categories, but only when there is something to resolve.

        flight-summary is the only endpoint that returns a category, and it is
        also the most expensive call in the system — 3 credits per record over a
        48-hour window, measured at 60 credits for a single 24-hour query. Live
        records arrive AMBIGUOUS and stay that way without it, so it is worth
        paying for exactly when at least one live record exists.

        No live records means nothing to resolve, and the empty flight picture is
        already correctly represented by the completed live query.
        """
        if live_record_count < 1:
            self.db.record_queue_decision(
                iteration_id, "R2_FLIGHT_HIST", "CAP_DEPTH",
                source_type="FLIGHT_HISTORY", city_name=city_name,
                signal_id=signal_id,
                detail="no live records to resolve; skipping the expensive "
                       "flight-summary call",
                stage=self.stage,
            )
            return None
        return self.enqueue(
            iteration_id=iteration_id, session_id=session_id,
            source_type="FLIGHT_HISTORY", endpoint=EP_FLIGHT_SUMMARY,
            params=self._flight_params(iata, tracks, history=True),
            rule_code="R2_FLIGHT_HIST", priority=PRIO_FLIGHT_HISTORY,
            tip_depth=2, city_id=city_id, city_name=city_name,
            signal_id=signal_id,
        )

    # ==================================================================
    # Mode: schedule (stage 8)
    # ==================================================================

    def run_tip(self, iteration_id: int, session_id: int) -> dict[str, int]:
        """Stage 4: turn triaged social signals into collection against the
        other three sources.

        One tip per city, not per post. Ten posts about the same deployment are
        one piece of evidence about one place, and tipping per post would
        multiply identical queries that the dedup guard would then have to
        reject — wasting the guard on work that should never have been proposed.

        The highest-salience signal is credited as the tipping signal so
        `query_queue.tipped_by_signal_id` points at the strongest evidence for
        the decision rather than an arbitrary one.
        """
        tracks = self.db.session_tracks(session_id)
        counts: dict[str, int] = {"cities_tipped": 0, "refused_signals": 0}

        for city in self.db.get_cities(session_id):
            city_id = int(city["city_id"])
            social = [
                row for row in self.db.signals_for_city(iteration_id, city_id)
                if row["signal_type"] == "SOCIAL"
            ]
            # A query is a purchase, so tipping needs a higher bar than scoring
            # does. Before this gate a single post with a MISSING salience and no
            # timestamp booked the full paid follow-on set for a seeded city —
            # and that signal was then excluded from correlation by the window
            # check, so the money bought evidence that could not score.
            eligible = []
            for row in social:
                decision = sensitivity.may_tip(dict(row), self.config)
                if decision.allowed:
                    eligible.append(row)
                    continue
                counts["refused_signals"] += 1
                self.db.record_queue_decision(
                    iteration_id, "R4_TIP_GATE", "CITY_NOT_ADMITTED",
                    city_name=city["name"], signal_id=int(row["signal_id"]),
                    detail=f"signal not eligible to tip: {decision.reason}",
                    stage=self.stage,
                )
            social = eligible
            if not social:
                continue
            strongest = max(social, key=lambda r: float(r["salience"] or 0.0))
            tipped = self.tip_from_social(
                iteration_id=iteration_id, session_id=session_id,
                city_id=city_id, city_name=city["name"], state=city["state"],
                signal_id=int(strongest["signal_id"]), tracks=tracks,
            )
            if tipped:
                counts["cities_tipped"] += 1
            for source_type, count in tipped.items():
                counts[source_type] = counts.get(source_type, 0) + count

        self.db.log(
            self.agent_name, "INFO",
            f"Tipped {counts['cities_tipped']} city/cities from social signals",
            iteration_id=iteration_id, **counts,
        )
        return counts

    def run_escalate(self, iteration_id: int, session_id: int) -> dict[str, int]:
        """Post-collection escalation, run between the two collection passes.

        Two rules fire here because both depend on what collection just
        returned, which is knowable only after the first pass:

          R2  live flight records exist, so their categories are worth resolving
          R6  a flight signal arrived for a city with no lodging or car query,
              so the surge signature entered through the flight door

        History is escalated per DESTINATION AIRPORT that actually returned
        records, not per airport mapped to the city. flight-summary is the most
        expensive call in the system, and a city with three mapped airports
        would otherwise buy three 48-hour windows to resolve records that came
        from one of them.
        """
        tracks = self.db.session_tracks(session_id)
        counts: dict[str, int] = {}

        for city in self.db.get_cities(session_id):
            city_id = int(city["city_id"])
            flights = [
                row for row in self.db.signals_for_city(iteration_id, city_id)
                if row["signal_type"] == "FLIGHT"
            ]
            if not flights:
                continue

            unresolved: dict[str, int] = {}
            for row in flights:
                if row["category_confidence"] != "AMBIGUOUS":
                    continue
                iata = (row["dest_iata"] or "").upper()
                if iata:
                    unresolved[iata] = unresolved.get(iata, 0) + 1

            for iata, record_count in sorted(unresolved.items()):
                if self.escalate_to_history(
                    iteration_id=iteration_id, session_id=session_id,
                    city_id=city_id, city_name=city["name"], iata=iata,
                    live_record_count=record_count, signal_id=None,
                    tracks=tracks,
                ):
                    counts["FLIGHT_HISTORY"] = counts.get("FLIGHT_HISTORY", 0) + 1

            # R6 credits the strongest flight record as the tipping signal.
            strongest = max(flights, key=lambda r: 0 if r["flight_category"]
                            == "AMBIGUOUS" else 1)
            booked = self.tip_from_flight(
                iteration_id=iteration_id, session_id=session_id,
                city_id=city_id, city_name=city["name"], state=city["state"],
                signal_id=int(strongest["signal_id"]), tracks=tracks,
            )
            for source_type, count in booked.items():
                counts[source_type] = counts.get(source_type, 0) + count

        if counts:
            self.db.log(
                self.agent_name, "INFO",
                "Escalated after collection: "
                + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
                iteration_id=iteration_id, **counts,
            )
        return counts

    def run_schedule(
        self, iteration_id: int, session_id: int, tracks: Sequence[str]
    ) -> dict[str, int]:
        """Stage 8: enqueue work for future iterations.

        Scheduling means writing a row with a not_before timestamp. Nothing
        sleeps and nothing polls; the next POST /iterations claims whatever has
        come due. That keeps "iterations are triggered by an API call" true.
        """
        counts = {"revisit": 0, "carried_forward": 0}
        revisit_minutes = int(self.cfg.get("revisit_minutes", 180))
        due = utcnow() + timedelta(minutes=revisit_minutes)

        # R7: a city with a MEDIUM-or-better alert gets looked at again.
        for correlation in self.db.get_correlations(iteration_id):
            if enums.band_index(correlation["band"]) < enums.band_index("MEDIUM"):
                continue
            city = self.db.one(
                "SELECT * FROM cities WHERE city_id = ?", (correlation["city_id"],)
            )
            if city is None:
                continue
            for endpoint, params in self.build_social_queries(
                city["name"], city["state"], tracks
            ):
                if self.enqueue(
                    iteration_id=iteration_id, session_id=session_id,
                    source_type="SOCIAL", endpoint=endpoint, params=params,
                    rule_code="R7_REVISIT", priority=PRIO_SCHEDULED,
                    origin="SCHEDULED", city_id=int(city["city_id"]),
                    city_name=city["name"], not_before=due,
                    schedule_for_later=True,
                ):
                    counts["revisit"] += 1

        # Carry forward anything the budget refused, at a better priority so it
        # is preferentially retried once the allocation refreshes.
        for row in self.db.all(
            "SELECT * FROM query_queue WHERE iteration_id = ? "
            "AND status = 'SKIPPED_BUDGET'",
            (iteration_id,),
        ):
            if self.enqueue(
                iteration_id=iteration_id, session_id=session_id,
                source_type=row["source_type"], endpoint=row["endpoint"],
                params=json.loads(row["params_json"]),
                rule_code="CARRY_FORWARD",
                priority=max(0, int(row["priority"]) - 10),
                tip_depth=int(row["tip_depth"]), origin="CARRIED_FORWARD",
                city_id=row["city_id"], location_id=row["location_id"],
                not_before=due, schedule_for_later=True,
            ):
                counts["carried_forward"] += 1

        self.db.log(
            self.agent_name, "INFO",
            f"Scheduled {counts['revisit']} revisits, "
            f"{counts['carried_forward']} carried forward",
            iteration_id=iteration_id, **counts,
        )
        return counts


def admit_city(
    db: SurgeDB,
    cfg: Mapping[str, Any],
    *,
    iteration_id: int,
    session_id: int,
    name: str,
    signals: Sequence[Mapping[str, Any]],
    expand_cities: bool,
    stage: str | None = None,
) -> int | None:
    """Resolve a city mentioned in a post to a city_id, admitting it if allowed.

    A module-level rule rather than an agent method, because two agents need it:
    TriageAgent must attach a city_id to each social signal, and QueueAgent must
    admit cities during tipping. Sharing a deterministic function keeps the "no
    agent calls another directly" rule intact — an agent importing a pure rule is
    not an agent invoking an agent.

    An LLM proposes candidate cities; this decides whether to admit them.
    Corroboration from two independent source domains stops a single viral post
    from steering collection into a city nobody is deploying to.
    """
    jurisdictions = getattr(getattr(db, "mission", None),
                            "jurisdictions", geo.NO_EQUIVALENTS)
    canonical, _method = geo.resolve_city(name, jurisdictions)
    if canonical is None:
        canonical = geo.normalise(name)
    existing = db.find_city(session_id, canonical)
    if existing is not None:
        return int(existing["city_id"])

    # 9.9. Some places are ONE operational unit under two names, and which
    # pairs those are is a mission's judgement — the engine reads them from the
    # pack's `equivalents`, it does not know them.
    #
    # Why this exists: a session named one place, and a source reported the
    # same activity under the containing administrative unit's name. Measured
    # live — the article was collected, judged relevant at salience 0.85, and
    # refused because the two strings did not match. The evidence the system
    # exists to surface was paid for, judged, and dropped on a name.
    #
    # Checked only AFTER the exact match, and only against places the session
    # already named. It cannot widen collection to somewhere nobody asked
    # about: an equivalence is a statement that two names mean one unit, not a
    # licence to admit a neighbouring one.
    for alternative in jurisdictions.others(canonical):
        existing = db.find_city(session_id, alternative)
        if existing is None:
            continue
        # Recorded, because deciding that one place name means another is an
        # analytical decision and a reader of the signal would otherwise see
        # a row for one place with no account of how a report naming another
        # produced it.
        # `agent_log` rather than `queue_decisions`: the outcome vocabulary
        # there is CHECK-constrained and SQLite cannot alter a CHECK in place,
        # so a new value would force a table rebuild for a note.
        db.log(
            "admit_city", "INFO",
            f"{name!r} resolved to {canonical!r} and was matched to the "
            f"session's {existing['name']!r} — the mission declares these one "
            f"jurisdiction",
            iteration_id=iteration_id, session_id=session_id,
            named=name, canonical=canonical, matched=alternative,
            city_id=int(existing["city_id"]), stage=stage,
        )
        return int(existing["city_id"])

    def refuse(outcome: str, detail: str) -> None:
        db.record_queue_decision(
            iteration_id, "ADMIT_CITY", outcome,
            city_name=name, detail=detail, stage=stage,
        )

    if not expand_cities:
        refuse("CITY_NOT_ADMITTED", "expand_cities=false; city not in user list")
        return None

    # Counted in PUBLISHERS and CLAIMS, not raw host strings. `www.apnews.com`
    # and `apnews.com` were two "domains"; so were "associated press" and
    # apnews.com, because the news normaliser puts a display name in the domain
    # field when it has no host. And two outlets reprinting one wire story are
    # two publishers but ONE claim. Admission takes the LOWER of the two, so it
    # cannot be satisfied by breadth of republication alone.
    publishers, claims = provenance.corroboration(signals)
    independent = min(publishers, claims)
    min_domains = int(cfg.get("min_independent_domains", 2))
    if independent < min_domains:
        refuse(
            "CITY_NOT_ADMITTED",
            f"corroborated by {publishers} independent publisher(s) making "
            f"{claims} distinct claim(s), need {min_domains} of each",
        )
        return None

    peak = max((float(s.get("salience") or 0.0) for s in signals), default=0.0)
    min_salience = float(cfg.get("min_expansion_salience", 0.6))
    if peak < min_salience:
        refuse(
            "CITY_NOT_ADMITTED",
            f"peak salience {peak:.2f} below {min_salience}",
        )
        return None

    max_expanded = int(cfg.get("max_expanded_cities", 5))
    if db.count_expanded_cities(session_id) >= max_expanded:
        refuse("CAP_CITY", f"{max_expanded} expanded cities already admitted")
        return None

    display, state = geo.split_state(name)
    city_id = db.insert_city(
        session_id, display, canonical=canonical, state=state,
        is_seed=False, admitted_by="TIP", admitted_iteration=iteration_id,
    )
    db.log(
        "QueueAgent", "INFO", f"Admitted city by tip: {display}",
        iteration_id=iteration_id, city_id=city_id,
        publishers=publishers, claims=claims, peak_salience=peak,
    )
    return city_id
