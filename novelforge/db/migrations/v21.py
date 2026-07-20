"""Migration v20 -> v21: project writing profiles and language isolation."""
from __future__ import annotations

import sqlite3

from . import column_exists, register, table_exists


_PROFILE_TABLE = """
CREATE TABLE IF NOT EXISTS writing_profile_versions (
    id                     TEXT PRIMARY KEY,
    revision               INTEGER NOT NULL UNIQUE CHECK(revision >= 1),
    profile_id             TEXT NOT NULL,
    catalog_version        TEXT NOT NULL,
    content_language       TEXT NOT NULL,
    target_market          TEXT NOT NULL,
    publication_format     TEXT NOT NULL,
    primary_genre          TEXT,
    story_tags_json        TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(story_tags_json)),
    audience_rating        TEXT NOT NULL DEFAULT 'general',
    content_warnings_json  TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(content_warnings_json)),
    overrides_json         TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(overrides_json)),
    snapshot_json          TEXT NOT NULL CHECK(json_valid(snapshot_json)),
    effective_from_chapter INTEGER NOT NULL CHECK(effective_from_chapter >= 1),
    prompt_version         TEXT NOT NULL,
    changed_by             TEXT NOT NULL DEFAULT 'user',
    change_reason          TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def _add_column(conn: sqlite3.Connection, table: str, name: str, decl: str) -> None:
    if not column_exists(conn, table, name):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


@register("21")
def migrate_v21(conn: sqlite3.Connection) -> None:
    conn.execute(_PROFILE_TABLE)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_writing_profile_versions_effective "
        "ON writing_profile_versions(effective_from_chapter, revision DESC)"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_writing_profile_versions_no_update "
        "BEFORE UPDATE ON writing_profile_versions "
        "BEGIN SELECT RAISE(ABORT, 'writing_profile_versions is append-only'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_writing_profile_versions_no_delete "
        "BEFORE DELETE ON writing_profile_versions "
        "BEGIN SELECT RAISE(ABORT, 'writing_profile_versions is append-only'); END"
    )

    if table_exists(conn, "style_anchors"):
        _add_column(conn, "style_anchors", "language", "TEXT NOT NULL DEFAULT 'zh-CN'")
        _add_column(conn, "style_anchors", "source_type", "TEXT NOT NULL DEFAULT 'legacy'")
        _add_column(conn, "style_anchors", "source_note", "TEXT")
        # Existing anchors have no recorded rights confirmation.  Keep them for
        # review, but do not inject them into generation until explicitly approved.
        _add_column(
            conn,
            "style_anchors",
            "rights_confirmed",
            "INTEGER NOT NULL DEFAULT 0 CHECK(rights_confirmed IN (0,1))",
        )
        _add_column(
            conn, "style_anchors", "profile_tags",
            "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(profile_tags))",
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_style_anchors_language_emotion "
            "ON style_anchors(language, emotion)"
        )

    if table_exists(conn, "pipeline_run"):
        _add_column(conn, "pipeline_run", "content_language", "TEXT")
        _add_column(conn, "pipeline_run", "writing_profile_id", "TEXT")
        _add_column(conn, "pipeline_run", "writing_profile_version", "INTEGER")
        _add_column(conn, "pipeline_run", "prompt_version", "TEXT")
        _add_column(conn, "pipeline_run", "model_id", "TEXT")

    if table_exists(conn, "draft_index"):
        _add_column(conn, "draft_index", "content_language", "TEXT")
        _add_column(conn, "draft_index", "writing_profile_id", "TEXT")
        _add_column(conn, "draft_index", "writing_profile_version", "INTEGER")
        _add_column(conn, "draft_index", "prompt_version", "TEXT")
