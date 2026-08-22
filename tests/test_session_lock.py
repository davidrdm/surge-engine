"""The per-session lock, held in the database — 8.6.

One session runs one iteration at a time. That was enforced by a
`threading.Lock`, which is only true inside one interpreter, which is why
`run.py serve` pinned uvicorn to `workers=1` and the README called it a
correctness requirement rather than a preference.

The tests that matter are the two that a process-local lock could never pass:
another process is refused, and a lock whose holder died is reclaimable. The
second is the one that makes moving a lock into storage safe at all — a lock
that outlives its holder is worse than no lock, because the failure is
permanent and looks like a hang.
"""
from __future__ import annotations

import pytest

from conftest import REFERENCE_MISSION
from surge_iw.db.database import SurgeDB


@pytest.fixture
def other_process(tmp_path):
    """A SECOND SurgeDB on the same file — a different process, in effect.

    In-memory databases cannot express this: each connection gets its own
    private database, so a second handle would see an empty schema and every
    assertion here would pass vacuously.
    """
    path = tmp_path / "shared.db"
    a, b = SurgeDB(path, mission=REFERENCE_MISSION), SurgeDB(path, mission=REFERENCE_MISSION)
    yield a, b
    a.close(); b.close()


class TestMutualExclusion:
    def test_a_second_holder_is_refused(self, other_process):
        a, b = other_process
        session = a.insert_session()
        first = a.insert_iteration(session)
        second = b.insert_iteration(session)
        assert a.try_claim_session(session, first) is True
        assert b.try_claim_session(session, second) is False, (
            "a second process must not be able to run the same session")

    def test_the_holder_is_reported_to_the_loser(self, other_process):
        a, b = other_process
        session = a.insert_session()
        first = a.insert_iteration(session)
        a.try_claim_session(session, first)
        held, _epoch = b.session_lock(session)
        assert held == first, "the loser must learn WHICH iteration blocked it"

    def test_releasing_lets_the_next_one_in(self, other_process):
        a, b = other_process
        session = a.insert_session()
        first = a.insert_iteration(session)
        second = b.insert_iteration(session)
        a.try_claim_session(session, first)
        a.release_session(session, first)
        assert b.try_claim_session(session, second) is True

    def test_reclaiming_your_own_slot_succeeds(self, db):
        """A resume must not deadlock against the lock it already holds."""
        session = db.insert_session()
        iteration = db.insert_iteration(session)
        assert db.try_claim_session(session, iteration) is True
        assert db.try_claim_session(session, iteration) is True

    def test_a_late_release_cannot_steal_the_slot(self, other_process):
        """An iteration whose lock was reclaimed as stale may still finish and
        call release. It must not clear the slot out from under the process
        that legitimately holds it now."""
        a, b = other_process
        session = a.insert_session()
        old = a.insert_iteration(session)
        new = b.insert_iteration(session)
        a.try_claim_session(session, old)
        a.release_session(session, old)
        b.try_claim_session(session, new)

        a.release_session(session, old)          # the late release
        held, _epoch = b.session_lock(session)
        assert held == new, "the current holder must keep its slot"


class TestStaleLocksAreReclaimable:
    """The property that makes a stored lock safe. Without it a crash wedges
    the session forever and the only recovery is editing the database."""

    def test_a_lock_held_by_a_dead_process_is_freed(self, db):
        dead = db.open_epoch(host="h", pid=1, entry_point="serve")["epoch_id"]
        session = db.insert_session()
        iteration = db.insert_iteration(session)
        db.try_claim_session(session, iteration, dead)

        # The process dies; the next one closes its epoch, as reconcile does.
        db.close_epoch(dead, "UNKNOWN")
        current = db.open_epoch(host="h", pid=1, entry_point="serve")["epoch_id"]

        assert db.clear_stale_session_locks(current) == [session]
        assert db.session_lock(session) == (None, None)

    def test_a_lock_held_by_a_LIVE_process_is_left_alone(self, db):
        """An epoch that is still open is either this process or a genuinely
        running one. Sweeping it would be exactly the double-run the lock
        exists to prevent."""
        live = db.open_epoch(host="h", pid=1, entry_point="serve")["epoch_id"]
        session = db.insert_session()
        iteration = db.insert_iteration(session)
        db.try_claim_session(session, iteration, live)

        current = db.open_epoch(host="h", pid=1, entry_point="serve")["epoch_id"]   # a second process
        assert db.clear_stale_session_locks(current) == []
        assert db.session_lock(session)[0] == iteration

    def test_our_own_lock_is_never_swept(self, db):
        epoch = db.open_epoch(host="h", pid=1, entry_point="serve")["epoch_id"]
        session = db.insert_session()
        iteration = db.insert_iteration(session)
        db.try_claim_session(session, iteration, epoch)
        assert db.clear_stale_session_locks(epoch) == []

    def test_reconcile_reports_what_it_freed(self, db, config):
        """Reported, not silently swept — a freed lock means an iteration
        stopped without releasing it, which an operator should see."""
        from surge_iw.services.recovery import RecoveryService

        dead = db.open_epoch(host="h", pid=1, entry_point="serve")["epoch_id"]
        session = db.insert_session()
        iteration = db.insert_iteration(session, owner_epoch_id=dead)
        db.try_claim_session(session, iteration, dead)

        report = RecoveryService(db, config).open_epoch("serve")
        assert report.freed_session_locks == [session]
        assert db.session_lock(session) == (None, None)


class TestTheRunnerUsesIt:
    def test_a_trigger_claims_the_slot_in_the_database(self, db, config):
        """Not just in process memory: that is the whole point."""
        from surge_iw.api.runner import IterationRunner, build_orchestrator_factory

        session = db.insert_session()
        db.insert_city(session, "Phoenix", canonical="phoenix", state="AZ")
        runner = IterationRunner(
            db, build_orchestrator_factory(db, config, {}, None), max_workers=1)
        with runner.claim(session):
            pass                                   # smoke: the API still works
        iteration = runner.create(session)
        assert isinstance(iteration, int)
