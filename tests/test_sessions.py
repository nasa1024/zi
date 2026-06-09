"""Sessions / Turns / SSE 测试（Group 9/10）。"""
from __future__ import annotations

import json

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
    r = client.post("/v1/projects", json={"name": "测试项目", "genre": "xuanhuan"})
    assert r.status_code == 201
    return r.json()["project_id"]


@pytest.fixture
def session(client, project):
    r = client.post(f"/v1/{project}/sessions", json={
        "client": "api", "actor": "test_user", "mode": "auto_promote"
    })
    assert r.status_code == 201
    return r.json()


# ── Sessions CRUD ──────────────────────────────────────────────────────────────

class TestSessions:
    def test_create_session(self, client, project):
        r = client.post(f"/v1/{project}/sessions", json={
            "client": "cli", "actor": "author1", "mode": "human_gate"
        })
        assert r.status_code == 201
        body = r.json()
        assert body["client"] == "cli"
        assert body["actor"] == "author1"
        assert body["mode"] == "human_gate"
        assert body["session_id"].startswith("sess")

    def test_get_session(self, client, project, session):
        sid = session["session_id"]
        r = client.get(f"/v1/{project}/sessions/{sid}")
        assert r.status_code == 200
        assert r.json()["session_id"] == sid

    def test_get_session_not_found(self, client, project):
        r = client.get(f"/v1/{project}/sessions/nonexistent")
        assert r.status_code == 404

    def test_list_sessions(self, client, project):
        client.post(f"/v1/{project}/sessions", json={"client": "web", "actor": "a1"})
        client.post(f"/v1/{project}/sessions", json={"client": "chat", "actor": "a2"})
        r = client.get(f"/v1/{project}/sessions")
        assert r.status_code == 200
        assert len(r.json()) >= 2

    def test_end_session(self, client, project, session):
        sid = session["session_id"]
        r = client.post(f"/v1/{project}/sessions/{sid}/end", json={
            "summary": "审校完成，共写3章"
        })
        assert r.status_code == 200
        body = r.json()
        assert body["ended_at"] is not None

    def test_session_project_not_found(self, client, tmp_data):
        r = client.post("/v1/nonexistent/sessions", json={"client": "api", "actor": "x"})
        assert r.status_code == 404


# ── Turns ──────────────────────────────────────────────────────────────────────

class TestTurns:
    def test_create_sync_turn(self, client, project, session):
        sid = session["session_id"]
        r = client.post(f"/v1/{project}/sessions/{sid}/turns", json={
            "kind": "command",
            "intent": "pipeline_run",
            "payload": {"chapter_no": 1, "chapter_goal": "开局"},
            "stream": False,
        })
        assert r.status_code == 201
        body = r.json()
        assert body["seq"] == 1
        assert body["status"] == "done"
        assert body["kind"] == "command"

    def test_create_stream_turn_returns_turn_id(self, client, project, session):
        sid = session["session_id"]
        r = client.post(f"/v1/{project}/sessions/{sid}/turns", json={
            "kind": "long_task",
            "intent": "write_chapter",
            "payload": {"chapter_no": 5},
            "stream": True,
        })
        assert r.status_code == 201
        body = r.json()
        assert "turn_id" in body
        assert body["stream"] is True

    def test_turn_seq_increments(self, client, project, session):
        sid = session["session_id"]
        r1 = client.post(f"/v1/{project}/sessions/{sid}/turns", json={
            "kind": "command", "payload": {}, "stream": False
        })
        r2 = client.post(f"/v1/{project}/sessions/{sid}/turns", json={
            "kind": "command", "payload": {}, "stream": False
        })
        assert r1.json()["seq"] == 1
        assert r2.json()["seq"] == 2

    def test_list_turns(self, client, project, session):
        sid = session["session_id"]
        client.post(f"/v1/{project}/sessions/{sid}/turns", json={
            "kind": "command", "payload": {}, "stream": False
        })
        client.post(f"/v1/{project}/sessions/{sid}/turns", json={
            "kind": "command", "payload": {}, "stream": False
        })
        r = client.get(f"/v1/{project}/sessions/{sid}/turns")
        assert r.status_code == 200
        turns = r.json()
        assert len(turns) == 2
        seqs = [t["seq"] for t in turns]
        assert seqs == sorted(seqs)

    def test_turn_session_not_found(self, client, project):
        r = client.post(f"/v1/{project}/sessions/nonexistent/turns", json={
            "kind": "command", "payload": {}, "stream": False
        })
        assert r.status_code == 404


# ── Turn Events + SSE ──────────────────────────────────────────────────────────

class TestTurnEvents:
    def _create_sync_turn(self, client, project, session):
        sid = session["session_id"]
        r = client.post(f"/v1/{project}/sessions/{sid}/turns", json={
            "kind": "command", "payload": {"x": 1}, "stream": False
        })
        return r.json()

    def test_sync_turn_creates_result_event(self, client, project, session):
        """同步 turn 完成后应写入 result event。"""
        turn = self._create_sync_turn(client, project, session)
        sid = session["session_id"]
        tid = turn["turn_id"]
        r = client.get(f"/v1/{project}/sessions/{sid}/turns/{tid}/events")
        assert r.status_code == 200
        events = r.json()
        assert len(events) >= 1
        assert any(e["event_type"] == "result" for e in events)

    def test_events_since_id(self, client, project, session):
        """since_id 参数过滤已接收的事件。"""
        turn = self._create_sync_turn(client, project, session)
        sid = session["session_id"]
        tid = turn["turn_id"]
        events_all = client.get(
            f"/v1/{project}/sessions/{sid}/turns/{tid}/events"
        ).json()
        if not events_all:
            return  # 无事件，跳过
        first_id = events_all[0]["id"]
        events_after = client.get(
            f"/v1/{project}/sessions/{sid}/turns/{tid}/events?since_id={first_id}"
        ).json()
        assert all(e["id"] > first_id for e in events_after)

    def test_sse_stream_returns_text_event_stream(self, client, project, session):
        """SSE 端点返回 text/event-stream content-type。"""
        # 先创建一个 stream turn
        sid = session["session_id"]
        r = client.post(f"/v1/{project}/sessions/{sid}/turns", json={
            "kind": "long_task", "payload": {}, "stream": True
        })
        tid = r.json()["turn_id"]

        # SSE 端点
        stream_r = client.get(
            f"/v1/{project}/sessions/{sid}/turns/{tid}/stream",
            headers={"Accept": "text/event-stream"},
        )
        assert stream_r.status_code == 200
        assert "text/event-stream" in stream_r.headers.get("content-type", "")

    def test_sse_stream_contains_done_event(self, client, project, session):
        """SSE 流末尾包含 done 事件。"""
        sid = session["session_id"]
        r = client.post(f"/v1/{project}/sessions/{sid}/turns", json={
            "kind": "long_task", "payload": {}, "stream": True
        })
        tid = r.json()["turn_id"]
        stream_r = client.get(f"/v1/{project}/sessions/{sid}/turns/{tid}/stream")
        body = stream_r.text
        assert "event: done" in body
