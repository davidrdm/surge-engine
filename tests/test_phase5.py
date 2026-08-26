"""Phases 5 and 5a: correlation, alerting, scheduling, and the hotel price signal.

Two properties carry most of the weight here.

The LLM writes prose and nothing else — `alerts.confidence_score` must be
byte-identical to `correlations.score`, and an unavailable model must still
produce an alert rather than withholding a scored finding.

And the price signal must stay inside the LODGING family. `distinct_types` feeds
the band thresholds, so a fifth family would silently move every band in the
system.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from conftest import AIRLIFT, ANCHOR
from surge_iw.agents.alerting import AlertAgent, _fallback_summary
from surge_iw.agents.correlation import CorrelationAgent
from surge_iw.agents.queueing import QueueAgent
from surge_iw.base import scoring
from surge_iw.base.agent import AgentError
from surge_iw.connectors import staying as st
from surge_iw.db.database import iso
from test_triage import FakeLLM


def signal(db, iteration, city, **fields):
    return db.insert_signal(iteration_id=iteration, city_id=city, **fields)


def four_signal_city(db, iteration, city, location=None):
    """A city with all four families present and strong.

    The social rows carry the reference pack's `chatter` stream: since pack
    version 2 the mission scores its streams by name, and a streamless SOCIAL
    row would be an unknown kind weighted at zero.
    """
    for index, domain in enumerate(("a.com", "b.org", "c.net"), start=1):
        signal(db, iteration, city, signal_type="SOCIAL", track=AIRLIFT.name,
               stream="chatter",
               observed_at=iso(ANCHOR - timedelta(hours=index)),
               url=f"https://{domain}/{index}", source_domain=domain,
               salience=0.95, snippet="Crews staging at the fairground",
               quality=0.95)
    for index, domain in enumerate(("d.news", "e.news", "f.news"), start=1):
        signal(db, iteration, city, signal_type="SOCIAL", track=AIRLIFT.name,
               stream="local_news",
               observed_at=iso(ANCHOR - timedelta(hours=index)),
               url=f"https://{domain}/{index}", source_domain=domain,
               salience=0.95, snippet="Organisers confirm the load-in",
               quality=0.95)
    for index in range(3):
        signal(db, iteration, city, signal_type="FLIGHT",
               observed_at=iso(ANCHOR - timedelta(minutes=20)),
               fr24_id=f"m{index}", callsign=f"RCH{index}", aircraft_type="C17",
               origin_iata="DOV", dest_iata="PHX", flight_category="M",
               category_confidence="CONFIRMED", flight_status="airborne_inbound",
               eta=f"2026-07-27T1{index}:40:00Z", quality=1.0)
    signal(db, iteration, city, signal_type="LODGING", location_id=location,
           observed_at=iso(ANCHOR), provider_ref="L1", item_name="Loft",
           near_available=0, base_available=30, drop_pct=100.0,
           distance_km=3.0, quality=1.0)
    signal(db, iteration, city, signal_type="CAR", observed_at=iso(ANCHOR),
           provider_ref="PHX", vehicle_class="FVAR", people_capacity=12,
           is_on_airport=True, near_available=0, base_available=20,
           drop_pct=100.0, distance_km=2.9, quality=1.0)


@pytest.fixture
def scored(db, config, session, iteration):
    """A Phoenix with four families of evidence, already correlated."""
    city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
    location = db.insert_key_location(city, "Riverside Fairground")
    four_signal_city(db, iteration, city, location)
    assert CorrelationAgent(db, config).run(iteration) is True
    return city, location


# ===========================================================================
# Phase 5 — correlation
# ===========================================================================


class TestCorrelation:
    def test_a_four_family_city_scores_high(self, db, config, session,
                                            iteration, scored):
        city, _ = scored
        row = db.one(
            "SELECT * FROM correlations WHERE city_id = ? AND track = ?",
            (city, "AIRSHOW"))
        assert row["band"] == "HIGH"
        # Five: the four engine families plus the pack's promoted LOCAL_NEWS.
        assert row["distinct_types"] == 5
        assert row["data_completeness"] == 1.0
        assert row["rule_trace"]

    def test_the_score_records_the_tunables_it_was_computed_under(
        self, db, config, session, iteration, scored
    ):
        """Correlation is the one judgement made without a model, so it writes
        no receipt — and the tunables behind a score otherwise lived only in a
        config file anyone may edit afterwards. Found live: re-scoring a
        stored iteration produced different numbers and nothing on the row
        could say whether the engine or the operator's config had moved."""
        from surge_iw.services import receipts

        city, _ = scored
        row = db.one(
            "SELECT * FROM correlations WHERE city_id = ? AND track = ?",
            (city, "AIRSHOW"))
        assert row["config_hash"] == receipts.config_fingerprint(config)

    def test_a_changed_tunable_changes_the_recorded_hash(
        self, db, config, session, iteration, scored
    ):
        """The point of recording it: a score computed under different
        settings must be distinguishable from one computed under these."""
        from surge_iw.agents.correlation import CorrelationAgent
        from surge_iw.services import receipts

        city, _ = scored
        before = db.one(
            "SELECT config_hash FROM correlations WHERE city_id = ? "
            "AND track = ?", (city, "AIRSHOW"))["config_hash"]
        config["correlation"]["car_drop_full_scale"] = 33.0
        CorrelationAgent(db, config).run(iteration)
        after = db.one(
            "SELECT config_hash FROM correlations WHERE city_id = ? "
            "AND track = ?", (city, "AIRSHOW"))["config_hash"]
        assert after != before
        assert after == receipts.config_fingerprint(config)

    def test_the_working_is_recorded(self, db, config, session, iteration, scored):
        """contributions_json is what makes a score arguable after the fact."""
        city, _ = scored
        # Named track, not whichever row comes first: the evidence is
        # attributed to one track, so the others legitimately score without it.
        row = db.one(
            "SELECT * FROM correlations WHERE city_id = ? AND track = ?",
            (city, "AIRSHOW"))
        contributions = json.loads(row["contributions_json"])
        assert set(contributions) >= {"chatter", "flight_M", "lodging", "car"}
        assert sum(contributions.values()) == pytest.approx(row["score"], abs=0.001)

    def test_every_contributing_signal_is_linked(self, db, config, session,
                                                 iteration, scored):
        city, _ = scored
        correlation = db.one(
            "SELECT * FROM correlations WHERE city_id = ? AND track = ?",
            (city, "AIRSHOW"))
        linked = db.correlation_signals(correlation["correlation_id"])
        assert len(linked) == 11    # 3 chatter + 3 local_news + 3 flight + 1 + 1
        assert {row["signal_type"] for row in linked} == {
            "SOCIAL", "FLIGHT", "LODGING", "CAR"}

    def test_every_track_is_scored_from_one_collection(self, db, config, session,
                                                       iteration, scored, mission):
        """One collection pass, scored once per track.

        Asserted against the loaded mission rather than a hardcoded pair, so
        the test says what it means — every track gets scored — instead of
        encoding how many tracks one particular mission happens to have.
        """
        city, _ = scored
        tracks = {r["track"] for r in db.get_correlations(iteration)}
        assert tracks == set(mission.tracks)

    def test_a_quiet_city_produces_no_correlation(self, db, config, session,
                                                  iteration):
        """Recording a zero for every quiet city on every track would bury the
        real ones."""
        db.insert_city(session, "Tucson", canonical="tucson")
        CorrelationAgent(db, config).run(iteration)
        assert db.get_correlations(iteration) == []

    def test_a_failed_query_caps_the_band_even_with_full_evidence(
        self, db, config, session, iteration
    ):
        """The single most important property in the system."""
        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        four_signal_city(db, iteration, city)
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="CAR",
            endpoint="/search-rental-car", params={}, dedup_key="k1",
            city_id=city)
        db.fail_query(query_id, "401 expired")

        CorrelationAgent(db, config).run(iteration)
        row = db.one("SELECT * FROM correlations WHERE track = 'AIRSHOW'")
        assert row["band"] == "MEDIUM"
        assert row["band_capped"] == 1
        assert "CAR" in row["failed_sources"]
        assert row["data_completeness"] < 1.0

    def test_a_city_with_only_gaps_is_still_correlated(self, db, config, session,
                                                       iteration):
        """Otherwise a total collection failure would look like a quiet city."""
        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="LODGING",
            endpoint="/search", params={}, dedup_key="k1", city_id=city)
        db.fail_query(query_id, "500")
        CorrelationAgent(db, config).run(iteration)
        rows = db.get_correlations(iteration)
        assert rows and rows[0]["data_completeness"] < 1.0

    def test_correlation_never_uses_an_llm(self):
        """Asserted structurally: the agent has no model client at all."""
        from surge_iw.base.agent import BaseAgent, LLMAgent
        assert issubclass(CorrelationAgent, BaseAgent)
        assert not issubclass(CorrelationAgent, LLMAgent)


# ===========================================================================
# Phase 5 — alerting
# ===========================================================================


class TestAlerting:
    def test_an_alert_is_written_with_its_evidence(self, db, config, session,
                                                   iteration, scored):
        llm = FakeLLM({"summary": "Three military aircraft inbound to Phoenix."})
        assert AlertAgent(db, config, llm).run(iteration) is True

        alerts = db.get_alerts(session)
        assert alerts
        airshow = next(a for a in alerts if a["track"] == "AIRSHOW")
        assert airshow["confidence_band"] == "HIGH"
        assert "military aircraft" in airshow["summary"]
        assert airshow["earliest_eta"] == "2026-07-27T10:40:00Z"

    def test_the_model_cannot_move_the_score(self, db, config, session,
                                             iteration, scored):
        """The gate. A model that returns a number must not change anything."""
        llm = FakeLLM({"summary": "Nothing to see here.",
                       "confidence_score": 0.01, "band": "LOW", "score": 0.01})
        AlertAgent(db, config, llm).run(iteration)

        for alert in db.get_alerts(session):
            correlation = db.get_correlation(alert["correlation_id"])
            assert alert["confidence_score"] == correlation["score"]
            assert alert["confidence_band"] == correlation["band"]

    def test_a_model_failure_still_produces_an_alert(self, db, config, session,
                                                     iteration, scored):
        """A scored finding that reaches nobody is worse than clumsy prose."""
        agent = AlertAgent(db, config, FakeLLM())
        agent._call_llm_json = lambda *a, **k: (_ for _ in ()).throw(
            AgentError("model unavailable"))
        assert agent.run(iteration) is True

        alerts = db.get_alerts(session)
        assert alerts
        airshow = next(a for a in alerts if a["track"] == "AIRSHOW")
        assert "fallback" in airshow["model"]
        assert airshow["summary"]
        assert airshow["confidence_band"] == "HIGH"

    def test_an_empty_summary_falls_back(self, db, config, session, iteration,
                                         scored):
        AlertAgent(db, config, FakeLLM({"summary": "   "})).run(iteration)
        assert "fallback" in db.get_alerts(session)[0]["model"]

    def test_the_caveat_is_deterministic_not_model_written(
        self, db, config, session, iteration
    ):
        """So it cannot be softened, omitted or paraphrased into something
        less alarming."""
        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        four_signal_city(db, iteration, city)
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="CAR",
            endpoint="/search-rental-car", params={}, dedup_key="k1",
            city_id=city)
        db.fail_query(query_id, "401")
        CorrelationAgent(db, config).run(iteration)
        AlertAgent(db, config, FakeLLM({"summary": "All clear."})).run(iteration)

        alert = next(a for a in db.get_alerts(session)
                     if a["track"] == "AIRSHOW")
        assert "not evidence of their absence" in alert["caveat"]
        assert "car" in alert["caveat"]
        assert "capped" in alert["caveat"]

    def test_correlations_below_the_floor_produce_no_alert(
        self, db, config, session, iteration
    ):
        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        signal(db, iteration, city, signal_type="LODGING",
               observed_at=iso(ANCHOR), provider_ref="L1",
               near_available=29, base_available=30, drop_pct=3.3,
               distance_km=3.0)
        CorrelationAgent(db, config).run(iteration)
        AlertAgent(db, config, FakeLLM({"summary": "x"})).run(iteration)
        assert db.get_alerts(session) == []

    def test_the_model_is_not_shown_the_score(self, db, config, session,
                                              iteration, scored):
        """Showing a number it is told not to characterise invites it to."""
        llm = FakeLLM({"summary": "ok"})
        AlertAgent(db, config, llm).run(iteration)
        for prompt in llm.prompts:
            brief = json.loads(prompt)
            assert "score" not in brief
            assert "band" not in brief
            assert "confidence" not in json.dumps(brief).lower()

    def test_rerunning_does_not_duplicate_alerts(self, db, config, session,
                                                 iteration, scored):
        agent = AlertAgent(db, config, FakeLLM({"summary": "ok"}))
        agent.run(iteration)
        first = len(db.get_alerts(session))
        agent.run(iteration)
        assert len(db.get_alerts(session)) == first

    def test_alerts_are_returned_most_severe_first(self, db, config, session,
                                                   iteration, scored):
        """Anyone opening the list needs HIGH before LOW, not whichever
        actor track happened to be scored last."""
        AlertAgent(db, config, FakeLLM({"summary": "ok"})).run(iteration)
        order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        bands = [a["confidence_band"] for a in db.get_alerts(session)]
        assert bands == sorted(bands, key=lambda band: -order[band])

    def test_the_fallback_summary_names_what_was_seen(self):
        brief = {
            "city": "Phoenix, AZ", "track": "AIRSHOW",
            "social_posts": [{}, {}],
            "flights": [{"category": "M", "category_certainty": "CONFIRMED"}],
            "lodging": [{"availability_drop_pct": 92.0}],
            "rental_cars": [{"availability_drop_pct": 84.0}],
        }
        text = _fallback_summary(brief)
        assert "Phoenix, AZ" in text
        assert "2 social media report(s)" in text
        assert "confirmed military/government" in text
        assert "92%" in text


# ===========================================================================
# Phase 5a — hotel price escalation
# ===========================================================================


class TestPriceEscalationScoring:
    def test_price_alone_carries_the_lodging_family(self, corr_cfg):
        """A price exists for a property whose calendar does not — roughly
        fourteen listings in fifteen, measured live."""
        rows = [{"signal_type": "LODGING", "price_near": 280.0,
                 "price_baseline": 200.0, "distance_km": 3.0,
                 "observed_at": iso(ANCHOR), "track": "UNKNOWN",
                 "signal_id": 1}]
        result = scoring.correlate(rows, track=AIRLIFT, anchor_at=ANCHOR,
                                   cfg=corr_cfg)
        assert result.contributions["lodging"] > 0

    def test_the_stronger_measurement_wins(self, corr_cfg):
        """Averaging would let a missing calendar dilute a real price signal."""
        price_only = [{"price_near": 280.0, "price_baseline": 200.0}]
        avail_only = [{"near_available": 0, "base_available": 30}]
        both = [{"price_near": 280.0, "price_baseline": 200.0,
                 "near_available": 0, "base_available": 30}]
        q_price = scoring.lodging_quality(price_only, corr_cfg)
        q_avail = scoring.lodging_quality(avail_only, corr_cfg)
        assert scoring.lodging_quality(both, corr_cfg) == max(q_price, q_avail)

    def test_price_escalation_is_aggregated_not_averaged(self):
        """One cheap room tripling must not outweigh a steady set."""
        rows = [{"price_near": 300.0, "price_baseline": 100.0},
                {"price_near": 900.0, "price_baseline": 900.0}]
        assert scoring.price_escalation(rows) == pytest.approx(20.0)

    def test_a_price_fall_is_not_a_signal(self):
        assert scoring.price_escalation(
            [{"price_near": 80.0, "price_baseline": 200.0}]) == 0.0

    def test_properties_priced_in_one_window_only_are_ignored(self):
        rows = [{"price_near": 300.0, "price_baseline": None},
                {"price_near": None, "price_baseline": 100.0}]
        assert scoring.price_escalation(rows) == 0.0

    def test_lodging_price_stays_in_the_lodging_family(self):
        """The gate: a fifth family would silently move every band threshold."""
        assert scoring.SOURCE_TYPE_FAMILY["LODGING_PRICE"] == "LODGING"
        assert scoring.SOURCE_TYPE_FAMILY["LODGING"] == "LODGING"
        assert len(scoring.FAMILIES) == 4

    def test_a_failed_price_query_is_a_lodging_gap(self, corr_cfg):
        result = scoring.correlate([], track=AIRLIFT, anchor_at=ANCHOR,
                                   cfg=corr_cfg,
                                   unreliable_source_types=["LODGING_PRICE"])
        assert result.failed_families == ["LODGING"]
        assert result.data_completeness == 0.75

    def test_price_and_availability_gaps_count_once(self, corr_cfg):
        result = scoring.correlate(
            [], track=AIRLIFT, anchor_at=ANCHOR, cfg=corr_cfg,
            unreliable_source_types=["LODGING", "LODGING_PRICE"])
        assert result.failed_families == ["LODGING"]
        assert result.data_completeness == 0.75


class TestPricePairing:
    def test_only_properties_priced_in_both_windows_are_paired(self):
        near = [{"property_ref": "g1", "name": "Hyatt", "price_min": 320.0},
                {"property_ref": "g2", "name": "Westin", "price_min": 210.0}]
        base = [{"property_ref": "g1", "name": "Hyatt", "price_min": 200.0}]
        signals = st.price_signal(near, base)
        assert [s["provider_ref"] for s in signals] == ["g1"]
        assert signals[0]["price_near"] == 320.0
        assert signals[0]["price_baseline"] == 200.0

    def test_availability_columns_are_not_populated(self):
        """A zero there would read as total scarcity."""
        near = [{"property_ref": "g1", "name": "H", "price_min": 300.0}]
        base = [{"property_ref": "g1", "name": "H", "price_min": 200.0}]
        signal_row = st.price_signal(near, base)[0]
        assert "near_available" not in signal_row
        assert "base_available" not in signal_row

    def test_a_missing_price_drops_the_pair(self):
        near = [{"property_ref": "g1", "name": "H", "price_min": None}]
        base = [{"property_ref": "g1", "name": "H", "price_min": 200.0}]
        assert st.price_signal(near, base) == []

    def test_identity_is_the_listing_id_we_asked_about(self):
        """Direct mode (8.11). The row identity is the id we supplied, echoed
        back — not a name the provider chose, and not a Google id that measured
        live is never returned at all."""
        row = st._normalise_price_compare({
            "ota": "airbnb", "platform": "airbnb", "listingId": "24993534",
            "totalPrice": 400.45, "nightlyPrice": 167, "nights": 2,
            "currency": "USD", "fees": {"taxes": 66.45},
            "url": "https://www.airbnb.com/rooms/24993534",
        })
        assert row["property_ref"] == "24993534"
        assert row["price_min"] == 400.45
        assert row["price_nightly"] == 167
        assert row["platform"] == "airbnb"

    def test_offers_are_unwrapped_from_the_single_object_response(self):
        """The real shape, which the Google-mode parser never handled: one
        object with per-listing `offers[]`, not a list. This raised SchemaError
        on the first live call."""
        body = {"data": {"status": "completed", "result": {
            "min": 400.45, "median": 439.23, "property": "airbnb:1, airbnb:2",
            "offers": [
                {"listingId": "1", "platform": "airbnb", "totalPrice": 400.45},
                {"listingId": "2", "platform": "airbnb", "totalPrice": 478.0},
            ]}}}
        offers = st._unwrap_offers(body)
        assert [o["listingId"] for o in offers] == ["1", "2"]

    def test_a_batch_that_priced_nothing_is_empty_not_an_error(self):
        """`all_actors_failed` is charged 0 credits and happens for real — no
        listing could be quoted for a check-in today. Partial coverage is
        normal and `price_signal` pairs only what appears in both windows."""
        assert st._unwrap_offers(
            {"data": {"status": "failed",
                      "error": {"code": "all_actors_failed"}}}) == []
        assert st._unwrap_offers({"data": {"result": {"offers": []}}}) == []


class TestPriceCompareIsGated:
    def test_no_price_query_is_enqueued_while_disabled(self, db, config,
                                                       session, iteration):
        """The gate: off by default until the per-call credit cost is measured."""
        assert config["staying"]["enable_price_compare"] is False
        city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        db.insert_key_location(city, "Riverside Fairground")
        config["tipping"]["max_queries_per_city"] = 99
        QueueAgent(db, config).tip_from_social(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", state="AZ", signal_id=None, tracks=["AIRSHOW"])
        types = {q["source_type"] for q in db.get_queue(iteration)}
        assert "LODGING_PRICE" not in types

    def test_enabling_it_adds_one_query_per_key_location(self, db, config,
                                                        session, iteration):
        config["staying"]["enable_price_compare"] = True
        config["tipping"]["max_queries_per_city"] = 99
        city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        db.insert_key_location(city, "Riverside Fairground")
        db.insert_key_location(city, "County Recorder")
        enqueued = QueueAgent(db, config).tip_from_social(
            iteration_id=iteration, session_id=session, city_id=city,
            city_name="Phoenix", state="AZ", signal_id=None, tracks=["AIRSHOW"])
        assert enqueued["LODGING_PRICE"] == 2
        rows = [q for q in db.get_queue(iteration)
                if q["source_type"] == "LODGING_PRICE"]
        assert all(r["rule_code"] == "R9_LODGING_PRICE" for r in rows)
        assert all(r["location_id"] is not None for r in rows)


class TestPriceCollection:
    def test_a_price_signal_is_written_with_null_availability(
        self, db, config, session, iteration
    ):
        from surge_iw.agents.collection import CollectionAgent

        city = db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        location = db.insert_key_location(city, "Riverside Fairground")
        db.enqueue_query(
            session_id=session, iteration_id=iteration,
            source_type="LODGING_PRICE", endpoint="/price-compare",
            params={"location": "Riverside Fairground, Phoenix, AZ"},
            dedup_key="k1", city_id=city, location_id=location)

        # Direct mode prices the set the AVAILABILITY path pinned, so that set
        # has to exist first — pricing does not run its own discovery.
        db.put_geo_cache(
            "LISTING_SET", "airbnb|Riverside Fairground, Phoenix, AZ",
            [{"platform": "airbnb", "listing_id": "L1"},
             {"platform": "airbnb", "listing_id": "L2"}],
            resolved_by="API", ttl_days=14)

        near = [{"property_ref": "L1", "platform": "airbnb", "price_min": 320.0}]
        base = [{"property_ref": "L1", "platform": "airbnb", "price_min": 200.0}]

        class Stub:
            provider = "STAYING"

            def __init__(self):
                self.calls: list[dict] = []

            def price_compare(self, params, **kwargs):
                self.calls.append(dict(params))
                rows = near if len(self.calls) == 1 else base
                return st.SearchResult(records=rows, meta={"creditsCharged": 3})

        stub = Stub()
        CollectionAgent(db, config, {"STAYING": stub}).run(
            iteration, source_types=("LODGING_PRICE",))

        signals = db.signals_by_type(iteration, "LODGING")
        assert len(signals) == 1
        assert signals[0]["price_near"] == 320.0
        assert signals[0]["price_baseline"] == 200.0
        # The availability columns must stay NULL, not zero.
        assert signals[0]["near_available"] is None
        assert signals[0]["base_available"] is None
        assert signals[0]["location_id"] == location

    def test_both_windows_price_the_same_pinned_listings(
        self, db, config, session, iteration
    ):
        """The point of direct mode. Both calls carry the SAME listing ids, so
        the two windows cannot price different properties — which is exactly
        what Google mode did when measured live.

        Both windows are also shifted by `price_lead_days`, because no listing
        could be quoted for a check-in today or tomorrow.
        """
        from surge_iw.agents.collection import CollectionAgent

        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        location = db.insert_key_location(city, "Riverside Fairground")
        db.enqueue_query(
            session_id=session, iteration_id=iteration,
            source_type="LODGING_PRICE", endpoint="/price-compare",
            params={"location": "Riverside Fairground, Phoenix"},
            dedup_key="k1", city_id=city, location_id=location)
        db.put_geo_cache(
            "LISTING_SET", "airbnb|Riverside Fairground, Phoenix",
            [{"platform": "airbnb", "listing_id": "L1"},
             {"platform": "airbnb", "listing_id": "L2"}],
            resolved_by="API", ttl_days=14)

        rows = [{"property_ref": "L1", "platform": "airbnb", "price_min": 300.0}]

        class Stub:
            provider = "STAYING"

            def __init__(self):
                self.calls: list[dict] = []

            def price_compare(self, params, **kwargs):
                self.calls.append(dict(params))
                return st.SearchResult(records=rows, meta={})

        stub = Stub()
        CollectionAgent(db, config, {"STAYING": stub}).run(
            iteration, source_types=("LODGING_PRICE",))

        assert len(stub.calls) == 2
        assert stub.calls[0]["listings"] == "airbnb:L1,airbnb:L2"
        assert stub.calls[1]["listings"] == stub.calls[0]["listings"], (
            "both windows must price the same listings")
        assert "location" not in stub.calls[0], "direct mode, not Google mode"

        from datetime import date, timedelta
        lead = config["staying"]["price_lead_days"]
        assert date.fromisoformat(stub.calls[0]["checkIn"]) >= \
            date.today() + timedelta(days=lead), (
            "a check-in inside the lead time cannot be quoted at all")

    def test_pricing_without_a_pinned_set_is_skipped_not_discovered(
        self, db, config, session, iteration
    ):
        """Discovery belongs to the availability path. Re-resolving here would
        pay for /search twice and could pin a different set."""
        from surge_iw.agents.collection import CollectionAgent

        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        location = db.insert_key_location(city, "Riverside Fairground")
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration,
            source_type="LODGING_PRICE", endpoint="/price-compare",
            params={"location": "Nowhere, Phoenix"},
            dedup_key="k2", city_id=city, location_id=location)

        class Stub:
            provider = "STAYING"

            def price_compare(self, params, **kwargs):
                raise AssertionError("must not call the provider")

        CollectionAgent(db, config, {"STAYING": Stub()}).run(
            iteration, source_types=("LODGING_PRICE",))
        assert db.get_query(query_id)["status"] == "SKIPPED_NO_MAPPING"

    def test_no_property_priced_in_both_windows_is_skipped_not_scored(
        self, db, config, session, iteration
    ):
        from surge_iw.agents.collection import CollectionAgent

        city = db.insert_city(session, "Phoenix", canonical="phoenix")
        location = db.insert_key_location(city, "Riverside Fairground")
        query_id = db.enqueue_query(
            session_id=session, iteration_id=iteration,
            source_type="LODGING_PRICE", endpoint="/price-compare",
            params={"location": "x"}, dedup_key="k1", city_id=city,
            location_id=location)

        class Stub:
            provider = "STAYING"

            def __init__(self):
                self.calls = 0

            def price_compare(self, params, **kwargs):
                self.calls += 1
                rows = ([{"property_ref": "g1", "name": "H", "price_min": 300.0}]
                        if self.calls == 1 else [])
                return st.SearchResult(records=rows, meta={})

        CollectionAgent(db, config, {"STAYING": Stub()}).run(
            iteration, source_types=("LODGING_PRICE",))

        assert db.signals_by_type(iteration, "LODGING") == []
        row = db.one("SELECT * FROM query_queue WHERE query_id = ?", (query_id,))
        assert row["status"] == "SKIPPED_NO_MAPPING"
        # And that gap belongs to LODGING, not a fifth family.
        assert db.unreliable_source_types(iteration, city) == ["LODGING_PRICE"]
        assert scoring.SOURCE_TYPE_FAMILY["LODGING_PRICE"] == "LODGING"


class TestScheduling:
    def test_a_medium_alert_schedules_a_revisit(self, db, config, session,
                                                iteration, scored):
        from surge_iw.db.database import utcnow

        QueueAgent(db, config).run_schedule(iteration, session, ["AIRSHOW"])
        follow_ons = db.all(
            "SELECT * FROM query_queue WHERE iteration_id IS NULL "
            "AND origin = 'SCHEDULED'")
        assert follow_ons
        assert all(r["not_before"] > iso(utcnow()) for r in follow_ons)

    def test_scheduled_work_is_adopted_by_the_next_iteration(
        self, db, config, session, iteration, scored
    ):
        from datetime import timedelta as td

        from surge_iw.db.database import utcnow

        QueueAgent(db, config).run_schedule(iteration, session, ["AIRSHOW"])
        db._exec("UPDATE query_queue SET not_before = ? "
                 "WHERE iteration_id IS NULL",
                 (iso(utcnow() - td(hours=1)),))

        nxt = db.insert_iteration(session)
        counts = QueueAgent(db, config).run_seed(nxt, session)
        assert counts["adopted"] > 0
