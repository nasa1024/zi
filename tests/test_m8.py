"""M8 测试：Autopilot 进度 SSE 推送。全部 FakeProvider/monkeypatch，无网络。"""
from __future__ import annotations

import json
import queue as _queue
import time

import pytest
from fastapi.testclient import TestClient


# ── fixtures（与 test_autopilot.py 同款）──────────────────────────────────────

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    import novelforge.app.autopilot_manager as ap_mod
    import novelforge.app.deps as deps_mod
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
    resp = client.post("/v1/projects", json={"name": "M8测试", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _open_conn(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


def _patch_orchestrator(monkeypatch, *, delay: float = 0.0, with_stages: bool = False):
    """替换 generate_chapter + build_gateway；可选发 stage 进度。"""
    import novelforge.control_plane.llm.factory as factory_mod
    from novelforge.control_plane.orchestrator import ChapterOutcome, Orchestrator

    def fake_generate(self, chapter, conn, *, chapter_goal="", progress_cb=None, **kw):
        if delay:
            time.sleep(delay)
        if with_stages and progress_cb:
            progress_cb("recall", "ok", {})
            progress_cb("draft", "ok", {"chars": 3000})
        return ChapterOutcome(chapter=chapter, ok=True, usage_tokens=100, usage_usd=0.01)

    monkeypatch.setattr(Orchestrator, "generate_chapter", fake_generate)
    monkeypatch.setattr(factory_mod, "build_gateway", lambda cfg, ledger=None: None)


def _wait_finish(client, project, sid, timeout=5.0):
    deadline = time.time() + timeout
    s = None
    while time.time() < deadline:
        s = client.get(f"/v1/{project}/autopilot/{sid}").json()
        if s["status"] not in ("running", "degraded"):
            return s
        time.sleep(0.05)
    return s


# ── Manager 广播单元测试 ──────────────────────────────────────────────────────

class TestManagerBroadcast:
    def test_subscriber_receives_events_and_sentinel(self, client, project, monkeypatch):
        """订阅者收到 stage → chapter_done → finished → None 哨兵。"""
        from novelforge.app.autopilot_manager import get_autopilot_manager
        _patch_orchestrator(monkeypatch, delay=0.3, with_stages=True)

        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 2, "mode": "auto_promote",
        })
        assert r.status_code == 202
        sid = r.json()["session_id"]
        q = get_autopilot_manager().subscribe(sid)   # delay 窗口内完成订阅

        events = []
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                item = q.get(timeout=1)
            except _queue.Empty:
                continue
            if item is None:
                break
            events.append(item)
        else:
            pytest.fail("未收到 None 哨兵")

        kinds = [(e["event"], e.get("reason"), e.get("stage")) for e in events]
        assert ("stage", None, "draft") in kinds
        chapter_done = [e for e in events
                        if e["event"] == "session" and e["reason"] == "chapter_done"]
        assert len(chapter_done) == 2
        assert chapter_done[-1]["session"]["chapters_done"] == 2
        finished = [e for e in events
                    if e["event"] == "session" and e["reason"] == "finished"]
        assert finished and finished[0]["session"]["status"] == "completed"

    def test_slow_subscriber_does_not_block_loop(self, client, project, monkeypatch):
        """队列打满（maxsize=256）丢事件而非阻塞写章线程。"""
        from novelforge.app.autopilot_manager import AutopilotSession, get_autopilot_manager
        import datetime
        mgr = get_autopilot_manager()
        s = AutopilotSession(
            session_id="aps_t", project_id=project, from_chapter=1, to_chapter=1,
            current_chapter=1, status="running", policy_mode="auto_promote",
            started_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
        q = mgr.subscribe("aps_t")
        for _ in range(300):                      # 超过 maxsize
            mgr._emit(s, {"event": "stage"})
        assert q.qsize() <= 256                    # 不抛、不阻塞

    def test_unsubscribe_cleans_up(self, client, project):
        from novelforge.app.autopilot_manager import get_autopilot_manager
        mgr = get_autopilot_manager()
        q = mgr.subscribe("aps_x")
        mgr.unsubscribe("aps_x", q)
        assert "aps_x" not in mgr._subscribers


# ── SSE 端点集成测试 ──────────────────────────────────────────────────────────

def _read_sse_events(resp_iter, *, until_reason: str, max_lines: int = 500) -> list[dict]:
    events = []
    for i, line in enumerate(resp_iter):
        if i > max_lines:
            pytest.fail("SSE 行数超限仍未见终态")
        if not line.startswith("data: "):
            continue
        ev = json.loads(line[6:])
        events.append(ev)
        if ev.get("event") == "session" and ev.get("reason") == until_reason:
            break
    return events


class TestEventsEndpoint:
    def test_live_stream_snapshot_then_progress_then_finished(self, client, project, monkeypatch):
        _patch_orchestrator(monkeypatch, delay=0.3, with_stages=True)
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 2, "mode": "auto_promote",
        })
        sid = r.json()["session_id"]

        with client.stream("GET", f"/v1/{project}/autopilot/{sid}/events") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            events = _read_sse_events(resp.iter_lines(), until_reason="finished")

        assert events[0]["event"] == "session" and events[0]["reason"] == "snapshot"
        assert any(e["event"] == "stage" and e["stage"] == "draft" for e in events)
        assert any(e["event"] == "session" and e["reason"] == "chapter_done" for e in events)
        assert events[-1]["reason"] == "finished"
        assert events[-1]["session"]["status"] == "completed"
        assert events[-1]["session"]["chapters_done"] == 2

    def test_finished_session_gets_single_snapshot(self, client, project, monkeypatch):
        """已结束会话：一条快照即关流。"""
        _patch_orchestrator(monkeypatch)
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 1, "mode": "auto_promote",
        })
        sid = r.json()["session_id"]
        _wait_finish(client, project, sid)

        with client.stream("GET", f"/v1/{project}/autopilot/{sid}/events") as resp:
            events = [json.loads(l[6:]) for l in resp.iter_lines()
                      if l.startswith("data: ")]
        assert len(events) == 1
        assert events[0]["reason"] == "snapshot"
        assert events[0]["session"]["status"] == "completed"

    def test_db_only_session_snapshot(self, client, project):
        """进程重启后只剩 DB 行的会话：快照 status=interrupted 后关流。"""
        conn = _open_conn(project)
        conn.execute(
            "INSERT INTO autopilot_sessions"
            "(session_id, project_id, from_chapter, to_chapter, current_chapter,"
            " status, policy_mode, chapters_done, req_json, started_at)"
            " VALUES('aps_db', ?, 1, 5, 3, 'running', 'auto_promote', 2, '{}',"
            " '2026-01-01T00:00:00')",
            (project,),
        )
        conn.commit()
        conn.close()

        with client.stream("GET", f"/v1/{project}/autopilot/aps_db/events") as resp:
            events = [json.loads(l[6:]) for l in resp.iter_lines()
                      if l.startswith("data: ")]
        assert len(events) == 1
        assert events[0]["session"]["status"] == "interrupted"

    def test_unknown_session_404(self, client, project):
        r = client.get(f"/v1/{project}/autopilot/aps_nope/events")
        assert r.status_code == 404
