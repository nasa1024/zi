"""Migration v3 → v4: Add pacing_cursor table, beats.value_axis/arc_id, tool_call_log (§10 R12/B2/B4, §12.5)."""
from __future__ import annotations
import sqlite3
from . import register


@register("4")
def migrate_v4(conn: sqlite3.Connection) -> None:
    """Add pacing_cursor, beats.value_axis/arc_id, tool_call_log."""
    # beats: add value_axis and arc_id if missing
    beats_cols = [r[1] for r in conn.execute("PRAGMA table_info(beats)")]
    if "value_axis" not in beats_cols:
        conn.execute("ALTER TABLE beats ADD COLUMN value_axis TEXT")
    if "arc_id" not in beats_cols:
        conn.execute("ALTER TABLE beats ADD COLUMN arc_id TEXT")

    # pacing_cursor: new single-row accumulator table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pacing_cursor (
            id                        INTEGER PRIMARY KEY CHECK(id = 1),
            chapters_since_big_payoff INTEGER NOT NULL DEFAULT 0,
            kchars_since_small_payoff REAL    NOT NULL DEFAULT 0,
            buildup                   INTEGER NOT NULL DEFAULT 0,
            recent_high_streak        INTEGER NOT NULL DEFAULT 0,
            updated_at                TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # tool_call_log: append-only audit table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_call_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id         TEXT    NOT NULL,
            chapter        INTEGER NOT NULL,
            skill          TEXT    NOT NULL,
            step           INTEGER NOT NULL,
            tool_name      TEXT    NOT NULL,
            args_json      TEXT    NOT NULL,
            result_digest  TEXT    NOT NULL,
            latency_ms     INTEGER NOT NULL,
            provider       TEXT,
            model          TEXT,
            note           TEXT,
            ts             TEXT    NOT NULL DEFAULT (datetime('now')),
            CHECK (json_valid(args_json))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tcl_run  ON tool_call_log(run_id, step)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tcl_chap ON tool_call_log(chapter, tool_name)")
