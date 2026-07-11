"""Migration v17 -> v18: close the L0/L1/L2 memory lifecycle.

The original tables could store drafts, atoms, and scenes, but they could not
distinguish "not processed" from "processed with no result" and L1 atoms lacked
machine-readable evidence metadata.  v18 adds replay/health fields and
conservatively marks legacy atoms without verifiable source lines as unanchored.
"""
from __future__ import annotations

import sqlite3

from . import column_exists, register


_MEMORY_BASELINE = """
CREATE TABLE IF NOT EXISTS draft_index (
    id TEXT PRIMARY KEY,
    chapter INTEGER NOT NULL,
    revision_round INTEGER NOT NULL DEFAULT 0,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    word_count INTEGER,
    status TEXT NOT NULL DEFAULT 'draft',
    volume_no INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chapter, revision_round)
);
CREATE INDEX IF NOT EXISTS idx_draft_chapter ON draft_index(chapter, revision_round);

CREATE TABLE IF NOT EXISTS l1_atoms (
    id TEXT PRIMARY KEY,
    chapter INTEGER NOT NULL,
    draft_id TEXT,
    atom_text TEXT NOT NULL,
    anchor TEXT,
    extracted_by TEXT,
    candidate_id TEXT,
    cold_start INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(draft_id) REFERENCES draft_index(id)
);
CREATE INDEX IF NOT EXISTS idx_l1_chapter ON l1_atoms(chapter);

CREATE TABLE IF NOT EXISTS l2_scenes (
    id TEXT PRIMARY KEY,
    chapter INTEGER NOT NULL,
    scene_seq INTEGER NOT NULL,
    scene_text TEXT NOT NULL,
    summary TEXT,
    embedding_model TEXT,
    embedding_dim INTEGER,
    embedding_version TEXT,
    indexed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chapter, scene_seq)
);
CREATE INDEX IF NOT EXISTS idx_l2_chapter ON l2_scenes(chapter, scene_seq);

CREATE TABLE IF NOT EXISTS scene_vec (
    scene_id TEXT PRIMARY KEY,
    chapter INTEGER NOT NULL,
    dim INTEGER NOT NULL,
    vec_json TEXT NOT NULL,
    model TEXT,
    version TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scene_vec_chapter ON scene_vec(chapter);

CREATE VIRTUAL TABLE IF NOT EXISTS drafts_fts USING fts5(
    draft_id UNINDEXED,
    chapter_no UNINDEXED,
    line_start UNINDEXED,
    line_end UNINDEXED,
    body,
    tokenize = "unicode61 remove_diacritics 2"
);
"""


@register("18")
def migrate_v18(conn: sqlite3.Connection) -> None:
    # 每张表都独立 IF NOT EXISTS；无条件执行才能修复“只缺 L2/FTS”
    # 这类已被手工标记版本的局部数据库。
    conn.executescript(_MEMORY_BASELINE)
    for column, declaration in (
        ("fts_indexed_at", "TEXT"),
        ("l1_status", "TEXT NOT NULL DEFAULT 'pending' "
         "CHECK(l1_status IN ('pending','completed','failed'))"),
        ("l1_extracted_at", "TEXT"),
        ("l2_status", "TEXT NOT NULL DEFAULT 'pending' "
         "CHECK(l2_status IN ('pending','completed','failed'))"),
        ("l2_aggregated_at", "TEXT"),
    ):
        if not column_exists(conn, "draft_index", column):
            conn.execute(f"ALTER TABLE draft_index ADD COLUMN {column} {declaration}")

    for column, declaration in (
        ("atom_type", "TEXT"),
        ("payload_json", "TEXT"),
        ("source_line_start", "INTEGER"),
        ("source_line_end", "INTEGER"),
        ("content_hash", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'active' "
         "CHECK(status IN ('active','unanchored','superseded'))"),
    ):
        if not column_exists(conn, "l1_atoms", column):
            conn.execute(f"ALTER TABLE l1_atoms ADD COLUMN {column} {declaration}")

    # 旧修订的 L1 先标记 superseded；该历史状态的优先级高于证据
    # 完整性，避免后续 unanchored 收紧覆盖修订关系。
    conn.execute(
        "UPDATE l1_atoms SET status='superseded'"
        " WHERE status<>'superseded' AND draft_id IS NOT NULL AND EXISTS ("
        " SELECT 1 FROM draft_index source"
        " JOIN draft_index newer"
        "   ON newer.chapter=source.chapter"
        "  AND newer.revision_round>source.revision_round"
        " WHERE source.id=l1_atoms.draft_id)"
    )

    # 其余旧 L1 行没有可核验的源草稿行号/content hash，不能因
    # 新列的 DEFAULT 'active' 被误当成有效证据。只收紧仍为 active 的行。
    conn.execute(
        "UPDATE l1_atoms SET status='unanchored'"
        " WHERE status='active' AND ("
        " draft_id IS NULL OR source_line_start IS NULL OR"
        " source_line_end IS NULL OR content_hash IS NULL)"
    )

    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_l1_candidate ON l1_atoms(candidate_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_l1_draft_hash
            ON l1_atoms(draft_id, content_hash)
            WHERE draft_id IS NOT NULL AND content_hash IS NOT NULL;
    """)
