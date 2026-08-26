"""SurgeDB — the communication bus between agents.

Raw sqlite3, no ORM. Follows the conventions in iw_database.py: one connection
with a Row factory, Python-layer enum validation before every write, an _insert
helper returning the new rowid, and an idempotent schema script applied at
construction.

Two things differ from iw_database.py, both forced by this system's shape:

  * The REST layer is multi-threaded, so the connection is opened with
    check_same_thread=False and every write goes through an RLock. SQLite
    serialises writes anyway; the lock exists so that read-modify-write
    sequences in Python (claim-then-update, count-then-insert) are atomic.

  * WAL is enabled for file-backed databases so a long iteration's writes do not
    block the API's reads. WAL is unavailable for :memory:, which is fine
    because tests are single-threaded.

Agents never write SQL. Everything they need is a method here.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import enums
from ..services import governance
from ..services.redact import redact_payload, redact_text

SCHEMA_VERSION = 16
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

class SchemaUpgradeError(RuntimeError):
    """A version-12 upgrade that could not be completed safely.

    Always names the table and says whether anything was written. Nothing ever
    is: the whole upgrade runs in one transaction, so a failure leaves the
    database byte-for-byte unchanged.
    """


#: Columns renamed in place, where nothing else about the table changes.
#:
#: RENAME COLUMN preserves the column's type, its NOT NULL, its DEFAULT **and
#: its CHECK**, rewriting the name everywhere the schema mentions it. That is
#: enough here and not enough for `_REBUILDS`, because neither of these two
#: ever carried a mission vocabulary in a CHECK. A table belongs to one tuple
#: or the other, never both.
#: WARNING to anyone renaming things in bulk: the OLD names below are data,
#: not references. A project-wide search-and-replace of `actor_type` -> `track`
#: rewrote them to `("signals", "track", "track")` and silently turned this
#: migration into a no-op for five of six tables. Nothing caught it, because
#: every test opens a `:memory:` database built fresh from schema.sql, where
#: nothing is ever stale. `tests/test_migration_v12.py` now builds a real v11
#: file and upgrades it, which is the only test that can.
_RENAMES: tuple[tuple[str, str, str], ...] = (
    ("triage_decisions", "actor_type", "track"),
    ("alerts", "actor_track", "track"),
)

#: Tables that must be REWRITTEN rather than altered.
#:
#: SQLite has no DROP CONSTRAINT, so a CHECK that enumerated one mission's
#: vocabulary can only be removed by rebuilding the table around it. Each entry
#: is (table, old column, new column); old == new means the column keeps its
#: name and loses only its constraint.
#:
#: `sessions` is here for a different reason: it had no CHECK, but its DEFAULT
#: was one mission's track pair, and a DEFAULT is equally unalterable. Left
#: as a rename, anyone reading schema.sql would still find one mission's tracks
#: written into the engine.
_REBUILDS: tuple[tuple[str, str, str], ...] = (
    ("sessions", "actor_tracks", "tracks"),
    ("key_locations", "location_type", "location_type"),
    ("signals", "actor_type", "track"),
    ("correlations", "actor_track", "track"),
)


#: CHECK vocabularies widened after version 12, as (table, column, value).
#:
#: SQLite cannot ALTER a CHECK, so permitting one more value means rebuilding
#: the table around it — the same surgery as `_REBUILDS`, for a different
#: reason. Kept separate because the staleness test differs: a rebuilt table
#: still HAS a CHECK on the column, so `_is_stale`'s "does a CHECK exist" probe
#: would report it stale forever and rebuild on every open. What marks these
#: done is the value itself appearing in the stored DDL.
_VOCABULARY: tuple[tuple[str, str, str], ...] = (
    ("query_queue", "skip_reason", "THIN_PAIRED_SAMPLE"),
)


def _index_ddl(index: str) -> str:
    """The CREATE INDEX statement for `index`, lifted out of schema.sql.

    Same argument as `_table_ddl`: the swapped index and a freshly created one
    must have ONE definition between them, or a swapped database quietly keeps
    an old shape the next edit to schema.sql never reaches.
    """
    text = _SCHEMA_PATH.read_text(encoding="utf-8")
    # Terminated at a line consisting of ");", like `_table_ddl` — never at
    # the first bare semicolon, which a comment inside the definition may
    # legally contain (found the hard way: the truncated DDL executed as
    # "incomplete input" on every pre-v15 database). Swap-listed indexes are
    # therefore written in the multi-line form.
    match = re.search(
        rf"^CREATE (?:UNIQUE )?INDEX IF NOT EXISTS {re.escape(index)}\b"
        rf"(?:.|\n)*?^\);",
        text, re.MULTILINE)
    if match is None:                              # pragma: no cover — typo guard
        raise SchemaUpgradeError(
            f"schema.sql has no CREATE INDEX for {index!r}")
    return match.group(0)


def _table_ddl(table: str) -> str:
    """The CREATE TABLE statement for `table`, lifted out of schema.sql.

    So the rebuilt table and a freshly created one have ONE definition between
    them. Copying the DDL into Python would work until the day someone edited
    schema.sql and a rebuilt database quietly kept the old shape.
    """
    text = _SCHEMA_PATH.read_text(encoding="utf-8")
    match = re.search(
        rf"^CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((?:.|\n)*?^\);",
        text, re.MULTILINE)
    if match is None:                              # pragma: no cover — typo guard
        raise SchemaUpgradeError(
            f"schema.sql has no CREATE TABLE for {table!r}")
    return match.group(0).rstrip(";")


#: Columns added after version 1. `CREATE TABLE IF NOT EXISTS` is a no-op on an
#: existing table, so a schema edit reaches a database created by an earlier
#: version only through an explicit ALTER. Every entry must be nullable and
#: without a non-constant default, which is all SQLite's ALTER TABLE accepts.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("query_queue", "created_at", "TEXT"),
    ("query_queue", "created_iteration_id",
     "INTEGER REFERENCES iterations(iteration_id)"),
    ("queue_decisions", "stage", "TEXT"),
    # Phase 6a. Ordering is safe: _apply_schema executes schema.sql (which
    # creates process_epochs) before _migrate runs these ALTERs.
    ("iterations", "owner_epoch_id",
     "INTEGER REFERENCES process_epochs(epoch_id)"),
    ("iterations", "interrupted_at", "TEXT"),
    ("iterations", "interrupted_stage", "TEXT"),
    # Phase 7. `state` is authoritative; `relevant` is derived from it.
    ("triage_decisions", "state", "TEXT"),
    ("triage_decisions", "fault_detail", "TEXT"),
    ("triage_decisions", "schema_version", "TEXT"),
    ("signals", "signal_state", "TEXT"),
    ("signals", "state_reason", "TEXT"),
    ("signals", "publisher_key", "TEXT"),
    ("signals", "publisher_method", "TEXT"),
    ("signals", "claim_key", "TEXT"),
    ("signals", "location_method", "TEXT"),
    # 8.1. Ordering is safe for the same reason as 6a's: _apply_schema runs
    # schema.sql (which creates `receipts`) before _migrate runs these ALTERs.
    ("triage_decisions", "receipt_id", "INTEGER REFERENCES receipts(receipt_id)"),
    ("alerts", "receipt_id", "INTEGER REFERENCES receipts(receipt_id)"),
    # 8.2. A constant default IS accepted by SQLite's ALTER TABLE and backfills
    # existing rows, which is what we want: an alert written before review
    # existed has genuinely not been reviewed.
    ("alerts", "review_state", "TEXT NOT NULL DEFAULT 'UNREVIEWED'"),
    ("alerts", "reviewed_at", "TEXT"),
    ("alerts", "reviewed_by", "TEXT"),
    ("alerts", "review_note", "TEXT"),
    ("iterations", "cancel_requested_at", "TEXT"),
    ("iterations", "cancel_requested_by", "TEXT"),
    ("iterations", "cancel_reason", "TEXT"),
    # Which stages did not run. A skipped stage is a coverage gap with no other
    # trace — see base/scoring.STAGE_SOURCE_TYPES.
    ("iterations", "skipped_stages_json", "TEXT"),
    # 8.6. The per-session lock, moved out of process memory.
    ("sessions", "running_iteration_id",
     "INTEGER REFERENCES iterations(iteration_id)"),
    ("sessions", "running_epoch_id",
     "INTEGER REFERENCES process_epochs(epoch_id)"),
    # 8.7(b). Why a correlation did or did not become an alert, recorded by the
    # agent that decides it. Before this the answer existed only as the gap
    # between `correlations.score` and `correlation.alert_min_score`, which a
    # reader could reconstruct only by holding the config — and only if they
    # already knew that was the rule. Same principle as `queue_decisions` and
    # `signals.state_reason`: a decision not to act is still a decision.
    # NULL means ALERTING has not run yet, which is distinct from every value.
    ("correlations", "alert_decision", "TEXT"),
    ("correlations", "alert_decision_reason", "TEXT"),
    # 8.8. Which iteration this one is a re-triage of. Nullable self-reference:
    # the pair is otherwise reconstructible only by comparing anchor times,
    # which is a coincidence rather than a record.
    ("iterations", "retry_of_iteration_id",
     "INTEGER REFERENCES iterations(iteration_id)"),
    # 9.4. One comparable acquisition value per signal. The constant default
    # backfills an existing database with UNRECORDED, which is the honest
    # answer for a row collected before anything attested this — the field
    # records an attestation, and there was none.
    ("signals", "collection_class",
     "TEXT NOT NULL DEFAULT 'UNRECORDED' CHECK (collection_class IN "
     "('DIRECT','INTERMEDIARY_LIVE','INTERMEDIARY_CACHED','UNRECORDED'))"),
    ("signals", "collection_basis", "TEXT"),
    # 9.6. Competing explanations for a correlation. NULL means the correlation
    # predates the rules, which is distinct from an empty list — that one says
    # no family contributed and there is nothing to explain away.
    ("correlations", "alternatives_json", "TEXT"),
    # 9.11. The families with NOTHING collected, stored beside the detailed
    # `failed_sources` because the two answer different questions and the
    # alert caveat needs both: one says what failed, the other says what is
    # genuinely unknown.
    ("correlations", "failed_families", "TEXT"),
    # 9.10. What each flight family was measured against. NULL means the
    # correlation predates baselining, which is distinct from UNBASELINED —
    # that one means the rule ran and had too few samples.
    ("correlations", "flight_baseline_json", "TEXT"),
    # 9.13. NULL means the correlation predates the check, which is distinct
    # from a recorded `new_this_iteration: false`.
    ("correlations", "evidence_freshness_json", "TEXT"),
    # Version 12. Which mission a session ran under. NULL means it predates
    # missions entirely — i.e. the vocabulary that used to be built in — which
    # is a
    # different fact from a session that ran under a pack that has since been
    # deleted. Added here, before `_upgrade` runs, because the rebuild copies
    # `sessions` by name and every column in the new definition must already
    # exist on the live table for that copy to be total.
    ("sessions", "mission", "TEXT"),
    ("receipts", "mission_id", "TEXT"),
    ("receipts", "mission_hash", "TEXT"),
    # Version 14. The hash of the user message that was ACCEPTED, which is not
    # the one this receipt's other fields describe whenever `attempts > 1`:
    # `_call_llm_json` rewrites the request with the parse error and the failed
    # reply before retrying. Everything else on the receipt — prompt_hash,
    # input_hash, batch_key — describes the FIRST variant, so a reconstruction
    # could report a retried classification as byte-exact when it was not.
    # NULL means the receipt predates this column, which is a different fact
    # from a request that could not be reconstructed.
    ("receipts", "prompt_user_hash", "TEXT"),
    # Version 15. Which mission stream a row belongs to, on every table where
    # per-stream work must stay distinguishable: queries (seeding and
    # cooldown), refusals (gap attribution), signals (scoring kind and
    # banding family), judgements (resume and retry are per stream), and
    # skips (a rescan must recognise its own refusals per stream). NULL means
    # the implicit stream — a no-streams mission, or a pre-v15 row — which is
    # a different fact from a stream that has since been renamed. Plain TEXT,
    # never a CHECK: stream ids are mission vocabulary, and a mission
    # vocabulary in a CHECK is the mistake version 12 existed to remove.
    ("query_queue", "stream", "TEXT"),
    ("queue_decisions", "stream", "TEXT"),
    ("signals", "stream", "TEXT"),
    ("triage_decisions", "stream", "TEXT"),
    ("triage_skips", "stream", "TEXT"),
    # Version 15. Operator-calendar events overlapping the scored window,
    # snapshotted verbatim. NULL = predates the feature or no calendar, which
    # is a different fact from "[]" = calendar present, nothing matched.
    ("correlations", "calendar_matches_json", "TEXT"),
    ("correlations", "config_hash", "TEXT"),
)

#: Indexes whose DEFINITION changed, as `(index name, guard fragment)`.
#:
#: An index cannot be altered in place, but unlike a table it can be dropped
#: and recreated without touching a row — so this needs none of `_REBUILDS`'
#: ceremony: no snapshot, no transaction choreography, no column mapping. The
#: guard is a fragment of the CURRENT definition; while the stored DDL lacks
#: it, the index is stale and is swapped. Runs after `_migrate`, because the
#: new definition may name a column `_MIGRATIONS` has only just added.
#:
#: ONE entry per index, and its fragment names the MOST RECENT addition to
#: that index — an older shape lacks it and is rebuilt to today's definition
#: whole, so a database arriving from any earlier version takes one swap.
#:
#: v15: `idx_sig_dedup` gains COALESCE(stream, ''). Old rows all coalesce to
#: '', so the new key is the old key plus a constant — uniqueness holds after
#: the swap exactly where it held before.
#: v16: `idx_sig_dedup` gains COALESCE(city_id, -1), so one observation about
#: two cities is two rows. Strictly WIDENS the key: every pair distinct under
#: the old index stays distinct, so the swap cannot fail on existing rows.
_INDEX_SWAPS: tuple[tuple[str, str], ...] = (
    ("idx_sig_dedup", "COALESCE(city_id"),
)


class StrandedRunError(RuntimeError):
    """Raised when a stage would overwrite the record of an interrupted run.

    `start_agent_run` replaces by deleting, so re-running a stage whose row is
    still RUNNING destroys the only durable evidence that the process died
    inside it. Recovery has to happen first.
    """


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    datetime.utcnow() is deprecated and returns a naive datetime, which silently
    breaks the 48-hour correlation window the moment anything compares it to an
    aware value. Everything in this system is aware and UTC.
    """
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    """Serialise to ISO-8601 for storage. Naive input is assumed UTC."""
    dt = dt or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    """Parse a stored timestamp back to an aware UTC datetime.

    Tolerates the trailing 'Z' that external APIs emit, which
    datetime.fromisoformat cannot parse on Python 3.10.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class SurgeDB:
    """The database bus. One instance per process, shared by every agent."""

    def __init__(
        self,
        db_path: str | Path = "surge_iw.db",
        *,
        create_if_missing: bool = True,
        mission: Any = None,
    ) -> None:
        """`mission` is the loaded mission pack, or None if none is configured.

        It lives here rather than being threaded through every agent for two
        reasons. The database is the one object every agent already holds — it
        is the communication bus, by design — so this adds no new plumbing and
        creates no second copy that could drift from the first. And the write
        paths below are where the mission's vocabularies actually have to be
        enforced, now that they are no longer CHECK constraints: `signals.track`
        and the rest are plain TEXT in SQLite, and this is the only layer that
        sees every write to them.

        None is legitimate — `init-db` and contract generation need a schema,
        not a mission — but then any write of a mission-owned column is refused
        rather than accepted unchecked. Accepting one would be a validation
        layer that reads as enforcement and is not, which is exactly what
        dropping the CHECK constraints removed.
        """
        self.mission = mission
        self.path = str(db_path)
        self._is_memory = self.path in (":memory:", "")
        self._lock = threading.RLock()

        if not self._is_memory and not create_if_missing:
            if not Path(self.path).exists():
                raise FileNotFoundError(f"No database at {self.path}")

        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if not self._is_memory:
            # WAL lets the API read while an iteration writes. Meaningless for
            # :memory:, where SQLite silently keeps journal_mode=memory.
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")

        self._apply_schema()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _apply_schema(self) -> None:
        with self._lock:
            self.conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            # Order matters twice over. _migrate FIRST, because the rebuild
            # copies by name from the live table and every column _MIGRATIONS
            # knows about has to exist there before the copy runs. _upgrade
            # BEFORE _repair_observed_at, because that one issues DML, and a
            # transaction left open by it would turn the rebuild's
            # `PRAGMA foreign_keys = OFF` into a silent no-op.
            self._migrate()
            self._swap_indexes()
            upgraded = self._upgrade()
            repaired = self._repair_observed_at()
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at) "
                "VALUES (?,?)",
                (SCHEMA_VERSION, iso()),
            )
            self.conn.commit()
        for line in upgraded:
            # After the commit, so the change is durable before it is claimed.
            self.log("SurgeDB", "INFO",
                     f"Schema upgraded to version {SCHEMA_VERSION}: {line}")
        if repaired:
            # After the commit, so the repair is durable before it is claimed.
            self.log("SurgeDB", "WARNING",
                     f"Canonicalised {repaired} signal timestamp(s) stored in "
                     f"a vendor dialect; they were being excluded from the "
                     f"correlation window by a string comparison (9.12)")

    def _repair_observed_at(self) -> int:
        """Rewrite signal timestamps stored in a non-canonical dialect (9.12).

        Driven by the DATA rather than by a version number, on the same
        reasoning as `_migrate`: it is then idempotent and correct even for a
        database whose recorded version is missing or wrong. After one pass no
        row matches the predicate, so the cost settles at one indexed scan of
        nothing.

        This rewrites an analytical record, which is not done lightly. It
        changes the SPELLING of an instant and never the instant: every value
        is parsed and re-emitted, so a row that cannot be parsed is left
        exactly as it arrived. The vendor's original wording also survives in
        `raw_results.payload_json` until its retention deadline.
        """
        rows = self.conn.execute(
            "SELECT signal_id, observed_at FROM signals "
            "WHERE observed_at IS NOT NULL AND observed_at NOT LIKE '%+00:00'"
        ).fetchall()
        repaired = 0
        for row in rows:
            parsed = parse_iso(row["observed_at"])
            if parsed is None:
                continue
            canonical = iso(parsed)
            if canonical != row["observed_at"]:
                self.conn.execute(
                    "UPDATE signals SET observed_at = ? WHERE signal_id = ?",
                    (canonical, row["signal_id"]),
                )
                repaired += 1
        return repaired

    # ------------------------------------------------------------------
    # Schema version 12: the mission vocabularies leave the schema
    # ------------------------------------------------------------------

    def _upgrade(self) -> list[str]:
        """Rename and rebuild the tables that carried mission vocabulary.

        Everything here happens in ONE transaction. A half-applied upgrade —
        two tables renamed and two still constrained — is a shape no version
        number describes and no code could read, so the outcome has to be
        binary. SQLite makes that possible because its DDL is transactional.

        Runs before `_repair_observed_at`, which issues DML: Python's sqlite3
        opens an implicit transaction for DML and not for DDL, and
        `PRAGMA foreign_keys = OFF` is a SILENT no-op inside a transaction.
        """
        widen = [entry for entry in _VOCABULARY if self._lacks_value(*entry)]
        stale = [entry for entry in _RENAMES + _REBUILDS
                 if self._is_stale(*entry)]
        if not stale and not widen:
            return []

        backup = self._snapshot()
        done: list[str] = []
        # Must be outside any transaction to take effect at all.
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        if not self._is_memory:
            # A successful upgrade should be durable before it is reported.
            self.conn.execute("PRAGMA synchronous = FULL")
        try:
            # IMMEDIATE, not deferred: take the write lock now, so a second
            # process holding one fails here rather than halfway through.
            self.conn.execute("BEGIN IMMEDIATE")
            for table, old, new in _RENAMES:
                if not self._is_stale(table, old, new):
                    continue
                self.conn.execute(
                    f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
                done.append(f"{table}.{old} -> {new} (renamed)")
            for table, old, new in _REBUILDS:
                if not self._is_stale(table, old, new):
                    continue
                done.append(self._rebuild(table, old, new))
            for table, column, value in _VOCABULARY:
                if not self._lacks_value(table, column, value):
                    continue
                # Same rebuild, column keeping its name: only the CHECK moves.
                done.append(
                    f"{self._rebuild(table, column, column)} "
                    f"[+{value}]")
            orphans = self.conn.execute("PRAGMA foreign_key_check").fetchall()
            if orphans:
                raise SchemaUpgradeError(
                    f"foreign_key_check found {len(orphans)} orphaned row(s) "
                    f"after the rebuild; nothing has been committed. The "
                    f"database is unchanged and a snapshot is at {backup}.")
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            raise SchemaUpgradeError(
                f"Schema upgrade to version {SCHEMA_VERSION} failed and was "
                f"rolled back; the database is byte-for-byte unchanged. "
                f"{type(exc).__name__}: {exc}"
                + (f" A snapshot was taken at {backup}." if backup else "")
            ) from exc
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")
            if not self._is_memory:
                self.conn.execute("PRAGMA synchronous = NORMAL")
        if not self._is_memory:
            # So the -wal does not carry a mixed-schema tail.
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return done

    def _snapshot(self) -> str | None:
        """Copy the database before rewriting it, or refuse to proceed.

        `VACUUM INTO` rather than a file copy: it is one statement, consistent
        across a WAL database, refuses to overwrite an existing target, and
        cannot be half-done. A plain `cp` of the main file is wrong whenever a
        `-wal` exists, which is exactly when it matters.

        This upgrade is one-way — afterwards the previous revision of the code
        cannot read the file — so the snapshot IS the downgrade path, and
        failing to take one is a reason to stop rather than a warning.
        """
        if self._is_memory:
            return None
        target = f"{self.path}.pre-v{SCHEMA_VERSION}.bak"
        if Path(target).exists():
            raise SchemaUpgradeError(
                f"A snapshot already exists at {target}. That means an earlier "
                f"upgrade attempt got this far. Move or delete it once you "
                f"have confirmed you no longer need it, then start again.")
        self.conn.commit()
        try:
            self.conn.execute("VACUUM INTO ?", (target,))
        except sqlite3.Error as exc:
            raise SchemaUpgradeError(
                f"Could not snapshot the database to {target} ({exc}). "
                f"Refusing to upgrade: the upgrade cannot be undone, so a "
                f"database that cannot be copied must not be rewritten."
            ) from exc
        check = sqlite3.connect(target)
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if ok != "ok":
            raise SchemaUpgradeError(
                f"The snapshot at {target} failed its integrity check "
                f"({ok!r}). Refusing to upgrade.")
        return target

    def _table_sql(self, table: str) -> str:
        """The live CREATE TABLE text, which is where a CHECK is visible.

        SQLite exposes no metadata for CHECK constraints anywhere else — not
        in PRAGMA table_info, not in a pragma of its own — so the stored DDL
        is the only way to tell a constrained column from an unconstrained one.
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return (row["sql"] or "") if row else ""

    def _is_stale(self, table: str, old: str, new: str) -> bool:
        """True while `table` still has its pre-mission shape.

        Two independent facts make it stale, and either is enough:

        * it still names the old column; or
        * it still carries a CHECK enumerating that column's values.

        The second clause is what covers a column whose NAME does not change
        (`key_locations.location_type`), where a column-presence test would
        report "already done" while the CHECK was still there. It is also the
        seatbelt if a table were ever listed in both tuples, because
        RENAME COLUMN rewrites a CHECK rather than dropping it.
        """
        ddl = self._table_sql(table)
        if not ddl:
            return False                            # table not created yet
        if old != new and re.search(rf"\b{re.escape(old)}\b", ddl):
            return True
        return re.search(rf"\b{re.escape(new)}\s+IN\s*\(", ddl,
                         re.IGNORECASE) is not None

    def _lacks_value(self, table: str, column: str, value: str) -> bool:
        """True while `table`'s stored DDL does not yet permit `value`.

        Read from the DDL because that is the only place SQLite records a
        CHECK. Scoped to the table, not the whole schema, so a value that
        happens to appear in another table's vocabulary cannot mask this one.
        """
        ddl = self._table_sql(table)
        if not ddl:
            return False                            # table not created yet
        return f"'{value}'" not in ddl

    def _rebuild(self, table: str, old: str, new: str) -> str:
        """Rewrite one table around a constraint SQLite cannot drop.

        The copy names every column explicitly. `INSERT INTO t2 SELECT * FROM
        t1` would be shorter and is the standard recipe, and here it silently
        corrupts data: `_MIGRATIONS` appends with ADD COLUMN, so a live table's
        column ORDER does not match schema.sql's, and because the displaced
        columns are all TEXT, SQLite accepts the transposition without a word.
        """
        new_ddl = _table_ddl(table)
        live = [row["name"] for row in
                self.conn.execute(f"PRAGMA table_info({table})")]
        self.conn.execute(f"DROP TABLE IF EXISTS {table}__new")
        self.conn.execute(new_ddl.replace(table, f"{table}__new", 1))
        target = [row["name"] for row in
                  self.conn.execute(f"PRAGMA table_info({table}__new)")]

        # old name -> new name, for the one column being renamed.
        source_of = {new: old}
        missing = [c for c in target if source_of.get(c, c) not in live]
        if missing:
            raise SchemaUpgradeError(
                f"{table}: the new definition wants column(s) "
                f"{', '.join(missing)} that the live table does not have. "
                f"Refusing to copy — a name-matched insert would leave them "
                f"empty without saying so.")
        dropped = [c for c in live
                   if c not in target and c != old]
        if dropped:
            raise SchemaUpgradeError(
                f"{table}: live column(s) {', '.join(dropped)} are absent from "
                f"the new definition and would be silently discarded. Add them "
                f"to schema.sql or remove them deliberately.")
        pk = next((row["name"] for row in
                   self.conn.execute(f"PRAGMA table_info({table})")
                   if row["pk"]), None)
        if pk is not None and pk not in target:
            raise SchemaUpgradeError(
                f"{table}: the primary key {pk} is not being copied. "
                f"AUTOINCREMENT would renumber every row, and each child row "
                f"would then point at the wrong parent while foreign_key_check "
                f"still passed.")

        indexes = [row["sql"] for row in self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL", (table,))]
        for sql in indexes:
            if re.search(rf"\b{re.escape(old)}\b", sql):
                raise SchemaUpgradeError(
                    f"{table}: index DDL names the renamed column {old!r} and "
                    f"would be replayed against a column that no longer "
                    f"exists: {sql}")

        seq = self.conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
        ).fetchone()

        columns = ", ".join(target)
        sources = ", ".join(source_of.get(c, c) for c in target)
        before = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        self.conn.execute(
            f"INSERT INTO {table}__new ({columns}) "
            f"SELECT {sources} FROM {table}")
        after = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table}__new").fetchone()["n"]
        if before != after:
            raise SchemaUpgradeError(
                f"{table}: copied {after} row(s) out of {before}.")

        self.conn.execute(f"DROP TABLE {table}")
        self.conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
        for sql in indexes:
            self.conn.execute(sql)
        if seq is not None:
            # DROP TABLE takes the AUTOINCREMENT high-water mark with it, and
            # the copy resets it to max(rowid). A rollback that deleted the
            # newest rows leaves seq > max, and letting it fall back would
            # hand out an id an exported alert already used.
            self.conn.execute(
                "DELETE FROM sqlite_sequence WHERE name = ?", (table,))
            self.conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?,?)",
                (table, seq["seq"]))
        label = f"{table}.{old} -> {new}" if old != new else f"{table}.{new}"
        return f"{label} (rebuilt, {after} row(s))"

    def _migrate(self) -> None:
        """Add columns that post-date the database file on disk.

        Driven by the live table definition rather than by the recorded version
        number, so it is idempotent and correct even for a database whose
        schema_version row is missing or wrong. A fresh database gets the
        columns from schema.sql and this finds nothing to do.
        """
        for table, column, decl in _MIGRATIONS:
            existing = {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if not existing:                       # table not created yet
                continue
            if column not in existing:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )

    def _swap_indexes(self) -> None:
        """Recreate any index whose stored definition is stale.

        Driven by the live `sqlite_master` DDL rather than a version number,
        like everything else in this pipeline, so it is idempotent and correct
        even for a database whose recorded version is missing or wrong. The
        DROP+CREATE rebuilds the index over existing rows — a one-time cost on
        a large table, and never a data rewrite.
        """
        for index, fragment in _INDEX_SWAPS:
            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (index,)).fetchone()
            if row is None or row["sql"] is None:
                continue                      # not created yet, or auto-index
            if fragment in row["sql"]:
                continue                      # already the current shape
            self.conn.execute(f"DROP INDEX {index}")
            self.conn.execute(_index_ddl(index))

    def _mission(self, field: str) -> Any:
        """The loaded mission, or a refusal that says why there is none.

        Every mission-owned column funnels through here. Before version 12
        these values were CHECK-constrained, so SQLite refused a bad one even
        if Python forgot to; now SQLite accepts any text and this is the only
        thing standing between an LLM's output and the analytical record.
        """
        if self.mission is None:
            raise enums.EnumViolation(
                f"{field} cannot be validated: this database was opened with "
                f"no mission loaded, and the permitted values are the "
                f"mission's. Configure `mission.name`, or write only the "
                f"tables that carry no mission vocabulary.")
        return self.mission

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> "SurgeDB":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Primitives
    # ------------------------------------------------------------------

    def _insert(self, sql: str, params: Sequence[Any]) -> int:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return int(cur.lastrowid)

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute a write and return the affected row count."""
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.rowcount

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = 0) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        if row is None or row[0] is None:
            return default
        return row[0]

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def insert_session(
        self,
        *,
        label: str | None = None,
        expand_cities: bool = False,
        tracks: Iterable[str] | None = None,
        config: dict[str, Any] | None = None,
    ) -> int:
        mission = self._mission("tracks")
        # None means "every track this mission defines", which is the sensible
        # default and the only one the engine can supply — it has no opinion
        # about which of a mission's tracks matter.
        tracks = ([mission.track(t) for t in tracks]
                  if tracks is not None else list(mission.tracks))
        if not tracks:
            raise ValueError("A session needs at least one track.")
        return self._insert(
            "INSERT INTO sessions "
            "(created_at, label, expand_cities, tracks, config_json, status, "
            " mission) "
            "VALUES (?,?,?,?,?,?,?)",
            (iso(), label, int(expand_cities), ",".join(tracks),
             json.dumps(config or {}, sort_keys=True), "ACTIVE",
             mission.label),
        )

    def get_session(self, session_id: int) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        )

    def session_tracks(self, session_id: int) -> list[str]:
        row = self.get_session(session_id)
        if row is None:
            return []
        return [t for t in row["tracks"].split(",") if t]

    def close_session(self, session_id: int) -> None:
        self._exec(
            "UPDATE sessions SET status = 'CLOSED' WHERE session_id = ?",
            (session_id,),
        )

    # ------------------------------------------------------------------
    # Cities and key locations
    # ------------------------------------------------------------------

    def insert_city(
        self,
        session_id: int,
        name: str,
        *,
        canonical: str,
        state: str | None = None,
        is_seed: bool = True,
        admitted_by: str = "USER",
        admitted_iteration: int | None = None,
    ) -> int:
        enums.validate(admitted_by, enums.ADMITTED_BY, "admitted_by")
        return self._insert(
            "INSERT INTO cities "
            "(session_id, name, state, canonical, is_seed, admitted_by, "
            " admitted_iteration) VALUES (?,?,?,?,?,?,?)",
            (session_id, name, state, canonical, int(is_seed), admitted_by,
             admitted_iteration),
        )

    def find_city(self, session_id: int, canonical: str) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM cities WHERE session_id = ? AND canonical = ?",
            (session_id, canonical),
        )

    def get_city(self, city_id: int | None) -> sqlite3.Row | None:
        """One city by id, or None. Used where a refusal must name the city
        it applies to rather than being broadcast to every city."""
        if city_id is None:
            return None
        return self.one("SELECT * FROM cities WHERE city_id = ?", (city_id,))

    def get_cities(self, session_id: int) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM cities WHERE session_id = ? ORDER BY city_id",
            (session_id,),
        )

    def count_expanded_cities(self, session_id: int) -> int:
        return int(self.scalar(
            "SELECT COUNT(*) FROM cities WHERE session_id = ? AND is_seed = 0",
            (session_id,),
        ))

    def check_location_type(self, location_type: str | None) -> None:
        """Validate a location type WITHOUT writing anything.

        Exists so a caller can refuse before it commits. `insert_key_location`
        is reached only after the session and its cities are already in the
        database, so a check that lives only there turns an unknown value into
        a half-built session rather than a refusal — which is what happened
        when the mission vocabularies left the schema and `KeyLocationIn`'s
        field validator went with them. One implementation, two callers.
        """
        enums.validate_optional(
            location_type, self._mission("location_type").location_types,
            "location_type"
        )

    def insert_key_location(
        self,
        city_id: int,
        name: str,
        *,
        address: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        location_type: str | None = None,
    ) -> int:
        self.check_location_type(location_type)
        return self._insert(
            "INSERT INTO key_locations "
            "(city_id, name, address, lat, lon, location_type) VALUES (?,?,?,?,?,?)",
            (city_id, name, address, lat, lon, location_type),
        )

    # ------------------------------------------------------------------
    # Operator calendar
    # ------------------------------------------------------------------

    def insert_calendar_events(
        self, session_id: int, events: Sequence[Mapping[str, Any]],
        *, source_name: str | None = None,
    ) -> tuple[int, list[str]]:
        """Append events to a session's calendar. Returns (inserted, warnings).

        One transaction and one `added_at` for the whole batch, because the
        batch is one operator action and reconstruction filters on that
        instant. A duplicate — same (city, name, start) already on the session
        — is a warning rather than an error, mirroring `add_cities`: re-loading
        a grown file must be a safe way to append.
        """
        added_at = iso()
        inserted, warnings = 0, []
        with self._lock:
            for event in events:
                try:
                    self.conn.execute(
                        "INSERT INTO calendar_events "
                        "(session_id, name, city_label, city_canonical, "
                        " starts_at, ends_at, category, note, source_name, "
                        " added_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (session_id, event["name"], event["city_label"],
                         event["city_canonical"], event["starts_at"],
                         event["ends_at"], event.get("category"),
                         event.get("note"), source_name, added_at))
                    inserted += 1
                except sqlite3.IntegrityError:
                    warnings.append(
                        f"{event['city_label']}: {event['name']!r} starting "
                        f"{event['starts_at']} is already on this session's "
                        f"calendar; not added again")
            self.conn.commit()
        return inserted, warnings

    def calendar_events(
        self, session_id: int, *, added_before: str | None = None,
        city_canonical: str | None = None,
    ) -> list[sqlite3.Row]:
        """A session's calendar, oldest addition first.

        `added_before` is the reconstruction filter: an iteration's context is
        exactly the events with added_at <= its started_at, so a later append
        cannot change what an earlier receipt hashed. Ordered by
        (added_at, event_id) — insertion order — for the same reason: the
        prompt block must serialise identically on every rebuild.
        """
        sql = ("SELECT * FROM calendar_events WHERE session_id = ?")
        params: list[Any] = [session_id]
        if added_before is not None:
            sql += " AND added_at <= ?"
            params.append(added_before)
        if city_canonical is not None:
            sql += " AND city_canonical = ?"
            params.append(city_canonical)
        return self.all(sql + " ORDER BY added_at, event_id", tuple(params))

    def get_key_locations(self, city_id: int) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM key_locations WHERE city_id = ? ORDER BY location_id",
            (city_id,),
        )

    # ------------------------------------------------------------------
    # Geo cache
    # ------------------------------------------------------------------

    def put_geo_cache(
        self,
        kind: str,
        lookup_key: str,
        value: Any,
        *,
        resolved_by: str = "TABLE",
        ttl_days: int | None = None,
    ) -> None:
        enums.validate(kind, enums.GEO_CACHE_KINDS, "kind")
        enums.validate(resolved_by, enums.GEO_RESOLVED_BY, "resolved_by")
        expires = iso(utcnow() + timedelta(days=ttl_days)) if ttl_days else None
        self._exec(
            "INSERT INTO geo_cache "
            "(kind, lookup_key, value_json, resolved_by, resolved_at, expires_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(kind, lookup_key) DO UPDATE SET "
            "  value_json = excluded.value_json, "
            "  resolved_by = excluded.resolved_by, "
            "  resolved_at = excluded.resolved_at, "
            "  expires_at = excluded.expires_at",
            (kind, lookup_key, json.dumps(value), resolved_by, iso(), expires),
        )

    def get_geo_cache(self, kind: str, lookup_key: str) -> Any | None:
        """Return the cached value, or None if absent or expired.

        An UNRESOLVED entry is a real cached answer, not a miss: it records that
        a lookup was attempted and failed, so callers stop retrying it. Such
        entries return None but leave the row in place for the audit trail.
        """
        row = self.one(
            "SELECT * FROM geo_cache WHERE kind = ? AND lookup_key = ?",
            (kind, lookup_key),
        )
        if row is None:
            return None
        expires = parse_iso(row["expires_at"])
        if expires is not None and expires <= utcnow():
            return None
        if row["resolved_by"] == "UNRESOLVED":
            return None
        return json.loads(row["value_json"])

    def geo_cache_attempted(self, kind: str, lookup_key: str) -> bool:
        """True if this lookup has already been tried, successfully or not."""
        return self.one(
            "SELECT 1 FROM geo_cache WHERE kind = ? AND lookup_key = ?",
            (kind, lookup_key),
        ) is not None

    # ------------------------------------------------------------------
    # Iterations and agent runs
    # ------------------------------------------------------------------

    def insert_iteration(
        self,
        session_id: int,
        *,
        anchor_at: datetime | None = None,
        owner_epoch_id: int | None = None,
        retry_of_iteration_id: int | None = None,
    ) -> int:
        with self._lock:
            seq = int(self.scalar(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM iterations "
                "WHERE session_id = ?",
                (session_id,), default=1,
            ))
            anchor = iso(anchor_at) if anchor_at else iso()
            return self._insert(
                "INSERT INTO iterations "
                "(session_id, seq, anchor_at, started_at, stage, owner_epoch_id,"
                " retry_of_iteration_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (session_id, seq, anchor, iso(), "SEEDING", owner_epoch_id,
                 retry_of_iteration_id),
            )

    def get_iteration(self, iteration_id: int) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM iterations WHERE iteration_id = ?", (iteration_id,)
        )

    def set_stage(self, iteration_id: int, stage: str) -> None:
        enums.validate(stage, enums.STAGES, "stage")
        self._exec(
            "UPDATE iterations SET stage = ? WHERE iteration_id = ?",
            (stage, iteration_id),
        )

    def finish_iteration(
        self,
        iteration_id: int,
        *,
        outcome: str,
        error_message: str | None = None,
    ) -> None:
        """Close an iteration. Deliberately does NOT touch `degradations_json`.

        It used to overwrite it with whatever the caller passed, which meant the
        three failure paths that pass no notes silently erased every degradation
        the agents had recorded. Degradations are append-only and have one
        writer; see `append_degradation`.
        """
        enums.validate(outcome, enums.ITERATION_OUTCOMES, "outcome")
        stage = "FAILED" if outcome == "FAILED" else "COMPLETE"
        self._exec(
            "UPDATE iterations SET stage = ?, outcome = ?, finished_at = ?, "
            "error_message = ? WHERE iteration_id = ?",
            (stage, outcome, iso(), error_message, iteration_id),
        )

    def set_budget_plan(self, iteration_id: int, plan: dict[str, Any]) -> None:
        self._exec(
            "UPDATE iterations SET budget_plan_json = ? WHERE iteration_id = ?",
            (json.dumps(plan, sort_keys=True), iteration_id),
        )

    def start_agent_run(
        self, iteration_id: int, agent: str, stage: str,
        *, replace_running: bool = False,
    ) -> int:
        """Open a run record, replacing any previous one for this key.

        The replacement is a delete, and that is a trap worth guarding: a row
        left `RUNNING` by a process that died is the only durable trace of the
        interruption, and re-running the stage would erase it before anything
        could reconcile it. So a `RUNNING` row raises unless the caller says
        explicitly that it means to discard it.

        The one legitimate double-write is unaffected — `_stage_collect_tipped`
        runs CollectionAgent twice under the same key, but the first row is
        COMPLETE or FAILED by then.
        """
        with self._lock:
            existing = self.one(
                "SELECT run_id, status FROM agent_runs "
                "WHERE iteration_id = ? AND agent = ? AND stage = ?",
                (iteration_id, agent, stage),
            )
            if (existing is not None and existing["status"] == "RUNNING"
                    and not replace_running):
                raise StrandedRunError(
                    f"agent_runs {existing['run_id']} ({agent}/{stage} of "
                    f"iteration {iteration_id}) is still RUNNING. Reconcile the "
                    "iteration before re-running it, or the record of the "
                    "interruption is destroyed."
                )
            self._exec(
                "DELETE FROM agent_runs WHERE iteration_id = ? AND agent = ? "
                "AND stage = ?",
                (iteration_id, agent, stage),
            )
            return self._insert(
                "INSERT INTO agent_runs "
                "(iteration_id, agent, stage, status, started_at) "
                "VALUES (?,?,?,?,?)",
                (iteration_id, agent, stage, "RUNNING", iso()),
            )

    def interrupt_agent_runs(
        self, iteration_id: int, detail: str
    ) -> list[sqlite3.Row]:
        """Close every RUNNING row for an iteration. Returns what was closed.

        Every agent, not only the orchestrator's own stage rows — otherwise a
        stage report shows a CollectionAgent that has been running since the
        crash.
        """
        with self._lock:
            open_rows = self.all(
                "SELECT * FROM agent_runs WHERE iteration_id = ? "
                "AND status = 'RUNNING' ORDER BY run_id",
                (iteration_id,),
            )
            if open_rows:
                self._exec(
                    "UPDATE agent_runs SET status = 'INTERRUPTED', "
                    "finished_at = ?, error_message = ? "
                    "WHERE iteration_id = ? AND status = 'RUNNING'",
                    (iso(), detail[:2000], iteration_id),
                )
            return open_rows

    # ------------------------------------------------------------------
    # Process epochs
    # ------------------------------------------------------------------

    def open_epoch(
        self, *, host: str, pid: int, entry_point: str
    ) -> sqlite3.Row:
        """Record this process instance and return its row."""
        enums.validate(entry_point, enums.ENTRY_POINTS, "entry_point")
        epoch_id = self._insert(
            "INSERT INTO process_epochs "
            "(started_at, host, pid, entry_point) VALUES (?,?,?,?)",
            (iso(), host, int(pid), entry_point),
        )
        return self.get_epoch(epoch_id)

    def get_epoch(self, epoch_id: int) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM process_epochs WHERE epoch_id = ?", (epoch_id,)
        )

    def open_epochs(self, *, before: int | None = None) -> list[sqlite3.Row]:
        """Epochs that never recorded an ending — i.e. that died.

        `before` excludes the current epoch, which is trivially still open.
        """
        if before is None:
            return self.all(
                "SELECT * FROM process_epochs WHERE ended_at IS NULL "
                "AND shutdown_kind IS NULL ORDER BY epoch_id"
            )
        return self.all(
            "SELECT * FROM process_epochs WHERE ended_at IS NULL "
            "AND shutdown_kind IS NULL AND epoch_id < ? ORDER BY epoch_id",
            (before,),
        )

    def close_epoch(
        self,
        epoch_id: int,
        kind: str,
        *,
        stranded: Iterable[int] = (),
        closed_by: int | None = None,
    ) -> None:
        """Record how a process ended.

        UNKNOWN deliberately leaves `ended_at` NULL. It is written by a later
        process onto a predecessor it found open, and that process does not know
        when its predecessor died — inventing a timestamp that later reads as
        fact is worse than admitting the gap.
        """
        enums.validate(kind, enums.SHUTDOWN_KINDS, "shutdown_kind")
        ids = sorted({int(i) for i in stranded})
        self._exec(
            "UPDATE process_epochs SET ended_at = ?, shutdown_kind = ?, "
            "stranded_json = ?, closed_by_epoch = ? WHERE epoch_id = ?",
            (None if kind == "UNKNOWN" else iso(), kind,
             json.dumps(ids) if ids else None, closed_by, epoch_id),
        )

    def previous_epoch(self, epoch_id: int) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM process_epochs WHERE epoch_id < ? "
            "ORDER BY epoch_id DESC LIMIT 1",
            (epoch_id,),
        )

    def set_owner_epoch(self, iteration_id: int, epoch_id: int) -> None:
        """Claim an iteration for this process. Called on start and on resume."""
        self._exec(
            "UPDATE iterations SET owner_epoch_id = ? WHERE iteration_id = ?",
            (epoch_id, iteration_id),
        )

    def unfinished_iterations(
        self, *, not_owned_by: int | None = None
    ) -> list[sqlite3.Row]:
        """Iterations that never reached an outcome.

        The predicate never consults `process_epochs.ended_at`: an epoch killed
        with SIGKILL has none, and that is precisely the case this exists for.
        Ownership alone decides.
        """
        sql = ["SELECT * FROM iterations WHERE finished_at IS NULL "
               "AND outcome IS NULL AND owner_epoch_id IS NOT NULL"]
        params: list[Any] = []
        if not_owned_by is not None:
            sql.append("AND owner_epoch_id != ?")
            params.append(not_owned_by)
        return self.all(" ".join(sql) + " ORDER BY iteration_id", params)

    def interrupted_iterations(
        self, session_id: int | None = None
    ) -> list[sqlite3.Row]:
        """Marked interrupted and not yet resumed or abandoned.

        Narrower than `unfinished_iterations` on purpose: this answers "what did
        a process crash leave behind", which only the reconcile can stamp. It is
        NOT the predicate that decides whether a new iteration may start — see
        8.7(a) and `unfinished_iterations`.
        """
        sql = ["SELECT * FROM iterations WHERE interrupted_at IS NOT NULL "
               "AND finished_at IS NULL"]
        params: list[Any] = []
        if session_id is not None:
            sql.append("AND session_id = ?")
            params.append(session_id)
        return self.all(" ".join(sql) + " ORDER BY iteration_id", params)

    def open_iterations(
        self, session_id: int | None = None
    ) -> list[sqlite3.Row]:
        """Every iteration that has not closed, however it came to be open.

        Distinct from `unfinished_iterations` above, which is the *reconcile's*
        predicate and additionally requires `owner_epoch_id IS NOT NULL` and
        `outcome IS NULL` — it asks "which runs did an epoch abandon". This one
        asks the simpler operational question: what is still open on this
        session, and therefore what blocks the next iteration.

        `interrupted_at` is stamped in exactly one place — the reconcile, on
        detecting a crashed epoch — so it names a strict subset of the runs that
        are still outstanding. A manual walk stepped partway and left, or a
        cancellation recorded against an iteration that was not on a worker,
        leaves it NULL; the iteration then reported as PENDING and the next
        trigger was allowed, which is the defect 8.7(a) fixes.

        `finished_at IS NULL` is the whole rule. Both exits (resume, abandon)
        set it, and the one sanctioned way back to open — `discard_last()` while
        debugging — sets it back to NULL deliberately, so an iteration reopened
        for debugging blocks a new run exactly as it should.
        """
        sql = ["SELECT * FROM iterations WHERE finished_at IS NULL"]
        params: list[Any] = []
        if session_id is not None:
            sql.append("AND session_id = ?")
            params.append(session_id)
        return self.all(" ".join(sql) + " ORDER BY iteration_id", params)

    # ------------------------------------------------------------------
    # Degradations
    #
    # `iterations.degradations_json` is append-only and has exactly ONE writer,
    # here. It had four — this method, `finish_iteration`, and a private copy in
    # both the orchestrator and RecoveryService — each doing its own
    # read-modify-write, and `finish_iteration` *overwrote* the column, so a
    # failure path that passed no notes erased everything the agents had
    # recorded.
    #
    # Each entry carries the SOURCE that wrote it: a stage name, or "recovery"
    # for an operator action, or "collection-gaps" for the derived summary. That
    # is what makes a note retractable when its stage is discarded. Without it,
    # discarding TRIAGING left "10 of 20 post(s) were not judged" on an
    # iteration whose re-run had judged all twenty — and `_finish` reads these
    # to decide PARTIAL, so the iteration stayed degraded by a gap that no
    # longer existed. Observed in a real database.
    # ------------------------------------------------------------------

    #: Sources that are not stages, and so are never retracted by a rollback.
    DEGRADATION_RECOVERY = "recovery"
    DEGRADATION_GAPS = "collection-gaps"

    def _degradations(self, iteration_id: int) -> list[dict[str, Any]]:
        """Raw entries, normalising the legacy bare-string shape."""
        row = self.get_iteration(iteration_id)
        if row is None or not row["degradations_json"]:
            return []
        try:
            raw = json.loads(row["degradations_json"])
        except (TypeError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict) and "note" in item:
                out.append({"source": item.get("source"),
                            "note": str(item["note"])})
            else:
                # Written before entries carried a source. Unattributed, so a
                # rollback leaves it alone — overstating a gap is the safe
                # direction, and inventing an attribution would be worse.
                out.append({"source": None, "note": str(item)})
        return out

    def append_degradation(
        self, iteration_id: int, note: str, *, source: str | None = None
    ) -> None:
        """Record one thing an iteration could not do.

        Idempotent per (source, note): a resumed stage that degrades the same
        way twice should not report the gap twice.
        """
        if self.get_iteration(iteration_id) is None:
            return
        entries = self._degradations(iteration_id)
        entry = {"source": source, "note": note}
        if entry not in entries:
            entries.append(entry)
        self._write_degradations(iteration_id, entries)

    def replace_degradation(
        self, iteration_id: int, source: str, note: str
    ) -> None:
        """Append `note`, first dropping anything else this source had said.

        For a DERIVED note — the collection-gap summary is recomputed on every
        `_finish`, so an earlier one describing gaps a resume has since closed
        must not survive alongside the new one.
        """
        entries = [e for e in self._degradations(iteration_id)
                   if e["source"] != source]
        entries.append({"source": source, "note": note})
        self._write_degradations(iteration_id, entries)

    def discard_degradations(self, iteration_id: int, source: str) -> int:
        """Retract what one stage recorded, because its rows are being deleted.

        Returns how many were removed. Called by stage rollback: the rows a
        stage wrote and the notes it wrote about what it could not write are the
        same record, and deleting one while keeping the other leaves the
        iteration asserting a gap that no longer exists.
        """
        entries = self._degradations(iteration_id)
        kept = [e for e in entries if e["source"] != source]
        if len(kept) != len(entries):
            self._write_degradations(iteration_id, kept)
        return len(entries) - len(kept)

    def degradation_notes(self, iteration_id: int) -> list[str]:
        """The notes alone, for display and for the outcome decision."""
        return [e["note"] for e in self._degradations(iteration_id)]

    def _write_degradations(
        self, iteration_id: int, entries: list[dict[str, Any]]
    ) -> None:
        self._exec(
            "UPDATE iterations SET degradations_json = ? WHERE iteration_id = ?",
            (json.dumps(entries), iteration_id),
        )

    def mark_interrupted(
        self, iteration_id: int, stage: str | None
    ) -> bool:
        """Stamp the interruption once. Returns False if already stamped.

        Guarded so a second reconcile cannot restamp the time or duplicate the
        degradation note the caller appends alongside it.
        """
        return self._exec(
            "UPDATE iterations SET interrupted_at = ?, interrupted_stage = ? "
            "WHERE iteration_id = ? AND interrupted_at IS NULL",
            (iso(), stage, iteration_id),
        ) > 0

    def finish_agent_run(
        self, run_id: int, status: str, error_message: str | None = None
    ) -> None:
        enums.validate(status, enums.AGENT_RUN_STATUSES, "status")
        self._exec(
            "UPDATE agent_runs SET status = ?, finished_at = ?, "
            "error_message = ? WHERE run_id = ?",
            (status, iso(), error_message, run_id),
        )

    def get_agent_runs(self, iteration_id: int) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM agent_runs WHERE iteration_id = ? ORDER BY run_id",
            (iteration_id,),
        )

    def get_iterations(
        self, session_id: int, limit: int = 50
    ) -> list[sqlite3.Row]:
        """Most recent first — an operator watching a session wants the latest."""
        return self.all(
            "SELECT * FROM iterations WHERE session_id = ? "
            "ORDER BY seq DESC LIMIT ?",
            (session_id, limit),
        )

    def iteration_counts(self, iteration_id: int) -> dict[str, int]:
        """What one iteration produced, for the poll response.

        Skipped queries are reported separately from failed ones. Both are
        coverage gaps, but they have different remedies: a failure needs
        investigation, an exhausted allocation needs either patience or money.
        """
        queue = {
            row["status"]: int(row["n"]) for row in self.all(
                "SELECT status, COUNT(*) AS n FROM query_queue "
                "WHERE iteration_id = ? GROUP BY status",
                (iteration_id,),
            )
        }
        skipped = sum(
            count for status, count in queue.items()
            if status.startswith("SKIPPED_")
        )
        one = lambda sql: int(self.scalar(sql, (iteration_id,)))  # noqa: E731
        return {
            "queries_enqueued": sum(queue.values()),
            "queries_pending": queue.get("PENDING", 0)
            + queue.get("IN_PROGRESS", 0),
            "queries_executed": queue.get("COMPLETE", 0),
            "queries_failed": queue.get("FAILED", 0),
            "queries_skipped": skipped,
            "raw_results": one(
                "SELECT COUNT(*) FROM raw_results WHERE iteration_id = ?"),
            "signals": one(
                "SELECT COUNT(*) FROM signals WHERE iteration_id = ?"),
            "triage_decisions": one(
                "SELECT COUNT(*) FROM triage_decisions WHERE iteration_id = ?"),
            "correlations": one(
                "SELECT COUNT(*) FROM correlations WHERE iteration_id = ?"),
            "alerts": one(
                "SELECT COUNT(*) FROM alerts WHERE iteration_id = ?"),
        }

    # ------------------------------------------------------------------
    # Query queue
    # ------------------------------------------------------------------

    def enqueue_query(
        self,
        *,
        session_id: int,
        iteration_id: int | None,
        source_type: str,
        endpoint: str,
        params: dict[str, Any],
        dedup_key: str,
        priority: int = 50,
        tip_depth: int = 0,
        origin: str = "TIP",
        city_id: int | None = None,
        location_id: int | None = None,
        tipped_by_signal_id: int | None = None,
        rule_code: str | None = None,
        not_before: datetime | None = None,
        created_iteration_id: int | None = None,
        stream: str | None = None,
    ) -> int:
        """Insert a queue row. Raises sqlite3.IntegrityError on a duplicate.

        The duplicate case is a normal outcome, not an error condition — the
        caller in QueueAgent catches it and records a DEDUPED decision. The
        uniqueness guarantee lives in idx_qq_dedup rather than in a prior SELECT
        so that it holds even under concurrent enqueues.

        `created_iteration_id` defaults to `iteration_id` and only differs for
        scheduled work, whose owning iteration is deliberately NULL.
        """
        enums.validate(source_type, enums.SOURCE_TYPES, "source_type")
        enums.validate(origin, enums.QUERY_ORIGINS, "origin")
        if stream is not None:
            self._mission("stream").stream_id(stream)
        return self._insert(
            "INSERT INTO query_queue "
            "(session_id, iteration_id, source_type, endpoint, params_json, "
            " stream, city_id, location_id, dedup_key, priority, tip_depth, "
            " tipped_by_signal_id, rule_code, not_before, status, origin, "
            " created_at, created_iteration_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING',?,?,?)",
            (session_id, iteration_id, source_type, endpoint,
             json.dumps(params, sort_keys=True), stream, city_id, location_id,
             dedup_key, priority, tip_depth, tipped_by_signal_id, rule_code,
             iso(not_before) if not_before else None, origin,
             iso(),
             iteration_id if created_iteration_id is None
             else created_iteration_id),
        )

    def claim_next_query(
        self, iteration_id: int, source_types: Iterable[str]
    ) -> sqlite3.Row | None:
        """Atomically take the highest-priority PENDING query and mark it live.

        The lock makes the select-then-update indivisible, so two collection
        threads cannot claim the same row.
        """
        types = list(source_types)
        if not types:
            return None
        placeholders = ",".join("?" * len(types))
        with self._lock:
            row = self.one(
                f"SELECT * FROM query_queue "
                f"WHERE iteration_id = ? AND status = 'PENDING' "
                f"  AND source_type IN ({placeholders}) "
                f"ORDER BY priority ASC, query_id ASC LIMIT 1",
                (iteration_id, *types),
            )
            if row is None:
                return None
            self._exec(
                "UPDATE query_queue SET status = 'IN_PROGRESS' WHERE query_id = ?",
                (row["query_id"],),
            )
            # Re-read so the caller sees IN_PROGRESS rather than the PENDING it
            # was a moment ago. Returning the pre-update row is harmless today
            # because collection reads only the endpoint and params, but a stale
            # status on a row that was just claimed is a trap for the next
            # caller who trusts it.
            return self.one(
                "SELECT * FROM query_queue WHERE query_id = ?",
                (row["query_id"],),
            )

    def complete_query(self, query_id: int, result_count: int) -> None:
        self._exec(
            "UPDATE query_queue SET status = 'COMPLETE', result_count = ?, "
            "executed_at = ? WHERE query_id = ?",
            (result_count, iso(), query_id),
        )

    def fail_query(self, query_id: int, error_message: str) -> None:
        """Mark a query failed.

        A failure is emphatically not an empty result. It sets executed_at so
        the cooldown still applies (no hammering a broken endpoint), but
        correlation reads the FAILED status as a coverage gap.
        """
        self._exec(
            "UPDATE query_queue SET status = 'FAILED', error_message = ?, "
            "executed_at = ? WHERE query_id = ?",
            ((redact_text(error_message) or "")[:2000], iso(), query_id),
        )

    def interrupt_query(self, query_id: int, detail: str) -> None:
        """Mark a query lost to a process exit.

        Deliberately not `fail_query`: FAILED means the vendor was asked and
        answered badly, and it sets `executed_at`, which drives the cooldown
        suppression. A query that was claimed and may never have been sent is a
        different fact, and conflating them corrupts the failure-rate view an
        operator uses to diagnose a broken key.

        It still counts as a coverage gap — INTERRUPTED is in
        UNRELIABLE_QUERY_STATUSES — which is what stops an abandoned iteration
        from reporting full coverage on collection that never happened.
        """
        self._exec(
            "UPDATE query_queue SET status = 'INTERRUPTED', error_message = ? "
            "WHERE query_id = ?",
            ((redact_text(detail) or "")[:2000], query_id),
        )

    def skip_query(self, query_id: int, status: str, skip_reason: str,
                   detail: str = "") -> None:
        """Record a query that yielded nothing usable, and why.

        **`executed_at` is set when the query actually called a vendor**, and
        that is read from the `api_calls` ledger rather than passed in by the
        caller. A caller that had to remember would eventually forget, and the
        ledger already knows — it is written by the connector on every call.

        This is not bookkeeping. `executed_at` is what drives the cooldown
        (`last_execution` reads `MAX(executed_at)`), so a query that spent real
        money and left it NULL is not merely mis-recorded: it is outside the
        cooldown and will be reissued next iteration and spend again. Measured
        live on the reference run — one Staying query made 13 calls for 100
        credits and recorded `executed_at` NULL, with nothing to stop it
        repeating.

        `detail` says which of a reason's cases fired. It was previously
        computed, logged, and dropped, so the row could not answer a question
        the log could.
        """
        enums.validate(status, enums.QUERY_STATUSES, "status")
        enums.validate(skip_reason, enums.SKIP_REASONS, "skip_reason")
        spent = int(self.scalar(
            "SELECT COUNT(*) FROM api_calls WHERE query_id = ?", (query_id,)
        ) or 0)
        self._exec(
            "UPDATE query_queue SET status = ?, skip_reason = ?, "
            "error_message = ?, executed_at = COALESCE(executed_at, ?) "
            "WHERE query_id = ?",
            (status, skip_reason, detail or None,
             iso() if spent else None, query_id),
        )

    def count_queued(self, iteration_id: int) -> int:
        return int(self.scalar(
            "SELECT COUNT(*) FROM query_queue WHERE iteration_id = ?",
            (iteration_id,),
        ))

    def count_queued_for_city(self, iteration_id: int, city_id: int) -> int:
        return int(self.scalar(
            "SELECT COUNT(*) FROM query_queue "
            "WHERE iteration_id = ? AND city_id = ?",
            (iteration_id, city_id),
        ))

    def last_execution(self, dedup_key: str) -> datetime | None:
        """When this exact query last ran, across all iterations.

        Drives the cooldown guard. Counts FAILED and COMPLETE alike: a broken
        endpoint should not be retried on a tight loop either.
        """
        return parse_iso(self.scalar(
            "SELECT MAX(executed_at) FROM query_queue "
            "WHERE dedup_key = ? AND executed_at IS NOT NULL",
            (dedup_key,), default=None,
        ))

    def has_queued(
        self, iteration_id: int, city_id: int, source_types: Iterable[str]
    ) -> bool:
        """Whether any query of these types already exists for this city.

        Used by the flight-escalation rule, which must not re-tip lodging that
        the social rules already queued.
        """
        types = list(source_types)
        if not types:
            return False
        placeholders = ",".join("?" * len(types))
        return self.one(
            f"SELECT 1 FROM query_queue WHERE iteration_id = ? AND city_id = ? "
            f"AND source_type IN ({placeholders}) LIMIT 1",
            (iteration_id, city_id, *types),
        ) is not None

    def get_queue(self, iteration_id: int) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM query_queue WHERE iteration_id = ? "
            "ORDER BY priority, query_id",
            (iteration_id,),
        )

    def get_query(self, query_id: int) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM query_queue WHERE query_id = ?", (query_id,)
        )

    def session_queue(
        self, session_id: int, *, iteration_id: int | None = None,
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        """Queue rows for a session, newest first, including future work."""
        sql = ["SELECT * FROM query_queue WHERE session_id = ?"]
        params: list[Any] = [session_id]
        if iteration_id is not None:
            sql.append("AND (iteration_id = ? OR created_iteration_id = ?)")
            params.extend([iteration_id, iteration_id])
        params.append(limit)
        return self.all(
            " ".join(sql) + " ORDER BY query_id DESC LIMIT ?", params
        )

    def queue_status_counts(
        self, session_id: int, *, iteration_id: int | None = None
    ) -> dict[str, int]:
        sql = ["SELECT status, COUNT(*) AS n FROM query_queue WHERE session_id = ?"]
        params: list[Any] = [session_id]
        if iteration_id is not None:
            sql.append("AND iteration_id = ?")
            params.append(iteration_id)
        return {
            row["status"]: int(row["n"])
            for row in self.all(" ".join(sql) + " GROUP BY status", params)
        }

    def pending_scheduled(self, session_id: int) -> list[sqlite3.Row]:
        """Follow-ons written for a future iteration and not yet claimed."""
        return self.all(
            "SELECT * FROM query_queue WHERE session_id = ? "
            "AND iteration_id IS NULL AND status = 'PENDING' "
            "ORDER BY not_before, query_id",
            (session_id,),
        )

    def due_scheduled_queries(
        self, session_id: int, now: datetime | None = None
    ) -> list[sqlite3.Row]:
        """Follow-ons from earlier iterations whose not_before has passed."""
        return self.all(
            "SELECT * FROM query_queue "
            "WHERE session_id = ? AND iteration_id IS NULL AND status = 'PENDING' "
            "  AND (not_before IS NULL OR not_before <= ?) "
            "ORDER BY priority, query_id",
            (session_id, iso(now) if now else iso()),
        )

    def adopt_query(self, query_id: int, iteration_id: int) -> None:
        """Attach a previously-scheduled query to the iteration now running."""
        self._exec(
            "UPDATE query_queue SET iteration_id = ? WHERE query_id = ?",
            (iteration_id, query_id),
        )

    def unreliable_source_types(
        self, iteration_id: int, city_id: int
    ) -> list[str]:
        """Source types for this city that failed or were skipped.

        This is the input to the data-completeness calculation. It is the reason
        a broken API key cannot masquerade as "nothing found".
        """
        placeholders = ",".join("?" * len(enums.UNRELIABLE_QUERY_STATUSES))
        rows = self.all(
            f"SELECT DISTINCT source_type FROM query_queue "
            f"WHERE iteration_id = ? AND city_id = ? "
            f"  AND status IN ({placeholders})",
            (iteration_id, city_id, *sorted(enums.UNRELIABLE_QUERY_STATUSES)),
        )
        return sorted(r["source_type"] for r in rows)

    def unreliable_social_streams(
        self, iteration_id: int, city_id: int
    ) -> list[str | None]:
        """Streams whose SOCIAL queries for this city failed or were skipped,
        plus refusals a guard recorded per stream. The per-stream half of
        `unreliable_source_types` + `refused_source_types`, for mapping social
        losses to the family each stream occupies."""
        placeholders = ",".join("?" * len(enums.UNRELIABLE_QUERY_STATUSES))
        streams = {
            row["stream"] for row in self.all(
                f"SELECT DISTINCT stream FROM query_queue "
                f"WHERE iteration_id = ? AND city_id = ? "
                f"  AND source_type = 'SOCIAL' "
                f"  AND status IN ({placeholders})",
                (iteration_id, city_id,
                 *sorted(enums.UNRELIABLE_QUERY_STATUSES)))
        }
        refusals = ("CAP_CITY", "CAP_ITERATION", "BUDGET_EXHAUSTED",
                    "NO_MAPPING")
        ref_ph = ",".join("?" * len(refusals))
        streams |= {
            row["stream"] for row in self.all(
                f"SELECT DISTINCT stream FROM queue_decisions "
                f"WHERE iteration_id = ? AND source_type = 'SOCIAL' "
                f"  AND stream IS NOT NULL AND outcome IN ({ref_ph})",
                (iteration_id, *refusals))
        }
        return sorted(streams, key=lambda v: (v is None, v or ""))

    def unreliable_sources(
        self, iteration_id: int, city_id: int
    ) -> list[tuple[str, str]]:
        """Failed or skipped queries as (source_type, endpoint) pairs (9.11).

        The endpoint is what a reader needs and the source type alone does not
        give them. `LODGING` covers two endpoints — availability through
        `/search` and pricing through `/price-compare` — and they fail for
        different reasons, at different rates. Staying's calendar coverage is
        sparse enough that the availability leg routinely returns too few
        paired listings to score while the price leg succeeds, and a caveat
        reading "lodging unavailable" is wrong about that.

        Note the endpoint recorded is the query's ENTRY endpoint. The lodging
        availability path calls `/search` and then `/availability`; a pairing
        shortfall happens at the second and the row still says `/search`. The
        query's own `skip_reason` and `error_message` carry the precise cause.
        """
        placeholders = ",".join("?" * len(enums.UNRELIABLE_QUERY_STATUSES))
        rows = self.all(
            f"SELECT DISTINCT source_type, endpoint FROM query_queue "
            f"WHERE iteration_id = ? AND city_id = ? "
            f"  AND status IN ({placeholders})",
            (iteration_id, city_id, *sorted(enums.UNRELIABLE_QUERY_STATUSES)),
        )
        return sorted((r["source_type"], r["endpoint"]) for r in rows)

    def collected_source_types(
        self, iteration_id: int, city_id: int
    ) -> list[str]:
        """Source types that actually COMPLETED for this city (9.11).

        The other half of the gap question. A family is only a coverage gap
        when nothing in it was collected — if the price leg succeeded, lodging
        was measured, and scoring it as absent would understate what is known
        about the city while the alert told the reader the opposite.
        """
        rows = self.all(
            "SELECT DISTINCT source_type FROM query_queue "
            "WHERE iteration_id = ? AND city_id = ? AND status = 'COMPLETE'",
            (iteration_id, city_id),
        )
        return sorted(r["source_type"] for r in rows)

    def flight_baseline_samples(
        self, city_id: int, *, before_iteration: int, since: datetime,
        exclude_bands: Sequence[str] = ("MEDIUM", "HIGH"),
    ) -> list[tuple[int, str, str | None, int]]:
        """Historical flight counts for a city, one row per iteration/category.

        The samples for 9.10's baseline, and they cost nothing: every
        flight-summary response the system already buys is an observation of
        that airport's normal traffic, and `signals` is the analytical record
        that outlives the payload's retention deadline. A dedicated baseline
        query per airport per iteration would be the most expensive call in the
        system run for no new evidence.

        Three rules the owner settled:

        * **Only prior iterations.** `before_iteration` excludes the run being
          scored, so the baseline is what NORMAL looked like rather than a
          number the current surge helped set.
        * **Contaminated samples are dropped.** An iteration whose correlation
          for this city alerted at MEDIUM or above is excluded, so a sustained
          surge cannot quietly become its own baseline and erase itself.
        * **A zero is a sample.** An iteration that collected flights and found
          none of a category is evidence that the category is normally absent.
          Returned as an explicit 0 row rather than an omission, because
          omitting it biases the median upward — the exact direction that would
          hide a surge.
        """
        placeholders = ",".join("?" * len(exclude_bands)) or "NULL"
        sampled = self.all(
            f"SELECT DISTINCT q.iteration_id FROM query_queue q "
            f"JOIN iterations i USING (iteration_id) "
            f"WHERE q.city_id = ? AND q.iteration_id < ? "
            f"  AND q.source_type LIKE 'FLIGHT%' AND q.status = 'COMPLETE' "
            f"  AND i.started_at >= ? "
            f"  AND q.iteration_id NOT IN ("
            f"    SELECT iteration_id FROM correlations "
            f"    WHERE city_id = ? AND band IN ({placeholders}))",
            (city_id, before_iteration, iso(since), city_id, *exclude_bands),
        )
        iterations = [int(r["iteration_id"]) for r in sampled]
        if not iterations:
            return []

        marks = ",".join("?" * len(iterations))
        counted = self.all(
            f"SELECT iteration_id, flight_category, category_confidence, "
            f"       COUNT(DISTINCT COALESCE(fr24_id, registration, callsign, "
            f"                               CAST(signal_id AS TEXT))) AS n "
            f"  FROM signals "
            f" WHERE city_id = ? AND signal_type = 'FLIGHT' "
            f"   AND iteration_id IN ({marks}) "
            f" GROUP BY iteration_id, flight_category, category_confidence",
            (city_id, *iterations),
        )
        rows = [(int(r["iteration_id"]), r["flight_category"],
                 r["category_confidence"], int(r["n"])) for r in counted]
        # Every sampled iteration must appear for every category seen across
        # the window, so an absent category reads as zero rather than as no
        # sample at all.
        categories = {(c, conf) for _i, c, conf, _n in rows}
        present = {(i, c, conf) for i, c, conf, _n in rows}
        for iteration_id in iterations:
            for category, confidence in categories:
                if (iteration_id, category, confidence) not in present:
                    rows.append((iteration_id, category, confidence, 0))
        return sorted(rows)

    def refused_source_types(
        self, iteration_id: int, city_id: int | None = None
    ) -> list[str]:
        """Source types a guard refused to enqueue at all.

        A capped or priced-out query leaves NO query_queue row, so
        `unreliable_source_types` — which reads that table — cannot see it. The
        first live iteration made the consequence concrete: the per-city cap
        refused all twelve seed queries for one track, that track was never
        collected, and its correlation still reported completeness as though
        only triage had degraded.

        Planned collection that did not happen is a coverage gap whether the
        guard sat before or after the queue row was written.
        """
        refusals = ("CAP_CITY", "CAP_ITERATION", "BUDGET_EXHAUSTED")
        placeholders = ",".join("?" * len(refusals))
        sql = [f"SELECT DISTINCT source_type FROM queue_decisions "
               f"WHERE iteration_id = ? AND source_type IS NOT NULL "
               f"AND outcome IN ({placeholders})"]
        params: list[Any] = [iteration_id, *refusals]
        if city_id is not None:
            # queue_decisions carries a city NAME, not an id — the refusal can
            # precede the city existing at all.
            sql.append("AND (city_name IS NULL OR city_name = "
                       "(SELECT name FROM cities WHERE city_id = ?))")
            params.append(city_id)
        return sorted(r["source_type"] for r in self.all(" ".join(sql), params))

    def record_queue_decision(
        self,
        iteration_id: int,
        rule_code: str,
        outcome: str,
        *,
        source_type: str | None = None,
        city_name: str | None = None,
        dedup_key: str | None = None,
        signal_id: int | None = None,
        detail: str | None = None,
        stage: str | None = None,
        stream: str | None = None,
    ) -> int:
        enums.validate(outcome, enums.QUEUE_DECISION_OUTCOMES, "outcome")
        enums.validate_optional(stage, enums.STAGES, "stage")
        if stream is not None:
            self._mission("stream").stream_id(stream)
        return self._insert(
            "INSERT INTO queue_decisions "
            "(iteration_id, rule_code, outcome, source_type, stream, "
            " city_name, dedup_key, signal_id, detail, decided_at, stage) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (iteration_id, rule_code, outcome, source_type, stream,
             city_name, dedup_key, signal_id, detail, iso(), stage),
        )

    def get_queue_decisions(self, iteration_id: int) -> list[sqlite3.Row]:
        """Queue decisions in the order they were made.

        The ORDER BY is not optional. idx_qd_iter covers (iteration_id, outcome),
        so an unordered SELECT is served straight from that index and arrives
        sorted by outcome — DEDUPED before ENQUEUED — which looks like a
        chronology and is not one.
        """
        return self.all(
            "SELECT * FROM queue_decisions WHERE iteration_id = ? "
            "ORDER BY decision_id",
            (iteration_id,),
        )

    def decision_counts(self, iteration_id: int) -> dict[str, int]:
        rows = self.all(
            "SELECT outcome, COUNT(*) AS n FROM queue_decisions "
            "WHERE iteration_id = ? GROUP BY outcome",
            (iteration_id,),
        )
        return {r["outcome"]: int(r["n"]) for r in rows}

    # ------------------------------------------------------------------
    # Raw results
    # ------------------------------------------------------------------

    def insert_raw_result(
        self,
        *,
        query_id: int,
        iteration_id: int,
        source_type: str,
        provider: str,
        payload: Any,
        retention_days: int,
    ) -> int:
        enums.validate(provider, enums.PROVIDERS, "provider")
        enums.validate(source_type, enums.SOURCE_TYPES, "source_type")
        # Scrubbed on the way in, not on the way out. A payload can echo an
        # auth header or carry vendor session material (Priceline's checkoutUrl
        # embeds a booking refCode), and once it is in the database it will be
        # read back into logs, API responses and assistant context.
        return self._insert(
            "INSERT INTO raw_results "
            "(query_id, iteration_id, source_type, provider, payload_json, "
            " retrieved_at, purge_after) VALUES (?,?,?,?,?,?,?)",
            (query_id, iteration_id, source_type, provider,
             # Two independent passes, and neither replaces the other:
             # governance drops fields we have no business KEEPING (Priceline
             # booking capability), redact removes SECRETS by pattern.
             json.dumps(redact_payload(
                 governance.strip_for_storage(provider, payload))), iso(),
             iso(utcnow() + timedelta(days=retention_days))),
        )

    def get_raw_result(self, raw_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM raw_results WHERE raw_id = ?", (raw_id,))

    def triaged_urls(self, iteration_id: int) -> set[tuple[str | None, str]]:
        """(stream, url) pairs already ruled on in this iteration.

        Resume correctness depends on this rather than on which raw_results rows
        have decisions. Posts are deduplicated by (stream, URL) across
        payloads, so when the same article arrives from three queries only one
        of those payloads receives that stream's decision row — the others
        would look untriaged forever and be re-judged on every resume. Keyed
        per STREAM because the same URL is legitimately judged once under each
        stream's criteria; a plain URL set would let one stream's judgement
        silently satisfy another's.
        """
        return {
            (row["stream"], row["url"]) for row in self.all(
                "SELECT DISTINCT stream, url FROM triage_decisions "
                "WHERE iteration_id = ? AND url IS NOT NULL",
                (iteration_id,),
            )
        }

    def uncovered_triage_decisions(self, iteration_id: int) -> list[sqlite3.Row]:
        """Posts an iteration requested a judgement on and never got one (8.8).

        The candidate set for a re-triage, and the reason it is built from
        `triage_decisions` rather than from `raw_results`:

        * **`TRIAGE_UNCOVERED` is exactly the retryable set.** ACCEPTED and
          REJECTED are completed judgements — a rejection is a real analytical
          conclusion, not a failure — and must never be retried.
        * **A post dropped by `triage.max_post_age_hours` has no row here at
          all**, so it cannot appear. The requirement that stale posts are not
          re-judged is satisfied by construction rather than by a second filter
          that could drift from the first.
        * **Re-gathering would silently process a different set.** The staleness
          cutoff is `utcnow() - max_post_age_hours`, evaluated when the posts
          were gathered. A retry an hour later re-derives it against a later
          `now`, so posts that were inside the window during the failed call
          fall outside it now — and the operator would have no way to see that
          the retry had quietly covered less than it was asked to.

        `raw_id` is a LEFT JOIN because retention may have purged the payload
        (`ON DELETE SET NULL`). A row whose evidence is gone cannot be re-judged
        and the caller reports it rather than passing an empty body to a model.
        """
        placeholders = ",".join("?" * len(enums.TRIAGE_UNCOVERED))
        return self.all(
            f"SELECT t.triage_id, t.url, t.raw_id, t.state, t.stream, "
            f"       t.fault_detail, r.payload_json "
            f"FROM triage_decisions t "
            f"LEFT JOIN raw_results r ON r.raw_id = t.raw_id "
            f"WHERE t.iteration_id = ? AND t.state IN ({placeholders}) "
            f"ORDER BY t.triage_id",
            (iteration_id, *sorted(enums.TRIAGE_UNCOVERED)),
        )

    def collected_raw_results(
        self, iteration_id: int, source_type: str = "SOCIAL"
    ) -> list[sqlite3.Row]:
        """Every collected payload of this type, decided or not.

        Was `untriaged_raw_results`, which excluded a payload as soon as ANY
        decision referenced it. Resume is per POST, not per payload: one
        response carries many posts, and a crash after the first batch left the
        rest of that response permanently unjudged while the stage reported
        itself re-entrant. Filtering happens per URL in `TriageAgent._gather`,
        against `triaged_urls` and `recorded_skips`, because that is the
        granularity at which a judgement exists.
        """
        return self.all(
            # The query's stream rides along: a payload's stream is a property
            # of the query that fetched it, and `raw_results` deliberately
            # does not duplicate it. LEFT JOIN so a legacy row whose query is
            # gone still gathers (stream NULL = the implicit stream).
            "SELECT r.*, q.stream AS stream FROM raw_results r "
            "LEFT JOIN query_queue q ON q.query_id = r.query_id "
            "WHERE r.iteration_id = ? AND r.source_type = ? ORDER BY r.raw_id",
            (iteration_id, source_type),
        )

    def recorded_skips(
        self, iteration_id: int
    ) -> tuple[set[tuple[int | None, str]], set[tuple[str | None, str]]]:
        """What `_gather` has already written off, so a resume is idempotent.

        Two shapes, because the reasons have two shapes. A structural refusal
        is a fact about one payload and there may be several of them in it, so
        it is keyed `(raw_id, reason)` — a rescan of the same bytes produces
        the same count, so recording the pair once is enough. STALE is a fact
        about one post under one stream's gathering, keyed (stream, url).

        Without this, resuming a stage re-recorded every refusal and the
        coverage report double-counted work that was already on the record.
        """
        pairs: set[tuple[int | None, str]] = set()
        stale: set[tuple[str | None, str]] = set()
        for row in self.all(
            "SELECT raw_id, reason, url, stream FROM triage_skips "
            "WHERE iteration_id = ?",
            (iteration_id,),
        ):
            if row["reason"] == "STALE":
                if row["url"]:
                    stale.add((row["stream"], str(row["url"])))
            else:
                pairs.add((row["raw_id"], str(row["reason"])))
        return pairs, stale

    def purge_expired_raw(self, now: datetime | None = None) -> int:
        """Delete raw payloads past their retention deadline.

        FR24's licence requires deletion 30 days after first receipt. Signals
        derived from a purged payload survive with a dangling raw_id, which is
        deliberate: the analytical record must outlive the licensed raw data.
        """
        return self._exec(
            "DELETE FROM raw_results WHERE purge_after <= ?",
            (iso(now) if now else iso(),),
        )

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    _SIGNAL_FIELDS = (
        "url", "author", "platform", "source_domain", "snippet", "salience",
        "activity_type", "imminence_hours",
        "publisher_key", "publisher_method", "claim_key", "location_method",
        "fr24_id", "callsign", "registration", "aircraft_type", "origin_iata",
        "dest_iata", "operating_as", "flight_category", "category_confidence",
        "flight_status", "eta",
        "provider_ref", "item_name", "near_available", "near_total",
        "base_available", "base_total", "drop_pct", "price_near",
        "price_baseline", "discount_pct_near", "discount_pct_base",
        "distance_km", "truncated",
        "vehicle_class", "vehicle_class_name", "people_capacity",
        "bag_capacity", "partner_code", "partner_name", "counter_type",
        "is_on_airport", "is_peer_to_peer", "field_map_ver",
    )

    def insert_signal(
        self,
        *,
        iteration_id: int,
        signal_type: str,
        raw_id: int | None = None,
        city_id: int | None = None,
        location_id: int | None = None,
        stream: str | None = None,
        track: str = "UNKNOWN",
        observed_at: datetime | str | None = None,
        quality: float = 0.0,
        signal_state: str = "CONFIRMED",
        state_reason: str | None = None,
        collection_class: str = "UNRECORDED",
        collection_basis: str | None = None,
        **fields: Any,
    ) -> int:
        enums.validate(signal_type, enums.SIGNAL_TYPES, "signal_type")
        self._mission("track").attribution(track)
        if stream is not None:
            self._mission("stream").stream_id(stream)
        enums.validate(signal_state, enums.SIGNAL_STATES, "signal_state")
        # 9.4. Defaults to UNRECORDED rather than to a plausible value: a
        # writer that forgets should produce a visible absence, not a claim
        # about how the record was obtained.
        enums.validate(collection_class, enums.COLLECTION_CLASSES,
                       "collection_class")
        enums.validate_optional(
            fields.get("flight_category"), enums.FLIGHT_CATEGORIES,
            "flight_category",
        )
        enums.validate_optional(
            fields.get("category_confidence"), enums.CATEGORY_CONFIDENCE,
            "category_confidence",
        )
        enums.validate_optional(
            fields.get("flight_status"), enums.FLIGHT_STATUSES, "flight_status"
        )
        unknown = set(fields) - set(self._SIGNAL_FIELDS)
        if unknown:
            raise ValueError(f"Unknown signal field(s): {sorted(unknown)}")

        quality = max(0.0, min(1.0, float(quality)))
        # 9.12. Canonicalised, never stored in the vendor's dialect.
        #
        # `recent_signals_for_city` compares `observed_at` as a STRING, which
        # is only sound if every value has one spelling. API Direct returns
        # "2026-08-12 12:08:39" with a space; ' ' is 0x20 and 'T' is 0x54, so a
        # space-separated stamp sorts BEFORE any 'T' stamp on the same date —
        # and the window threshold is written by `iso()`, which uses 'T'.
        #
        # Found in live iteration 14: two Atlanta social signals 158 hours old,
        # inside a 168-hour window, were dropped by that comparison. Both
        # correlations then scored with no social contribution at all and the
        # alert rested entirely on flight and car background. Silent evidence
        # loss, which is the failure this system exists to prevent.
        #
        # An unparseable value is stored as it arrived rather than discarded:
        # `in_window` will exclude it anyway, and a timestamp nobody can read
        # is still a record that one was supplied.
        if isinstance(observed_at, datetime):
            observed_at = iso(observed_at)
        elif isinstance(observed_at, str):
            parsed = parse_iso(observed_at)
            if parsed is not None:
                observed_at = iso(parsed)

        cols = ["iteration_id", "raw_id", "signal_type", "city_id",
                "location_id", "stream", "track", "observed_at", "quality",
                "signal_state", "state_reason",
                "collection_class", "collection_basis"]
        vals: list[Any] = [iteration_id, raw_id, signal_type, city_id,
                           location_id, stream, track, observed_at, quality,
                           signal_state, state_reason,
                           collection_class, collection_basis]
        for name in self._SIGNAL_FIELDS:
            if name in fields:
                cols.append(name)
                value = fields[name]
                if isinstance(value, bool):
                    value = int(value)
                vals.append(value)

        placeholders = ",".join("?" * len(cols))
        return self._insert(
            f"INSERT INTO signals ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )

    def update_signal_category(
        self, signal_id: int, category: str, confidence: str
    ) -> None:
        """Upgrade a flight signal's category after corroboration.

        Live-position records arrive AMBIGUOUS because that response carries no
        category field. flight-summary is the only endpoint that returns one, so
        this is how an AMBIGUOUS record becomes CONFIRMED — and the only way a
        military-weighted score is ever justified.
        """
        enums.validate(category, enums.FLIGHT_CATEGORIES, "flight_category")
        enums.validate(confidence, enums.CATEGORY_CONFIDENCE, "category_confidence")
        self._exec(
            "UPDATE signals SET flight_category = ?, category_confidence = ? "
            "WHERE signal_id = ?",
            (category, confidence, signal_id),
        )

    def signals_for_city(
        self, iteration_id: int, city_id: int
    ) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM signals WHERE iteration_id = ? AND city_id = ? "
            "ORDER BY signal_id",
            (iteration_id, city_id),
        )

    def signals_by_type(
        self, iteration_id: int, signal_type: str
    ) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM signals WHERE iteration_id = ? AND signal_type = ? "
            "ORDER BY signal_id",
            (iteration_id, signal_type),
        )

    def recent_signals_for_city(
        self, session_id: int, city_id: int, since: datetime
    ) -> list[sqlite3.Row]:
        """Signals for a city across iterations, within the correlation window.

        Joined through iterations so the window can span the previous run — a
        flight observed 30 minutes before this iteration started is still live
        evidence.
        """
        return self.all(
            "SELECT s.* FROM signals s "
            "JOIN iterations i USING (iteration_id) "
            "WHERE i.session_id = ? AND s.city_id = ? "
            "  AND s.observed_at IS NOT NULL AND s.observed_at >= ? "
            "ORDER BY s.signal_id",
            (session_id, city_id, iso(since)),
        )

    # ------------------------------------------------------------------
    # The per-session lock (8.6)
    # ------------------------------------------------------------------

    def try_claim_session(
        self, session_id: int, iteration_id: int, epoch_id: int | None = None
    ) -> bool:
        """Take the session's run slot. True if acquired, False if held.

        A conditional UPDATE, so acquisition is atomic inside SQLite rather
        than a read-then-write race between two workers. This is what lets the
        "one iteration per session" guarantee survive more than one process —
        the in-process `threading.Lock` it replaces was only ever true within a
        single interpreter, which is why `run.py serve` pinned `workers=1`.

        Re-claiming a slot this same iteration already holds succeeds, so a
        resume does not deadlock against its own lock.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE sessions SET running_iteration_id = ?, "
                "running_epoch_id = ? WHERE session_id = ? "
                "AND (running_iteration_id IS NULL OR running_iteration_id = ?)",
                (iteration_id, epoch_id, session_id, iteration_id),
            )
            self.conn.commit()
            return bool(cur.rowcount)

    def release_session(self, session_id: int, iteration_id: int) -> None:
        """Give the slot back, but only if this iteration still holds it.

        Conditional on ownership: a late release from an iteration whose lock
        was already reclaimed as stale must not clear the slot out from under
        whoever holds it now.
        """
        self._exec(
            "UPDATE sessions SET running_iteration_id = NULL, "
            "running_epoch_id = NULL WHERE session_id = ? "
            "AND running_iteration_id = ?",
            (session_id, iteration_id),
        )

    def session_lock(self, session_id: int) -> tuple[int | None, int | None]:
        """(iteration_id, epoch_id) holding this session, or (None, None)."""
        row = self.one(
            "SELECT running_iteration_id, running_epoch_id FROM sessions "
            "WHERE session_id = ?", (session_id,))
        if row is None:
            return (None, None)
        return (row["running_iteration_id"], row["running_epoch_id"])

    def clear_stale_session_locks(self, current_epoch_id: int) -> list[int]:
        """Release slots held by a process that is no longer running.

        Called from the startup reconcile. Without it a crash wedges the session
        permanently and the operator's only recovery is editing the database by
        hand — the lock would outlive the process holding it, which is the
        classic failure of moving a lock into storage.

        A lock is stale when its epoch is not the current one AND that epoch has
        been CLOSED. An epoch still open is either this process or a genuinely
        live one; reconcile closes dead predecessors before this runs, so
        "closed" is the right predicate rather than a timeout.

        Closed means `shutdown_kind IS NOT NULL`, deliberately NOT
        `ended_at IS NOT NULL`. A crashed predecessor is closed as UNKNOWN with
        `ended_at` left NULL on purpose — we do not know when it died and a
        made-up timestamp would later read as fact. Keying on `ended_at` would
        therefore match every case except the crash, which is the only case
        this exists for.
        """
        rows = self.all(
            "SELECT s.session_id FROM sessions s "
            "JOIN process_epochs e ON e.epoch_id = s.running_epoch_id "
            "WHERE s.running_iteration_id IS NOT NULL "
            "AND s.running_epoch_id != ? AND e.shutdown_kind IS NOT NULL",
            (current_epoch_id,),
        )
        freed = [int(r["session_id"]) for r in rows]
        for session_id in freed:
            self._exec(
                "UPDATE sessions SET running_iteration_id = NULL, "
                "running_epoch_id = NULL WHERE session_id = ?", (session_id,))
        return freed

    # ------------------------------------------------------------------
    # Idempotency, cancellation, review (8.2)
    # ------------------------------------------------------------------

    def find_idempotency_key(
        self, session_id: int, key: str
    ) -> sqlite3.Row | None:
        """A prior, unexpired response for this key, or None.

        Expired rows are treated as absent rather than deleted here: a read
        path that mutates is a surprise, and the purge runs on its own.
        """
        return self.one(
            "SELECT * FROM idempotency_keys WHERE session_id = ? "
            "AND idempotency_key = ? AND expires_at > ?",
            (session_id, key, iso()),
        )

    def record_idempotency_key(
        self, *, session_id: int, key: str, request_hash: str,
        iteration_id: int | None, status_code: int, response: Any,
        ttl_hours: float,
    ) -> int:
        """Remember what this key answered, so a retry can replay it."""
        expires = iso(utcnow() + timedelta(hours=float(ttl_hours)))
        return self._insert(
            "INSERT OR REPLACE INTO idempotency_keys "
            "(session_id, idempotency_key, request_hash, iteration_id, "
            " status_code, response_json, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (session_id, key, request_hash, iteration_id, status_code,
             json.dumps(response, default=str), iso(), expires),
        )

    def purge_expired_idempotency_keys(self) -> int:
        """Drop keys past their TTL. Returns how many went."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM idempotency_keys WHERE expires_at <= ?", (iso(),))
            self.conn.commit()
            return int(cur.rowcount or 0)

    def request_cancel(
        self, iteration_id: int, *, requested_by: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Ask an iteration to stop at its next stage boundary.

        A request, not a kill. The orchestrator honours it between stages and
        then finalises, because an iteration that has already bought FR24
        records must still correlate and alert on them — otherwise the money is
        spent and the evidence discarded.
        """
        self._exec(
            "UPDATE iterations SET cancel_requested_at = ?, "
            "cancel_requested_by = ?, cancel_reason = ? WHERE iteration_id = ?",
            (iso(), requested_by, (reason or "")[:500] or None, iteration_id),
        )

    def record_skipped_stages(self, iteration_id: int, stages: list[str]) -> None:
        """Record stages that did not run, so correlation can count the gap."""
        self._exec(
            "UPDATE iterations SET skipped_stages_json = ? "
            "WHERE iteration_id = ?",
            (json.dumps(sorted(set(stages))), iteration_id),
        )

    def skipped_stages(self, iteration_id: int) -> list[str]:
        """Stages this iteration never ran. Empty for an ordinary run."""
        row = self.one(
            "SELECT skipped_stages_json FROM iterations WHERE iteration_id = ?",
            (iteration_id,),
        )
        if row is None or not row["skipped_stages_json"]:
            return []
        try:
            value = json.loads(row["skipped_stages_json"])
        except (TypeError, ValueError):
            return []
        return [str(v) for v in value] if isinstance(value, list) else []

    def cancel_requested(self, iteration_id: int) -> bool:
        """Whether a stop has been asked for. Read at every stage boundary."""
        row = self.one(
            "SELECT cancel_requested_at FROM iterations WHERE iteration_id = ?",
            (iteration_id,),
        )
        return bool(row and row["cancel_requested_at"])

    def set_review_state(
        self, alert_id: int, state: str, *, reviewed_by: str | None = None,
        note: str | None = None,
    ) -> None:
        """Record a human decision about DISTRIBUTION.

        Nothing analytical moves: the score, the band and the evidence are
        untouched. Returning to UNREVIEWED clears the attribution rather than
        leaving a reviewer's name on a state they no longer hold.
        """
        enums.validate(state, enums.REVIEW_STATES, "review_state")
        if state == "UNREVIEWED":
            self._exec(
                "UPDATE alerts SET review_state = ?, reviewed_at = NULL, "
                "reviewed_by = NULL, review_note = NULL WHERE alert_id = ?",
                (state, alert_id),
            )
            return
        self._exec(
            "UPDATE alerts SET review_state = ?, reviewed_at = ?, "
            "reviewed_by = ?, review_note = ? WHERE alert_id = ?",
            (state, iso(), reviewed_by, (note or "")[:2000] or None, alert_id),
        )

    # ------------------------------------------------------------------
    # Classification receipts (8.1)
    # ------------------------------------------------------------------

    #: Columns `insert_receipt` accepts, in insert order. Kept as data so
    #: `services.receipts.Receipt.as_row()` and this writer cannot drift apart
    #: silently — a field added to the dataclass and not here raises.
    _RECEIPT_COLUMNS = (
        "iteration_id", "kind", "provider", "model_requested", "model_served",
        "response_id", "system_fingerprint", "tokens_in", "tokens_out",
        "attempts", "temperature", "max_tokens", "prompt_version",
        "prompt_hash", "prompt_user_hash",
        "schema_version", "rules_version", "normaliser_version",
        "code_revision", "package_version", "config_hash", "batch_key",
        "input_hash", "mission_id", "mission_hash",
    )

    def insert_receipt(self, iteration_id: int | None, row: Mapping[str, Any]) -> int:
        """Record how one model call was made. Returns the receipt_id.

        `row` is `services.receipts.Receipt.as_row()`. An unknown key is an
        error rather than a silent drop: a provenance field that goes missing
        without complaint is worse than no provenance at all, because the
        record still looks complete.
        """
        enums.validate(str(row.get("kind")), enums.RECEIPT_KINDS, "kind")
        unknown = set(row) - set(self._RECEIPT_COLUMNS) - {"iteration_id"}
        if unknown:
            raise ValueError(f"unknown receipt field(s): {sorted(unknown)}")
        values = {**row, "iteration_id": iteration_id}
        placeholders = ",".join("?" * len(self._RECEIPT_COLUMNS))
        return self._insert(
            f"INSERT INTO receipts ({','.join(self._RECEIPT_COLUMNS)}, "
            f"created_at) VALUES ({placeholders},?)",
            tuple(values.get(c) for c in self._RECEIPT_COLUMNS) + (iso(),),
        )

    def get_receipt(self, receipt_id: int | None) -> sqlite3.Row | None:
        """One receipt, or None. None is ordinary — see the column comment."""
        if receipt_id is None:
            return None
        return self.one("SELECT * FROM receipts WHERE receipt_id = ?",
                        (receipt_id,))

    def receipt_for_signal(self, signal_id: int | None) -> sqlite3.Row | None:
        """The receipt behind the judgement that created a signal.

        Only SOCIAL signals have one — the rest are derived deterministically
        from vendor records and no model was involved, which is itself the
        answer a reader of the evidence trail needs.
        """
        if signal_id is None:
            return None
        return self.one(
            "SELECT r.* FROM receipts r "
            "JOIN triage_decisions t ON t.receipt_id = r.receipt_id "
            "WHERE t.signal_id = ?",
            (signal_id,),
        )

    def insert_triage_decision(
        self,
        *,
        iteration_id: int,
        raw_id: int,
        state: str,
        rationale: str,
        model: str,
        url: str | None = None,
        track: str | None = None,
        cities: list[str] | None = None,
        locations: list[str] | None = None,
        salience: float | None = None,
        imminence_hours: float | None = None,
        signal_id: int | None = None,
        fault_detail: str | None = None,
        schema_version: str | None = None,
        receipt_id: int | None = None,
        stream: str | None = None,
    ) -> int:
        """Record one judgement, or one failure to obtain a judgement.

        `relevant` is derived rather than passed. It cannot express the
        distinction that matters — a considered rejection versus an answer that
        never arrived — and deriving it here means no caller can set the two
        columns inconsistently.
        """
        enums.validate(state, enums.TRIAGE_STATES, "state")
        if track is not None:
            self._mission("track").attribution(track)
        if stream is not None:
            self._mission("stream").stream_id(stream)
        return self._insert(
            "INSERT INTO triage_decisions "
            "(iteration_id, raw_id, url, relevant, state, fault_detail, "
            " stream, track, cities_json, locations_json, salience, "
            " imminence_hours, rationale, signal_id, receipt_id, model, "
            " schema_version, decided_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iteration_id, raw_id, url, int(state == "ACCEPTED"), state,
             fault_detail, stream, track,
             json.dumps(cities or []), json.dumps(locations or []),
             salience, imminence_hours, rationale, signal_id, receipt_id,
             model, schema_version, iso()),
        )

    def insert_triage_skip(
        self,
        *,
        iteration_id: int,
        reason: str,
        raw_id: int | None = None,
        url: str | None = None,
        observed_at: str | None = None,
        cutoff_at: str | None = None,
        max_post_age_hours: float | None = None,
        detail: str | None = None,
        items_lost: int | None = None,
        stream: str | None = None,
    ) -> int:
        """Record a collected post that never reached the model (8.9)."""
        enums.validate(reason, enums.TRIAGE_SKIP_REASONS, "reason")
        if stream is not None:
            self._mission("stream").stream_id(stream)
        return self._insert(
            "INSERT INTO triage_skips "
            "(iteration_id, raw_id, url, reason, stream, observed_at, "
            " cutoff_at, max_post_age_hours, detail, items_lost, skipped_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (iteration_id, raw_id, url, reason, stream, observed_at,
             cutoff_at, max_post_age_hours,
             (detail or None) and detail[:2000], items_lost, iso()),
        )

    def triage_skip_counts(self, iteration_id: int) -> dict[str, int]:
        """Skips by reason. Empty when nothing was dropped before the model."""
        return {
            row["reason"]: int(row["n"]) for row in self.all(
                "SELECT reason, COUNT(*) AS n FROM triage_skips "
                "WHERE iteration_id = ? GROUP BY reason ORDER BY reason",
                (iteration_id,),
            )
        }

    def triage_skips(
        self, iteration_id: int, reason: str | None = None
    ) -> list[sqlite3.Row]:
        """The skip rows themselves, optionally one reason."""
        sql = ["SELECT * FROM triage_skips WHERE iteration_id = ?"]
        params: list[Any] = [iteration_id]
        if reason is not None:
            enums.validate(reason, enums.TRIAGE_SKIP_REASONS, "reason")
            sql.append("AND reason = ?")
            params.append(reason)
        return self.all(" ".join(sql) + " ORDER BY skip_id", params)

    def triage_state_counts(self, iteration_id: int) -> dict[str, int]:
        """Judgements by state, for the iteration report and the API."""
        rows = self.all(
            "SELECT state, COUNT(*) AS n FROM triage_decisions "
            "WHERE iteration_id = ? GROUP BY state",
            (iteration_id,),
        )
        return {row["state"] or "UNKNOWN": int(row["n"]) for row in rows}

    def triage_uncovered(self, iteration_id: int) -> int:
        """Posts that were never actually judged.

        The input to the SOCIAL coverage gap. An unjudged post has no city — the
        model is what would have told us — so the gap cannot be attributed to
        one, and the honest reading is that every city in the iteration is
        affected.
        """
        placeholders = ",".join("?" * len(enums.TRIAGE_UNCOVERED))
        return int(self.scalar(
            f"SELECT COUNT(*) FROM triage_decisions WHERE iteration_id = ? "
            f"AND state IN ({placeholders})",
            (iteration_id, *sorted(enums.TRIAGE_UNCOVERED)),
        ))

    def triage_uncovered_by_stream(
        self, iteration_id: int
    ) -> dict[str | None, int]:
        """Unjudged posts counted per stream, for per-family gap attribution.

        A stream occupies a banding family, so a gap in one stream's judgement
        must reach THAT family — gapping all of them would overstate the loss,
        and gapping none would hide it. `None` means the row predates streams
        or its stream was never recorded; the caller treats that as a gap for
        every social-derived family, conservatively.
        """
        placeholders = ",".join("?" * len(enums.TRIAGE_UNCOVERED))
        rows = self.all(
            f"SELECT stream, COUNT(*) AS n FROM triage_decisions "
            f"WHERE iteration_id = ? AND state IN ({placeholders}) "
            f"GROUP BY stream",
            (iteration_id, *sorted(enums.TRIAGE_UNCOVERED)),
        )
        return {row["stream"]: int(row["n"]) for row in rows}

    # ------------------------------------------------------------------
    # Correlations and alerts
    # ------------------------------------------------------------------

    def upsert_correlation(
        self,
        *,
        iteration_id: int,
        city_id: int,
        track: str,
        score: float,
        band: str,
        distinct_types: int,
        contributions: dict[str, float],
        data_completeness: float,
        failed_sources: Iterable[str] = (),
        failed_families: Iterable[str] | None = None,
        flight_baseline: Mapping[str, Any] | None = None,
        evidence_freshness: Mapping[str, Any] | None = None,
        band_capped: bool = False,
        rule_trace: str,
        alternatives: Sequence[Mapping[str, str]] | None = None,
        calendar_matches: Sequence[Mapping[str, Any]] | None = None,
        config_hash: str | None = None,
    ) -> int:
        self._mission("track").track(track)
        enums.validate(band, enums.BANDS, "band")
        with self._lock:
            self._exec(
                "INSERT INTO correlations "
                "(iteration_id, city_id, track, score, band, "
                " distinct_types, contributions_json, data_completeness, "
                " failed_sources, failed_families, band_capped, rule_trace, "
                " alternatives_json, flight_baseline_json, "
                " evidence_freshness_json, calendar_matches_json, "
                " config_hash, computed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(iteration_id, city_id, track) DO UPDATE SET "
                "  score = excluded.score, band = excluded.band, "
                "  distinct_types = excluded.distinct_types, "
                "  contributions_json = excluded.contributions_json, "
                "  data_completeness = excluded.data_completeness, "
                "  failed_sources = excluded.failed_sources, "
                "  failed_families = excluded.failed_families, "
                "  band_capped = excluded.band_capped, "
                "  rule_trace = excluded.rule_trace, "
                "  alternatives_json = excluded.alternatives_json, "
                "  flight_baseline_json = excluded.flight_baseline_json, "
                "  evidence_freshness_json = excluded.evidence_freshness_json, "
                "  calendar_matches_json = excluded.calendar_matches_json, "
                "  config_hash = excluded.config_hash, "
                "  computed_at = excluded.computed_at",
                (iteration_id, city_id, track, round(float(score), 4),
                 band, distinct_types,
                 json.dumps(contributions, sort_keys=True),
                 round(float(data_completeness), 4),
                 ",".join(sorted(failed_sources)),
                 ",".join(sorted(failed_families if failed_families is not None
                                 else failed_sources)),
                 int(band_capped),
                 rule_trace,
                 None if alternatives is None
                 else json.dumps(list(alternatives)),
                 None if flight_baseline is None
                 else json.dumps(flight_baseline, sort_keys=True),
                 None if evidence_freshness is None
                 else json.dumps(evidence_freshness, sort_keys=True),
                 # NULL and [] read differently downstream: NULL is "no
                 # calendar on this session" (or a pre-v15 row), [] is "a
                 # calendar was consulted and nothing overlapped".
                 None if calendar_matches is None
                 else json.dumps(list(calendar_matches)),
                 config_hash,
                 iso()),
            )
            row = self.one(
                "SELECT correlation_id FROM correlations "
                "WHERE iteration_id = ? AND city_id = ? AND track = ?",
                (iteration_id, city_id, track),
            )
            return int(row["correlation_id"])

    def link_correlation_signal(
        self, correlation_id: int, signal_id: int, contribution: float
    ) -> None:
        self._exec(
            "INSERT INTO correlation_signals "
            "(correlation_id, signal_id, contribution) VALUES (?,?,?) "
            "ON CONFLICT(correlation_id, signal_id) DO UPDATE SET "
            "  contribution = excluded.contribution",
            (correlation_id, signal_id, round(float(contribution), 6)),
        )

    def get_correlation(self, correlation_id: int) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM correlations WHERE correlation_id = ?",
            (correlation_id,),
        )

    def get_correlations(self, iteration_id: int) -> list[sqlite3.Row]:
        return self.all(
            "SELECT * FROM correlations WHERE iteration_id = ? "
            "ORDER BY score DESC, correlation_id",
            (iteration_id,),
        )

    def set_alert_decision(
        self, correlation_id: int, decision: str, reason: str
    ) -> None:
        """Record what ALERTING decided about a correlation, and why (8.7b).

        Written by the agent that makes the call, at the moment it makes it.
        The alternative — leaving a reader to compare `score` against a config
        value they may not be holding — is how a deliberate decision becomes
        indistinguishable from an oversight.
        """
        enums.validate(decision, enums.ALERT_DECISIONS, "alert_decision")
        self._exec(
            "UPDATE correlations SET alert_decision = ?, "
            "alert_decision_reason = ? WHERE correlation_id = ?",
            (decision, reason[:2000], correlation_id),
        )

    def correlation_signals(self, correlation_id: int) -> list[sqlite3.Row]:
        return self.all(
            "SELECT s.*, cs.contribution FROM correlation_signals cs "
            "JOIN signals s USING (signal_id) "
            "WHERE cs.correlation_id = ? ORDER BY s.signal_type, s.signal_id",
            (correlation_id,),
        )

    def insert_alert(
        self,
        *,
        correlation_id: int,
        session_id: int,
        iteration_id: int,
        city_id: int,
        track: str,
        confidence_score: float,
        confidence_band: str,
        summary: str,
        model: str,
        caveat: str | None = None,
        earliest_eta: str | None = None,
        receipt_id: int | None = None,
    ) -> int:
        self._mission("track").track(track)
        enums.validate(confidence_band, enums.ALERT_BANDS, "confidence_band")
        return self._insert(
            "INSERT INTO alerts "
            "(correlation_id, session_id, iteration_id, city_id, track, "
            " confidence_score, confidence_band, summary, caveat, "
            " earliest_eta, receipt_id, model, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (correlation_id, session_id, iteration_id, city_id, track,
             round(float(confidence_score), 4), confidence_band, summary,
             caveat, earliest_eta, receipt_id, model, iso()),
        )

    def get_alert(self, alert_id: int) -> sqlite3.Row | None:
        """One alert with its city joined, matching get_alerts' row shape."""
        return self.one(
            "SELECT a.*, c.name AS city_name, c.state AS city_state "
            "FROM alerts a JOIN cities c USING (city_id) WHERE a.alert_id = ?",
            (alert_id,),
        )

    def get_alerts(
        self,
        session_id: int,
        *,
        since: datetime | None = None,
        min_band: str | None = None,
        city_id: int | None = None,
        track: str | None = None,
        iteration_id: int | None = None,
    ) -> list[sqlite3.Row]:
        sql = [
            "SELECT a.*, c.name AS city_name, c.state AS city_state "
            "FROM alerts a JOIN cities c USING (city_id) "
            "WHERE a.session_id = ?"
        ]
        params: list[Any] = [session_id]
        if since is not None:
            sql.append("AND a.created_at >= ?")
            params.append(iso(since))
        if city_id is not None:
            sql.append("AND a.city_id = ?")
            params.append(city_id)
        if track is not None:
            sql.append("AND a.track = ?")
            params.append(track)
        if iteration_id is not None:
            sql.append("AND a.iteration_id = ?")
            params.append(iteration_id)
        # Most severe first, then most recent. Ordering by recency alone put
        # whichever actor track happened to be scored second at the top, which
        # is arbitrary to a reader — anyone opening this list needs the
        # HIGH bands in front of the LOW ones.
        rows = self.all(
            " ".join(sql)
            + " ORDER BY CASE a.confidence_band"
              "   WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END DESC,"
              " a.created_at DESC, a.alert_id DESC",
            params,
        )
        if min_band:
            floor = enums.band_index(min_band)
            rows = [r for r in rows
                    if enums.band_index(r["confidence_band"]) >= floor]
        return rows

    # ------------------------------------------------------------------
    # Logging and quota
    # ------------------------------------------------------------------

    def log(
        self,
        agent: str,
        level: str,
        message: str,
        *,
        iteration_id: int | None = None,
        **extra: Any,
    ) -> int:
        enums.validate(level, enums.LOG_LEVELS, "level")
        clean = {k: v for k, v in extra.items() if v is not None}
        return self._insert(
            "INSERT INTO agent_log "
            "(iteration_id, agent, level, message, extra_json, logged_at) "
            "VALUES (?,?,?,?,?,?)",
            (iteration_id, agent, level, redact_text(message),
             json.dumps(redact_payload(clean), sort_keys=True, default=str)
             if clean else None,
             iso()),
        )

    def get_log(
        self, iteration_id: int, agent: str | None = None
    ) -> list[sqlite3.Row]:
        if agent:
            return self.all(
                "SELECT * FROM agent_log WHERE iteration_id = ? AND agent = ? "
                "ORDER BY log_id",
                (iteration_id, agent),
            )
        return self.all(
            "SELECT * FROM agent_log WHERE iteration_id = ? ORDER BY log_id",
            (iteration_id,),
        )

    def record_api_call(
        self,
        *,
        provider: str,
        endpoint: str,
        units: float,
        iteration_id: int | None = None,
        query_id: int | None = None,
        http_status: int | None = None,
        records_returned: int | None = None,
        latency_ms: int | None = None,
        error_message: str | None = None,
    ) -> int:
        enums.validate(provider, enums.PROVIDERS, "provider")
        return self._insert(
            "INSERT INTO api_calls "
            "(iteration_id, query_id, provider, endpoint, http_status, units, "
            " records_returned, latency_ms, called_at, error_message) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (iteration_id, query_id, provider, endpoint, http_status,
             float(units), records_returned, latency_ms, iso(),
             redact_text(error_message)),
        )

    def set_budget(
        self, provider: str, endpoint: str | None, period: str, limit_units: float
    ) -> None:
        enums.validate(provider, enums.PROVIDERS, "provider")
        enums.validate(period, enums.BUDGET_PERIODS, "period")
        # NULL endpoint means provider-wide. SQLite treats NULLs as distinct in
        # UNIQUE indexes, so the ON CONFLICT target cannot match a NULL row;
        # delete-then-insert keeps provider-wide budgets idempotent.
        with self._lock:
            if endpoint is None:
                self._exec(
                    "DELETE FROM api_budgets WHERE provider = ? "
                    "AND endpoint IS NULL AND period = ?",
                    (provider, period),
                )
                self._insert(
                    "INSERT INTO api_budgets "
                    "(provider, endpoint, period, limit_units) VALUES (?,?,?,?)",
                    (provider, None, period, float(limit_units)),
                )
            else:
                self._exec(
                    "INSERT INTO api_budgets "
                    "(provider, endpoint, period, limit_units) VALUES (?,?,?,?) "
                    "ON CONFLICT(provider, endpoint, period) DO UPDATE SET "
                    "  limit_units = excluded.limit_units",
                    (provider, endpoint, period, float(limit_units)),
                )

    def get_budgets(self, provider: str | None = None) -> list[sqlite3.Row]:
        if provider:
            return self.all(
                "SELECT * FROM api_budgets WHERE provider = ? "
                "ORDER BY provider, endpoint, period",
                (provider,),
            )
        return self.all(
            "SELECT * FROM api_budgets ORDER BY provider, endpoint, period"
        )

    def units_used(
        self,
        provider: str,
        endpoint: str | None = None,
        *,
        since: datetime | None = None,
        iteration_id: int | None = None,
    ) -> float:
        sql = ["SELECT COALESCE(SUM(units), 0) FROM api_calls WHERE provider = ?"]
        params: list[Any] = [provider]
        if endpoint is not None:
            sql.append("AND endpoint = ?")
            params.append(endpoint)
        if since is not None:
            sql.append("AND called_at >= ?")
            params.append(iso(since))
        if iteration_id is not None:
            sql.append("AND iteration_id = ?")
            params.append(iteration_id)
        return float(self.scalar(" ".join(sql), params, default=0.0))
