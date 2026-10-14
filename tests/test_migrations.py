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


def _make_partial_v18_db(path: Path) -> sqlite3.Connection:
    """创建一个已被旧代码标成 v18、但缺 L2/FTS 且尚无
    fact_candidates.source_draft_id 的局部数据库。"""
    from novelforge.db.connection import connect, set_meta

    conn = connect(path)
    conn.executescript("""
        CREATE TABLE meta_kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
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
            volume_no INTEGER,
            fts_indexed_at TEXT,
            l1_status TEXT NOT NULL DEFAULT 'pending',
            l1_extracted_at TEXT,
            l2_status TEXT NOT NULL DEFAULT 'pending',
            l2_aggregated_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(chapter, revision_round)
        );
        CREATE TABLE l1_atoms (
            id TEXT PRIMARY KEY,
            chapter INTEGER NOT NULL,
            draft_id TEXT,
            atom_text TEXT NOT NULL,
            anchor TEXT,
            extracted_by TEXT,
            candidate_id TEXT,
            atom_type TEXT,
            payload_json TEXT,
            source_line_start INTEGER,
            source_line_end INTEGER,
            content_hash TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            cold_start INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(draft_id) REFERENCES draft_index(id)
        );
        CREATE TABLE fact_candidates (
            candidate_id TEXT PRIMARY KEY,
            proposal_json TEXT NOT NULL,
            source_chapter INTEGER
        );
    """)
    set_meta(conn, "schema_version", "18")
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
        for col in ("fts_indexed_at", "l1_status", "l1_extracted_at",
                    "l2_status", "l2_aggregated_at"):
            assert column_exists(conn, "draft_index", col), f"v18 缺列 {col}"
        for col in ("atom_type", "payload_json", "source_line_start",
                    "source_line_end", "content_hash", "status"):
            assert column_exists(conn, "l1_atoms", col), f"v18 缺列 {col}"
        # v18 baseline 必须无条件补齐：该 v4 fixture 原本没有 L2/FTS。
        for table in ("l2_scenes", "scene_vec", "drafts_fts", "fact_candidates"):
            assert table_exists(conn, table), f"迁移后缺表 {table}"
        assert column_exists(conn, "fact_candidates", "source_draft_id")
        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(fact_candidates)")
        }
        assert "idx_cand_source_draft" in indexes

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

        # v13: 细纲契约列 + style_anchors（chapter_cards 同样从未被迁移创建过——缺表整建）
        assert table_exists(conn, "chapter_cards")
        for col in ("target_emotion", "opening_hook_type",
                    "hook_type", "expectation_score"):
            assert column_exists(conn, "chapter_cards", col), f"缺列 {col}"
        assert table_exists(conn, "style_anchors")

        # v14: volumes 卷级 Objective + KR
        for col in ("objective", "key_results"):
            assert column_exists(conn, "volumes", col), f"缺列 {col}"

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

    def test_v21_migrated_style_anchor_rights_are_unconfirmed_and_boolean(self, tmp_path):
        from novelforge.db.migrations.v21 import migrate_v21

        conn = _make_v4_db(tmp_path / "v21_anchor_rights.db")
        conn.executescript("""
            CREATE TABLE style_anchors (
                id TEXT PRIMARY KEY,
                emotion TEXT NOT NULL,
                title TEXT,
                content TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO style_anchors(id, emotion, content)
            VALUES('legacy_anchor','紧张','旧锚点内容');
        """)

        migrate_v21(conn)

        row = conn.execute(
            "SELECT content, language, source_type, rights_confirmed"
            " FROM style_anchors"
            " WHERE id='legacy_anchor'"
        ).fetchone()
        assert row["content"] == "旧锚点内容"
        assert row["language"] == "zh-CN"
        assert row["source_type"] == "legacy"
        assert row["rights_confirmed"] == 0
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO style_anchors("
                "id, emotion, content, rights_confirmed)"
                " VALUES('invalid_rights','紧张','无效',2)"
            )
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

    def test_v18_to_v19_repairs_memory_and_candidate_provenance(self, tmp_path):
        """v19 修复旧 v18 baseline，补来源草稿并收紧 L1 历史状态。"""
        from novelforge import SCHEMA_VERSION
        from novelforge.db.migrations import column_exists, migrate, table_exists

        conn = _make_partial_v18_db(tmp_path / "partial_v18.db")
        conn.executemany(
            "INSERT INTO draft_index(id,chapter,revision_round,file_path,sha256)"
            " VALUES(?,?,?,?,?)",
            (
                ("draft_old", 1, 0, "l0/ch0001_r00.txt", "old"),
                ("draft_new", 1, 1, "l0/ch0001_r01.txt", "new"),
            ),
        )
        conn.executemany(
            "INSERT INTO l1_atoms("
            " id,chapter,draft_id,atom_text,source_line_start,source_line_end,"
            " content_hash,status) VALUES(?,?,?,?,?,?,?,?)",
            (
                # 属于旧修订且证据不全：superseded 仍优先于 unanchored。
                ("atom_old", 1, "draft_old", "旧修订", None, None, None, "active"),
                # 最新修订但缺行号/hash：不可核验。
                ("atom_unanchored", 1, "draft_new", "无证据", None, None, None, "active"),
                # 最新修订且证据完整：保持 active。
                ("atom_active", 1, "draft_new", "可核验", 2, 2, "hash-new", "active"),
            ),
        )
        conn.execute(
            "INSERT INTO fact_candidates(candidate_id,proposal_json,source_chapter)"
            " VALUES('cand_old','{}',1)"
        )
        conn.commit()

        old, new = migrate(conn)

        assert (old, new) == ("18", SCHEMA_VERSION)
        assert SCHEMA_VERSION == "22"
        for table in ("l2_scenes", "scene_vec", "drafts_fts"):
            assert table_exists(conn, table), f"v19 未修复 v18 缺表 {table}"
        assert column_exists(conn, "fact_candidates", "source_draft_id")
        candidate = conn.execute(
            "SELECT candidate_id, source_draft_id FROM fact_candidates"
            " WHERE candidate_id='cand_old'"
        ).fetchone()
        assert candidate["candidate_id"] == "cand_old"
        assert candidate["source_draft_id"] is None
        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(fact_candidates)")
        }
        assert "idx_cand_source_draft" in indexes
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id,status FROM l1_atoms")
        }
        assert statuses == {
            "atom_old": "superseded",
            "atom_unanchored": "unanchored",
            "atom_active": "active",
        }
        conn.close()

    def test_check_and_migrate_is_fail_closed(self, tmp_path, monkeypatch):
        """启动迁移异常必须上抛，不得只打印警告后继续运行。"""
        import novelforge.db.migrations as migrations
        from novelforge.db.connection import init_db, set_meta

        conn = init_db(tmp_path / "fail_closed.db")
        set_meta(conn, "schema_version", "18")
        conn.commit()

        def fail_migration(_conn):
            raise RuntimeError("synthetic migration failure")

        monkeypatch.setattr(migrations, "migrate", fail_migration)
        with pytest.raises(RuntimeError, match="synthetic migration failure"):
            migrations.check_and_migrate(conn)
        conn.close()

    def test_project_registry_does_not_cache_failed_migration(
        self, tmp_path, monkeypatch,
    ):
        """迁移失败后同一项目下次连接仍必须再次尝试迁移。"""
        import novelforge.app.deps as deps
        import novelforge.db.migrations as migrations
        from novelforge.app.deps import ProjectMeta, ProjectRegistry
        from novelforge.db.connection import init_db, set_meta

        db = tmp_path / "registry_fail.db"
        conn = init_db(db)
        set_meta(conn, "schema_version", "18")
        conn.commit()
        conn.close()

        monkeypatch.setattr(deps, "_REGISTRY_FILE", tmp_path / "missing-projects.json")
        registry = ProjectRegistry()
        registry._projects["p1"] = ProjectMeta(
            project_id="p1",
            name="migration failure",
            genre="xuanhuan",
            db_path=str(db),
            created_at="2026-07-11T00:00:00",
        )
        calls = 0

        def fail_migration(_conn):
            nonlocal calls
            calls += 1
            raise RuntimeError("registry migration failure")

        monkeypatch.setattr(migrations, "check_and_migrate", fail_migration)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="registry migration failure"):
                registry.open_conn("p1")

        assert calls == 2
        assert "p1" not in registry._migrated_projects

    def test_project_registry_requires_confirmed_current_version(
        self, tmp_path, monkeypatch,
    ):
        """check_and_migrate 即使返回，registry 也必须亲自确认版本。"""
        import novelforge.app.deps as deps
        import novelforge.db.migrations as migrations
        from novelforge.app.deps import ProjectMeta, ProjectRegistry
        from novelforge.db.connection import init_db, set_meta

        db = tmp_path / "registry_stale.db"
        conn = init_db(db)
        set_meta(conn, "schema_version", "18")
        conn.commit()
        conn.close()

        monkeypatch.setattr(deps, "_REGISTRY_FILE", tmp_path / "missing-projects.json")
        registry = ProjectRegistry()
        registry._projects["p2"] = ProjectMeta(
            project_id="p2",
            name="stale migration",
            genre="xuanhuan",
            db_path=str(db),
            created_at="2026-07-11T00:00:00",
        )
        monkeypatch.setattr(migrations, "check_and_migrate", lambda _conn: None)

        with pytest.raises(RuntimeError, match="schema 版本未就绪"):
            registry.open_conn("p2")
        assert "p2" not in registry._migrated_projects

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

    def test_v13_fresh_db_has_contract_columns_and_style_anchors(self, tmp_path):
        """新库走 schema.sql 基线，必须直接含 v13 列/表（不经迁移链）。"""
        from novelforge.db.connection import init_db
        from novelforge.db.migrations import column_exists, table_exists

        conn = init_db(tmp_path / "fresh13.db")
        for col in ("target_emotion", "opening_hook_type",
                    "hook_type", "expectation_score"):
            assert column_exists(conn, "chapter_cards", col), f"schema.sql 基线缺列 {col}"
        assert table_exists(conn, "style_anchors")
        anchor_cols = {r["name"] for r in conn.execute("PRAGMA table_info(style_anchors)")}
        assert {"id", "emotion", "title", "content", "enabled", "created_at"} <= anchor_cols
        conn.close()

    def test_v13_alters_existing_chapter_cards(self, tmp_path):
        """已有 chapter_cards 表（v12 形态）的库：v13 走 ALTER 补列且保数据。"""
        from novelforge.db.migrations import migrate, column_exists

        conn = _make_v4_db(tmp_path / "v4cc.db")
        conn.executescript("""
            CREATE TABLE chapter_cards (
                id              TEXT PRIMARY KEY,
                chapter         INTEGER NOT NULL UNIQUE,
                title           TEXT,
                pov_entity_id   TEXT,
                goal            TEXT,
                summary         TEXT,
                word_count      INTEGER,
                hook_text       TEXT,
                status          TEXT NOT NULL DEFAULT 'planned',
                draft_id        TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        conn.execute(
            "INSERT INTO chapter_cards(id, chapter, title) VALUES('cc_old', 1, '旧章')")
        conn.commit()

        migrate(conn)

        row = conn.execute("SELECT * FROM chapter_cards WHERE id='cc_old'").fetchone()
        assert row is not None and row["title"] == "旧章"
        assert row["hook_type"] is None and row["expectation_score"] is None
        assert column_exists(conn, "chapter_cards", "target_emotion")
        conn.close()

    def test_v14_fresh_db_has_objective_kr(self, tmp_path):
        """新库基线（schema.sql）直接含 v14 volumes 列。"""
        from novelforge.db.connection import init_db
        from novelforge.db.migrations import column_exists

        conn = init_db(tmp_path / "fresh14.db")
        for col in ("objective", "key_results"):
            assert column_exists(conn, "volumes", col), f"schema.sql 基线缺列 {col}"
        conn.close()

    def test_v14_alters_existing_volumes(self, tmp_path):
        """已有 volumes 表（v5 形态）的库：v14 走 ALTER 补列且保数据。"""
        from novelforge.db.migrations import migrate, column_exists

        conn = _make_v4_db(tmp_path / "v4vol.db")
        conn.executescript("""
            CREATE TABLE volumes (
                id              TEXT PRIMARY KEY,
                volume_no       INTEGER NOT NULL,
                title           TEXT NOT NULL,
                synopsis        TEXT,
                start_chapter   INTEGER,
                end_chapter     INTEGER,
                status          TEXT NOT NULL DEFAULT 'writing',
                rolling_summary TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(volume_no)
            );
        """)
        conn.execute(
            "INSERT INTO volumes(id, volume_no, title) VALUES('v1', 1, '第一卷')")
        conn.commit()

        migrate(conn)

        row = conn.execute("SELECT * FROM volumes WHERE id='v1'").fetchone()
        assert row is not None and row["title"] == "第一卷"
        assert row["objective"] is None and row["key_results"] is None
        assert column_exists(conn, "volumes", "objective")
        conn.close()

    def test_v15_fresh_db_has_scene_vec(self, tmp_path):
        """新库基线（schema.sql）直接含 v15 scene_vec 表（向量召回回退存储）。"""
        from novelforge.db.connection import init_db
        from novelforge.db.migrations import table_exists

        conn = init_db(tmp_path / "fresh15.db")
        assert table_exists(conn, "scene_vec")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(scene_vec)")}
        assert {"scene_id", "chapter", "dim", "vec_json", "model", "version"} <= cols
        conn.close()

    def test_v15_migrate_creates_scene_vec(self, tmp_path):
        """从 v4 迁移到最新版应创建 scene_vec 表。"""
        from novelforge.db.migrations import migrate, table_exists

        conn = _make_v4_db(tmp_path / "v4vec.db")
        migrate(conn)
        assert table_exists(conn, "scene_vec")
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
