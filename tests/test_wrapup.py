"""收尾测试：planner 前缀化、CacheHint user_prefix、全局梗概（M1-⑥ / M2-② 遗留项）。

全部 FakeProvider，无网络。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from novelforge.control_plane.llm.anthropic_provider import _apply_user_prefix_cache


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
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
    resp = client.post("/v1/projects", json={"name": "收尾测试", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _open_conn(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


_DRAFT_RESPONSE = (
    "```draft\n" + "陆天踏入山门，" * 100 + "\n```\n"
    "```proposals\n"
    '[{"op":"add","fact_type":"power_rank","entity":"陆天",'
    '"new":{"subject":"陆天","predicate":"境界","object":"炼气一层"},'
    '"valid_from_chapter":1}]\n'
    "```"
)


def _build_orch(project):
    from novelforge.config import NovelForgeConfig
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.orchestrator import Orchestrator
    from novelforge.control_plane.skill_registry import SkillRegistry
    from novelforge.skills import register_default_skills

    def factory(messages, model=""):
        user = str(messages[-1].content) if messages else ""
        if "本章任务" in user:
            return _DRAFT_RESPONSE
        if "一致性问题" in user:
            return "修订稿正文。" * 150
        if "章正文：" in user:
            return "本章摘要。"
        if "卷《" in user:           # 全局梗概调用
            return "全书至此：陆天入门修行。"
        if "各章摘要" in user:
            return "本卷至今：初入山门。"
        return "[]"

    fake = FakeProvider(factory=factory)
    gw = LLMGateway(fake, BudgetLedger(max_tokens=10_000_000, max_usd=100.0,
                                       max_revise_rounds=100))
    reg = SkillRegistry()
    register_default_skills(reg)
    cfg = NovelForgeConfig(project_id=project)
    cfg.provider.provider = "fake"
    return Orchestrator(gw, reg, cfg), fake


def _seed_entity(project):
    from novelforge.ids import new_id
    conn = _open_conn(project)
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type) VALUES(?,?,?)",
        (new_id("ent"), "陆天", "character"),
    )
    conn.commit()
    conn.close()


# ── ① planner 前缀化 + CacheHint 传递 ─────────────────────────────────────────

class TestPlannerPrefix:
    def test_planner_shares_stable_prefix_and_passes_cache_hint(self, client, project):
        _seed_entity(project)
        orch, fake = _build_orch(project)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="入门")
            assert outcome.ok, outcome.error
        finally:
            conn.close()

        planner_calls = [c for c in fake.calls
                         if c["system"] and "策划助手" in c["system"]]
        draft_calls = [c for c in fake.calls
                       if c["system"] and "创作助手" in c["system"]]
        assert planner_calls and draft_calls

        planner_user = str(planner_calls[0]["messages"][-1].content)
        draft_user = str(draft_calls[0]["messages"][-1].content)
        # planner 与 draft 的 user 消息从第 0 字节起共享稳定前缀
        assert planner_user.startswith("## 世界设定（稳定）")
        assert "## 规划任务" in planner_user
        stable_len = planner_calls[0]["cache_hint"].user_prefix_chars
        assert stable_len > 0
        assert planner_user[:stable_len] == draft_user[:stable_len]
        assert "陆天" in planner_user[:stable_len]
        # draft / 软检查也带 cache_hint，且前缀长度一致
        assert draft_calls[0]["cache_hint"].user_prefix_chars == stable_len
        soft_calls = [c for c in fake.calls
                      if c["system"] and "一致性审稿员" in c["system"]]
        assert soft_calls and soft_calls[0]["cache_hint"].user_prefix_chars == stable_len


class TestApplyUserPrefixCache:
    def test_splits_first_user_message(self):
        msgs = [{"role": "user", "content": "STABLE" + "X" * 100}]
        _apply_user_prefix_cache(msgs, 6, "ephemeral")
        blocks = msgs[0]["content"]
        assert isinstance(blocks, list) and len(blocks) == 2
        assert blocks[0]["text"] == "STABLE"
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in blocks[1]

    def test_noop_when_prefix_zero_or_too_long(self):
        msgs = [{"role": "user", "content": "short"}]
        _apply_user_prefix_cache(msgs, 0, "ephemeral")
        assert msgs[0]["content"] == "short"
        _apply_user_prefix_cache(msgs, 99, "ephemeral")
        assert msgs[0]["content"] == "short"

    def test_only_first_user_message(self):
        msgs = [
            {"role": "assistant", "content": "a" * 50},
            {"role": "user", "content": "S" * 10 + "x" * 50},
            {"role": "user", "content": "S" * 10 + "y" * 50},
        ]
        _apply_user_prefix_cache(msgs, 10, "ephemeral")
        assert isinstance(msgs[1]["content"], list)
        assert isinstance(msgs[2]["content"], str)


# ── ② 全局梗概 ────────────────────────────────────────────────────────────────

class TestGlobalSynopsis:
    def test_update_writes_meta_kv_and_recall_consumes(self, client, project):
        from novelforge.control_plane.orchestrator import _update_global_synopsis
        from novelforge.memory.recall import gather_hard_context

        orch, _ = _build_orch(project)
        conn = _open_conn(project)
        try:
            conn.execute(
                "INSERT INTO volumes(id, volume_no, title, start_chapter, end_chapter, rolling_summary)"
                " VALUES('vol1', 1, '第一卷', 1, 10, '本卷至今：初入山门')",
            )
            conn.commit()
            _update_global_synopsis(conn, orch._gw)
            row = conn.execute(
                "SELECT value FROM meta_kv WHERE key='global_synopsis'"
            ).fetchone()
            assert row["value"] == "全书至此：陆天入门修行。"

            pack = gather_hard_context([], 5, conn)
            assert pack.global_synopsis == "全书至此：陆天入门修行。"
            ctx_str = pack.to_dynamic_context_str()
            assert ctx_str.startswith("## 全书至此")
        finally:
            conn.close()

    def test_triggered_every_10_chapters(self, client, project):
        from novelforge.control_plane.orchestrator import _persist_chapter_summary

        orch, _ = _build_orch(project)
        conn = _open_conn(project)
        try:
            conn.execute(
                "INSERT INTO volumes(id, volume_no, title, start_chapter, end_chapter, rolling_summary)"
                " VALUES('vol1', 1, '第一卷', 1, 20, '本卷至今')",
            )
            conn.commit()
            # 第 9 章：不触发
            _persist_chapter_summary(conn, orch._gw, 9, "第九章正文" * 100)
            row = conn.execute(
                "SELECT value FROM meta_kv WHERE key='global_synopsis'"
            ).fetchone()
            assert row is None
            # 第 10 章：触发
            _persist_chapter_summary(conn, orch._gw, 10, "第十章正文" * 100)
            row = conn.execute(
                "SELECT value FROM meta_kv WHERE key='global_synopsis'"
            ).fetchone()
            assert row is not None and row["value"]
        finally:
            conn.close()

    def test_empty_inputs_noop(self, client, project):
        from novelforge.control_plane.orchestrator import _update_global_synopsis
        orch, _ = _build_orch(project)
        conn = _open_conn(project)
        try:
            _update_global_synopsis(conn, orch._gw)   # 无卷摘要无章摘要
            row = conn.execute(
                "SELECT value FROM meta_kv WHERE key='global_synopsis'"
            ).fetchone()
        finally:
            conn.close()
        assert row is None
