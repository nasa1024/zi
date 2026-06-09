"""Migration v5 → v6: pipeline_run 状态机表（F6 崩溃幂等恢复）。"""
from __future__ import annotations
import sqlite3
from . import register


@register("6")
def migrate_v6(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_run (
            run_id          TEXT PRIMARY KEY,
            chapter         INTEGER NOT NULL,
            project_id      TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'running'
                                CHECK(status IN ('running','completed','crashed')),
            draft_id        TEXT,
            started_at      TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at     TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pipeline_run_chapter"
        " ON pipeline_run(project_id, chapter)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pipeline_run_status"
        " ON pipeline_run(status)"
    )
