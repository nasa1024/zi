"""Migration v7 → v8: autopilot_sessions 持久化表（M1-③ 会话断点恢复）。"""
from __future__ import annotations
import sqlite3
from . import register


@register("8")
def migrate_v8(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autopilot_sessions (
            session_id      TEXT PRIMARY KEY,
            project_id      TEXT NOT NULL,
            from_chapter    INTEGER NOT NULL,
            to_chapter      INTEGER NOT NULL,
            current_chapter INTEGER NOT NULL,
            status          TEXT NOT NULL,
            policy_mode     TEXT NOT NULL,
            chapters_done   INTEGER NOT NULL DEFAULT 0,
            budget_tokens_total INTEGER NOT NULL DEFAULT 0,
            budget_usd_total    REAL    NOT NULL DEFAULT 0.0,
            consecutive_hard_issues INTEGER NOT NULL DEFAULT 0,
            last_error      TEXT,
            req_json        TEXT NOT NULL DEFAULT '{}',
            resumed_from    TEXT,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            heartbeat_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_autopilot_sessions_status"
        " ON autopilot_sessions(project_id, status)"
    )
