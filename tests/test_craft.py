"""一致性豁免 + 伏笔管理 API 测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
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
    r = client.post("/v1/projects", json={"name": "测试", "genre": "xuanhuan"})
    assert r.status_code == 201
    return r.json()["project_id"]


# ── Exemptions ────────────────────────────────────────────────────────────────

class TestExemptions:
    def test_create_exemption(self, client, project):
        r = client.post(f"/v1/{project}/exemptions", json={
            "scope": "fact",
            "scope_ref": "fact-001",
            "exempt_tag": "power_decrease",
            "reason": "主角故意封印修为",
            "rule_codes": ["MONO_POWER"],
            "valid_from_chapter": 5,
            "valid_to_chapter": 10,
            "created_by": "author",
        })
        assert r.status_code == 201
        body = r.json()
        assert body["exempt_tag"] == "power_decrease"
        assert body["rule_codes"] == ["MONO_POWER"]
        assert body["valid_from_chapter"] == 5

    def test_list_exemptions_empty(self, client, project):
        r = client.get(f"/v1/{project}/exemptions")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_exemptions(self, client, project):
        client.post(f"/v1/{project}/exemptions", json={
            "scope": "entity", "scope_ref": "e1",
            "exempt_tag": "timeline_jump", "reason": "穿越",
        })
        client.post(f"/v1/{project}/exemptions", json={
            "scope": "chapter", "scope_ref": "3",
            "exempt_tag": "item_loss", "reason": "道具被夺",
        })
        r = client.get(f"/v1/{project}/exemptions")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_list_exemptions_filter_by_scope(self, client, project):
        client.post(f"/v1/{project}/exemptions", json={
            "scope": "entity", "scope_ref": "e1",
            "exempt_tag": "x", "reason": "r",
        })
        client.post(f"/v1/{project}/exemptions", json={
            "scope": "chapter", "scope_ref": "2",
            "exempt_tag": "y", "reason": "r",
        })
        r = client.get(f"/v1/{project}/exemptions?scope=entity")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["scope"] == "entity"

    def test_delete_exemption(self, client, project):
        r1 = client.post(f"/v1/{project}/exemptions", json={
            "scope": "global", "scope_ref": "*",
            "exempt_tag": "any", "reason": "test",
        })
        eid = r1.json()["id"]
        r2 = client.delete(f"/v1/{project}/exemptions/{eid}")
        assert r2.status_code == 204
        r3 = client.get(f"/v1/{project}/exemptions")
        assert len(r3.json()) == 0

    def test_delete_nonexistent_exemption(self, client, project):
        r = client.delete(f"/v1/{project}/exemptions/99999")
        assert r.status_code == 404

    def test_exemption_project_not_found(self, client, tmp_data):
        r = client.get("/v1/nonexistent/exemptions")
        assert r.status_code == 404


# ── Foreshadow ────────────────────────────────────────────────────────────────

class TestForeshadow:
    def test_create_foreshadow(self, client, project):
        r = client.post(f"/v1/{project}/foreshadow", json={
            "label": "神秘令牌",
            "description": "陆天从废墟中找到的奇异令牌，来源不明",
            "planted_chapter": 3,
            "due_chapter": 50,
            "importance": 5,
        })
        assert r.status_code == 201
        body = r.json()
        assert body["label"] == "神秘令牌"
        assert body["state"] == "planted"
        assert body["planted_chapter"] == 3
        assert body["importance"] == 5

    def test_list_foreshadow_empty(self, client, project):
        r = client.get(f"/v1/{project}/foreshadow")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_foreshadow(self, client, project):
        client.post(f"/v1/{project}/foreshadow", json={
            "label": "fs1", "description": "d1", "planted_chapter": 1,
        })
        client.post(f"/v1/{project}/foreshadow", json={
            "label": "fs2", "description": "d2", "planted_chapter": 5,
        })
        r = client.get(f"/v1/{project}/foreshadow")
        assert r.status_code == 200
        assert len(r.json()) == 2
        # 按 planted_chapter 排序
        chapters = [x["planted_chapter"] for x in r.json()]
        assert chapters == sorted(chapters)

    def test_list_foreshadow_filter_by_state(self, client, project):
        r1 = client.post(f"/v1/{project}/foreshadow", json={
            "label": "planted_fs", "description": "d", "planted_chapter": 1,
        })
        fs_id = r1.json()["id"]
        client.patch(f"/v1/{project}/foreshadow/{fs_id}", json={"state": "paid_off", "paid_off_chapter": 10})
        client.post(f"/v1/{project}/foreshadow", json={
            "label": "another", "description": "d", "planted_chapter": 2,
        })
        r = client.get(f"/v1/{project}/foreshadow?state=planted")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["label"] == "another"

    def test_get_foreshadow(self, client, project):
        r1 = client.post(f"/v1/{project}/foreshadow", json={
            "label": "target", "description": "d", "planted_chapter": 2,
        })
        fs_id = r1.json()["id"]
        r2 = client.get(f"/v1/{project}/foreshadow/{fs_id}")
        assert r2.status_code == 200
        assert r2.json()["id"] == fs_id

    def test_get_foreshadow_not_found(self, client, project):
        r = client.get(f"/v1/{project}/foreshadow/nonexistent")
        assert r.status_code == 404

    def test_update_foreshadow_state(self, client, project):
        r1 = client.post(f"/v1/{project}/foreshadow", json={
            "label": "伏笔", "description": "d", "planted_chapter": 1,
        })
        fs_id = r1.json()["id"]
        r2 = client.patch(f"/v1/{project}/foreshadow/{fs_id}", json={
            "state": "paid_off",
            "paid_off_chapter": 30,
        })
        assert r2.status_code == 200
        assert r2.json()["state"] == "paid_off"
        assert r2.json()["paid_off_chapter"] == 30

    def test_delete_foreshadow(self, client, project):
        r1 = client.post(f"/v1/{project}/foreshadow", json={
            "label": "del_fs", "description": "d", "planted_chapter": 1,
        })
        fs_id = r1.json()["id"]
        r2 = client.delete(f"/v1/{project}/foreshadow/{fs_id}")
        assert r2.status_code == 204
        r3 = client.get(f"/v1/{project}/foreshadow/{fs_id}")
        assert r3.status_code == 404

    def test_overdue_foreshadow(self, client, project):
        # due_chapter=5 的伏笔在 as_of=10 时应过期
        r1 = client.post(f"/v1/{project}/foreshadow", json={
            "label": "过期伏笔", "description": "d", "planted_chapter": 1,
            "due_chapter": 5,
        })
        # due_chapter=20 的伏笔不应在 as_of=10 时出现
        client.post(f"/v1/{project}/foreshadow", json={
            "label": "未过期", "description": "d", "planted_chapter": 1,
            "due_chapter": 20,
        })
        r2 = client.get(f"/v1/{project}/foreshadow/overdue?as_of_chapter=10")
        assert r2.status_code == 200
        labels = [x["label"] for x in r2.json()]
        assert "过期伏笔" in labels
        assert "未过期" not in labels

    def test_foreshadow_project_not_found(self, client, tmp_data):
        r = client.get("/v1/nonexistent/foreshadow")
        assert r.status_code == 404
