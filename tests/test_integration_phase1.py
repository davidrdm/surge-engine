"""End-to-end walk through everything Phase 1 builds, via the database bus.

No LLM and no network. The two stages that need them — triage and alert prose —
are stood in for by writing the rows they would write, which is exactly what the
bus abstraction is supposed to make possible: each agent reads its inputs from
SQLite and writes its outputs there, so a stage can be replaced by its output
without the neighbours noticing.

What this proves that the unit tests do not: the pieces compose. A social signal
tips real queries, those queries produce signals that correlate into a banded
alert, and every link in the evidence chain resolves back to the query that
produced it.
"""
from __future__ import annotations

import json
from datetime import timedelta

from conftest import AIRLIFT, ANCHOR
from surge_iw.agents.queueing import QueueAgent
from surge_iw.base import scoring
from surge_iw.db.database import iso
from surge_iw.models import Alert
from surge_iw.services.budget import BudgetGuard, provider_for_endpoint, units_for
from surge_iw.services.retention import RetentionService, retention_days


def _bootstrap(db, config, *, expand_cities=False):
    """A session with one city and one key location, plus a first iteration."""
    session = db.insert_session(
        label="AZ display season", expand_cities=expand_cities,
        tracks=["AIRSHOW", "CONCERT_TOUR"], config=config,
    )
    city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
    location = db.insert_key_location(
        city, "Riverside Fairground",
        location_type="FAIRGROUND", lat=33.4484, lon=-112.0740,
    )
    iteration = db.insert_iteration(session, anchor_at=ANCHOR)
    return session, city, location, iteration


def _collect(db, iteration, query_row, payload, *, config, signals=()):
    """Stand in for CollectionAgent: record the payload, then the signals."""
    provider = provider_for_endpoint(query_row["endpoint"])
    raw_id = db.insert_raw_result(
        query_id=query_row["query_id"], iteration_id=iteration,
        source_type=query_row["source_type"], provider=provider,
        payload=payload, retention_days=retention_days(config, provider),
    )
    db.record_api_call(
        provider=provider, endpoint=query_row["endpoint"],
        units=units_for(provider, query_row["endpoint"], len(payload)),
        records_returned=len(payload), http_status=200,
        iteration_id=iteration, query_id=query_row["query_id"],
    )
    ids = [
        db.insert_signal(iteration_id=iteration, raw_id=raw_id, **fields)
        for fields in signals
    ]
    db.complete_query(query_row["query_id"], result_count=len(payload))
    return raw_id, ids


class TestFullPipelinePhase1:
    def test_social_tip_produces_a_high_confidence_alert(self, db, config,
                                                        corr_cfg):
        config["tipping"]["max_queries_per_city"] = 99
        session, city, location, iteration = _bootstrap(db, config)
        budget = BudgetGuard(db, config)
        budget.seed_budgets()
        budget.plan_iteration(iteration)
        agent = QueueAgent(db, config, budget=budget)
        tracks = db.session_tracks(session)

        # --- Stage 1: seed --------------------------------------------------
        db.set_stage(iteration, "SEEDING")
        seeded = agent.run_seed(iteration, session)
        assert seeded["seeded"] > 0

        # --- Stage 2/3: collect social, then triage into a signal -----------
        db.set_stage(iteration, "COLLECTING_SOCIAL")
        social_query = db.claim_next_query(iteration, ["SOCIAL"])
        assert social_query is not None

        posts = [
            {"url": "https://x.com/a/1", "author": "reporter",
             "domain": "x.com", "snippet": "Demonstration team jets on the "
             "flightline at the Riverside Fairground tonight."},
            {"url": "https://apnews.com/b/2", "author": "AP",
             "domain": "apnews.com", "snippet": "Organisers confirm a second "
             "display team for Phoenix."},
        ]
        raw_id, social_ids = _collect(
            db, iteration, social_query, posts, config=config,
            signals=[
                {
                    "signal_type": "SOCIAL", "city_id": city,
                    "location_id": location, "track": "AIRSHOW",
                    "observed_at": iso(ANCHOR - timedelta(hours=2)),
                    "quality": 0.9, "url": posts[0]["url"],
                    "author": posts[0]["author"], "platform": "twitter",
                    "source_domain": "x.com", "snippet": posts[0]["snippet"],
                    "salience": 0.88, "activity_type": "static display",
                    "imminence_hours": 12.0,
                },
                {
                    "signal_type": "SOCIAL", "city_id": city,
                    "location_id": location, "track": "AIRSHOW",
                    "observed_at": iso(ANCHOR - timedelta(hours=3)),
                    "quality": 0.9, "url": posts[1]["url"],
                    "author": posts[1]["author"], "platform": "news",
                    "source_domain": "apnews.com", "snippet": posts[1]["snippet"],
                    "salience": 0.92, "activity_type": "static display",
                    "imminence_hours": 24.0,
                },
            ],
        )
        db.set_stage(iteration, "TRIAGING")
        for post, signal_id in zip(posts, social_ids):
            db.insert_triage_decision(
                iteration_id=iteration, raw_id=raw_id, state="ACCEPTED",
                rationale="names a display team and a key facility",
                model="stub", url=post["url"], track=AIRLIFT.name,
                cities=["Phoenix"], salience=0.9, signal_id=signal_id,
            )

        # --- Stage 4: tip ---------------------------------------------------
        db.set_stage(iteration, "TIPPING")
        tipped = agent.tip_from_social(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", state="AZ", signal_id=social_ids[0],
            tracks=tracks,
        )
        assert tipped["FLIGHT_LIVE"] == 1
        assert tipped["LODGING"] == 1
        assert tipped["CAR"] == 1

        # Expensive history is not bought speculatively.
        queued = {r["source_type"] for r in db.get_queue(iteration)}
        assert "FLIGHT_LIVE" in queued and "FLIGHT_HISTORY" not in queued

        # --- Stage 5: collect the tipped queries ----------------------------
        db.set_stage(iteration, "COLLECTING_TIPPED")

        live_query = db.claim_next_query(iteration, ["FLIGHT_LIVE"])
        live_payload = [
            {"fr24_id": f"fr{i}", "callsign": f"RCH{i}", "reg": f"N{i}",
             "type": "C17", "orig_iata": "DOV", "dest_iata": "PHX",
             "eta": f"2026-07-27T1{i}:40:00Z"}
            for i in range(1, 4)
        ]
        _collect(
            db, iteration, live_query, live_payload, config=config,
            signals=[
                {
                    "signal_type": "FLIGHT", "city_id": city,
                    "track": "UNKNOWN",
                    "observed_at": iso(ANCHOR - timedelta(minutes=20)),
                    "quality": 1.0, "fr24_id": f["fr24_id"],
                    "callsign": f["callsign"], "registration": f["reg"],
                    "aircraft_type": f["type"], "origin_iata": f["orig_iata"],
                    "dest_iata": f["dest_iata"],
                    # No category field exists on a live-positions response.
                    "flight_category": "AMBIGUOUS",
                    "category_confidence": "AMBIGUOUS",
                    "flight_status": "airborne_inbound", "eta": f["eta"],
                }
                for f in live_payload
            ],
        )

        # Live records exist, so the expensive category resolution is now
        # worth buying — and only now.
        escalated = agent.escalate_to_history(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", iata="PHX", live_record_count=3,
            signal_id=social_ids[0], tracks=tracks,
        )
        assert escalated is not None
        history_query = db.claim_next_query(iteration, ["FLIGHT_HISTORY"])
        history_payload = [
            {"fr24_id": f"fr{i}", "callsign": f"RCH{i}", "category": "M"}
            for i in range(1, 4)
        ]
        _collect(db, iteration, history_query, history_payload, config=config)
        for record in history_payload:
            db._exec(
                "UPDATE signals SET flight_category = 'M', "
                "category_confidence = 'CONFIRMED' "
                "WHERE iteration_id = ? AND fr24_id = ?",
                (iteration, record["fr24_id"]),
            )

        lodging_query = db.claim_next_query(iteration, ["LODGING"])
        _collect(
            db, iteration, lodging_query, [{"id": "L1"}], config=config,
            signals=[{
                "signal_type": "LODGING", "city_id": city,
                "location_id": location, "track": "UNKNOWN",
                "observed_at": iso(ANCHOR), "quality": 1.0,
                "provider_ref": "L1", "item_name": "Downtown Loft",
                "near_available": 2, "near_total": 30,
                "base_available": 28, "base_total": 30,
                "drop_pct": 92.9, "distance_km": 4.2,
            }],
        )

        car_query = db.claim_next_query(iteration, ["CAR"])
        _collect(
            db, iteration, car_query,
            [{"totalResultsAvailable": 3, "resultsCount": 3}], config=config,
            signals=[{
                "signal_type": "CAR", "city_id": city, "track": "UNKNOWN",
                "observed_at": iso(ANCHOR), "quality": 1.0,
                "provider_ref": "PHX", "vehicle_class": "FVAR",
                "vehicle_class_name": "Full-size Van", "people_capacity": 12,
                "partner_code": "AC", "partner_name": "ACE",
                "counter_type": "ON_AIR_SHUTTLE", "is_on_airport": True,
                "is_peer_to_peer": False, "near_available": 1, "near_total": 3,
                "base_available": 18, "base_total": 20, "drop_pct": 94.4,
                "distance_km": 2.9, "field_map_ver": "2026-07-27",
            }],
        )

        # --- Stage 6: correlate ---------------------------------------------
        db.set_stage(iteration, "CORRELATING")
        signals = [dict(r) for r in db.signals_for_city(iteration, city)]
        result = scoring.correlate(
            signals, track=AIRLIFT, anchor_at=ANCHOR,
            # `corr_cfg`, not the loaded mission's: this asserts an exact band,
            # so the weights and the thresholds have to come from the same
            # place. AIRLIFT pins the weights; this pins the rest.
            cfg=corr_cfg,
            unreliable_source_types=db.unreliable_source_types(iteration, city),
        )
        assert result.band == "HIGH"
        assert result.distinct_types == 4
        assert result.failed_families == []
        assert result.caveat() is None
        assert result.earliest_eta == "2026-07-27T11:40:00Z"

        correlation_id = db.upsert_correlation(
            iteration_id=iteration, city_id=city, track=AIRLIFT.name,
            score=result.score, band=result.band,
            distinct_types=result.distinct_types,
            contributions=result.contributions,
            data_completeness=result.data_completeness,
            failed_sources=result.failed_families,
            band_capped=result.band_capped, rule_trace=result.rule_trace,
        )
        for signal_id, contribution in result.signal_contributions.items():
            db.link_correlation_signal(correlation_id, signal_id, contribution)

        # --- Stage 7: alert -------------------------------------------------
        db.set_stage(iteration, "ALERTING")
        db.insert_alert(
            correlation_id=correlation_id, session_id=session,
            iteration_id=iteration, city_id=city, track=AIRLIFT.name,
            confidence_score=result.score, confidence_band=result.band,
            summary="Three military-category aircraft are inbound to Phoenix "
                    "Sky Harbor while lodging and full-size van availability "
                    "near the Riverside Fairground have collapsed.",
            caveat=result.caveat(), earliest_eta=result.earliest_eta,
            model="stub",
        )

        # --- Stage 8: schedule ----------------------------------------------
        db.set_stage(iteration, "SCHEDULING")
        scheduled = agent.run_schedule(iteration, session, tracks)
        assert scheduled["revisit"] > 0
        db.finish_iteration(iteration, outcome="COMPLETE")

        # --- The delivered product ------------------------------------------
        rows = db.get_alerts(session)
        assert len(rows) == 1
        alert = Alert.from_rows(rows[0], db.correlation_signals(correlation_id))
        posts_out, flights_out, lodging_out, cars_out = alert.as_tuple()

        assert len(posts_out) == 2
        assert len(flights_out) == 3
        assert len(lodging_out) == 1
        assert len(cars_out) == 1
        assert alert.confidence_band == "HIGH"
        assert alert.city == "Phoenix, AZ"

        # The LLM copies the score; it never computes it.
        assert alert.confidence_score == db.get_correlation(correlation_id)["score"]

        # Category was resolved by the historical query, not assumed.
        assert all(f.category == "M" for f in flights_out)
        assert all(f.category_confidence == "CONFIRMED" for f in flights_out)
        # The ETA the old pipeline collected and then dropped at parse time.
        assert flights_out[0].eta
        assert cars_out[0].people_capacity == 12

        assert len(alert.as_positional()) == 4

    def test_every_signal_traces_back_to_a_query(self, db, config):
        """No analytical record without provenance.

        Each signal must resolve to a raw payload and each payload to the queue
        row that fetched it, so a reader can ask "where did this come from"
        and get an answer rather than a model's recollection.
        """
        config["tipping"]["max_queries_per_city"] = 99
        session, city, location, iteration = _bootstrap(db, config)
        agent = QueueAgent(db, config)
        agent.run_seed(iteration, session)
        query = db.claim_next_query(iteration, ["SOCIAL"])
        _collect(
            db, iteration, query, [{"url": "https://x/1"}], config=config,
            signals=[{
                "signal_type": "SOCIAL", "city_id": city,
                "observed_at": iso(ANCHOR), "url": "https://x/1",
                "source_domain": "x.com", "salience": 0.8,
            }],
        )
        chain = db.all(
            "SELECT s.signal_id, r.raw_id, q.query_id, q.endpoint, q.rule_code "
            "FROM signals s "
            "JOIN raw_results r USING (raw_id) "
            "JOIN query_queue q ON q.query_id = r.query_id"
        )
        assert len(chain) == 1
        assert chain[0]["rule_code"] == "R0_SEED"
        assert chain[0]["endpoint"]

    def test_audit_trail_records_the_decision_not_to_search(self, db, config):
        """expand_cities=false must leave a record, not silence."""
        session, city, location, iteration = _bootstrap(
            db, config, expand_cities=False
        )
        agent = QueueAgent(db, config)
        assert agent.admit_city(
            iteration_id=iteration, session_id=session, name="Tucson",
            signals=[{"source_domain": "a.com", "salience": 0.9},
                     {"source_domain": "b.org", "salience": 0.9}],
            expand_cities=bool(db.get_session(session)["expand_cities"]),
        ) is None
        decisions = db.get_queue_decisions(iteration)
        refusal = [d for d in decisions if d["outcome"] == "CITY_NOT_ADMITTED"]
        assert len(refusal) == 1
        assert refusal[0]["city_name"] == "Tucson"
        assert "expand_cities=false" in refusal[0]["detail"]
        assert not db.all(
            "SELECT 1 FROM query_queue WHERE city_id IS NOT ?", (city,)
        )


class TestDegradedCollectionEndToEnd:
    """A broken connector must produce a caveated alert, not a confident one.

    This is the failure direction that matters. The old connectors caught every
    exception and returned [], which made an expired token indistinguishable from
    "no military flights inbound" — and in this system that becomes a suppressed
    warning.
    """

    def test_failed_flight_query_caps_the_band_and_names_the_gap(self, db, config):
        config["tipping"]["max_queries_per_city"] = 99
        session, city, location, iteration = _bootstrap(db, config)
        agent = QueueAgent(db, config)
        tracks = db.session_tracks(session)

        social_id = db.insert_signal(
            iteration_id=iteration, signal_type="SOCIAL", city_id=city,
            location_id=location, track=AIRLIFT.name,
            observed_at=iso(ANCHOR - timedelta(hours=1)), quality=0.9,
            url="https://x/1", source_domain="x.com", salience=0.9,
        )
        db.insert_signal(
            iteration_id=iteration, signal_type="SOCIAL", city_id=city,
            track=AIRLIFT.name, observed_at=iso(ANCHOR - timedelta(hours=2)),
            url="https://apnews.com/2", source_domain="apnews.com", salience=0.9,
        )
        db.insert_signal(
            iteration_id=iteration, signal_type="SOCIAL", city_id=city,
            track=AIRLIFT.name, observed_at=iso(ANCHOR - timedelta(hours=3)),
            url="https://reuters.com/3", source_domain="reuters.com",
            salience=0.9,
        )
        agent.tip_from_social(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", state="AZ", signal_id=social_id, tracks=tracks,
        )

        # The live flight query fails with an auth error. History is never
        # enqueued, because it is only bought once live records exist.
        live_query = db.claim_next_query(iteration, ["FLIGHT_LIVE"])
        db.fail_query(live_query["query_id"], "401 Unauthorized: token expired")

        # Lodging and cars collapse, which alone would look alarming.
        lodging_query = db.claim_next_query(iteration, ["LODGING"])
        _collect(
            db, iteration, lodging_query, [{"id": "L1"}], config=config,
            signals=[{
                "signal_type": "LODGING", "city_id": city,
                "location_id": location, "observed_at": iso(ANCHOR),
                "provider_ref": "L1", "near_available": 0, "base_available": 30,
                "drop_pct": 100.0, "distance_km": 3.0,
            }],
        )
        car_query = db.claim_next_query(iteration, ["CAR"])
        _collect(
            db, iteration, car_query, [{"totalResultsAvailable": 0}],
            config=config,
            signals=[{
                "signal_type": "CAR", "city_id": city,
                "observed_at": iso(ANCHOR), "provider_ref": "PHX",
                "vehicle_class": "FVAR", "people_capacity": 12,
                "is_on_airport": True, "near_available": 0,
                "base_available": 20, "drop_pct": 100.0, "distance_km": 2.9,
            }],
        )

        unreliable = db.unreliable_source_types(iteration, city)
        assert set(unreliable) == {"FLIGHT_LIVE"}

        result = scoring.correlate(
            [dict(r) for r in db.signals_for_city(iteration, city)],
            track=AIRLIFT, anchor_at=ANCHOR, cfg=config["correlation"],
            unreliable_source_types=unreliable,
        )

        # Three families of real evidence, and the flight dimension unknown
        # rather than empty. MEDIUM is earned here on the strength of three
        # independent social domains plus two collapsed booking signals near the
        # facility, so band_capped is False — nothing was reduced. Capping
        # further would understate evidence that genuinely exists.
        assert result.distinct_types == 3
        assert result.band == "MEDIUM"
        assert result.band_capped is False
        assert result.failed_families == ["FLIGHT"]
        assert result.data_completeness == 0.75

        # What the reader is told: the gap is named, and its meaning spelled
        # out. This is the sentence that stops a missing flight picture from
        # reading as "no aircraft inbound".
        caveat = result.caveat()
        assert "flight" in caveat
        assert "not evidence of their absence" in caveat
        assert "75%" in caveat
        # No capping claim, because no capping happened.
        assert "capped" not in caveat

    def test_the_same_evidence_would_be_capped_if_it_reached_high(self, db, config):
        """The cap is what stops partial collection from ever reading HIGH."""
        session, city, location, iteration = _bootstrap(db, config)
        strong = [
            {
                "signal_type": "SOCIAL", "city_id": city,
                "observed_at": iso(ANCHOR), "url": f"https://{d}/1",
                "source_domain": d, "salience": 1.0,
            }
            for d in ("a.com", "b.org", "c.net")
        ] + [
            {
                "signal_type": "FLIGHT", "city_id": city,
                "observed_at": iso(ANCHOR), "fr24_id": f"m{i}",
                "flight_category": "M", "category_confidence": "CONFIRMED",
            }
            for i in range(3)
        ] + [
            {
                "signal_type": "LODGING", "city_id": city,
                "location_id": location, "observed_at": iso(ANCHOR),
                "provider_ref": "L1", "near_available": 0,
                "base_available": 30, "distance_km": 3.0,
            },
            {
                "signal_type": "CAR", "city_id": city,
                "observed_at": iso(ANCHOR), "provider_ref": "PHX",
                "vehicle_class": "FVAR", "people_capacity": 12,
                "is_on_airport": True, "near_available": 0,
                "base_available": 20, "distance_km": 2.9,
            },
        ]
        for fields in strong:
            db.insert_signal(iteration_id=iteration, **fields)
        signals = [dict(r) for r in db.signals_for_city(iteration, city)]

        clean = scoring.correlate(
            signals, track=AIRLIFT, anchor_at=ANCHOR,
            cfg=config["correlation"],
        )
        assert clean.band == "HIGH"

        degraded = scoring.correlate(
            signals, track=AIRLIFT, anchor_at=ANCHOR,
            cfg=config["correlation"], unreliable_source_types=["CAR"],
        )
        assert degraded.band == "MEDIUM"
        assert degraded.band_capped is True
        assert degraded.score == clean.score
        assert "capped" in degraded.caveat()

    def test_budget_exhaustion_degrades_rather_than_failing(self, db, config):
        """An exhausted quota must skip the query and finish the iteration."""
        config["tipping"]["max_queries_per_city"] = 99
        session, city, location, iteration = _bootstrap(db, config)
        budget = BudgetGuard(db, config)
        budget.seed_budgets()
        db.set_budget("PRICELINE", None, "MONTH", 0.0)
        agent = QueueAgent(db, config, budget=budget)

        tipped = agent.tip_from_social(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", state="AZ", signal_id=None,
            tracks=["AIRSHOW"],
        )
        assert "CAR" not in tipped
        refusals = [
            d for d in db.get_queue_decisions(iteration)
            if d["outcome"] == "BUDGET_EXHAUSTED"
        ]
        assert len(refusals) == 1
        assert "PRICELINE" in refusals[0]["detail"]

        # The iteration still completes, marked PARTIAL rather than FAILED.
        db.append_degradation(
            iteration, "car: PRICELINE monthly quota exhausted",
            source="TIPPING")
        db.finish_iteration(iteration, outcome="PARTIAL")
        row = db.get_iteration(iteration)
        assert row["outcome"] == "PARTIAL"
        assert any("PRICELINE" in n for n in db.degradation_notes(iteration))


class TestRetentionAcrossThePipeline:
    def test_purged_payload_leaves_the_alert_intact(self, db, config):
        """After 30 days the FR24 payload must go; the warning it produced stays."""
        session, city, location, iteration = _bootstrap(db, config)
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration,
            source_type="FLIGHT_LIVE",
            endpoint="/api/live/flight-positions/full", params={},
            dedup_key="k1", city_id=city,
        )
        raw_id = db.insert_raw_result(
            query_id=query_id, iteration_id=iteration,
            source_type="FLIGHT_LIVE", provider="FR24",
            payload=[{"fr24_id": "x"}],
            retention_days=retention_days(config, "FR24"),
        )
        signal_id = db.insert_signal(
            iteration_id=iteration, raw_id=raw_id, signal_type="FLIGHT",
            city_id=city, observed_at=iso(ANCHOR), fr24_id="x",
            flight_category="M", category_confidence="CONFIRMED",
        )
        correlation_id = db.upsert_correlation(
            iteration_id=iteration, city_id=city, track=AIRLIFT.name,
            score=0.35, band="LOW", distinct_types=1, contributions={},
            data_completeness=1.0, rule_trace="t",
        )
        db.link_correlation_signal(correlation_id, signal_id, 0.35)
        db.insert_alert(
            correlation_id=correlation_id, session_id=session,
            iteration_id=iteration, city_id=city, track=AIRLIFT.name,
            confidence_score=0.35, confidence_band="LOW", summary="s",
            model="stub",
        )

        db._exec(
            "UPDATE raw_results SET purge_after = ? WHERE raw_id = ?",
            (iso(ANCHOR - timedelta(days=1)), raw_id),
        )
        assert RetentionService(db, config).prune() == 1

        assert db.get_raw_result(raw_id) is None
        evidence = db.correlation_signals(correlation_id)
        assert len(evidence) == 1
        assert evidence[0]["flight_category"] == "M"
        assert evidence[0]["raw_id"] is None
        assert len(db.get_alerts(session)) == 1

    def test_fr24_retention_cannot_be_extended_by_configuration(self, config):
        config["flightradar"]["retention_days"] = 3650
        assert retention_days(config, "FR24") == 30


class TestAuditCompleteness:
    def test_every_queue_attempt_has_a_decision_row(self, db, config):
        """The count of decisions must equal the count of attempts, always."""
        config["tipping"]["max_queries_per_city"] = 3
        session, city, location, iteration = _bootstrap(db, config)
        agent = QueueAgent(db, config)
        attempts = 0
        for i in range(10):
            agent.enqueue(
                iteration_id=iteration, session_id=session,
                source_type="SOCIAL", endpoint="/v1/twitter/posts",
                params={"query": f"q{i}"}, rule_code="T", city_id=city,
            )
            attempts += 1
        assert sum(db.decision_counts(iteration).values()) == attempts
        assert db.count_queued(iteration) == 3

    def test_iteration_budget_plan_is_recorded_before_spending(self, db, config):
        session, city, location, iteration = _bootstrap(db, config)
        budget = BudgetGuard(db, config)
        budget.seed_budgets()
        plan = budget.plan_iteration(iteration)
        stored = json.loads(db.get_iteration(iteration)["budget_plan_json"])
        assert set(stored) == set(plan)
        assert all(v >= 0 for v in stored.values())
