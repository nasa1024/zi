"""Migration v6 → v7: sessions/turns/turn_events（§13.2 会话/turn 模型）。"""
from __future__ import annotations
import sqlite3
from . import register


@register("7")
def migrate_v7(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id            TEXT PRIMARY KEY,
            client        TEXT NOT NULL
                             CHECK(client IN ('cli','web','chat','api')),
            mode          TEXT
                             CHECK(mode IN ('human_gate','auto_promote','hybrid')),
            actor         TEXT NOT NULL,
            started_at    TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at      TEXT,
            budget_spent_tokens INTEGER NOT NULL DEFAULT 0,
            budget_spent_usd    REAL    NOT NULL DEFAULT 0.0,
            summary       TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS turns (
            id            TEXT PRIMARY KEY,
            session_id    TEXT NOT NULL REFERENCES sessions(id),
            seq           INTEGER NOT NULL,
            kind          TEXT NOT NULL
                             CHECK(kind IN ('command','chat','long_task')),
            intent        TEXT,
            request_json  TEXT NOT NULL,
            routed_endpoint TEXT,
            status        TEXT NOT NULL DEFAULT 'running'
                             CHECK(status IN ('running','done','error','canceled')),
            stream        INTEGER NOT NULL DEFAULT 0,
            result_json   TEXT,
            started_at    TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at   TEXT,
            UNIQUE(session_id, seq)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, seq)"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS turn_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id       TEXT NOT NULL REFERENCES turns(id),
            event_type    TEXT NOT NULL,
            data_json     TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_turn_events_turn ON turn_events(turn_id, id)"
    )
