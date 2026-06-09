"""L0 原子写入 + 崩溃恢复测试（Group 4：F6/F7）。"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest


# ── 辅助 fixture ──────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_conn(tmp_path):
    """带完整 schema 的 in-memory 连接（via file，供测试访问 pipeline_run 表）。"""
    from novelforge.db.connection import init_db
    db_path = tmp_path / "novel.db"
    conn = init_db(db_path)
    yield conn, tmp_path
    conn.close()


# ── atomic_write_l0 ───────────────────────────────────────────────────────────

class TestAtomicWriteL0:
    def test_creates_file_with_correct_content(self, tmp_path):
        from novelforge.db.l0 import atomic_write_l0
        l0_dir = tmp_path / "l0"
        path, sha256 = atomic_write_l0(l0_dir, "ch0001_r00.txt", "你好世界")
        assert path.exists()
        assert path.read_text(encoding="utf-8") == "你好世界"

    def test_returns_correct_sha256(self, tmp_path):
        from novelforge.db.l0 import atomic_write_l0
        l0_dir = tmp_path / "l0"
        text = "测试内容 abc"
        _, sha256 = atomic_write_l0(l0_dir, "ch0001_r00.txt", text)
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert sha256 == expected

    def test_creates_l0_dir_if_missing(self, tmp_path):
        from novelforge.db.l0 import atomic_write_l0
        l0_dir = tmp_path / "nested" / "l0"
        assert not l0_dir.exists()
        atomic_write_l0(l0_dir, "ch0001_r00.txt", "hello")
        assert l0_dir.exists()

    def test_no_tmp_file_left_after_write(self, tmp_path):
        from novelforge.db.l0 import atomic_write_l0
        l0_dir = tmp_path / "l0"
        atomic_write_l0(l0_dir, "ch0001_r00.txt", "content")
        tmp_files = list(l0_dir.glob("*.tmp"))
        assert tmp_files == []

    def test_overwrites_existing_file(self, tmp_path):
        from novelforge.db.l0 import atomic_write_l0
        l0_dir = tmp_path / "l0"
        atomic_write_l0(l0_dir, "ch0001_r00.txt", "旧内容")
        atomic_write_l0(l0_dir, "ch0001_r00.txt", "新内容")
        assert (l0_dir / "ch0001_r00.txt").read_text(encoding="utf-8") == "新内容"


# ── sweep_orphans ──────────────────────────────────────────────────────────────

class TestSweepOrphans:
    def test_cleans_tmp_files(self, tmp_path):
        from novelforge.db.l0 import sweep_orphans
        l0_dir = tmp_path / "l0"
        l0_dir.mkdir()
        # 残留 .tmp 文件
        (l0_dir / "ch0001_r00.txt.tmp").write_bytes(b"partial")
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE draft_index(id TEXT, file_path TEXT)")

        stats = sweep_orphans(conn, l0_dir)
        conn.close()

        assert stats["deleted_tmp_files"] == 1
        assert not (l0_dir / "ch0001_r00.txt.tmp").exists()

    def test_no_error_when_l0_dir_missing(self, tmp_path):
        from novelforge.db.l0 import sweep_orphans
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE draft_index(id TEXT, file_path TEXT)")
        stats = sweep_orphans(conn, tmp_path / "nonexistent_l0")
        conn.close()
        assert stats["deleted_tmp_files"] == 0

    def test_detects_orphaned_db_rows(self, tmp_path):
        from novelforge.db.l0 import sweep_orphans
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE draft_index(id TEXT, file_path TEXT)")
        # 指向不存在文件的行
        conn.execute(
            "INSERT INTO draft_index VALUES(?, ?)",
            ("draft_1", "l0/ch0001_r00.txt"),
        )
        conn.commit()
        l0_dir = tmp_path / "l0"
        l0_dir.mkdir()

        stats = sweep_orphans(conn, l0_dir)
        conn.close()
        assert stats["orphaned_db_rows"] == 1

    def test_existing_file_not_counted_as_orphan(self, tmp_path):
        from novelforge.db.l0 import atomic_write_l0, sweep_orphans
        l0_dir = tmp_path / "l0"
        atomic_write_l0(l0_dir, "ch0001_r00.txt", "content")

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE draft_index(id TEXT, file_path TEXT)")
        conn.execute(
            "INSERT INTO draft_index VALUES(?, ?)",
            ("draft_1", "l0/ch0001_r00.txt"),
        )
        conn.commit()

        stats = sweep_orphans(conn, l0_dir)
        conn.close()
        assert stats["orphaned_db_rows"] == 0


# ── sweep_crashed_runs ─────────────────────────────────────────────────────────

class TestSweepCrashedRuns:
    def test_marks_running_as_crashed(self, tmp_conn):
        from novelforge.db.l0 import sweep_crashed_runs
        conn, _ = tmp_conn
        conn.execute(
            "INSERT INTO pipeline_run(run_id, chapter, project_id, status)"
            " VALUES('run1', 1, 'proj', 'running')"
        )
        conn.execute(
            "INSERT INTO pipeline_run(run_id, chapter, project_id, status)"
            " VALUES('run2', 2, 'proj', 'completed')"
        )
        conn.commit()

        crashed = sweep_crashed_runs(conn)
        assert "run1" in crashed
        assert "run2" not in crashed

        row = conn.execute("SELECT status FROM pipeline_run WHERE run_id='run1'").fetchone()
        assert row["status"] == "crashed"

        row2 = conn.execute("SELECT status FROM pipeline_run WHERE run_id='run2'").fetchone()
        assert row2["status"] == "completed"

    def test_returns_empty_when_no_running(self, tmp_conn):
        from novelforge.db.l0 import sweep_crashed_runs
        conn, _ = tmp_conn
        result = sweep_crashed_runs(conn)
        assert result == []

    def test_idempotent_on_already_crashed(self, tmp_conn):
        from novelforge.db.l0 import sweep_crashed_runs
        conn, _ = tmp_conn
        conn.execute(
            "INSERT INTO pipeline_run(run_id, chapter, project_id, status)"
            " VALUES('run3', 3, 'proj', 'crashed')"
        )
        conn.commit()
        result = sweep_crashed_runs(conn)
        assert "run3" not in result


# ── pipeline_run 状态机集成测试 ────────────────────────────────────────────────

class TestPipelineRunStateMachine:
    def test_persist_draft_creates_draft_index_row(self, tmp_conn):
        from novelforge.control_plane.orchestrator import _persist_draft
        conn, tmp_path = tmp_conn
        db_path = str(tmp_path / "novel.db")
        draft_id = _persist_draft(conn, "第一章内容\n测试文本", 1, "proj", db_path)
        assert draft_id is not None
        row = conn.execute("SELECT * FROM draft_index WHERE id=?", (draft_id,)).fetchone()
        assert row is not None
        assert row["chapter"] == 1
        assert row["sha256"] is not None
        assert row["word_count"] > 0

    def test_persist_draft_writes_l0_file(self, tmp_conn):
        from novelforge.control_plane.orchestrator import _persist_draft
        conn, tmp_path = tmp_conn
        db_path = str(tmp_path / "novel.db")
        text = "第一章正文"
        draft_id = _persist_draft(conn, text, 1, "proj", db_path)
        assert draft_id is not None

        row = conn.execute("SELECT file_path FROM draft_index WHERE id=?", (draft_id,)).fetchone()
        file_path = tmp_path / row["file_path"]
        assert file_path.exists()
        assert file_path.read_text(encoding="utf-8") == text

    def test_persist_draft_revision_round_increments(self, tmp_conn):
        from novelforge.control_plane.orchestrator import _persist_draft
        conn, tmp_path = tmp_conn
        db_path = str(tmp_path / "novel.db")
        _persist_draft(conn, "初稿", 5, "proj", db_path)
        _persist_draft(conn, "修订稿", 5, "proj", db_path)

        rows = conn.execute(
            "SELECT revision_round FROM draft_index WHERE chapter=5 ORDER BY revision_round"
        ).fetchall()
        rounds = [r["revision_round"] for r in rows]
        assert rounds == [0, 1]

    def test_begin_and_complete_pipeline_run(self, tmp_conn):
        from novelforge.control_plane.orchestrator import (
            _begin_pipeline_run, _complete_pipeline_run
        )
        conn, _ = tmp_conn
        run_id = "test_run_1"
        _begin_pipeline_run(conn, 3, "proj", run_id)

        row = conn.execute("SELECT status FROM pipeline_run WHERE run_id=?", (run_id,)).fetchone()
        assert row["status"] == "running"

        _complete_pipeline_run(conn, run_id, "draft_abc")
        row = conn.execute("SELECT status, draft_id FROM pipeline_run WHERE run_id=?", (run_id,)).fetchone()
        assert row["status"] == "completed"
        assert row["draft_id"] == "draft_abc"

    def test_startup_sweep_marks_crashed_run(self, tmp_conn):
        from novelforge.control_plane.orchestrator import _begin_pipeline_run
        from novelforge.db.l0 import sweep_crashed_runs
        conn, _ = tmp_conn

        # 模拟崩溃：只有 begin，没有 complete
        _begin_pipeline_run(conn, 7, "proj", "orphan_run")

        # 启动期 sweep
        crashed = sweep_crashed_runs(conn)
        assert "orphan_run" in crashed
        row = conn.execute(
            "SELECT status FROM pipeline_run WHERE run_id='orphan_run'"
        ).fetchone()
        assert row["status"] == "crashed"
