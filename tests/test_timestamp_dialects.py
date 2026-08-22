"""Signal timestamps are stored in one spelling (9.12).

`recent_signals_for_city` compares `observed_at` as a STRING, which is only
sound if every value has the same shape. API Direct returns
`"2026-08-12 12:08:39"` with a space separator; `' '` is 0x20 and `'T'` is
0x54, so a space-separated stamp sorts BEFORE any `'T'` stamp on the same
date — and the window threshold is written by `iso()`, which uses `'T'`.

Found in live iteration 14. Two Atlanta social signals, 158 hours old against a
168-hour window, were dropped by that comparison. Both correlations then scored
with no social contribution at all, and the alert rested entirely on flight and
car background. Across the database 12 of 22 social signals were stored that
way, and the window query admitted **none** of the 8 that belonged in it.

Silent evidence loss is the failure this system exists to prevent, so the fix
is at the source: one canonical spelling on write, and a repair for rows
written before the rule existed.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import REFERENCE_MISSION
from surge_iw.db.database import SurgeDB, iso, parse_iso, utcnow

# The same instant, in every dialect these providers actually emit.
DIALECTS = [
    "2026-08-12 12:08:39",              # API Direct twitter: space, no zone
    "2026-08-12T12:08:39",              # T, naive
    "2026-08-12T12:08:39Z",             # FR24
    "2026-08-12T12:08:39+00:00",        # ours
]


class TestStorageIsCanonical:
    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_every_dialect_lands_in_one_spelling(self, db, iteration, dialect):
        signal_id = db.insert_signal(
            iteration_id=iteration, signal_type="SOCIAL",
            observed_at=dialect, url=f"https://x.com/{dialect}")
        stored = db.one("SELECT observed_at FROM signals WHERE signal_id = ?",
                        (signal_id,))["observed_at"]
        assert stored == "2026-08-12T12:08:39+00:00"

    def test_an_unreadable_stamp_is_kept_as_it_arrived(self, db, iteration):
        """`in_window` excludes it anyway, and a timestamp nobody can parse is
        still a record that one was supplied. Discarding it would destroy
        evidence to tidy a column."""
        signal_id = db.insert_signal(
            iteration_id=iteration, signal_type="SOCIAL",
            observed_at="last tuesday", url="https://x.com/1")
        assert db.one("SELECT observed_at FROM signals WHERE signal_id = ?",
                      (signal_id,))["observed_at"] == "last tuesday"

    def test_a_datetime_still_works(self, db, iteration):
        moment = utcnow()
        signal_id = db.insert_signal(
            iteration_id=iteration, signal_type="SOCIAL", observed_at=moment,
            url="https://x.com/2")
        assert db.one("SELECT observed_at FROM signals WHERE signal_id = ?",
                      (signal_id,))["observed_at"] == iso(moment)


class TestTheWindowQueryAgrees:
    """The bug was the disagreement between the SQL pre-filter and `in_window`,
    so the property to pin is that they now agree."""

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_a_signal_inside_the_window_is_returned(self, db, session,
                                                    iteration, dialect):
        city = db.insert_city(session, "Atlanta", canonical="atlanta")
        db.insert_signal(
            iteration_id=iteration, signal_type="SOCIAL", city_id=city,
            observed_at=dialect, url=f"https://x.com/{dialect}")
        # A threshold EARLIER the same day: the exact shape that broke it.
        since = parse_iso("2026-08-12T02:26:44.146213+00:00")
        assert len(db.recent_signals_for_city(session, city, since=since)) == 1

    def test_a_signal_outside_the_window_is_still_excluded(self, db, session,
                                                          iteration):
        city = db.insert_city(session, "Atlanta", canonical="atlanta")
        db.insert_signal(
            iteration_id=iteration, signal_type="SOCIAL", city_id=city,
            observed_at="2026-08-01 09:00:00", url="https://x.com/old")
        since = parse_iso("2026-08-12T02:26:44.146213+00:00")
        assert db.recent_signals_for_city(session, city, since=since) == []

    def test_the_sql_filter_and_in_window_agree(self, db, session, iteration):
        """The invariant that would have caught this: whatever the SQL admits
        must be what the scoring gate admits."""
        from surge_iw.base.scoring import in_window

        city = db.insert_city(session, "Atlanta", canonical="atlanta")
        anchor = parse_iso("2026-08-19T02:26:44.146213+00:00")
        for index, dialect in enumerate(DIALECTS):
            db.insert_signal(
                iteration_id=iteration, signal_type="SOCIAL", city_id=city,
                observed_at=dialect, url=f"https://x.com/{index}")
        window = timedelta(hours=168)
        rows = db.recent_signals_for_city(session, city, since=anchor - window)
        assert len(rows) == len(DIALECTS)
        for row in rows:
            assert in_window(dict(row), anchor, window)


class TestTheRepair:
    def test_it_rewrites_rows_written_before_the_rule(self, tmp_path):
        path = tmp_path / "legacy.db"
        with SurgeDB(str(path), mission=REFERENCE_MISSION) as db:
            session = db.insert_session()
            iteration = db.insert_iteration(session)
            signal_id = db.insert_signal(
                iteration_id=iteration, signal_type="SOCIAL",
                observed_at="2026-08-12T12:08:39+00:00", url="https://x.com/1")
            # Force the legacy dialect back in, as an older build would have.
            db._exec("UPDATE signals SET observed_at = '2026-08-12 12:08:39' "
                     "WHERE signal_id = ?", (signal_id,))

        with SurgeDB(str(path), mission=REFERENCE_MISSION) as db:                 # reopening repairs
            assert db.one("SELECT observed_at FROM signals")["observed_at"] == \
                "2026-08-12T12:08:39+00:00"
            assert db.one("SELECT message FROM agent_log WHERE agent='SurgeDB'")

    def test_it_changes_the_spelling_and_never_the_instant(self, tmp_path):
        path = tmp_path / "instant.db"
        with SurgeDB(str(path), mission=REFERENCE_MISSION) as db:
            session = db.insert_session()
            iteration = db.insert_iteration(session)
            signal_id = db.insert_signal(
                iteration_id=iteration, signal_type="SOCIAL",
                observed_at="2026-08-12T12:08:39+00:00", url="https://x.com/1")
            db._exec("UPDATE signals SET observed_at = '2026-08-12 12:08:39' "
                     "WHERE signal_id = ?", (signal_id,))
        with SurgeDB(str(path), mission=REFERENCE_MISSION) as db:
            assert parse_iso(db.one("SELECT observed_at FROM signals"
                                    )["observed_at"]) == \
                parse_iso("2026-08-12 12:08:39")

    def test_it_leaves_an_unreadable_stamp_alone(self, tmp_path):
        path = tmp_path / "junk.db"
        with SurgeDB(str(path), mission=REFERENCE_MISSION) as db:
            session = db.insert_session()
            iteration = db.insert_iteration(session)
            signal_id = db.insert_signal(
                iteration_id=iteration, signal_type="SOCIAL",
                observed_at="2026-08-12T12:08:39+00:00", url="https://x.com/1")
            db._exec("UPDATE signals SET observed_at = 'last tuesday' "
                     "WHERE signal_id = ?", (signal_id,))
        with SurgeDB(str(path), mission=REFERENCE_MISSION) as db:
            assert db.one("SELECT observed_at FROM signals")["observed_at"] == \
                "last tuesday"

    def test_it_is_idempotent(self, tmp_path):
        path = tmp_path / "twice.db"
        with SurgeDB(str(path), mission=REFERENCE_MISSION) as db:
            session = db.insert_session()
            iteration = db.insert_iteration(session)
            db.insert_signal(iteration_id=iteration, signal_type="SOCIAL",
                             observed_at="2026-08-12 12:08:39",
                             url="https://x.com/1")
        for _ in range(3):
            with SurgeDB(str(path), mission=REFERENCE_MISSION) as db:
                assert db._repair_observed_at() == 0, "nothing left to repair"
