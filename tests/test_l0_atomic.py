"""L0 原子写入 + 崩溃恢复测试（Group 4：F6/F7）。"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
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

    def test_rejects_l0_directory_symlink_outside_project(self, tmp_path):
        from novelforge.db.l0 import atomic_write_l0

        project = tmp_path / "project"
        outside = tmp_path / "outside"
        project.mkdir()
        outside.mkdir()
        link = project / "l0"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            # Windows normally requires Developer Mode or elevated privileges
            # for symlinks. A directory junction exercises the same resolve()
            # escape path without that privilege.
            if os.name != "nt":
                raise
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                pytest.skip("Windows account cannot create a symlink or junction")
        with pytest.raises(ValueError, match="越出项目根目录"):
            atomic_write_l0(project / "l0", "ch0001.txt", "secret")

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

    def test_path_escape_is_rejected_and_counted_orphan(self, tmp_path):
        from novelforge.db.connection import init_db
        from novelforge.db.l0 import load_draft_text_by_id, sweep_orphans

        db_path = tmp_path / "novel.db"
        conn = init_db(db_path)
        (tmp_path / "secret.txt").write_text("不应被 L0 读取", encoding="utf-8")
        conn.execute(
            "INSERT INTO draft_index(id, chapter, revision_round, file_path, sha256)"
            " VALUES('escape',1,0,'../secret.txt','x')"
        )
        conn.commit()

        assert load_draft_text_by_id(conn, "escape") is None
        stats = sweep_orphans(conn, tmp_path / "l0")
        assert stats["orphaned_db_rows"] == 1
        conn.close()


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

    def test_persist_draft_uses_connected_database_root_not_stale_config_path(self, tmp_conn):
        from novelforge.control_plane.orchestrator import _persist_draft

        conn, tmp_path = tmp_conn
        stale_db = tmp_path / "other-project" / "novel.db"
        draft_id = _persist_draft(conn, "连接所属项目正文", 2, "proj", str(stale_db))
        row = conn.execute(
            "SELECT file_path FROM draft_index WHERE id=?", (draft_id,)
        ).fetchone()
        assert (tmp_path / row["file_path"]).read_text(encoding="utf-8") == "连接所属项目正文"
        assert not (stale_db.parent / row["file_path"]).exists()

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

    def test_new_revision_invalidates_stale_narrative_summaries(self, tmp_conn):
        from novelforge.control_plane.orchestrator import _persist_draft

        conn, tmp_path = tmp_conn
        db_path = str(tmp_path / "novel.db")
        _persist_draft(conn, "旧稿正文", 3, "proj", db_path)
        conn.execute(
            "INSERT INTO chapter_summaries(id,chapter,summary)"
            " VALUES('sum3',3,'旧稿章摘要')"
        )
        conn.execute(
            "INSERT INTO volumes(id,volume_no,title,start_chapter,end_chapter,rolling_summary)"
            " VALUES('vol1',1,'第一卷',1,10,'包含旧稿的卷摘要')"
        )
        conn.execute(
            "INSERT INTO meta_kv(key,value) VALUES('global_synopsis','包含旧稿的全书梗概')"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        conn.commit()

        _persist_draft(conn, "新稿正文", 3, "proj", db_path)
        assert conn.execute(
            "SELECT COUNT(*) FROM chapter_summaries WHERE chapter=3"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT rolling_summary FROM volumes WHERE id='vol1'"
        ).fetchone()["rolling_summary"] is None
        assert conn.execute(
            "SELECT value FROM meta_kv WHERE key='global_synopsis'"
        ).fetchone() is None

    def test_concurrent_same_chapter_keeps_ids_files_and_fts_consistent(self, tmp_path):
        from novelforge.control_plane.orchestrator import _persist_draft
        from novelforge.db.connection import connect, init_db
        from novelforge.db.l0 import load_latest_draft_text

        db_path = tmp_path / "novel.db"
        init_db(db_path).close()
        barrier = threading.Barrier(2)

        def write(text: str) -> str | None:
            conn = connect(db_path)
            try:
                barrier.wait(timeout=5)
                return _persist_draft(conn, text, 9, "proj", str(db_path))
            finally:
                conn.close()

        texts = {"并发稿甲：山门风雪。", "并发稿乙：夜渡寒江。"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(write, texts))

        assert all(ids)
        assert len(set(ids)) == 2
        conn = connect(db_path)
        try:
            rows = conn.execute(
                "SELECT id, revision_round, file_path FROM draft_index"
                " WHERE chapter=9 ORDER BY revision_round"
            ).fetchall()
            assert [row["revision_round"] for row in rows] == [0, 1]
            assert {row["id"] for row in rows} == set(ids)
            assert len({row["file_path"] for row in rows}) == 2
            stored = {
                (tmp_path / row["file_path"]).read_text(encoding="utf-8") for row in rows
            }
            assert stored == texts

            latest_id = rows[-1]["id"]
            fts_ids = {
                row["draft_id"] for row in conn.execute(
                    "SELECT DISTINCT draft_id FROM drafts_fts WHERE chapter_no=9"
                ).fetchall()
            }
            assert fts_ids == {latest_id}
            assert load_latest_draft_text(conn, 9) in texts
        finally:
            conn.close()

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
