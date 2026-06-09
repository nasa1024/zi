"""MVP3 测试：FastAPI 端点契约 + revert + circuit breaker + bible render。

全部使用 in-memory SQLite + FakeProvider，无网络调用。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    """把 NOVELFORGE_DATA 指向临时目录，隔离每个测试的项目注册表。"""
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    # 重置单例
    import novelforge.app.deps as deps_mod
    deps_mod._registry = None
    yield tmp_path
    deps_mod._registry = None


@pytest.fixture
def client(tmp_data):
    from novelforge.app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project(client):
    """创建一个测试项目，返回 project_id。"""
    resp = client.post("/v1/projects", json={"name": "测试小说", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


# ── 辅助 ─────────────────────────────────────────────────────────────────────

def _seed_entity(conn, name="主角"):
    from novelforge.ids import new_id
    eid = new_id("ent")
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type) VALUES(?,?,?)",
        (eid, name, "character"),
    )
    conn.commit()
    return eid


def _seed_canon_fact(conn, eid, predicate="境界", obj="炼气一层", fact_type="character_trait"):
    from novelforge.ids import new_id
    fid = new_id("fact")
    rid = new_id("rev")
    # fact
    conn.execute(
        "INSERT INTO facts(id, entity_id, subject, fact_type, predicate, object,"
        "  status, valid_from_chapter, current_revision_id)"
        " VALUES(?,?,?,?,?,?,'canon',1,?)",
        (fid, eid, eid, fact_type, predicate, obj, rid),
    )
    # fact_revision (enables revert)
    conn.execute(
        "INSERT INTO fact_revisions(id, fact_id, revision_no, op, new_object, new_status,"
        "  valid_from_chapter, reason, actor, policy_mode)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (rid, fid, 1, "add", obj, "canon", 1, "init", "system", "human_gate"),
    )
    conn.commit()
    return fid, rid


def _open_project_conn(client, project_id: str) -> sqlite3.Connection:
    """通过 registry 打开项目连接（测试用）。"""
    from novelforge.app.deps import get_registry
    reg = get_registry()
    return reg.open_conn(project_id)


# ── 1. Projects CRUD ─────────────────────────────────────────────────────────

class TestProjects:
    def test_create_returns_201(self, client, tmp_data):
        r = client.post("/v1/projects", json={"name": "新书", "genre": "wuxia"})
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "新书"
        assert body["genre"] == "wuxia"
        assert "project_id" in body

    def test_list_projects(self, client, project):
        r = client.get("/v1/projects")
        assert r.status_code == 200
        ids = [p["project_id"] for p in r.json()]
        assert project in ids

    def test_get_project(self, client, project):
        r = client.get(f"/v1/projects/{project}")
        assert r.status_code == 200
        assert r.json()["project_id"] == project

    def test_get_nonexistent_returns_404(self, client, tmp_data):
        r = client.get("/v1/projects/nonexistent_id")
        assert r.status_code == 404

    def test_archive_project(self, client, project):
        r = client.delete(f"/v1/projects/{project}")
        assert r.status_code == 204
        # 归档后不出现在列表
        r2 = client.get("/v1/projects")
        ids = [p["project_id"] for p in r2.json()]
        assert project not in ids


# ── 2. Health ─────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health(self, client, tmp_data):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# ── 3. Capture ────────────────────────────────────────────────────────────────

class TestCapture:
    def test_capture_returns_candidate_ids(self, client, project):
        r = client.post(f"/v1/{project}/capture", json={
            "source_chapter": 1,
            "source_kind": "manual",
            "proposals": [
                {"op": "add", "fact_type": "style", "entity": None,
                 "new": {"predicate": "hair", "object": "black"}, "valid_from_chapter": 1},
            ],
        })
        assert r.status_code == 202
        body = r.json()
        assert len(body["candidate_ids"]) == 1

    def test_capture_multiple_proposals(self, client, project):
        r = client.post(f"/v1/{project}/capture", json={
            "source_chapter": 2,
            "source_kind": "draft",
            "proposals": [
                {"op": "add", "fact_type": "style", "new": {}, "valid_from_chapter": 2},
                {"op": "add", "fact_type": "misc", "new": {}, "valid_from_chapter": 2},
            ],
        })
        assert r.status_code == 202
        assert len(r.json()["candidate_ids"]) == 2


# ── 4. Reviews ────────────────────────────────────────────────────────────────

class TestReviews:
    def _add_candidate(self, conn, project_id, fact_type="style"):
        from novelforge.ids import new_id
        cid = new_id("cand")
        prop = json.dumps({"op": "add", "fact_type": fact_type,
                           "new": {"subject": "x", "predicate": "p", "object": "o"},
                           "valid_from_chapter": 1})
        conn.execute(
            "INSERT INTO fact_candidates"
            "(candidate_id, op, entity_id, fact_type, proposal_json, status, risk_tier, source_chapter)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (cid, "add", None, fact_type, prop, "pending_review", "low", 1),
        )
        from novelforge.ids import new_id as nid
        conn.execute(
            "INSERT INTO review_queue(id, candidate_id, priority, risk_tier, reason, status)"
            " VALUES(?,?,?,?,?,?)",
            (nid("rq"), cid, 100, "low", "policy_review", "pending"),
        )
        conn.commit()
        return cid

    def test_list_reviews(self, client, project):
        conn = _open_project_conn(client, project)
        cid = self._add_candidate(conn, project)
        conn.close()

        r = client.get(f"/v1/{project}/reviews")
        assert r.status_code == 200
        ids = [item["candidate_id"] for item in r.json()]
        assert cid in ids

    def test_approve_review(self, client, project):
        conn = _open_project_conn(client, project)
        # 需要一个实体来支持 commit（entity-less style fact）
        cid = self._add_candidate(conn, project, fact_type="style")
        conn.close()

        r = client.post(f"/v1/{project}/reviews/{cid}/approve",
                        json={"actor": "test_user"})
        assert r.status_code == 200
        body = r.json()
        assert body["candidate_id"] == cid
        assert "fact_id" in body

    def test_reject_review(self, client, project):
        conn = _open_project_conn(client, project)
        cid = self._add_candidate(conn, project)
        conn.close()

        r = client.post(f"/v1/{project}/reviews/{cid}/reject",
                        json={"actor": "test_user", "reason": "证据不足"})
        assert r.status_code == 204

    def test_batch_approve(self, client, project):
        conn = _open_project_conn(client, project)
        cids = [self._add_candidate(conn, project) for _ in range(2)]
        conn.close()

        r = client.post(f"/v1/{project}/reviews/batch_approve",
                        json={"candidate_ids": cids, "actor": "batch_user"})
        assert r.status_code == 200
        body = r.json()
        assert len(body["approved"]) + len(body["skipped"]) == 2


# ── 5. Revert ─────────────────────────────────────────────────────────────────

class TestRevert:
    def test_revert_fact(self, client, project):
        conn = _open_project_conn(client, project)
        eid = _seed_entity(conn)
        fid, rid1 = _seed_canon_fact(conn, eid, obj="炼气一层")

        # 追加第二次修订（有前一版本才能 revert）
        from novelforge.ids import new_id
        rid2 = new_id("rev")
        conn.execute(
            "INSERT INTO fact_revisions(id, fact_id, revision_no, op,"
            "  old_object, new_object, old_status, new_status,"
            "  valid_from_chapter, reason, actor, policy_mode)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid2, fid, 2, "update", "炼气一层", "炼气二层",
             "canon", "canon", 3, "upgrade", "system", "human_gate"),
        )
        conn.execute(
            "UPDATE facts SET object='炼气二层', current_revision_id=?, version=1 WHERE id=?",
            (rid2, fid),
        )
        conn.commit()
        conn.close()

        r = client.post(f"/v1/{project}/facts/{fid}/revert",
                        json={"actor": "editor", "reason": "境界写错了"})
        assert r.status_code == 200
        body = r.json()
        assert body["fact_id"] == fid
        assert "promotion_log_id" in body

    def test_revert_nonexistent_fact(self, client, project):
        r = client.post(f"/v1/{project}/facts/nonexistent/revert",
                        json={"actor": "editor", "reason": "测试"})
        assert r.status_code == 404


# ── 6. Bible render ───────────────────────────────────────────────────────────

class TestBible:
    def test_bible_empty_db(self, client, project):
        r = client.get(f"/v1/{project}/bible")
        assert r.status_code == 200
        body = r.json()
        assert body["is_readonly"] is True
        assert "Story Bible" in body["content"]

    def test_bible_with_facts(self, client, project):
        conn = _open_project_conn(client, project)
        eid = _seed_entity(conn, "林尘")
        _seed_canon_fact(conn, eid, predicate="境界", obj="金丹期")
        conn.close()

        r = client.get(f"/v1/{project}/bible")
        assert r.status_code == 200
        content = r.json()["content"]
        assert "林尘" in content
        assert "金丹期" in content

    def test_bible_json_format(self, client, project):
        r = client.get(f"/v1/{project}/bible?format=json")
        assert r.status_code == 200
        # content 是合法 JSON 字符串
        parsed = json.loads(r.json()["content"])
        assert "entities" in parsed


# ── 7. State query ────────────────────────────────────────────────────────────

class TestState:
    def test_state_query(self, client, project):
        conn = _open_project_conn(client, project)
        eid = _seed_entity(conn, "主角")
        from novelforge.ids import new_id
        pr_id = new_id("pr")
        conn.execute(
            "INSERT INTO power_ranks(id, system_name, rank_name, rank_order) VALUES(?,?,?,?)",
            (pr_id, "修仙", "炼气一层", 1),
        )
        conn.execute(
            "INSERT INTO character_power_log(id, entity_id, system_name, rank_id, rank_order,"
            " change_chapter, change_type) VALUES(?,?,?,?,?,?,?)",
            (new_id("cpl"), eid, "修仙", pr_id, 1, 1, "init"),
        )
        conn.commit()
        conn.close()

        r = client.post(f"/v1/{project}/state",
                        json={"as_of_chapter": 5})
        assert r.status_code == 200
        body = r.json()
        assert body["as_of_chapter"] == 5
        assert "主角" in body["power_ranks"]
        assert body["power_ranks"]["主角"] == "炼气一层"


# ── 8. Circuit breaker ────────────────────────────────────────────────────────

class TestCircuitBreaker:
    def test_revise_rounds_limit(self):
        from novelforge.control_plane.budget import BudgetLedger, CircuitBreaker, CircuitTripped
        ledger = BudgetLedger(max_tokens=999999, max_usd=999.0, max_revise_rounds=2)
        cb = CircuitBreaker(ledger)

        ledger.charge_revise_round()
        ledger.charge_revise_round()
        # 第3次 guard 应触发 CircuitTripped
        with pytest.raises(CircuitTripped) as exc_info:
            cb.guard(100, 100, "deepseek-v4-pro")
        assert exc_info.value.reason == "revise_rounds"

    def test_token_limit(self):
        from novelforge.control_plane.budget import BudgetLedger, CircuitBreaker, CircuitTripped
        ledger = BudgetLedger(max_tokens=1000, max_usd=999.0)
        cb = CircuitBreaker(ledger)
        with pytest.raises(CircuitTripped) as exc_info:
            cb.guard(600, 500, "deepseek-v4-pro")
        assert exc_info.value.reason == "tokens"

    def test_no_trip_within_budget(self):
        from novelforge.control_plane.budget import BudgetLedger, CircuitBreaker
        ledger = BudgetLedger(max_tokens=100_000, max_usd=10.0, max_revise_rounds=3)
        cb = CircuitBreaker(ledger)
        cb.guard(1000, 1000, "deepseek-v4-flash")  # 不应抛出


# ── 9. Pipeline run（FakeProvider）───────────────────────────────────────────

class TestPipelineRun:
    def test_pipeline_run_fake(self, client, project):
        """用 FakeProvider 跑 pipeline，验证端点不崩溃。"""
        r = client.post(f"/v1/{project}/pipeline/run", json={
            "chapter_no": 1,
            "chapter_goal": "主角觉醒",
        })
        # FakeProvider 没有 api_key，应返回 200（outcome.ok 可能 False 但端点不崩溃）
        assert r.status_code == 200
        body = r.json()
        assert "run_id" in body
        assert body["chapter_no"] == 1
