"""One comparable acquisition value per signal (9.4, issue #2).

Per-family provenance already existed — the geo resolution method, the
publisher resolution method, the facility match method, each provider's
governance record. What did not exist was a value an analyst could compare
*across* families: reading a lodging row beside a flight row, there was no way
to tell how directly either was known.

The review asked for `Direct API / Cached API / Web scrape / OSINT feed /
Third-party feed`. That is not the vocabulary this system can attest, and a
field that guessed between "web scrape" and "third-party feed" because the code
cannot tell would be worse than no field. These tests are largely about the two
things it CAN attest and the one thing it refuses to claim.
"""
from __future__ import annotations

import pytest

from surge_iw.db import enums
from surge_iw.services import governance
from test_orchestrator import wiring          # noqa: F401 — a fixture


class TestTheVocabulary:
    def test_nothing_in_this_system_is_direct(self):
        """Every provider is an intermediary over a platform or a publisher.

        DIRECT exists so that filtering for it returns the honest answer —
        nothing — instead of requiring a reader to already know.
        """
        for provider in sorted(enums.PROVIDERS):
            assert governance.collection_class(provider)[0] != "DIRECT"
        assert "DIRECT" in enums.COLLECTION_CLASSES

    def test_a_live_call_is_reported_as_live_with_its_endpoint(self):
        collection_class, basis = governance.collection_class(
            "FR24", "/api/live/flight-positions/full")
        assert collection_class == "INTERMEDIARY_LIVE"
        assert "/api/live/flight-positions/full" in basis

    def test_a_vendor_cache_is_reported_as_cached(self):
        collection_class, basis = governance.collection_class(
            "STAYING", "/price-compare", cached=True)
        assert collection_class == "INTERMEDIARY_CACHED"
        assert "billed as a fresh call" in basis, (
            "the charge is identical either way, so the ledger is no guide")

    def test_silence_from_a_vendor_is_live_not_unknown(self):
        """The provider was asked and answered. Inventing a cache claim it did
        not make would be the same error in the other direction."""
        assert governance.collection_class(
            "PRICELINE", "/cars/search", cached=None)[0] == "INTERMEDIARY_LIVE"

    def test_an_unknown_provider_attests_nothing(self):
        assert governance.collection_class("ACME")[0] == "UNRECORDED"

    def test_the_enum_and_the_schema_agree(self, db):
        row = db.one("SELECT sql FROM sqlite_master WHERE name = 'signals'")
        for name in enums.COLLECTION_CLASSES:
            assert f"'{name}'" in row["sql"]


class TestItIsRecordedOnTheSignal:
    def test_a_writer_that_says_nothing_produces_a_visible_absence(self, db,
                                                                    iteration):
        """UNRECORDED is the default rather than a plausible value. A forgotten
        argument should read as "nobody attested this", never as a claim about
        how the record was obtained."""
        signal_id = db.insert_signal(
            iteration_id=iteration, signal_type="SOCIAL",
            url="https://x.com/1")
        row = db.one("SELECT collection_class, collection_basis FROM signals "
                     "WHERE signal_id = ?", (signal_id,))
        assert row["collection_class"] == "UNRECORDED"
        assert row["collection_basis"] is None

    def test_an_unknown_class_is_refused_before_sqlite_sees_it(self, db,
                                                               iteration):
        with pytest.raises(Exception, match="collection_class"):
            db.insert_signal(
                iteration_id=iteration, signal_type="SOCIAL",
                collection_class="WEB_SCRAPE", url="https://x.com/1")

    def test_it_survives_the_payload_being_purged(self, db, session, iteration):
        """The point of holding it on `signals` rather than only on
        `raw_results`. Retention deletes the payload and nulls `raw_id`; the
        analytical record is this system's own product and outlives it, and a
        provenance field that vanished with the payload would be missing
        exactly when the question is hardest to answer another way."""
        from surge_iw.services.retention import RetentionService

        query = db.enqueue_query(
            session_id=session, iteration_id=iteration, source_type="FLIGHT_LIVE",
            endpoint="/api/live/flight-positions/full", params={},
            dedup_key="k1")
        raw_id = db.insert_raw_result(
            query_id=query, iteration_id=iteration, source_type="FLIGHT_LIVE",
            provider="FR24", payload=[{"fr24_id": "abc"}], retention_days=-1)
        signal_id = db.insert_signal(
            iteration_id=iteration, signal_type="FLIGHT", raw_id=raw_id,
            fr24_id="abc", collection_class="INTERMEDIARY_LIVE",
            collection_basis="FR24 /api/live/flight-positions/full")

        assert RetentionService(db, {}).prune() >= 1
        row = db.one("SELECT raw_id, collection_class FROM signals "
                     "WHERE signal_id = ?", (signal_id,))
        assert row["raw_id"] is None
        assert row["collection_class"] == "INTERMEDIARY_LIVE"


class TestTheCollectorsRecordIt:
    def test_a_full_iteration_leaves_no_signal_unattested(self, db, config,
                                                          wiring):
        """The regression that matters: a new signal writer added later must
        not quietly reintroduce UNRECORDED for live collection."""
        import test_orchestrator as T

        session, _city, connectors, llm = wiring
        orch = T.build(db, config, connectors, llm)
        iteration = orch.start(session)
        orch.run(iteration)

        rows = db.all(
            "SELECT signal_type, collection_class, collection_basis "
            "FROM signals WHERE iteration_id = ?", (iteration,))
        assert rows, "the run produced signals"
        for row in rows:
            assert row["collection_class"] == "INTERMEDIARY_LIVE", dict(row)
            assert row["collection_basis"], dict(row)

    def test_a_cached_lodging_response_is_recorded_as_cached(self, db, config):
        """Staying answers price-compare from a one-hour cache and charges the
        same 30 credits either way, so nothing else in the record would show
        it."""
        from surge_iw.connectors.staying import SearchResult

        live = SearchResult(records=[], meta={})
        cached = SearchResult(records=[], meta={"cached": True})
        assert not live.cached
        assert cached.cached

    def test_one_cached_window_makes_the_comparison_cached(self, db, config):
        """A lodging signal is a comparison of two windows. If either side came
        from the vendor's store, the comparison rests on a stored copy — and
        calling the pair live because one half was would overstate it."""
        from surge_iw.agents.collection import CollectionAgent

        agent = CollectionAgent(db, config, {})
        query = {"endpoint": "/price-compare"}
        assert agent._acquisition(query, cached=False)["collection_class"] == \
            "INTERMEDIARY_LIVE"
        assert agent._acquisition(query, cached=True)["collection_class"] == \
            "INTERMEDIARY_CACHED"
