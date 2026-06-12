"""Migration v8 → v9: chapter_summaries 表 + volumes.rolling_summary（M2-② 分层叙事摘要）。"""
from __future__ import annotations
import sqlite3
from . import register, column_exists


@register("9")
def migrate_v9(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chapter_summaries (
            id          TEXT PRIMARY KEY,
            chapter     INTEGER NOT NULL UNIQUE,
            summary     TEXT NOT NULL,
            volume_no   INTEGER,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chapter_summaries_chapter"
        " ON chapter_summaries(chapter)"
    )
    if not column_exists(conn, "volumes", "rolling_summary"):
        conn.execute("ALTER TABLE volumes ADD COLUMN rolling_summary TEXT")
