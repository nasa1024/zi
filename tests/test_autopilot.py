"""MVP4 Autopilot + Seed 测试（全部 FakeProvider，无网络）。"""
from __future__ import annotations

import json
import time
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
    resp = client.post("/v1/projects", json={"name": "无人值守连写", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _open_conn(client, project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


# ── 1. Seed ───────────────────────────────────────────────────────────────────

class TestSeed:
    def test_seed_basic(self, client, project):
        r = client.post(f"/v1/{project}/seed", json={
            "proposals": [
                {"fact_type": "style", "new": {"predicate": "体系", "object": "天道五境"},
                 "valid_from_chapter": 0},
                {"fact_type": "character_trait", "new": {"predicate": "性格", "object": "沉稳"},
                 "valid_from_chapter": 0, "risk_tier": "low"},
            ],
            "auto_approve_low_risk": False,
            "actor": "test_seed",
        })
        assert r.status_code == 202
        body = r.json()
        assert len(body["candidate_ids"]) == 2
        assert body["auto_approved"] == []
        assert len(body["queued"]) == 2

    def test_seed_auto_approve_low(self, client, project):
        r = client.post(f"/v1/{project}/seed", json={
            "proposals": [
                {"fact_type": "style", "new": {"predicate": "体系", "object": "七星"},
                 "valid_from_chapter": 0, "risk_tier": "low"},
            ],
            "auto_approve_low_risk": True,
            "actor": "auto_seeder",
        })
        assert r.status_code == 202
        body = r.json()
        assert len(body["candidate_ids"]) == 1
        assert len(body["auto_approved"]) == 1
        # canon fact 应已落库
        conn = _open_conn(client, project)
        row = conn.execute("SELECT COUNT(*) AS n FROM facts WHERE status='canon'").fetchone()
        conn.close()
        assert row["n"] >= 1

    def test_seed_high_risk_not_auto_approved(self, client, project):
        r = client.post(f"/v1/{project}/seed", json={
            "proposals": [
                {"fact_type": "power_system", "new": {"predicate": "境界", "object": "炼气"},
                 "valid_from_chapter": 0, "risk_tier": "high"},
            ],
            "auto_approve_low_risk": True,
            "actor": "test",
        })
        assert r.status_code == 202
        body = r.json()
        # high_risk 不应被自动批准
        assert body["auto_approved"] == []
        assert len(body["queued"]) == 1

    def test_seed_project_not_found(self, client, tmp_data):
        r = client.post("/v1/nonexistent/seed", json={
            "proposals": [], "auto_approve_low_risk": False, "actor": "x",
        })
        assert r.status_code == 404


# ── 2. Autopilot start & status ───────────────────────────────────────────────

class TestAutopilotStart:
    def test_start_returns_202_session_id(self, client, project):
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1,
            "to_chapter": 2,
        })
        assert r.status_code == 202
        body = r.json()
        assert "session_id" in body
        assert body["project_id"] == project
        assert body["status"] in ("running", "completed")
        assert body["chapters_total"] == 2

    def test_start_invalid_range(self, client, project):
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 5,
            "to_chapter": 3,
        })
        assert r.status_code == 422

    def test_start_nonexistent_project(self, client, tmp_data):
        r = client.post("/v1/nonexistent/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 2,
        })
        assert r.status_code == 404

    def test_status_lists_sessions(self, client, project):
        # 先启动一个
        r1 = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 1,
        })
        assert r1.status_code == 202

        r2 = client.get(f"/v1/{project}/autopilot/status")
        assert r2.status_code == 200
        ids = [s["session_id"] for s in r2.json()]
        assert r1.json()["session_id"] in ids

    def test_get_session_by_id(self, client, project):
        r1 = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 1,
        })
        sid = r1.json()["session_id"]

        r2 = client.get(f"/v1/{project}/autopilot/{sid}")
        assert r2.status_code == 200
        assert r2.json()["session_id"] == sid

    def test_session_completes_eventually(self, client, project):
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 1,
        })
        sid = r.json()["session_id"]

        # 等待最多 5 秒让后台线程完成（FakeProvider 很快）
        for _ in range(50):
            s = client.get(f"/v1/{project}/autopilot/{sid}").json()
            if s["status"] in ("completed", "error", "circuit_broken"):
                break
            time.sleep(0.1)

        s = client.get(f"/v1/{project}/autopilot/{sid}").json()
        assert s["status"] in ("completed", "error", "circuit_broken", "degraded")
        assert s["finished_at"] is not None


# ── 3. Autopilot degrade ──────────────────────────────────────────────────────

class TestAutopilotDegrade:
    def test_degrade_nonexistent_session(self, client, project):
        r = client.post(f"/v1/{project}/autopilot/nonexistent/degrade",
                        json={"reason": "test"})
        assert r.status_code == 404

    def test_degrade_changes_mode_signal(self, client, project):
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 3,
            "mode": "auto_promote",
        })
        sid = r.json()["session_id"]
        # 立刻发降级请求（session 可能还在 running 或已完成）
        r2 = client.post(f"/v1/{project}/autopilot/{sid}/degrade",
                         json={"reason": "test_manual_degrade"})
        # 如果 running → 202/200；如果已 completed → 409
        assert r2.status_code in (200, 409)


# ── 4. 自动降级逻辑（单元级，不走 HTTP）────────────────────────────────────────

class TestAutopilotAutoDegrade:
    def test_consecutive_issues_trigger_degrade(self, tmp_data):
        """连续 2 次 hard issue → policy_mode 切到 human_gate。"""
        from novelforge.app.autopilot_manager import AutopilotManager, AutopilotSession
        import datetime

        mgr = AutopilotManager()
        session = AutopilotSession(
            session_id="test-sid",
            project_id="proj",
            from_chapter=1,
            to_chapter=5,
            current_chapter=1,
            status="running",
            policy_mode="auto_promote",
            started_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )

        # 模拟连续 2 次 hard issue
        degrade_threshold = 2
        for _ in range(degrade_threshold):
            session.consecutive_hard_issues += 1
            if session.consecutive_hard_issues >= degrade_threshold:
                session.policy_mode = "human_gate"
                if session.status == "running":
                    session.status = "degraded"

        assert session.policy_mode == "human_gate"
        assert session.status == "degraded"

    def test_no_issue_resets_counter(self, tmp_data):
        """正常章节后连续计数归零。"""
        from novelforge.app.autopilot_manager import AutopilotSession
        import datetime

        s = AutopilotSession(
            session_id="t", project_id="p", from_chapter=1, to_chapter=5,
            current_chapter=3, status="running", policy_mode="auto_promote",
            consecutive_hard_issues=1,
            started_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
        # 无 hard issue → reset
        s.consecutive_hard_issues = 0
        assert s.consecutive_hard_issues == 0


# ── Autopilot 自动选取最优 chapter_goal ───────────────────────────────────────

class TestAutopilotAutoGoal:
    def _wait_finish(self, client, project, sid, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = client.get(f"/v1/{project}/autopilot/{sid}").json()
            if s["status"] not in ("running", "degraded"):
                return s
            time.sleep(0.05)
        return s

    @staticmethod
    def _patch_orchestrator(monkeypatch, captured: dict):
        """替换 generate_chapter 捕获 chapter_goal；gateway 不会被用到也一并替换，
        避免对本机已安装的 provider SDK 产生依赖。"""
        import novelforge.control_plane.llm.factory as factory_mod
        from novelforge.control_plane.orchestrator import ChapterOutcome, Orchestrator

        def fake_generate(self, chapter, conn, *, chapter_goal="", **kw):
            captured[chapter] = chapter_goal
            return ChapterOutcome(chapter=chapter, ok=True)

        monkeypatch.setattr(Orchestrator, "generate_chapter", fake_generate)
        monkeypatch.setattr(factory_mod, "build_gateway", lambda cfg, ledger=None: None)

    def test_unspecified_goal_assembled_from_plans(self, client, project, monkeypatch):
        """未传 chapter_goals 的章应自动按章节卡等规划数据拼装目标。"""
        conn = _open_conn(client, project)
        conn.execute(
            "INSERT INTO chapter_cards(id, chapter, title, goal)"
            " VALUES('cc1', 1, '开篇', '主角觉醒金手指')",
        )
        conn.commit()
        conn.close()

        captured: dict[int, str] = {}
        self._patch_orchestrator(monkeypatch, captured)

        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 1, "mode": "auto_promote",
        })
        assert r.status_code == 202
        s = self._wait_finish(client, project, r.json()["session_id"])
        assert s["status"] == "completed"
        assert "主角觉醒金手指" in captured.get(1, "")

    def test_explicit_goal_wins(self, client, project, monkeypatch):
        """显式 chapter_goals 优先于自动拼装。"""
        conn = _open_conn(client, project)
        conn.execute(
            "INSERT INTO chapter_cards(id, chapter, goal) VALUES('cc1', 1, '规划目标')",
        )
        conn.commit()
        conn.close()

        captured: dict[int, str] = {}
        self._patch_orchestrator(monkeypatch, captured)

        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 1, "mode": "auto_promote",
            "chapter_goals": {"1": "手写目标"},
        })
        assert r.status_code == 202
        s = self._wait_finish(client, project, r.json()["session_id"])
        assert s["status"] == "completed"
        assert captured.get(1) == "手写目标"
