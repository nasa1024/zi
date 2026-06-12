"""Schema 迁移系统测试。"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest


def _make_v4_db(path: Path) -> sqlite3.Connection:
    """创建一个模拟 v4 数据库（无 volumes/branches/新列）。"""
    from novelforge.db.connection import connect
    conn = connect(path)
    conn.executescript("""
        CREATE TABLE meta_kv (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE entities (
            id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL UNIQUE,
            entity_type TEXT NOT NULL DEFAULT 'character',
            first_appear_chapter INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            detail_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE facts (
            id TEXT PRIMARY KEY,
            entity_id TEXT,
            fact_type TEXT NOT NULL DEFAULT 'misc',
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            detail_json TEXT,
            status TEXT NOT NULL DEFAULT 'tentative',
            valid_from_chapter INTEGER NOT NULL DEFAULT 0,
            valid_to_chapter INTEGER,
            current_revision_id TEXT NOT NULL DEFAULT 'init',
            confidence REAL,
            risk_tier TEXT NOT NULL DEFAULT 'low',
            version INTEGER NOT NULL DEFAULT 0,
            injection_mode TEXT NOT NULL DEFAULT 'detected',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE draft_index (
            id TEXT PRIMARY KEY,
            chapter INTEGER NOT NULL,
            revision_round INTEGER NOT NULL DEFAULT 0,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            word_count INTEGER,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE l1_atoms (
            id TEXT PRIMARY KEY,
            chapter INTEGER NOT NULL,
            draft_id TEXT,
            atom_text TEXT NOT NULL,
            anchor TEXT,
            extracted_by TEXT,
            candidate_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    from novelforge.db.connection import set_meta
    set_meta(conn, "schema_version", "4")
    conn.commit()
    return conn


class TestMigrations:
    def test_no_migration_needed_for_current_version(self, tmp_path):
        """已是最新版本时，migrate() 直接返回相同版本。"""
        from novelforge.db.migrations import migrate
        from novelforge.db.connection import init_db

        db = tmp_path / "novel.db"
        conn = init_db(db)
        old, new = migrate(conn)
        assert old == new
        conn.close()

    def test_migrate_from_v4_to_latest(self, tmp_path):
        """从 v4 迁移到最新版：v5 新表/新列 + v6 pipeline_run 表都应存在。"""
        from novelforge.db.migrations import migrate, column_exists, table_exists
        from novelforge import SCHEMA_VERSION

        db = tmp_path / "novel_v4.db"
        conn = _make_v4_db(db)

        old, new = migrate(conn)
        assert old == "4"
        assert new == SCHEMA_VERSION

        # v5 新表
        assert table_exists(conn, "volumes")
        assert table_exists(conn, "branches")

        # v5 新列
        assert column_exists(conn, "facts", "volume_no")
        assert column_exists(conn, "facts", "branch_id")
        assert column_exists(conn, "draft_index", "volume_no")
        assert column_exists(conn, "l1_atoms", "cold_start")

        # v6 新表
        assert table_exists(conn, "pipeline_run")

        # v10/v11 新列（候选报告 + 逐章成本）
        assert column_exists(conn, "pipeline_run", "detail_json")
        assert column_exists(conn, "pipeline_run", "tokens_spent")
        assert column_exists(conn, "pipeline_run", "usd_spent")

        # v12: foreshadow 结算列 + foreshadow_log 审计表
        # （老库此前没有 foreshadow 表——v12 需整表创建而非 ALTER）
        assert table_exists(conn, "foreshadow")
        for col in ("last_mentioned_chapter", "advance_count",
                    "last_advanced_chapter", "origin"):
            assert column_exists(conn, "foreshadow", col), f"缺列 {col}"
        assert table_exists(conn, "foreshadow_log")

        # 版本号已更新
        from novelforge.db.connection import get_meta
        assert get_meta(conn, "schema_version") == SCHEMA_VERSION
        conn.close()

    def test_migrate_idempotent(self, tmp_path):
        """迁移幂等：对已迁移的库再次调用 migrate() 无副作用。"""
        from novelforge.db.migrations import migrate
        from novelforge import SCHEMA_VERSION

        db = tmp_path / "novel_v4.db"
        conn = _make_v4_db(db)

        migrate(conn)
        # 第二次调用
        old2, new2 = migrate(conn)
        assert old2 == new2 == SCHEMA_VERSION
        conn.close()

    def test_migrate_data_preserved(self, tmp_path):
        """迁移不删除现有数据。"""
        from novelforge.db.migrations import migrate

        db = tmp_path / "novel_v4.db"
        conn = _make_v4_db(db)

        # 写入旧格式数据
        conn.execute(
            "INSERT INTO facts(id, fact_type, subject, predicate, object,"
            " valid_from_chapter, current_revision_id)"
            " VALUES('f1','misc','陆天','境界','炼气',1,'r1')"
        )
        conn.commit()

        migrate(conn)

        row = conn.execute("SELECT * FROM facts WHERE id='f1'").fetchone()
        assert row is not None
        assert row["subject"] == "陆天"
        # 新列默认值
        assert row["volume_no"] is None
        assert row["branch_id"] is None
        conn.close()

    def test_new_db_no_migration_runs(self, tmp_path):
        """新建库通过 schema.sql 直接创建，版本即为 SCHEMA_VERSION。"""
        from novelforge.db.connection import init_db, get_meta
        from novelforge import SCHEMA_VERSION

        db = tmp_path / "new.db"
        conn = init_db(db)
        assert get_meta(conn, "schema_version") == SCHEMA_VERSION
        conn.close()

    def test_v12_fresh_db_has_settle_columns(self, tmp_path):
        """新库走 schema.sql 基线，必须直接含 v12 列/表（不经迁移链）。"""
        from novelforge.db.connection import init_db
        from novelforge.db.migrations import column_exists, table_exists

        conn = init_db(tmp_path / "fresh12.db")
        for col in ("last_mentioned_chapter", "advance_count",
                    "last_advanced_chapter", "origin"):
            assert column_exists(conn, "foreshadow", col), f"schema.sql 基线缺列 {col}"
        assert table_exists(conn, "foreshadow_log")
        conn.close()

    def test_v12_alters_existing_foreshadow(self, tmp_path):
        """已有 foreshadow 表（旧形态）的库：v12 走 ALTER 补列且保数据。"""
        from novelforge.db.migrations import migrate, column_exists

        conn = _make_v4_db(tmp_path / "v4fs.db")
        conn.executescript("""
            CREATE TABLE foreshadow (
                id              TEXT PRIMARY KEY,
                label           TEXT NOT NULL,
                description     TEXT NOT NULL,
                state           TEXT NOT NULL DEFAULT 'planted',
                planted_chapter INTEGER NOT NULL,
                due_chapter     INTEGER,
                paid_off_chapter INTEGER,
                related_entity_id TEXT,
                importance      INTEGER NOT NULL DEFAULT 3,
                fact_id         TEXT,
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        conn.execute(
            "INSERT INTO foreshadow(id, label, description, planted_chapter)"
            " VALUES('fs_old','旧伏笔','迁移前已存在',3)")
        conn.commit()

        migrate(conn)

        row = conn.execute("SELECT * FROM foreshadow WHERE id='fs_old'").fetchone()
        assert row is not None and row["label"] == "旧伏笔"
        assert row["advance_count"] == 0 and row["origin"] == "manual"
        assert column_exists(conn, "foreshadow", "last_mentioned_chapter")
        conn.close()

    def test_get_db_version(self, tmp_path):
        """get_db_version 读取当前版本。"""
        from novelforge.db.migrations import get_db_version
        from novelforge.db.connection import init_db
        from novelforge import SCHEMA_VERSION

        db = tmp_path / "ver.db"
        conn = init_db(db)
        assert get_db_version(conn) == SCHEMA_VERSION
        conn.close()

    def test_column_exists_helper(self, tmp_path):
        """column_exists 正确识别存在/不存在的列。"""
        from novelforge.db.migrations import column_exists
        from novelforge.db.connection import init_db

        db = tmp_path / "col.db"
        conn = init_db(db)
        assert column_exists(conn, "facts", "subject")
        assert column_exists(conn, "facts", "volume_no")   # 已在 v5
        assert not column_exists(conn, "facts", "nonexistent_col")
        conn.close()
