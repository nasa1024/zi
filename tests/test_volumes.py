"""MVP5 多卷/分支管理测试（全部 FakeProvider，无网络）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# ── fixtures ──────────────────────────────────────────────────────────────────

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
    resp = client.post("/v1/projects", json={"name": "测试小说", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


# ── Volume CRUD ───────────────────────────────────────────────────────────────

class TestVolumeCRUD:
    def test_create_volume(self, client, project):
        r = client.post(f"/v1/{project}/volumes", json={
            "volume_no": 1,
            "title": "第一卷：凡尘起点",
            "synopsis": "主角觉醒天道血脉",
            "start_chapter": 1,
            "end_chapter": 50,
        })
        assert r.status_code == 201
        body = r.json()
        assert body["volume_no"] == 1
        assert body["title"] == "第一卷：凡尘起点"
        assert body["status"] == "writing"
        assert "id" in body

    def test_create_duplicate_volume_no(self, client, project):
        client.post(f"/v1/{project}/volumes", json={"volume_no": 1, "title": "卷一"})
        r = client.post(f"/v1/{project}/volumes", json={"volume_no": 1, "title": "重复卷一"})
        assert r.status_code == 409

    def test_list_volumes_empty(self, client, project):
        r = client.get(f"/v1/{project}/volumes")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_volumes_ordered(self, client, project):
        client.post(f"/v1/{project}/volumes", json={"volume_no": 3, "title": "卷三"})
        client.post(f"/v1/{project}/volumes", json={"volume_no": 1, "title": "卷一"})
        client.post(f"/v1/{project}/volumes", json={"volume_no": 2, "title": "卷二"})
        r = client.get(f"/v1/{project}/volumes")
        assert r.status_code == 200
        nos = [v["volume_no"] for v in r.json()]
        assert nos == [1, 2, 3]

    def test_get_volume(self, client, project):
        client.post(f"/v1/{project}/volumes", json={"volume_no": 1, "title": "卷一"})
        r = client.get(f"/v1/{project}/volumes/1")
        assert r.status_code == 200
        assert r.json()["volume_no"] == 1

    def test_get_volume_not_found(self, client, project):
        r = client.get(f"/v1/{project}/volumes/999")
        assert r.status_code == 404

    def test_update_volume(self, client, project):
        client.post(f"/v1/{project}/volumes", json={"volume_no": 1, "title": "旧标题"})
        r = client.patch(f"/v1/{project}/volumes/1", json={
            "title": "新标题",
            "status": "completed",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["title"] == "新标题"
        assert body["status"] == "completed"

    def test_update_volume_no_change(self, client, project):
        """空 PATCH 不报错"""
        client.post(f"/v1/{project}/volumes", json={"volume_no": 1, "title": "卷一"})
        r = client.patch(f"/v1/{project}/volumes/1", json={})
        assert r.status_code == 200

    def test_delete_volume(self, client, project):
        client.post(f"/v1/{project}/volumes", json={"volume_no": 1, "title": "待删卷"})
        r = client.delete(f"/v1/{project}/volumes/1")
        assert r.status_code == 204
        # 确认已删除
        r2 = client.get(f"/v1/{project}/volumes/1")
        assert r2.status_code == 404

    def test_delete_volume_not_found(self, client, project):
        r = client.delete(f"/v1/{project}/volumes/999")
        assert r.status_code == 404

    def test_volume_project_not_found(self, client, tmp_data):
        r = client.get("/v1/nonexistent/volumes")
        assert r.status_code == 404


# ── Branch CRUD ───────────────────────────────────────────────────────────────

class TestBranchCRUD:
    def test_create_branch(self, client, project):
        r = client.post(f"/v1/{project}/branches", json={
            "branch_name": "if_ending_A",
            "fork_chapter": 50,
            "description": "主角选择隐退的 IF 结局",
        })
        assert r.status_code == 201
        body = r.json()
        assert body["branch_name"] == "if_ending_A"
        assert body["fork_chapter"] == 50
        assert body["status"] == "active"
        assert body["base_branch_id"] is None

    def test_create_branch_with_parent(self, client, project):
        r1 = client.post(f"/v1/{project}/branches", json={
            "branch_name": "main_alt",
            "fork_chapter": 10,
        })
        parent_id = r1.json()["id"]

        r2 = client.post(f"/v1/{project}/branches", json={
            "branch_name": "sub_alt",
            "fork_chapter": 20,
            "base_branch_id": parent_id,
        })
        assert r2.status_code == 201
        assert r2.json()["base_branch_id"] == parent_id

    def test_create_branch_invalid_parent(self, client, project):
        r = client.post(f"/v1/{project}/branches", json={
            "branch_name": "orphan",
            "fork_chapter": 10,
            "base_branch_id": "nonexistent-id",
        })
        assert r.status_code == 404

    def test_create_duplicate_branch_name(self, client, project):
        client.post(f"/v1/{project}/branches", json={"branch_name": "dup", "fork_chapter": 1})
        r = client.post(f"/v1/{project}/branches", json={"branch_name": "dup", "fork_chapter": 2})
        assert r.status_code == 409

    def test_list_branches_empty(self, client, project):
        r = client.get(f"/v1/{project}/branches")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_branches(self, client, project):
        client.post(f"/v1/{project}/branches", json={"branch_name": "br1", "fork_chapter": 1})
        client.post(f"/v1/{project}/branches", json={"branch_name": "br2", "fork_chapter": 2})
        r = client.get(f"/v1/{project}/branches")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_get_branch(self, client, project):
        r1 = client.post(f"/v1/{project}/branches", json={"branch_name": "target", "fork_chapter": 5})
        br_id = r1.json()["id"]
        r2 = client.get(f"/v1/{project}/branches/{br_id}")
        assert r2.status_code == 200
        assert r2.json()["id"] == br_id

    def test_get_branch_not_found(self, client, project):
        r = client.get(f"/v1/{project}/branches/nonexistent")
        assert r.status_code == 404

    def test_update_branch_status(self, client, project):
        r1 = client.post(f"/v1/{project}/branches", json={"branch_name": "to_merge", "fork_chapter": 3})
        br_id = r1.json()["id"]
        r2 = client.patch(f"/v1/{project}/branches/{br_id}", json={"status": "merged"})
        assert r2.status_code == 200
        assert r2.json()["status"] == "merged"

    def test_delete_branch(self, client, project):
        r1 = client.post(f"/v1/{project}/branches", json={"branch_name": "to_del", "fork_chapter": 5})
        br_id = r1.json()["id"]
        r2 = client.delete(f"/v1/{project}/branches/{br_id}")
        assert r2.status_code == 204
        r3 = client.get(f"/v1/{project}/branches/{br_id}")
        assert r3.status_code == 404

    def test_branch_project_not_found(self, client, tmp_data):
        r = client.get("/v1/nonexistent/branches")
        assert r.status_code == 404
