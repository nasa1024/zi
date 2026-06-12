"""Migration v12 → v13: 章节卡细纲契约列 + style_anchors 表（P1#7/#9）。

chapter_cards 历史上同样只在 schema.sql 基线里（无迁移创建过），
v4 老库可能整表缺失——缺表按 v13 形态整建，存在则补列。
"""
from __future__ import annotations
import sqlite3
from . import register, column_exists, table_exists

_CHAPTER_CARDS_V13 = """
CREATE TABLE IF NOT EXISTS chapter_cards (
    id              TEXT PRIMARY KEY,
    chapter         INTEGER NOT NULL UNIQUE,
    title           TEXT,
    pov_entity_id   TEXT,
    goal            TEXT,
    summary         TEXT,
    word_count      INTEGER,
    hook_text       TEXT,
    status          TEXT NOT NULL DEFAULT 'planned'
                        CHECK(status IN ('planned','drafted','reviewed','committed')),
    draft_id        TEXT,
    target_emotion    TEXT,
    opening_hook_type TEXT,
    hook_type         TEXT,
    expectation_score INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(pov_entity_id) REFERENCES entities(id)
);
"""

_STYLE_ANCHORS = """
CREATE TABLE IF NOT EXISTS style_anchors (
    id          TEXT PRIMARY KEY,
    emotion     TEXT NOT NULL,
    title       TEXT,
    content     TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_style_anchors_emotion ON style_anchors(emotion);
"""


@register("13")
def migrate_v13(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "chapter_cards"):
        conn.executescript(_CHAPTER_CARDS_V13)
    else:
        for col, decl in (
            ("target_emotion", "TEXT"),
            ("opening_hook_type", "TEXT"),
            ("hook_type", "TEXT"),
            ("expectation_score", "INTEGER"),
        ):
            if not column_exists(conn, "chapter_cards", col):
                conn.execute(f"ALTER TABLE chapter_cards ADD COLUMN {col} {decl}")
    conn.executescript(_STYLE_ANCHORS)
