"""Migration v21 -> v22: reader-experience contracts and multi-chapter batches.

One shared migration for the six v22 features so their schema never diverges:

- F1 promise ledger: ``foreshadow`` gains promise kind, long/short line, reader
  visibility, nesting parent and reminder cadence.
- F3 polarity: ``aftermath`` beat type (table rebuild of ``beats`` and
  ``chapter_plan_version_beats`` because SQLite cannot alter a CHECK), per
  chapter polarity/turning point in ``pacing_state`` and streak counters in
  ``pacing_cursor``.
- F5 payoff reactions: ``witnesses_json`` on both beat tables.
- F6 batch drafting: ``draft_batches`` / ``draft_batch_chapters`` and
  ``pipeline_run.batch_id``.

Every step is guarded so it is idempotent and tolerates databases that were
migrated from very old versions (some craft tables never existed there).
"""
from __future__ import annotations

import sqlite3

from . import column_exists, register, table_exists


_BEAT_TYPE_CHECK = (
    "CHECK(beat_type IN ('setup','turn','payoff_beat','tension_point',"
    "'aftermath','hook'))"
)

_BEATS_V22 = f"""
CREATE TABLE beats__v22 (
    id              TEXT PRIMARY KEY,
    chapter         INTEGER NOT NULL,
    seq             INTEGER NOT NULL,
    beat_type       TEXT NOT NULL {_BEAT_TYPE_CHECK},
    summary         TEXT NOT NULL,
    value_start     TEXT,
    value_end       TEXT,
    value_axis      TEXT,
    arc_id          TEXT,
    source_decision_id TEXT,
    constraint_level TEXT NOT NULL DEFAULT 'soft'
                        CHECK(constraint_level IN ('soft','hard')),
    tension_level   INTEGER,
    related_foreshadow_id TEXT,
    status          TEXT NOT NULL DEFAULT 'planned'
                        CHECK(status IN ('planned','drafted','satisfied','dropped')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    witnesses_json  TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(witnesses_json)),
    UNIQUE(chapter, seq),
    FOREIGN KEY(related_foreshadow_id) REFERENCES foreshadow(id)
)
"""

_VERSION_BEATS_V22 = f"""
CREATE TABLE chapter_plan_version_beats__v22 (
    id               TEXT PRIMARY KEY,
    plan_version_id  TEXT NOT NULL REFERENCES chapter_plan_versions(id),
    seq              INTEGER NOT NULL CHECK(seq >= 1),
    beat_type        TEXT NOT NULL {_BEAT_TYPE_CHECK},
    summary          TEXT NOT NULL,
    value_axis       TEXT,
    arc_id           TEXT,
    constraint_level TEXT NOT NULL DEFAULT 'soft'
                         CHECK(constraint_level IN ('soft','hard')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    witnesses_json   TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(witnesses_json)),
    UNIQUE(plan_version_id, seq)
)
"""

_BEAT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_beats_chapter ON beats(chapter, seq)",
    "CREATE INDEX IF NOT EXISTS idx_beats_type ON beats(beat_type)",
)
_VERSION_BEAT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_chapter_plan_version_beats_version"
    " ON chapter_plan_version_beats(plan_version_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_chapter_plan_version_beats_arc"
    " ON chapter_plan_version_beats(arc_id)",
)

_FORESHADOW_COLUMNS = (
    ("promise_kind", "TEXT"),
    ("line_type", "TEXT NOT NULL DEFAULT 'long' CHECK(line_type IN ('long','short','micro'))"),
    ("visibility", "TEXT NOT NULL DEFAULT 'reader' CHECK(visibility IN ('reader','hidden'))"),
    ("parent_id", "TEXT REFERENCES foreshadow(id)"),
    ("remind_every", "INTEGER CHECK(remind_every IS NULL OR remind_every >= 1)"),
)
_PACING_STATE_COLUMNS = (
    ("polarity", "TEXT CHECK(polarity IS NULL OR polarity IN ('+','-','±'))"),
    ("turning_point",
     "TEXT CHECK(turning_point IS NULL OR turning_point IN ('action','revelation'))"),
)
_PACING_CURSOR_COLUMNS = (
    ("low_streak", "INTEGER NOT NULL DEFAULT 0"),
    ("polarity_streak", "INTEGER NOT NULL DEFAULT 0"),
    ("last_polarity", "TEXT"),
)

_BATCH_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS draft_batches (
        id              TEXT PRIMARY KEY,
        start_chapter   INTEGER NOT NULL CHECK(start_chapter >= 1),
        end_chapter     INTEGER NOT NULL,
        status          TEXT NOT NULL DEFAULT 'running'
                            CHECK(status IN ('running','completed','partial','failed','canceled')),
        origin          TEXT NOT NULL DEFAULT 'api',
        model_id        TEXT,
        tokens_spent    INTEGER NOT NULL DEFAULT 0,
        usd_spent       REAL    NOT NULL DEFAULT 0,
        detail_json     TEXT    NOT NULL DEFAULT '{}' CHECK(json_valid(detail_json)),
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        CHECK(end_chapter >= start_chapter)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_draft_batches_status ON draft_batches(status)",
    """
    CREATE TABLE IF NOT EXISTS draft_batch_chapters (
        id              TEXT PRIMARY KEY,
        batch_id        TEXT NOT NULL REFERENCES draft_batches(id),
        chapter         INTEGER NOT NULL,
        seq             INTEGER NOT NULL CHECK(seq >= 1),
        draft_text      TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','accepted','rejected','discarded')),
        run_id          TEXT,
        reason          TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(batch_id, chapter)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_draft_batch_chapters_chapter"
    " ON draft_batch_chapters(chapter, status)",
)


def _add_columns(
    conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...],
) -> None:
    if not table_exists(conn, table):
        return
    for name, decl in columns:
        if not column_exists(conn, table, name):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return (row[0] if row else "") or ""


def _rebuild_beat_table(
    conn: sqlite3.Connection,
    table: str,
    create_sql: str,
    indexes: tuple[str, ...],
) -> None:
    """Swap ``table`` for the v22 definition, keeping every existing row."""
    if not table_exists(conn, table):
        return
    if "aftermath" in _table_sql(conn, table) and column_exists(
        conn, table, "witnesses_json"
    ):
        return
    temp = f"{table}__v22"
    conn.execute(f"DROP TABLE IF EXISTS {temp}")
    conn.execute(create_sql)
    old_columns = [
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    ]
    new_columns = {
        row[1] for row in conn.execute(f"PRAGMA table_info({temp})").fetchall()
    }
    shared = [name for name in old_columns if name in new_columns]
    column_list = ", ".join(shared)
    conn.execute(
        f"INSERT INTO {temp} ({column_list}) SELECT {column_list} FROM {table}"
    )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {temp} RENAME TO {table}")
    for statement in indexes:
        conn.execute(statement)


@register("22")
def migrate_v22(conn: sqlite3.Connection) -> None:
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        # F1 promise ledger
        _add_columns(conn, "foreshadow", _FORESHADOW_COLUMNS)

        # F3 + F5: aftermath beat type and witnesses on both beat tables
        _rebuild_beat_table(conn, "beats", _BEATS_V22, _BEAT_INDEXES)
        _rebuild_beat_table(
            conn, "chapter_plan_version_beats", _VERSION_BEATS_V22,
            _VERSION_BEAT_INDEXES,
        )

        # F3 polarity streaks
        _add_columns(conn, "pacing_state", _PACING_STATE_COLUMNS)
        _add_columns(conn, "pacing_cursor", _PACING_CURSOR_COLUMNS)

        # F6 multi-chapter batches
        for statement in _BATCH_TABLES:
            conn.execute(statement)
        _add_columns(conn, "pipeline_run", (("batch_id", "TEXT"),))
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise
