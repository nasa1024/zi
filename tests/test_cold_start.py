"""MVP5 冷启动反向抽取测试（FakeProvider，无网络）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    # 确保无 API key → 使用 FakeProvider
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("NOVELFORGE_API_KEY", raising=False)
    import novelforge.app.deps as deps_mod
    import novelforge.app.autopilot_manager as ap_mod
    deps_mod._registry = None
    ap_mod._manager = None
    yield tmp_path
    deps_mod._registry = None
    ap_mod._manager = None


@pytest.fixture
def client(tmp_data):
    from novelforge.app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project(client):
    resp = client.post("/v1/projects", json={"name": "冷启动测试", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


# ── 冷启动测试 ────────────────────────────────────────────────────────────────

class TestColdStart:
    def test_cold_start_basic(self, client, project):
        """单章冷启动：返回 202，候选进入 staging。"""
        r = client.post(f"/v1/{project}/cold_start", json={
            "chapters": [
                {
                    "chapter_no": 1,
                    "text": "陆天突破了筑基境，获得了天道之眼，击败了师兄赵猛。",
                }
            ],
            "actor": "test_cold",
        })
        assert r.status_code == 202
        body = r.json()
        assert body["chapters_processed"] == 1
        assert isinstance(body["candidate_ids"], list)
        assert isinstance(body["atom_ids"], list)
        assert len(body["atom_ids"]) == 1

    def test_cold_start_multi_chapter(self, client, project):
        """多章冷启动：每章产生独立 atom。"""
        r = client.post(f"/v1/{project}/cold_start", json={
            "chapters": [
                {"chapter_no": 1, "text": "第一章内容：主角觉醒。"},
                {"chapter_no": 2, "text": "第二章内容：主角拜师。"},
                {"chapter_no": 3, "text": "第三章内容：第一次战斗。"},
            ],
            "actor": "batch_extract",
        })
        assert r.status_code == 202
        body = r.json()
        assert body["chapters_processed"] == 3
        assert len(body["atom_ids"]) == 3

    def test_cold_start_candidates_in_staging(self, client, project):
        """提案应全部以 proposed 状态进入 fact_candidates，不自动 canon。"""
        client.post(f"/v1/{project}/cold_start", json={
            "chapters": [{"chapter_no": 1, "text": "主角陆天，修炼境界：炼气一层。"}],
            "actor": "test",
        })
        # 直接查库
        from novelforge.app.deps import get_registry
        conn = get_registry().open_conn(project)
        rows = conn.execute(
            "SELECT status, source_skill FROM fact_candidates"
        ).fetchall()
        conn.close()
        # 所有 cold_start 来的候选必须是 proposed，不能是 canon
        for row in rows:
            if row["source_skill"] == "cold_extract":
                assert row["status"] == "proposed", \
                    f"cold_start 候选不应自动 canon，实际 status={row['status']}"

    def test_cold_start_atoms_cold_start_flag(self, client, project):
        """l1_atoms 中 cold_start=1。"""
        r = client.post(f"/v1/{project}/cold_start", json={
            "chapters": [{"chapter_no": 5, "text": "某章文本。"}],
            "actor": "test",
        })
        atom_ids = r.json()["atom_ids"]
        from novelforge.app.deps import get_registry
        conn = get_registry().open_conn(project)
        for aid in atom_ids:
            row = conn.execute(
                "SELECT cold_start FROM l1_atoms WHERE id=?", (aid,)
            ).fetchone()
            assert row is not None
            assert row["cold_start"] == 1
        conn.close()

    def test_cold_start_project_not_found(self, client, tmp_data):
        r = client.post("/v1/nonexistent/cold_start", json={
            "chapters": [{"chapter_no": 1, "text": "test"}],
            "actor": "x",
        })
        assert r.status_code == 404

    def test_cold_start_empty_chapters_validation(self, client, project):
        """chapters 不能为空数组（min_length=1）。"""
        r = client.post(f"/v1/{project}/cold_start", json={
            "chapters": [],
            "actor": "x",
        })
        assert r.status_code == 422
