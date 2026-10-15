"""v22 migration: promise ledger columns, aftermath beats, polarity streaks, batches."""
from __future__ import annotations

import sqlite3

import pytest

from novelforge import SCHEMA_VERSION
from novelforge.db.connection import init_db
from novelforge.db.migrations import column_exists, migrate, table_exists
from novelforge.db.migrations.v22 import migrate_v22


_OLD_BEATS = """
CREATE TABLE beats (
    id TEXT PRIMARY KEY, chapter INTEGER NOT NULL, seq INTEGER NOT NULL,
    beat_type TEXT NOT NULL
        CHECK(beat_type IN ('setup','turn','payoff_beat','tension_point','hook')),
    summary TEXT NOT NULL, value_start TEXT, value_end TEXT, value_axis TEXT,
    arc_id TEXT, source_decision_id TEXT,
    constraint_level TEXT NOT NULL DEFAULT 'soft' CHECK(constraint_level IN ('soft','hard')),
    tension_level INTEGER, related_foreshadow_id TEXT,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK(status IN ('planned','drafted','satisfied','dropped')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(chapter, seq),
    FOREIGN KEY(related_foreshadow_id) REFERENCES foreshadow(id)
)
"""

_OLD_VERSION_BEATS = """
CREATE TABLE chapter_plan_version_beats (
    id TEXT PRIMARY KEY,
    plan_version_id TEXT NOT NULL REFERENCES chapter_plan_versions(id),
    seq INTEGER NOT NULL CHECK(seq >= 1),
    beat_type TEXT NOT NULL
        CHECK(beat_type IN ('setup','turn','payoff_beat','tension_point','hook')),
    summary TEXT NOT NULL, value_axis TEXT, arc_id TEXT,
    constraint_level TEXT NOT NULL DEFAULT 'soft' CHECK(constraint_level IN ('soft','hard')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(plan_version_id, seq)
)
"""


def _downgrade_to_v21(conn: sqlite3.Connection) -> None:
    """Turn a fresh v22 DB into the v21 shape for the tables v22 touches."""
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TABLE beats")
    conn.execute(_OLD_BEATS)
    conn.execute("DROP TABLE chapter_plan_version_beats")
    conn.execute(_OLD_VERSION_BEATS)
    for column in ("promise_kind", "line_type", "visibility", "parent_id", "remind_every"):
        conn.execute(f"ALTER TABLE foreshadow DROP COLUMN {column}")
    for column in ("polarity", "turning_point"):
        conn.execute(f"ALTER TABLE pacing_state DROP COLUMN {column}")
    for column in ("low_streak", "polarity_streak", "last_polarity"):
        conn.execute(f"ALTER TABLE pacing_cursor DROP COLUMN {column}")
    conn.execute("ALTER TABLE pipeline_run DROP COLUMN batch_id")
    conn.execute("DROP TABLE draft_batch_chapters")
    conn.execute("DROP TABLE draft_batches")
    conn.execute("UPDATE meta_kv SET value='21' WHERE key='schema_version'")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")


def test_schema_version_is_22():
    assert SCHEMA_VERSION == "22"


def test_fresh_schema_contains_v22_contract(tmp_path):
    conn = init_db(tmp_path / "fresh.db")
    try:
        for column in ("promise_kind", "line_type", "visibility", "parent_id", "remind_every"):
            assert column_exists(conn, "foreshadow", column)
        for table in ("beats", "chapter_plan_version_beats"):
            assert column_exists(conn, table, "witnesses_json")
        assert column_exists(conn, "pacing_state", "polarity")
        assert column_exists(conn, "pacing_cursor", "low_streak")
        assert column_exists(conn, "pipeline_run", "batch_id")
        assert table_exists(conn, "draft_batches")
        assert table_exists(conn, "draft_batch_chapters")
        conn.execute(
            "INSERT INTO beats(id, chapter, seq, beat_type, summary)"
            " VALUES('b1', 1, 1, 'aftermath', '余韵')"
        )
        row = conn.execute("SELECT witnesses_json FROM beats WHERE id='b1'").fetchone()
        assert row["witnesses_json"] == "[]"
        conn.execute(
            "INSERT INTO foreshadow(id, label, description, planted_chapter)"
            " VALUES('f1', '血仇', '灭门真凶', 1)"
        )
        fs = conn.execute(
            "SELECT line_type, visibility FROM foreshadow WHERE id='f1'"
        ).fetchone()
        assert (fs["line_type"], fs["visibility"]) == ("long", "reader")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO beats(id, chapter, seq, beat_type, summary)"
                " VALUES('b2', 1, 2, 'filler', 'x')"
            )
    finally:
        conn.close()


def test_upgrade_from_v21_preserves_rows_and_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    conn = init_db(path)
    _downgrade_to_v21(conn)
    conn.execute(
        "INSERT INTO beats(id, chapter, seq, beat_type, summary, value_axis, constraint_level)"
        " VALUES('b1', 3, 1, 'payoff_beat', '打脸', '轻视→敬畏', 'hard')"
    )
    conn.execute(
        "INSERT INTO chapter_plan_versions(id, chapter, version, payload_json, status)"
        " VALUES('pv1', 3, 1, '{}', 'active')"
    )
    conn.execute(
        "INSERT INTO chapter_plan_version_beats(id, plan_version_id, seq, beat_type, summary)"
        " VALUES('vb1', 'pv1', 1, 'hook', '悬念')"
    )
    conn.execute(
        "INSERT INTO foreshadow(id, label, description, planted_chapter)"
        " VALUES('f1', '血仇', '灭门真凶', 1)"
    )
    conn.commit()

    old, new = migrate(conn)
    assert (old, new) == ("21", "22")

    beat = conn.execute("SELECT * FROM beats WHERE id='b1'").fetchone()
    assert beat["summary"] == "打脸"
    assert beat["value_axis"] == "轻视→敬畏"
    assert beat["constraint_level"] == "hard"
    assert beat["witnesses_json"] == "[]"
    version_beat = conn.execute(
        "SELECT * FROM chapter_plan_version_beats WHERE id='vb1'"
    ).fetchone()
    assert version_beat["beat_type"] == "hook"
    fs = conn.execute("SELECT line_type, visibility FROM foreshadow WHERE id='f1'").fetchone()
    assert (fs["line_type"], fs["visibility"]) == ("long", "reader")

    conn.execute(
        "INSERT INTO beats(id, chapter, seq, beat_type, summary)"
        " VALUES('b2', 3, 2, 'aftermath', '余波')"
    )
    indexes = {
        row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='beats'"
        )
    }
    assert {"idx_beats_chapter", "idx_beats_type"} <= indexes
    conn.commit()

    # Re-running the migration body must be a no-op.
    migrate_v22(conn)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM beats").fetchone()[0] == 2
    assert not table_exists(conn, "beats__v22")
    conn.close()


def test_failed_v22_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    conn = init_db(path)
    _downgrade_to_v21(conn)
    conn.execute(
        "INSERT INTO beats(id, chapter, seq, beat_type, summary)"
        " VALUES('b1', 1, 1, 'setup', '开场')"
    )
    conn.commit()

    import novelforge.db.migrations.v22 as v22

    real_add_columns = v22._add_columns

    def boom(conn_, table, columns):
        # Fail after the beat tables were rebuilt so the rebuild must roll back too.
        if table == "pacing_state":
            raise RuntimeError("boom")
        return real_add_columns(conn_, table, columns)

    monkeypatch.setattr(v22, "_add_columns", boom)
    with pytest.raises(RuntimeError):
        migrate(conn)
    assert conn.execute(
        "SELECT value FROM meta_kv WHERE key='schema_version'"
    ).fetchone()[0] == "21"
    assert not column_exists(conn, "beats", "witnesses_json")
    assert not column_exists(conn, "foreshadow", "line_type")
    assert not table_exists(conn, "beats__v22")
    assert conn.execute("SELECT COUNT(*) FROM beats").fetchone()[0] == 1
    conn.close()
