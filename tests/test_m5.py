"""M5 测试：⑦ 质量分门控 + 润色 pass、⑧ 伏笔回收健康度。全部 FakeProvider，无网络。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from novelforge.craft.candidate_judge import score_chapter


# ── fixtures ──────────────────────────────────────────────────────────────────

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
    resp = client.post("/v1/projects", json={"name": "M5测试", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _open_conn(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


def _draft_response(body: str) -> str:
    return (
        f"```draft\n{body}\n```\n"
        "```proposals\n"
        '[{"op":"add","fact_type":"power_rank","entity":"陆天",'
        '"new":{"subject":"陆天","predicate":"境界","object":"炼气一层"},'
        '"valid_from_chapter":1}]\n'
        "```"
    )


def _make_gateway(responses=None, factory=None):
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    fake = FakeProvider(responses=responses, factory=factory)
    return LLMGateway(fake, BudgetLedger(max_tokens=10_000_000, max_usd=100.0,
                                         max_revise_rounds=100)), fake


# ── score_chapter 单元测试 ────────────────────────────────────────────────────

class TestScoreChapter:
    def test_parses_score(self):
        gw, _ = _make_gateway(responses=['{"score": 7.5, "reason": "钩子有力"}'])
        assert score_chapter(gw, "mid", "目标", "正文" * 500) == 7.5

    def test_clamps_to_range(self):
        gw, _ = _make_gateway(responses=['{"score": 15}'])
        assert score_chapter(gw, "mid", "", "正文" * 500) == 10.0

    def test_garbage_returns_none(self):
        gw, _ = _make_gateway(responses=["不是 JSON"])
        assert score_chapter(gw, "mid", "", "正文" * 500) is None

    def test_empty_draft_returns_none(self):
        gw, _ = _make_gateway(responses=['{"score": 7}'])
        assert score_chapter(gw, "mid", "", "") is None


# ── 质量门控流水线集成 ────────────────────────────────────────────────────────

class TestQualityGate:
    def _build_orch(self, project, *, scores: list[str], polish_text: str | None = None,
                    quality_enabled=True):
        """scores: 依次返回的评分 JSON；polish_text: 润色调用的返回正文。"""
        from novelforge.config import NovelForgeConfig
        from novelforge.control_plane.orchestrator import Orchestrator
        from novelforge.control_plane.skill_registry import SkillRegistry
        from novelforge.skills import register_default_skills

        score_queue = list(scores)

        def factory(messages, model=""):
            user = str(messages[-1].content) if messages else ""
            if user.startswith("当前章节："):   # planner：给合法 beats，避免 beat_contract 硬 block
                return ('[{"beat_type":"setup","summary":"铺垫","value_axis":"平静→紧张"},'
                        '{"beat_type":"hook","summary":"悬念","value_axis":"紧张→悬念"}]')
            if "本章任务" in user:
                return _draft_response("初稿正文，节奏平平。" * 100)
            if "一致性问题" in user:
                return "修订稿正文。" * 150
            if "工艺问题" in user:           # 润色调用
                return polish_text or ("润色后正文，钩子拉满。" * 100)
            if "本章目标" in user:           # 评分调用（_SCORE_SYSTEM 的 user 前缀）
                return score_queue.pop(0) if score_queue else '{"score": 5}'
            if "章正文：" in user:
                return "本章摘要。"
            return "[]"

        gw, fake = _make_gateway(factory=factory)
        reg = SkillRegistry()
        register_default_skills(reg)
        cfg = NovelForgeConfig(project_id=project)
        cfg.provider.provider = "fake"
        cfg.recall.enable_summaries = False
        cfg.quality.enabled = quality_enabled
        return Orchestrator(gw, reg, cfg), fake

    def test_low_score_triggers_polish_keeps_better(self, client, project):
        """低分（4）→ 润色 → 复评 8 → 保留润色稿，最终分 8。"""
        orch, fake = self._build_orch(
            project, scores=['{"score": 4.0}', '{"score": 8.0}'],
        )
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="入门")
            assert outcome.ok, outcome.error
            row = conn.execute(
                "SELECT quality_score FROM pipeline_run WHERE run_id=?",
                (outcome.run_id,),
            ).fetchone()
        finally:
            conn.close()
        assert outcome.quality_score == 8.0
        assert "润色后正文" in outcome.draft_text
        assert row["quality_score"] == 8.0

    def test_polish_made_worse_reverts(self, client, project):
        """润色复评 3 < 原 4 → 回退原稿，保留原分。"""
        orch, _ = self._build_orch(
            project, scores=['{"score": 4.0}', '{"score": 3.0}'],
        )
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn)
            assert outcome.ok
        finally:
            conn.close()
        assert outcome.quality_score == 4.0
        assert "润色后正文" not in outcome.draft_text

    def test_high_score_no_polish(self, client, project):
        """高分（9）且 warn 不足 → 不润色，单次评分。"""
        orch, fake = self._build_orch(project, scores=['{"score": 9.0}'])
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn)
            assert outcome.ok
        finally:
            conn.close()
        assert outcome.quality_score == 9.0
        polish_calls = [c for c in fake.calls
                        if "工艺问题" in str(c["messages"][-1].content)]
        assert not polish_calls

    def test_disabled_zero_extra_calls(self, client, project):
        orch, fake = self._build_orch(project, scores=[], quality_enabled=False)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn)
            assert outcome.ok
        finally:
            conn.close()
        assert outcome.quality_score is None
        score_calls = [c for c in fake.calls
                       if c["system"] and "打分" in c["system"]]
        assert not score_calls


# ── 伏笔健康度 ────────────────────────────────────────────────────────────────

class TestForeshadowHealth:
    def _seed_fs(self, project, rows):
        conn = _open_conn(project)
        for i, (label, state, due) in enumerate(rows):
            conn.execute(
                "INSERT INTO foreshadow(id, label, description, state, planted_chapter, due_chapter)"
                " VALUES(?,?,?,?,1,?)",
                (f"fs_{i}", label, label, state, due),
            )
        conn.commit()
        conn.close()

    def test_health_empty_green(self, client, project):
        r = client.get(f"/v1/{project}/foreshadow/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "green"
        assert body["open_count"] == 0

    def test_health_counts_and_status(self, client, project):
        # 已完成至第 5 章 → next=6；due<6 的未回收 = 逾期
        conn = _open_conn(project)
        conn.execute(
            "INSERT INTO pipeline_run(run_id, chapter, project_id, status)"
            " VALUES('r1', 5, ?, 'completed')", (project,),
        )
        conn.commit()
        conn.close()
        self._seed_fs(project, [
            ("黑袍人身份", "planted", 3),      # 逾期
            ("古剑来历", "overdue", 4),        # 逾期
            ("宗门大比", "planted", 7),        # 3 章内到期
            ("已回收", "paid_off", 2),         # 不计
        ])
        body = client.get(f"/v1/{project}/foreshadow/health").json()
        assert body["open_count"] == 3
        assert body["overdue_count"] == 2
        assert body["oldest_overdue_chapter"] == 3
        assert body["status"] == "yellow"
        assert [d["label"] for d in body["due_soon"]] == ["宗门大比"]

    def test_overdue_suggestion_prioritized(self, client, project):
        """逾期伏笔在「下一章」建议中置顶并带【逾期】标记。"""
        conn = _open_conn(project)
        conn.execute(
            "INSERT INTO pipeline_run(run_id, chapter, project_id, status)"
            " VALUES('r1', 5, ?, 'completed')", (project,),
        )
        conn.commit()
        conn.close()
        self._seed_fs(project, [("黑袍人身份", "planted", 3)])

        body = client.get(f"/v1/{project}/pipeline/next").json()
        assert body["sources"][0] == "foreshadow_overdue"
        assert body["suggested_goal"].startswith("【逾期伏笔")

    def test_flip_overdue_after_chapter(self, client, project):
        """生成章节后过期伏笔被翻转为 overdue。"""
        import sqlite3
        from novelforge.control_plane.orchestrator import _flip_overdue_foreshadow
        self._seed_fs(project, [("旧伏笔", "planted", 2), ("未到期", "planted", 9)])
        conn = _open_conn(project)
        _flip_overdue_foreshadow(conn, 5)
        rows = {r["label"]: r["state"] for r in
                conn.execute("SELECT label, state FROM foreshadow").fetchall()}
        conn.close()
        assert rows["旧伏笔"] == "overdue"
        assert rows["未到期"] == "planted"


# ── Autopilot 选项透传（候选数 / 质量评分 → 每章 cfg）─────────────────────────

class TestAutopilotOptionPassthrough:
    def test_n_candidates_and_quality_reach_cfg(self, client, project, monkeypatch):
        import time as _time

        import novelforge.control_plane.llm.factory as factory_mod
        from novelforge.control_plane.orchestrator import ChapterOutcome, Orchestrator

        captured: dict = {}

        def fake_generate(self, chapter, conn, *, chapter_goal="", **kw):
            captured["n_candidates"] = self._cfg.candidates.n_candidates
            captured["quality_enabled"] = self._cfg.quality.enabled
            return ChapterOutcome(chapter=chapter, ok=True)

        monkeypatch.setattr(Orchestrator, "generate_chapter", fake_generate)
        monkeypatch.setattr(factory_mod, "build_gateway", lambda cfg, ledger=None: None)

        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 1, "mode": "auto_promote",
            "n_candidates": 3, "quality_check": True,
        })
        assert r.status_code == 202
        sid = r.json()["session_id"]
        deadline = _time.time() + 5
        while _time.time() < deadline:
            s = client.get(f"/v1/{project}/autopilot/{sid}").json()
            if s["status"] not in ("running", "degraded"):
                break
            _time.sleep(0.05)
        assert s["status"] == "completed"
        assert captured == {"n_candidates": 3, "quality_enabled": True}

        # 启动参数随会话持久化（resume 沿用）
        conn = _open_conn(project)
        import json as _json
        row = conn.execute(
            "SELECT req_json FROM autopilot_sessions WHERE session_id=?", (sid,)
        ).fetchone()
        conn.close()
        req = _json.loads(row["req_json"])
        assert req["n_candidates"] == 3
        assert req["quality_check"] is True

    def test_out_of_range_n_candidates_422(self, client, project):
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 1, "mode": "auto_promote",
            "n_candidates": 9,
        })
        assert r.status_code == 422
