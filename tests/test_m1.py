"""M1 测试：③ Autopilot 会话持久化 + ⑥ Prompt 前缀稳定化。

全部 FakeProvider / monkeypatch，无网络。
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient


# ── fixtures（与 test_autopilot.py 同款）──────────────────────────────────────

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
    resp = client.post("/v1/projects", json={"name": "M1测试", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _open_conn(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


def _patch_orchestrator(monkeypatch, captured: dict | None = None):
    """替换 generate_chapter + build_gateway（与 test_autopilot.py 同款）。"""
    import novelforge.control_plane.llm.factory as factory_mod
    from novelforge.control_plane.orchestrator import ChapterOutcome, Orchestrator

    def fake_generate(self, chapter, conn, *, chapter_goal="", **kw):
        if captured is not None:
            captured[chapter] = chapter_goal
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


# ── ③ Autopilot 持久化 ────────────────────────────────────────────────────────

class TestAutopilotPersistence:
    def test_session_persisted_through_lifecycle(self, client, project, monkeypatch):
        """start → DB 有行；完成 → status/chapters_done/预算累计写穿。"""
        _patch_orchestrator(monkeypatch)
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 3, "mode": "auto_promote",
            "chapter_goals": {"2": "第二章目标"},
        })
        assert r.status_code == 202
        sid = r.json()["session_id"]

        s = _wait_finish(client, project, sid)
        assert s["status"] == "completed"

        conn = _open_conn(project)
        row = conn.execute(
            "SELECT * FROM autopilot_sessions WHERE session_id=?", (sid,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["status"] == "completed"
        assert row["chapters_done"] == 3
        assert row["budget_tokens_total"] == 300
        assert row["finished_at"] is not None
        req = json.loads(row["req_json"])
        assert req["chapter_goals"] == {"2": "第二章目标"}

    def test_orphan_running_row_marked_interrupted(self, client, project):
        """模拟进程重启：DB 残留 running 行、内存无会话 → status 接口标 interrupted。"""
        conn = _open_conn(project)
        conn.execute(
            "INSERT INTO autopilot_sessions"
            "(session_id, project_id, from_chapter, to_chapter, current_chapter,"
            " status, policy_mode, chapters_done, req_json, started_at)"
            " VALUES('aps_orphan', ?, 1, 10, 4, 'running', 'auto_promote', 3,"
            " '{\"mode\":\"auto_promote\"}', '2026-01-01T00:00:00')",
            (project,),
        )
        conn.commit()
        conn.close()

        r = client.get(f"/v1/{project}/autopilot/status")
        assert r.status_code == 200
        sessions = {s["session_id"]: s for s in r.json()}
        assert "aps_orphan" in sessions
        assert sessions["aps_orphan"]["status"] == "interrupted"

        # DB 行也已翻转（幂等：再查一次仍 interrupted）
        conn = _open_conn(project)
        row = conn.execute(
            "SELECT status FROM autopilot_sessions WHERE session_id='aps_orphan'"
        ).fetchone()
        conn.close()
        assert row["status"] == "interrupted"

    def test_resume_continues_from_breakpoint(self, client, project, monkeypatch):
        """resume：以 current_chapter 为起点开新会话，沿用原参数，链回旧会话。"""
        conn = _open_conn(project)
        conn.execute(
            "INSERT INTO autopilot_sessions"
            "(session_id, project_id, from_chapter, to_chapter, current_chapter,"
            " status, policy_mode, chapters_done, req_json, started_at)"
            " VALUES('aps_int', ?, 1, 5, 3, 'interrupted', 'auto_promote', 2,"
            " '{\"mode\":\"auto_promote\",\"chapter_goals\":{\"4\":\"恢复后的目标\"}}',"
            " '2026-01-01T00:00:00')",
            (project,),
        )
        conn.commit()
        conn.close()

        captured: dict[int, str] = {}
        _patch_orchestrator(monkeypatch, captured)

        r = client.post(f"/v1/{project}/autopilot/aps_int/resume")
        assert r.status_code == 202
        body = r.json()
        new_sid = body["session_id"]
        assert new_sid != "aps_int"
        assert body["from_chapter"] == 3   # max(current=3, next=1)
        assert body["to_chapter"] == 5

        s = _wait_finish(client, project, new_sid)
        assert s["status"] == "completed"
        # 只写第 3-5 章（已完成的 1-2 章不重写），且沿用原 chapter_goals
        assert sorted(captured.keys()) == [3, 4, 5]
        assert captured[4] == "恢复后的目标"

        conn = _open_conn(project)
        row = conn.execute(
            "SELECT resumed_from FROM autopilot_sessions WHERE session_id=?", (new_sid,)
        ).fetchone()
        conn.close()
        assert row["resumed_from"] == "aps_int"

    def test_resume_running_session_409(self, client, project, monkeypatch):
        _patch_orchestrator(monkeypatch)
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 1, "mode": "auto_promote",
        })
        sid = r.json()["session_id"]
        _wait_finish(client, project, sid)
        # completed 状态不可恢复
        r2 = client.post(f"/v1/{project}/autopilot/{sid}/resume")
        assert r2.status_code == 409

    def test_resume_nonexistent_404(self, client, project):
        r = client.post(f"/v1/{project}/autopilot/aps_nope/resume")
        assert r.status_code == 404


# ── ⑥ Prompt 前缀稳定化 ───────────────────────────────────────────────────────

_DRAFT_RESPONSE = (
    "```draft\n" + "陆天踏入山门，" * 100 + "\n```\n"
    "```proposals\n"
    '[{"op":"add","fact_type":"power_rank","entity":"陆天",'
    '"new":{"subject":"陆天","predicate":"境界","object":"炼气一层"},'
    '"valid_from_chapter":1}]\n'
    "```"
)


class TestStablePrefix:
    def _run_pipeline_with_fake(self, client, project):
        """直接构建 Orchestrator + FakeProvider，跑一章，返回 provider 调用记录。"""
        from novelforge.config import NovelForgeConfig
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.fake_provider import FakeProvider
        from novelforge.control_plane.llm.gateway import LLMGateway
        from novelforge.control_plane.orchestrator import Orchestrator
        from novelforge.control_plane.skill_registry import SkillRegistry
        from novelforge.skills import register_default_skills

        def factory(messages, model=""):
            # draft 调用返回完整双代码块；其余（planner/check/dedup）返回空数组
            user = str(messages[-1].content) if messages else ""
            if "本章任务" in user:
                return _DRAFT_RESPONSE
            return "[]"

        fake = FakeProvider(factory=factory)
        gw = LLMGateway(fake, BudgetLedger(max_tokens=10_000_000, max_usd=100.0))
        reg = SkillRegistry()
        register_default_skills(reg)
        cfg = NovelForgeConfig(project_id=project)
        cfg.provider.provider = "fake"
        orch = Orchestrator(gw, reg, cfg)

        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="主角入门")
        finally:
            conn.close()
        return fake.calls, outcome

    def _seed_setting(self, project):
        """造一点慢变设定，让 stable_context 非空。"""
        from novelforge.ids import new_id
        conn = _open_conn(project)
        conn.execute(
            "INSERT INTO entities(id, canonical_name, entity_type) VALUES(?,?,?)",
            (new_id("ent"), "陆天", "character"),
        )
        conn.commit()
        conn.close()

    def test_recall_pack_split_is_lossless(self):
        """to_context_str == stable + dynamic（兼容入口无信息丢失）。"""
        from novelforge.memory.recall import RecallPack
        pack = RecallPack(
            entities=[{"canonical_name": "陆天", "entity_type": "character"}],
            taboos=[{"rule_text": "禁止穿越", "reason": "世界观"}],
            power_states=[{"entity_id": "e1", "rank_name": "炼气", "rank_order": 1}],
        )
        stable = pack.to_stable_context_str()
        dynamic = pack.to_dynamic_context_str()
        assert "常驻禁忌" in stable and "核心实体" in stable
        assert "当前境界" in dynamic
        assert pack.to_context_str() == stable + "\n\n" + dynamic

    def test_draft_and_checks_share_byte_prefix(self, client, project):
        """同章 draft 与软检查的 user 消息从第 0 字节起共享 stable_context 前缀。"""
        self._seed_setting(project)
        calls, outcome = self._run_pipeline_with_fake(client, project)
        assert outcome.ok, outcome.error

        draft_calls = [c for c in calls
                       if c["system"] and "创作助手" in c["system"]]
        soft_calls = [c for c in calls
                      if c["system"] and "一致性审稿员" in c["system"]]
        assert draft_calls and soft_calls

        def user_text(call):
            return str(call["messages"][-1].content)

        draft_user = user_text(draft_calls[0])
        soft_user = user_text(soft_calls[0])
        # 两者都以同一稳定前缀开头
        assert draft_user.startswith("## 世界设定（稳定）")
        assert soft_user.startswith("## 世界设定（稳定）")
        prefix_len = 0
        for a, b in zip(draft_user, soft_user):
            if a != b:
                break
            prefix_len += 1
        # 公共前缀至少覆盖整个稳定块（含实体名）
        common = draft_user[:prefix_len]
        assert "陆天" in common
        assert "## 世界设定（稳定）" in common

    def test_cache_read_tokens_flow_to_outcome(self, client, project):
        """provider 返回的 cache_read 累计进 ledger 并透出 ChapterOutcome。"""
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.provider import Usage

        ledger = BudgetLedger(max_tokens=1000, max_usd=1.0)
        ledger.charge(Usage(input=100, output=50, cache_read=80, model="deepseek-v4-flash"))
        ledger.charge(Usage(input=100, output=50, cache_read=90, model="deepseek-v4-flash"))
        assert ledger.cache_read_tokens == 170
