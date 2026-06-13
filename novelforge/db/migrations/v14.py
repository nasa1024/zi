"""Migration v13 → v14: volumes 卷级 Objective + KR（P2#13）。

volumes 表由 v5 创建过（不像 chapter_cards/foreshadow 那样缺表），直接 ALTER；
仍按既定双路径写缺表兜底，防御未来 schema 重排。
"""
from __future__ import annotations
import sqlite3
from . import register, column_exists, table_exists

_VOLUMES_V14 = """
CREATE TABLE IF NOT EXISTS volumes (
    id              TEXT PRIMARY KEY,
    volume_no       INTEGER NOT NULL,
    title           TEXT NOT NULL,
    synopsis        TEXT,
    start_chapter   INTEGER,
    end_chapter     INTEGER,
    status          TEXT NOT NULL DEFAULT 'writing'
                        CHECK(status IN ('writing','completed','archived')),
    rolling_summary TEXT,
    objective       TEXT,
    key_results     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(volume_no)
);
"""


@register("14")
def migrate_v14(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "volumes"):
        conn.executescript(_VOLUMES_V14)
        return
    for col in ("objective", "key_results"):
        if not column_exists(conn, "volumes", col):
            conn.execute(f"ALTER TABLE volumes ADD COLUMN {col} TEXT")
