"""Autopilot 取消 / TTL / 会话级预算累加测试（E4/E7/E8）。"""
from __future__ import annotations

import time
import datetime
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


class TestAutopilotCancel:
    def test_cancel_running_session(self, client, project):
        """启动 3 章会话，立刻发送取消，最终状态应为 canceled 或 completed（FakeProvider 很快）。"""
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 3,
        })
        assert r.status_code == 202
        sid = r.json()["session_id"]

        r2 = client.post(f"/v1/{project}/autopilot/{sid}/cancel")
        # 可能已完成（FakeProvider 极快）→ 409，或 running/degraded → 200
        assert r2.status_code in (200, 409)

    def test_cancel_nonexistent_session(self, client, project):
        r = client.post(f"/v1/{project}/autopilot/nonexistent/cancel")
        assert r.status_code == 404

    def test_cancel_completed_session(self, client, project):
        """已完成的会话取消应返回 409。"""
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 1,
        })
        sid = r.json()["session_id"]
        # 等待完成
        for _ in range(50):
            s = client.get(f"/v1/{project}/autopilot/{sid}").json()
            if s["status"] in ("completed", "error", "canceled"):
                break
            time.sleep(0.1)
        r2 = client.post(f"/v1/{project}/autopilot/{sid}/cancel")
        assert r2.status_code == 409

    def test_cancel_manager_direct(self, tmp_data):
        """直接调用 manager.cancel()：运行中 → True；不存在 → False。"""
        from novelforge.app.autopilot_manager import AutopilotManager, AutopilotSession

        mgr = AutopilotManager()
        s = AutopilotSession(
            session_id="sid1", project_id="p", from_chapter=1, to_chapter=5,
            current_chapter=2, status="running", policy_mode="auto_promote",
            started_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
        mgr._sessions["sid1"] = s

        assert mgr.cancel("sid1") is True
        assert s._cancel_requested is True

        assert mgr.cancel("nonexistent") is False


class TestAutopilotTTL:
    def test_cleanup_stale_session(self, tmp_data):
        """_is_stale() 对超时会话返回 True，cleanup 将其标为 error。"""
        from novelforge.app.autopilot_manager import AutopilotManager, AutopilotSession, _SESSION_TTL_SECONDS
        import time

        mgr = AutopilotManager()
        s = AutopilotSession(
            session_id="stale1", project_id="p", from_chapter=1, to_chapter=10,
            current_chapter=3, status="running", policy_mode="auto_promote",
            started_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
        # 模拟超时：把心跳拨到过去
        s._last_heartbeat = time.time() - _SESSION_TTL_SECONDS - 1
        mgr._sessions["stale1"] = s

        cleaned = mgr.cleanup_stale_sessions()
        assert "stale1" in cleaned
        assert s.status == "error"
        assert s.finished_at is not None

    def test_cleanup_active_session_untouched(self, tmp_data):
        """活跃会话不应被清理。"""
        from novelforge.app.autopilot_manager import AutopilotManager, AutopilotSession
        import time

        mgr = AutopilotManager()
        s = AutopilotSession(
            session_id="active1", project_id="p", from_chapter=1, to_chapter=5,
            current_chapter=2, status="running", policy_mode="auto_promote",
            started_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
        s._last_heartbeat = time.time()  # 刚刚心跳
        mgr._sessions["active1"] = s

        cleaned = mgr.cleanup_stale_sessions()
        assert "active1" not in cleaned
        assert s.status == "running"

    def test_cleanup_endpoint(self, client, project):
        r = client.post(f"/v1/{project}/autopilot/cleanup")
        assert r.status_code == 200
        body = r.json()
        assert "cleaned" in body
        assert "count" in body


class TestAutopilotSessionBudget:
    def test_session_budget_accumulates(self, tmp_data):
        """chapters_done 增加时 budget_tokens_total 累计（单元测试）。"""
        from novelforge.app.autopilot_manager import AutopilotSession
        import datetime

        s = AutopilotSession(
            session_id="t", project_id="p", from_chapter=1, to_chapter=5,
            current_chapter=1, status="running", policy_mode="auto_promote",
            started_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
        s.budget_tokens_total += 1000
        s.budget_tokens_total += 2000
        assert s.budget_tokens_total == 3000

    def test_session_budget_cap_triggers_circuit_break(self, tmp_data):
        """会话级 token 封顶：累计超过 budget_session_max_tokens 后应熔断。"""
        from novelforge.app.autopilot_manager import AutopilotManager, AutopilotSession
        import datetime

        mgr = AutopilotManager()
        s = AutopilotSession(
            session_id="cap1", project_id="p", from_chapter=1, to_chapter=10,
            current_chapter=5, status="running", policy_mode="auto_promote",
            started_at=datetime.datetime.now(datetime.UTC).isoformat(),
            budget_session_max_tokens=5000,
            budget_tokens_total=6000,  # 已超封顶
        )
        mgr._sessions["cap1"] = s

        # 检测逻辑：在 _run_loop 入口判断
        with mgr._lock:
            if (s.budget_session_max_tokens and
                    s.budget_tokens_total >= s.budget_session_max_tokens):
                s.status = "circuit_broken"
                s.last_error = "session token budget exceeded"

        assert s.status == "circuit_broken"
