"""L0/L1/L2 记忆闭环回归：项目隔离路径、FTS、证据原子与维护 API。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from novelforge.control_plane.orchestrator import _persist_draft
from novelforge.db.connection import init_db
from novelforge.db.l0 import load_latest_draft_text
from novelforge.memory.l1_atoms import persist_proposal_atoms
from novelforge.memory.recall import _fts_search


def test_l0_relative_path_is_resolved_per_project(tmp_path):
    db_a = tmp_path / "a" / "novel.db"
    db_b = tmp_path / "b" / "novel.db"
    db_a.parent.mkdir()
    db_b.parent.mkdir()
    a = init_db(db_a)
    b = init_db(db_b)
    try:
        assert _persist_draft(a, "甲项目唯一正文", 1, "a", str(db_a))
        assert _persist_draft(b, "乙项目唯一正文", 1, "b", str(db_b))
        assert load_latest_draft_text(a, 1) == "甲项目唯一正文"
        assert load_latest_draft_text(b, 1) == "乙项目唯一正文"
    finally:
        a.close()
        b.close()


def test_persist_draft_indexes_latest_revision_for_as_of_recall(tmp_path):
    db = tmp_path / "project" / "novel.db"
    db.parent.mkdir()
    conn = init_db(db)
    try:
        _persist_draft(conn, "第一版在青云山门遭遇守卫。", 1, "p", str(db))
        hits = _fts_search("青云山门", 1, 10, conn)
        assert len(hits) == 1
        assert hits[0]["chapter"] == 1
        assert "青云山门" in hits[0]["snippet"]
        assert _fts_search("青云山门", 0, 10, conn) == []

        _persist_draft(conn, "第二版改为进入玄铁矿洞。", 1, "p", str(db))
        assert _fts_search("青云山门", 1, 10, conn) == []
        assert "玄铁矿洞" in _fts_search("玄铁矿洞", 1, 10, conn)[0]["snippet"]
        assert conn.execute("SELECT COUNT(*) FROM drafts_fts").fetchone()[0] == 1
    finally:
        conn.close()


def test_l1_atoms_are_idempotent_and_evidence_linked(tmp_path):
    db = tmp_path / "project" / "novel.db"
    db.parent.mkdir()
    conn = init_db(db)
    try:
        text = "林枫踏入青云山门。\n他从守门弟子手中取得玄铁令。"
        draft_id = _persist_draft(conn, text, 1, "p", str(db))
        proposal = {
            "op": "add",
            "fact_type": "item",
            "entity": "林枫",
            "new": {"subject": "林枫", "predicate": "获得", "object": "玄铁令"},
            "valid_from_chapter": 1,
        }
        first = persist_proposal_atoms(
            conn, chapter=1, draft_id=draft_id, draft_text=text,
            proposals=[proposal], candidate_ids=["cand_1"],
        )
        second = persist_proposal_atoms(
            conn, chapter=1, draft_id=draft_id, draft_text=text,
            proposals=[proposal], candidate_ids=["cand_1"],
        )
        assert first == second == {"atoms": 1, "anchored": 1, "unanchored": 0}
        row = conn.execute("SELECT * FROM l1_atoms").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM l1_atoms").fetchone()[0] == 1
        assert row["draft_id"] == draft_id
        assert row["candidate_id"] == "cand_1"
        assert row["source_line_start"] == 2
        assert row["status"] == "active"
        assert json.loads(row["payload_json"])["new"]["object"] == "玄铁令"
        draft = conn.execute("SELECT l1_status FROM draft_index WHERE id=?", (draft_id,)).fetchone()
        assert draft["l1_status"] == "completed"
    finally:
        conn.close()


def test_new_revision_supersedes_old_l1_and_backfill_never_rebinds_legacy_candidate(
    tmp_path,
):
    from novelforge.memory.l1_atoms import rebuild_l1_atoms

    db = tmp_path / "revision" / "novel.db"
    db.parent.mkdir()
    conn = init_db(db)
    try:
        old_text = "旧稿中林枫取得玄铁令。"
        old_id = _persist_draft(conn, old_text, 1, "p", str(db))
        proposal = {
            "op": "add", "fact_type": "item", "entity": "林枫",
            "new": {"subject": "林枫", "predicate": "获得", "object": "玄铁令"},
            "valid_from_chapter": 1,
        }
        persist_proposal_atoms(
            conn, chapter=1, draft_id=old_id, draft_text=old_text,
            proposals=[proposal], candidate_ids=["legacy_cand"],
        )
        conn.execute(
            "INSERT INTO fact_candidates(candidate_id,proposal_json,op,fact_type,status,"
            " risk_tier,source_chapter) VALUES(?,?,?,?,?,?,?)",
            ("legacy_cand", json.dumps(proposal, ensure_ascii=False), "add", "item",
             "proposed", "low", 1),
        )
        conn.commit()

        new_id = _persist_draft(conn, "新稿改为林枫空手离开。", 1, "p", str(db))
        assert new_id != old_id
        assert conn.execute(
            "SELECT status FROM l1_atoms WHERE draft_id=?", (old_id,)
        ).fetchone()["status"] == "superseded"

        stats = rebuild_l1_atoms(conn, force=True)
        assert stats["ambiguous_legacy_candidates"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM l1_atoms WHERE draft_id=?", (new_id,)
        ).fetchone()[0] == 0
        # 历史审计仍保留，且不会被 force 全表删除。
        assert conn.execute(
            "SELECT COUNT(*) FROM l1_atoms WHERE draft_id=? AND status='superseded'",
            (old_id,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    for name in ("NOVELFORGE_ENABLE_L2", "NOVELFORGE_ENABLE_VECTOR"):
        monkeypatch.delenv(name, raising=False)
    import novelforge.app.deps as deps_mod
    deps_mod._DATA_ROOT = tmp_path
    deps_mod._REGISTRY_FILE = tmp_path / "projects.json"
    deps_mod._registry = None
    from novelforge.app.main import app
    with TestClient(app) as client:
        yield client
    deps_mod._registry = None


def test_memory_health_settings_and_rebuild_api(api_client):
    created = api_client.post(
        "/v1/projects", json={"name": "记忆闭环", "genre": "xuanhuan"}
    ).json()
    pid = created["project_id"]
    from novelforge.app.deps import get_registry
    registry = get_registry()
    conn = registry.open_conn(pid)
    try:
        text = "林枫进入青云山门，并从长老处得到玄铁令。"
        draft_id = _persist_draft(conn, text, 1, pid, created["db_path"])
        proposal = {
            "op": "add", "fact_type": "item", "entity": "林枫",
            "new": {"subject": "林枫", "predicate": "获得", "object": "玄铁令"},
            "valid_from_chapter": 1,
        }
        conn.execute(
            "INSERT INTO fact_candidates(candidate_id,proposal_json,op,fact_type,status,"
            " risk_tier,source_chapter) VALUES(?,?,?,?,?,?,?)",
            ("cand_memory", json.dumps(proposal, ensure_ascii=False), "add", "item",
             "proposed", "low", 1),
        )
        conn.commit()
        assert draft_id
    finally:
        conn.close()

    settings = api_client.get(f"/v1/{pid}/memory/settings")
    assert settings.status_code == 200
    assert settings.json()["enable_l2"] is True
    assert settings.json()["enable_vector"] is True

    patched = api_client.patch(
        f"/v1/{pid}/memory/settings", json={"l2_every_n_chapters": 3}
    )
    assert patched.status_code == 200
    assert patched.json()["l2_every_n_chapters"] == 3

    rebuilt = api_client.post(
        f"/v1/{pid}/memory/rebuild",
        json={"rebuild_l0": True, "rebuild_l1": True, "rebuild_l2": True,
              "rebuild_vectors": True, "force": True},
    )
    assert rebuilt.status_code == 200, rebuilt.text
    result = rebuilt.json()["result"]
    assert result["l0"]["drafts"] == 1
    assert result["l1"]["atoms"] == 1
    assert result["l2"]["processed"] == 1
    assert result["vectors"]["indexed"] >= 1

    health = api_client.get(f"/v1/{pid}/memory/health")
    assert health.status_code == 200
    layers = health.json()["layers"]
    assert layers["l0"]["coverage_pct"] == 100.0
    assert layers["l1"]["coverage_pct"] == 100.0
    assert layers["l2"]["coverage_pct"] == 100.0

    recall = api_client.post(
        f"/v1/{pid}/recall",
        json={"entities": [], "as_of_chapter": 1, "keyword_query": "青云山门", "top_k": 20},
    )
    assert recall.status_code == 200
    sources = {item["source"] for item in recall.json()["items"]}
    assert "bm25" in sources
    assert "l2_scene" in sources


def test_project_and_env_memory_settings_reject_invalid_values(tmp_path, monkeypatch):
    from novelforge.config import (
        NovelForgeConfig, apply_project_memory_settings, apply_runtime_env,
        memory_env_overrides,
    )

    conn = init_db(tmp_path / "settings.db")
    try:
        conn.execute(
            "INSERT INTO meta_kv(key,value) VALUES('memory_settings',?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps({
                "enable_l2": "false",
                "enable_vector": 1,
                "l2_max_scenes_per_chapter": -5,
                "vector_top_k": "9",
            }),),
        )
        conn.commit()
        cfg = NovelForgeConfig()
        apply_project_memory_settings(cfg, conn)
        assert cfg.memory.enable_l2 is True
        assert cfg.recall.enable_vector is True
        assert cfg.memory.l2_max_scenes_per_chapter == 6
        assert cfg.memory.vector_top_k == 8

        monkeypatch.setenv("NOVELFORGE_ENABLE_L2", "typo")
        monkeypatch.setenv("NOVELFORGE_L2_MAX_SCENES", "-3")
        monkeypatch.setenv("NOVELFORGE_VECTOR_TOP_K", "999")
        monkeypatch.setenv("NOVELFORGE_RRF_K", "-1")
        monkeypatch.setenv("NOVELFORGE_EMBEDDING_DIM", "-8")
        apply_runtime_env(cfg)
        assert cfg.memory.enable_l2 is True
        assert cfg.memory.l2_max_scenes_per_chapter == 6
        assert cfg.memory.vector_top_k == 8
        assert cfg.memory.rrf_k == 60
        assert cfg.memory.embedding_dim == 256
        assert "enable_l2" not in memory_env_overrides()
        assert "l2_max_scenes_per_chapter" not in memory_env_overrides()
        assert "vector_top_k" not in memory_env_overrides()
    finally:
        conn.close()


def test_embedding_env_never_reuses_rest_auth_secret(monkeypatch):
    from novelforge.config import NovelForgeConfig, apply_embedding_env

    monkeypatch.setenv("NOVELFORGE_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("NOVELFORGE_API_KEY", "rest-auth-secret")
    for name in ("NOVELFORGE_EMBEDDING_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    cfg = NovelForgeConfig()
    apply_embedding_env(cfg)
    assert cfg.memory.embedding_api_key is None


def test_force_rebuild_embeds_each_scene_once_and_preserves_old_index_on_failure(
    tmp_path, monkeypatch,
):
    from novelforge.config import NovelForgeConfig
    from novelforge.memory.maintenance import rebuild_memory
    import novelforge.memory.maintenance as maintenance_mod

    db = tmp_path / "rebuild" / "novel.db"
    db.parent.mkdir()
    conn = init_db(db)
    try:
        text = "一段用于验证维护重建事务与 embedding 调用次数的场景正文。"
        assert _persist_draft(conn, text, 1, "p", str(db))

        class CountingEmbedder:
            name = "counting"
            version = "v1"
            dim = 3

            def __init__(self):
                self.calls = 0

            def embed(self, texts):
                self.calls += 1
                return [[1.0, 0.0, 0.0] for _ in texts]

        counting = CountingEmbedder()
        monkeypatch.setattr(maintenance_mod, "build_embedder", lambda *a, **k: counting)
        cfg = NovelForgeConfig(db_path=str(db))
        cfg.memory.enable_l2 = True
        cfg.recall.enable_vector = True
        result = rebuild_memory(
            conn, cfg, rebuild_l0=False, rebuild_l1=False,
            rebuild_l2=True, rebuild_vectors=True, force=True,
        )
        assert result["l2"]["processed"] == 1
        assert result["vectors"]["indexed"] == 1
        assert counting.calls == 1

        conn.execute("UPDATE l2_scenes SET summary='保留的高质量摘要'")
        conn.commit()
        before_vec = conn.execute("SELECT vec_json FROM scene_vec").fetchone()["vec_json"]

        class BrokenEmbedder:
            name = "broken"
            version = "v2"
            dim = 3

            def embed(self, texts):
                raise RuntimeError("embedding down")

        monkeypatch.setattr(
            maintenance_mod, "build_embedder", lambda *a, **k: BrokenEmbedder()
        )
        with pytest.raises(RuntimeError, match="embedding down"):
            rebuild_memory(
                conn, cfg, rebuild_l0=False, rebuild_l1=False,
                rebuild_l2=True, rebuild_vectors=True, force=True,
            )
        assert conn.execute("SELECT summary FROM l2_scenes").fetchone()["summary"] == "保留的高质量摘要"
        assert conn.execute("SELECT vec_json FROM scene_vec").fetchone()["vec_json"] == before_vec
    finally:
        conn.close()


def test_memory_health_counts_only_current_vectors_and_reports_orphans(tmp_path):
    from novelforge.config import NovelForgeConfig
    from novelforge.memory.embedding import HashEmbedder
    from novelforge.memory.l2_aggregate import aggregate_chapter_scenes
    from novelforge.memory.maintenance import memory_health

    db = tmp_path / "health" / "novel.db"
    db.parent.mkdir()
    conn = init_db(db)
    try:
        assert _persist_draft(conn, "山门旧事场景。", 1, "p", str(db))
        aggregate_chapter_scenes(
            conn, None, 1, "山门旧事场景。", embedder=HashEmbedder(dim=64)
        )
        conn.execute(
            "INSERT INTO scene_vec(scene_id,chapter,dim,vec_json,model,version)"
            " VALUES('orphan',1,128,'[]','hash','v1')"
        )
        conn.commit()

        cfg = NovelForgeConfig(db_path=str(db))
        cfg.memory.embedding_dim = 128
        cfg.recall.enable_vector = True
        health = memory_health(conn, cfg)
        details = health["layers"]["l2"]["details"]
        assert details["vectors"] == 0
        assert details["stale_vectors"] == 1
        assert details["orphan_vectors"] == 1
        assert details["vector_coverage_pct"] == 0.0
    finally:
        conn.close()
