"""M2 测试：② 分层叙事摘要 + ⑤ ConStory 检错清单与中段加压。

全部 FakeProvider，无网络。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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
    resp = client.post("/v1/projects", json={"name": "M2测试", "genre": "xuanhuan"})
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

_SUMMARY_TEXT = "陆天初入山门拜师，与同门起冲突；章末黑袍人现身。"


def _build_orch(project, *, enable_summaries=True):
    """构建 FakeProvider Orchestrator；factory 按 user 内容路由响应。"""
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
        if "章正文：" in user:           # 章摘要调用
            return _SUMMARY_TEXT
        if "各章摘要" in user:           # 卷 rollup 调用
            return "本卷至今：陆天入门并初露锋芒。"
        return "[]"

    fake = FakeProvider(factory=factory)
    # max_revise_rounds 放宽：生产中每章独立 ledger，测试里跨章复用同一个
    gw = LLMGateway(fake, BudgetLedger(max_tokens=10_000_000, max_usd=100.0,
                                       max_revise_rounds=100))
    reg = SkillRegistry()
    register_default_skills(reg)
    cfg = NovelForgeConfig(project_id=project)
    cfg.provider.provider = "fake"
    cfg.recall.enable_summaries = enable_summaries
    return Orchestrator(gw, reg, cfg), fake


# ── ② 分层叙事摘要 ────────────────────────────────────────────────────────────

class TestChapterSummaries:
    def test_summary_persisted_after_commit(self, client, project):
        orch, _ = _build_orch(project)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="入门")
            assert outcome.ok, outcome.error
            row = conn.execute(
                "SELECT summary FROM chapter_summaries WHERE chapter=1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["summary"] == _SUMMARY_TEXT

    def test_summary_injected_into_next_chapter_prompt(self, client, project):
        """第 1 章的摘要应出现在第 2 章 draft 的 user 消息里。"""
        orch, fake = _build_orch(project)
        conn = _open_conn(project)
        try:
            assert orch.generate_chapter(1, conn).ok
            assert orch.generate_chapter(2, conn).ok
        finally:
            conn.close()
        draft_calls = [c for c in fake.calls
                       if "本章任务" in str(c["messages"][-1].content)]
        assert len(draft_calls) == 2
        ch2_user = str(draft_calls[1]["messages"][-1].content)
        assert "前情摘要" in ch2_user
        assert _SUMMARY_TEXT in ch2_user

    def test_as_of_excludes_future_summaries(self, client, project):
        from novelforge.memory.recall import gather_hard_context
        conn = _open_conn(project)
        try:
            for ch in range(1, 9):
                conn.execute(
                    "INSERT INTO chapter_summaries(id, chapter, summary)"
                    " VALUES(?, ?, ?)",
                    (f"csum_{ch}", ch, f"第{ch}章摘要"),
                )
            conn.commit()
            pack = gather_hard_context([], 5, conn, summary_window=5)
        finally:
            conn.close()
        chapters = [r["chapter"] for r in pack.chapter_summaries]
        assert chapters == [1, 2, 3, 4, 5]          # 时间正序、不含 6-8
        assert "第5章摘要" in pack.to_dynamic_context_str()

    def test_disabled_flag_skips_everything(self, client, project):
        orch, fake = _build_orch(project, enable_summaries=False)
        conn = _open_conn(project)
        try:
            assert orch.generate_chapter(1, conn).ok
            row = conn.execute("SELECT COUNT(*) AS n FROM chapter_summaries").fetchone()
        finally:
            conn.close()
        assert row["n"] == 0
        assert not any("章正文：" in str(c["messages"][-1].content) for c in fake.calls)

    def test_volume_rollup_every_5_chapters(self, client, project):
        """第 5 章触发卷滚动摘要更新。"""
        from novelforge.control_plane.orchestrator import _persist_chapter_summary
        orch, _ = _build_orch(project)
        conn = _open_conn(project)
        try:
            conn.execute(
                "INSERT INTO volumes(id, volume_no, title, start_chapter, end_chapter)"
                " VALUES('vol1', 1, '第一卷', 1, 10)",
            )
            conn.commit()
            _persist_chapter_summary(conn, orch._gw, 5, "第五章正文" * 100)
            row = conn.execute(
                "SELECT rolling_summary FROM volumes WHERE volume_no=1"
            ).fetchone()
            srow = conn.execute(
                "SELECT volume_no FROM chapter_summaries WHERE chapter=5"
            ).fetchone()
        finally:
            conn.close()
        assert srow["volume_no"] == 1
        assert row["rolling_summary"] == "本卷至今：陆天入门并初露锋芒。"


# ── ⑤ ConStory 清单 + 中段加压 ────────────────────────────────────────────────

class TestConStoryChecklist:
    def test_soft_system_contains_19_subclasses(self):
        from novelforge.skills.continuity_check_skill import _SOFT_SYSTEM
        for marker in ("能力波动", "同步悖论", "命名混淆", "视角混乱", "地理矛盾",
                       # P1#8 findings 契约字段
                       "category", "evidence", "repair_scope"):
            assert marker in _SOFT_SYSTEM

    def test_volume_progress(self, client, project):
        from novelforge.control_plane.orchestrator import _volume_progress
        conn = _open_conn(project)
        try:
            assert _volume_progress(conn, 5) is None   # 无卷
            conn.execute(
                "INSERT INTO volumes(id, volume_no, title, start_chapter, end_chapter)"
                " VALUES('vol1', 1, '第一卷', 1, 11)",
            )
            conn.commit()
            assert _volume_progress(conn, 1) == 0.0
            assert _volume_progress(conn, 6) == 0.5    # (6-1)/(11-1)
            assert _volume_progress(conn, 11) == 1.0
            assert _volume_progress(conn, 99) is None  # 不在任何卷
        finally:
            conn.close()
