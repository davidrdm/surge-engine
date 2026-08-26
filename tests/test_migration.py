"""The schema upgrade, tested without a mission.

`_RENAMES`, `_REBUILDS` and `_VOCABULARY` rewrite tables in place. It is the
most destructive code in the system — it drops and recreates four tables and
copies every row across — and for a while the only test of it lived with the
pack whose historical databases motivated it. Ship the engine alone and the
rebuild had no coverage at all.

**The fixture is DERIVED, not vendored.** A pre-upgrade database is built by
taking the current `schema.sql` and reverting exactly the six columns the
upgrade touches: renaming each back and re-adding the CHECK that version 12
removed. Three consequences, and all three are why it is done this way:

  * the enumerated values are placeholders (`ALPHA`, `BRAVO`) rather than any
    mission's, so this file names no mission and the engine's own scan passes;
  * the fixture cannot drift from the schema, because it IS the schema;
  * every other column is present, which a hand-rolled subset would not
    manage — the rebuild copies by name and asserts the mapping is total, so a
    missing column makes the fixture the thing under test rather than the code.

What a particular pack's historical rows do across the same upgrade is that
pack's claim, and is tested in its own `tests/`.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from surge_iw.db.database import (
    _REBUILDS, _RENAMES, _VOCABULARY, SchemaUpgradeError, SurgeDB,
)

SCHEMA = Path(__file__).resolve().parents[1] / "surge_iw" / "db" / "schema.sql"

#: Placeholder vocabulary. Two values and the unattributed marker, which is the
#: shape every such column had: a closed set plus UNKNOWN.
OLD_TRACKS = ("ALPHA", "BRAVO")
OLD_LOCATIONS = ("SITE_ONE", "SITE_TWO", "OTHER")

#: (table, current column, pre-upgrade column, CHECK values or None).
#: Mirrors `_RENAMES + _REBUILDS`; `test_the_reversal_covers_every_upgrade_entry`
#: holds it to that rather than trusting the copy.
REVERTED = (
    ("sessions", "tracks", "actor_tracks", None),
    ("key_locations", "location_type", "location_type", OLD_LOCATIONS),
    ("signals", "track", "actor_type", OLD_TRACKS + ("UNKNOWN",)),
    ("correlations", "track", "actor_track", OLD_TRACKS),
    # No CHECK on either of these: they are in `_RENAMES`, which is rename-only
    # precisely because neither ever carried one. Giving one a CHECK here would
    # not be a harder test, it would be an IMPOSSIBLE one — `RENAME COLUMN`
    # rewrites a CHECK rather than dropping it, so the table would report itself
    # stale forever. That is `_is_stale`'s documented seatbelt, and the fixture
    # has to reflect the shape the engine actually upgrades from.
    ("triage_decisions", "track", "actor_type", None),
    ("alerts", "track", "actor_track", None),
)


def _revert(sql: str) -> str:
    """Turn the current schema back into its pre-upgrade shape."""
    for table, new, old, values in REVERTED:
        pattern = re.compile(
            rf"(CREATE TABLE IF NOT EXISTS {table} \()(.*?)(\n\);)", re.S)
        match = pattern.search(sql)
        assert match, f"{table} is not in schema.sql"
        body = match.group(2)
        # The whole body, not just the declaration: `correlations` carries a
        # table-level `UNIQUE (iteration_id, city_id, track)`, and renaming the
        # column without it produces a schema SQLite refuses to create.
        reverted = re.sub(rf"\b{new}\b", old, body) if new != old else body
        check = (f" CHECK ({old} IN ({','.join(repr(v) for v in values)}))"
                 if values else "")
        if check:
            reverted = re.sub(rf"^(\s*){old}\b(\s+)(TEXT[^,\n]*)",
                              rf"\g<1>{old}\g<2>\g<3>{check}", reverted,
                              count=1, flags=re.M)
        assert reverted != body, f"{table}.{new} was not reverted"
        sql = sql[:match.start()] + match.group(1) + reverted + match.group(3) \
            + sql[match.end():]
    # An index naming a reverted column would be replayed against the new name.
    sql = sql.replace("signals(track", "signals(actor_type")
    sql = sql.replace("correlations(track", "correlations(actor_track")
    # And the widening entry: drop the newest value from the skip vocabulary.
    for _table, _column, value in _VOCABULARY:
        sql = sql.replace(f",\n                         '{value}'", "")
        sql = sql.replace(f"'{value}',", "").replace(f",'{value}'", "")
        sql = sql.replace(f"'{value}'", "'HARD_STOP_PRIORITY'")
    return sql


def _insert(conn: sqlite3.Connection, table: str, **values) -> None:
    """Insert, filling every other NOT NULL column with a placeholder."""
    columns = dict(values)
    for row in conn.execute(f"PRAGMA table_info({table})"):
        name, kind, notnull, default, pk = (
            row[1], (row[2] or "").upper(), row[3], row[4], row[5])
        if name in columns or not notnull or default is not None or pk:
            continue
        columns[name] = 0.0 if "REAL" in kind else 0 if "INT" in kind else "x"
    keys = list(columns)
    conn.execute(
        f"INSERT INTO {table} ({','.join(keys)}) "
        f"VALUES ({','.join('?' * len(keys))})", [columns[k] for k in keys])


@pytest.fixture
def old(tmp_path: Path) -> Path:
    """A database in the pre-upgrade shape, with a row in every table the
    upgrade touches and one in each table that references them."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_revert(SCHEMA.read_text(encoding="utf-8")))
    _insert(conn, "sessions", session_id=1, label="s",
            actor_tracks="ALPHA,BRAVO", config_json="{}")
    _insert(conn, "cities", city_id=1, session_id=1, name="Phoenix",
            canonical="phoenix")
    _insert(conn, "iterations", iteration_id=1, session_id=1, seq=1,
            stage="SEEDING")
    _insert(conn, "key_locations", location_id=1, city_id=1, name="A Place",
            location_type="SITE_ONE")
    for index, actor in enumerate(("ALPHA", "UNKNOWN", "BRAVO"), start=1):
        _insert(conn, "signals", signal_id=index, iteration_id=1,
                signal_type="SOCIAL", city_id=1, actor_type=actor,
                url=f"u{index}")
    _insert(conn, "query_queue", query_id=1, session_id=1, iteration_id=1,
            source_type="LODGING", endpoint="/search", params_json="{}",
            dedup_key="d1", city_id=1, status="SKIPPED_NO_MAPPING",
            skip_reason="NO_LISTING_SET")
    _insert(conn, "triage_decisions", triage_id=1, iteration_id=1, url="u1",
            relevant=1, actor_type="ALPHA", signal_id=1)
    _insert(conn, "correlations", correlation_id=7, iteration_id=1, city_id=1,
            actor_track="ALPHA", score=0.5, band="MEDIUM", distinct_types=2,
            contributions_json="{}")
    conn.execute("INSERT INTO correlation_signals VALUES (7,1,0.3)")
    conn.execute("INSERT INTO correlation_signals VALUES (7,2,0.2)")
    _insert(conn, "alerts", alert_id=1, correlation_id=7, session_id=1,
            iteration_id=1, city_id=1, actor_track="ALPHA",
            confidence_score=0.5, confidence_band="MEDIUM")
    # Push the AUTOINCREMENT mark past max(rowid), as a rollback would leave it.
    conn.execute(
        "UPDATE sqlite_sequence SET seq = 25 WHERE name='correlations'")
    conn.commit()
    conn.close()
    return path


def columns(path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def ddl(path: Path, table: str) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


class TestTheFixtureIsHonest:
    """A derived fixture can drift into agreeing with the code by accident.
    These assert it is genuinely in the old shape before anything upgrades it."""

    def test_the_reversal_covers_every_upgrade_entry(self):
        assert {(t, old) for t, old, _new in _RENAMES + _REBUILDS} == \
            {(t, old) for t, _new, old, _v in REVERTED}, (
                "an entry was added to _RENAMES or _REBUILDS without a "
                "reversal here, so the upgrade path it drives is untested")

    def test_it_starts_with_the_old_names_and_the_checks(self, old: Path):
        for table, new, previous, values in REVERTED:
            assert previous in columns(old, table), f"{table}.{previous}"
            if new != previous:
                assert new not in columns(old, table)
            if values:
                assert f"CHECK ({previous} IN" in ddl(old, table), table

    def test_it_starts_without_the_widened_value(self, old: Path):
        for table, column, value in _VOCABULARY:
            assert value not in ddl(old, table), f"{table}.{column}"


class TestEveryTableIsUpgraded:
    """Asserted per table, because the bug this exists for left ONE of six
    correctly upgraded — a check that sampled would have passed."""

    @pytest.mark.parametrize("table,previous,new",
                             [(t, o, n) for t, o, n in _RENAMES + _REBUILDS])
    def test_the_column_is_renamed(self, old: Path, table, previous, new):
        SurgeDB(old).close()
        assert new in columns(old, table)
        if previous != new:
            assert previous not in columns(old, table)

    @pytest.mark.parametrize("table,previous,new", list(_REBUILDS))
    def test_the_check_is_gone(self, old: Path, table, previous, new):
        SurgeDB(old).close()
        assert f"CHECK ({new} IN" not in ddl(old, table)
        for value in OLD_TRACKS + OLD_LOCATIONS:
            assert f"'{value}'" not in ddl(old, table), (
                f"{table} still constrains {value}")

    @pytest.mark.parametrize("table,column,value", list(_VOCABULARY))
    def test_the_vocabulary_is_widened(self, old: Path, table, column, value):
        SurgeDB(old).close()
        assert value in ddl(old, table)


class TestNothingIsLost:
    """The rebuild drops and recreates four tables. Every way that loses a row
    silently has an assertion here."""

    TABLES = ("sessions", "cities", "iterations", "key_locations", "signals",
              "query_queue", "triage_decisions", "correlations",
              "correlation_signals", "alerts")

    def counts(self, path: Path) -> dict[str, int]:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in self.TABLES}
        finally:
            conn.close()

    def test_every_row_survives(self, old: Path):
        before = self.counts(old)
        SurgeDB(old).close()
        assert self.counts(old) == before

    def test_the_values_are_copied_verbatim(self, old: Path):
        """Not translated. A session that ran under one vocabulary truthfully
        ran under it, whatever the column is called afterwards."""
        SurgeDB(old).close()
        conn = sqlite3.connect(f"file:{old}?mode=ro", uri=True)
        assert conn.execute(
            "SELECT tracks FROM sessions").fetchone()[0] == "ALPHA,BRAVO"
        assert [r[0] for r in conn.execute(
            "SELECT track FROM signals ORDER BY signal_id")] == \
            ["ALPHA", "UNKNOWN", "BRAVO"]
        assert conn.execute(
            "SELECT location_type FROM key_locations").fetchone()[0] == "SITE_ONE"
        assert conn.execute(
            "SELECT track FROM correlations").fetchone()[0] == "ALPHA"
        assert conn.execute(
            "SELECT track FROM alerts").fetchone()[0] == "ALPHA"
        conn.close()

    def test_a_column_is_not_transposed(self, old: Path):
        """Live column order does not match `schema.sql`, because `_MIGRATIONS`
        appends. `INSERT ... SELECT *` would shift every TEXT column one place
        and SQLite would not raise — so the copy names columns explicitly, and
        this is what would catch it if that changed."""
        SurgeDB(old).close()
        conn = sqlite3.connect(f"file:{old}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT signal_type, city_id, url FROM signals "
            "WHERE signal_id = 1").fetchone()
        conn.close()
        assert row == ("SOCIAL", 1, "u1")

    def test_the_surrogate_key_is_preserved(self, old: Path):
        """`correlation_signals` points at `correlations.correlation_id`. Omit
        the PK from the copy and AUTOINCREMENT renumbers, repointing 1,052 live
        links silently while `foreign_key_check` still passes."""
        SurgeDB(old).close()
        conn = sqlite3.connect(f"file:{old}?mode=ro", uri=True)
        assert conn.execute(
            "SELECT correlation_id FROM correlations").fetchone()[0] == 7
        assert conn.execute(
            "SELECT COUNT(*) FROM correlation_signals WHERE correlation_id=7"
        ).fetchone()[0] == 2
        conn.close()

    def test_the_autoincrement_high_water_mark_is_restored(self, old: Path):
        """DROP TABLE loses it. A prior rollback can leave seq > max(rowid), so
        a reused id could collide with an already-exported alert."""
        SurgeDB(old).close()
        conn = sqlite3.connect(f"file:{old}?mode=ro", uri=True)
        assert conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='correlations'"
        ).fetchone()[0] == 25
        conn.close()

    def test_the_indexes_are_replayed(self, old: Path):
        """`idx_sig_dedup` is the sole guard against a duplicate signal in one
        iteration. A rebuild that dropped it would leave no error behind."""
        conn = sqlite3.connect(f"file:{old}?mode=ro", uri=True)
        before = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='signals' AND name NOT LIKE 'sqlite_%'")}
        conn.close()
        assert before, "premise: signals has indexes to lose"
        SurgeDB(old).close()
        conn = sqlite3.connect(f"file:{old}?mode=ro", uri=True)
        after = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='signals' AND name NOT LIKE 'sqlite_%'")}
        conn.close()
        assert after == before

    def test_referential_integrity_holds(self, old: Path):
        SurgeDB(old).close()
        conn = sqlite3.connect(f"file:{old}?mode=ro", uri=True)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        conn.close()


class TestItIsSafeToRunTwice:
    def test_a_second_open_changes_nothing(self, old: Path):
        SurgeDB(old).close()
        first = {t: ddl(old, t) for t, _o, _n in _RENAMES + _REBUILDS}
        SurgeDB(old).close()
        assert {t: ddl(old, t) for t, _o, _n in _RENAMES + _REBUILDS} == first

    def test_a_current_database_is_not_touched(self, tmp_path: Path):
        """The guard is the live DDL, not a version number, so a database
        already in the new shape must be left alone."""
        path = tmp_path / "current.db"
        SurgeDB(path).close()
        before = {t: ddl(path, t) for t, _o, _n in _RENAMES + _REBUILDS}
        SurgeDB(path).close()
        assert {t: ddl(path, t) for t, _o, _n in _RENAMES + _REBUILDS} == before


class TestTheV15IndexSwap:
    """v15 and v16 both changed `idx_sig_dedup`'s DEFINITION — the stream
    column, then the city — and an index cannot be altered in place.
    `_swap_indexes` drops and recreates any index whose stored DDL lacks the
    current guard fragment, so a database from ANY earlier version arrives at
    today's definition in one swap.

    The fixture is derived the same way as the vocabulary one above: the
    current schema with those additions stripped back out, so it cannot
    drift from `schema.sql`.
    """

    def _v14_shaped(self, tmp_path: Path) -> Path:
        sql = SCHEMA.read_text(encoding="utf-8")
        # Strip the six v15 columns (each is one declaration line, preceded by
        # its comment block — removing the declaration alone is enough for the
        # shape; comments are legal in the old file too).
        out = []
        for line in sql.splitlines():
            stripped = line.strip()
            if stripped.startswith("stream ") and stripped.endswith("TEXT,"):
                continue
            if stripped.startswith("calendar_matches_json"):
                continue
            if stripped == "COALESCE(stream, ''),":
                continue
            if stripped == "COALESCE(city_id, -1),":
                continue
            out.append(line)
        sql = "\n".join(out)
        # And the calendar table, which did not exist at v14.
        import re
        sql = re.sub(
            r"CREATE TABLE IF NOT EXISTS calendar_events \((?:.|\n)*?^\);",
            "", sql, flags=re.M)
        sql = sql.replace(
            "CREATE INDEX IF NOT EXISTS idx_calendar_session\n"
            "    ON calendar_events (session_id, city_canonical, starts_at);",
            "")
        path = tmp_path / "v14.db"
        conn = sqlite3.connect(path)
        conn.executescript(sql)
        _insert(conn, "sessions", session_id=1, label="s", tracks="A",
                config_json="{}")
        _insert(conn, "cities", city_id=1, session_id=1, name="Phoenix",
                canonical="phoenix")
        _insert(conn, "iterations", iteration_id=1, session_id=1, seq=1,
                stage="SEEDING")
        _insert(conn, "signals", signal_id=1, iteration_id=1,
                signal_type="SOCIAL", city_id=1, url="https://x.com/a")
        conn.commit()
        conn.close()
        return path

    def test_the_fixture_is_genuinely_old(self, tmp_path: Path):
        path = self._v14_shaped(tmp_path)
        assert "stream" not in columns(path, "signals")
        assert "COALESCE(stream" not in ddl_index(path, "idx_sig_dedup")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE name='calendar_events'").fetchone()[0] == 0
        conn.close()

    def test_opening_swaps_the_index_and_keeps_the_rows(self, tmp_path: Path):
        path = self._v14_shaped(tmp_path)
        SurgeDB(path).close()
        assert "stream" in columns(path, "signals")
        assert "COALESCE(stream" in ddl_index(path, "idx_sig_dedup")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE name='calendar_events'").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        conn.close()

    def test_the_swap_is_idempotent(self, tmp_path: Path):
        path = self._v14_shaped(tmp_path)
        SurgeDB(path).close()
        first = ddl_index(path, "idx_sig_dedup")
        SurgeDB(path).close()
        assert ddl_index(path, "idx_sig_dedup") == first

    def test_uniqueness_is_now_per_city(self, tmp_path: Path):
        """v16. One observation about two cities is two rows — correlation
        scores each city over its own rows, so they can never meet in one
        number, and omitting the city meant the second city's evidence was
        refused by the index and lost in silence."""
        path = self._v14_shaped(tmp_path)
        SurgeDB(path).close()
        assert "COALESCE(city_id" in ddl_index(path, "idx_sig_dedup")
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO cities (city_id, session_id, name, "
                     "canonical) VALUES (2,1,'Tucson','tucson')")
        base = ("INSERT INTO signals (iteration_id, signal_type, city_id, "
                "track, quality, signal_state, collection_class, stream, url) "
                "VALUES (1,'SOCIAL',?,'UNKNOWN',0,'CONFIRMED','UNRECORDED',"
                "'one',?)")
        conn.execute(base, (1, "https://x.com/b"))
        conn.execute(base, (2, "https://x.com/b"))      # other city: fine
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, (1, "https://x.com/b"))  # same city: refused
        conn.close()

    def test_uniqueness_is_now_per_stream(self, tmp_path: Path):
        """The point of the swap: the same URL may be one signal PER stream,
        while a within-stream duplicate is still refused — and pre-v15 rows
        (stream NULL) keep exactly their old dedup behaviour."""
        path = self._v14_shaped(tmp_path)
        SurgeDB(path).close()
        conn = sqlite3.connect(path)
        base = ("INSERT INTO signals (iteration_id, signal_type, city_id, "
                "track, quality, signal_state, collection_class, stream, url) "
                "VALUES (1,'SOCIAL',1,'UNKNOWN',0,'CONFIRMED','UNRECORDED',?,?)")
        conn.execute(base, ("one", "https://x.com/a"))
        conn.execute(base, ("two", "https://x.com/a"))     # other stream: fine
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, ("one", "https://x.com/a")) # same stream: refused
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, (None, "https://x.com/a"))  # NULL collides with
        conn.close()                                       # the pre-v15 row


def ddl_index(path: Path, index: str) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index,)).fetchone()
        return row[0] if row and row[0] else ""
    finally:
        conn.close()


class TestAFailureLeavesTheFileAlone:
    def test_a_row_the_new_definition_refuses_aborts_the_whole_upgrade(
        self, old: Path
    ):
        """The rebuild retroactively applies constraints that `ADD COLUMN`
        never could. A row that violates one must abort everything, not leave
        two tables upgraded and two not — a shape no version number describes.
        """
        conn = sqlite3.connect(old)
        # Past the CHECK, because the point is a row the OLD table tolerated
        # and the new definition does not — which is what `ADD COLUMN` leaves
        # behind when the declared schema constrains a column it could not.
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE signals SET signal_type = 'NOT_A_TYPE' "
                     "WHERE signal_id = 1")
        conn.commit()
        conn.close()

        with pytest.raises(SchemaUpgradeError) as exc:
            SurgeDB(old).close()
        assert "signals" in str(exc.value)

        # And nothing moved: the old shape is intact.
        for table, new, previous, _values in REVERTED:
            assert previous in columns(old, table), f"{table} was modified"
