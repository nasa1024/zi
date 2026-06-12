"""M3 测试：① 章节多候选 + 评分择优。全部 FakeProvider，无网络。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from novelforge.craft.candidate_judge import _parse_verdict, select_best


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
    resp = client.post("/v1/projects", json={"name": "M3测试", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _open_conn(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


def _draft_response(body: str, obj: str = "炼气一层") -> str:
    return (
        f"```draft\n{body}\n```\n"
        "```proposals\n"
        f'[{{"op":"add","fact_type":"power_rank","entity":"陆天",'
        f'"new":{{"subject":"陆天","predicate":"境界","object":"{obj}"}},'
        f'"valid_from_chapter":1}}]\n'
        "```"
    )


# ── select_best 单元测试 ──────────────────────────────────────────────────────

class TestSelectBest:
    def test_single_candidate_passthrough(self):
        r = select_best([{"draft_text": "x" * 2000, "proposals": [{}]}])
        assert r["winner"] == 0
        assert r["reason"] == "single_candidate"
        assert not r["judge_used"]

    def test_prescreen_eliminates_short_and_proposalless(self):
        cands = [
            {"draft_text": "短", "proposals": [{}]},            # 太短
            {"draft_text": "y" * 2000, "proposals": []},        # 无提案
            {"draft_text": "z" * 2000, "proposals": [{}]},      # 合格
        ]
        r = select_best(cands)
        assert r["winner"] == 2
        assert r["reason"] == "prescreen_single_survivor"
        assert not r["judge_used"]

    def test_all_failed_picks_longest(self):
        cands = [
            {"draft_text": "短稿一", "proposals": []},
            {"draft_text": "稍长一点的短稿", "proposals": []},
        ]
        r = select_best(cands)
        assert r["winner"] == 1
        assert r["reason"] == "prescreen_all_failed_pick_longest"

    def test_judge_picks_among_finalists(self):
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.fake_provider import FakeProvider
        from novelforge.control_plane.llm.gateway import LLMGateway

        fake = FakeProvider(responses=['{"winner": 1, "scores": [6.5, 8.0], "reason": "钩子更强"}'])
        gw = LLMGateway(fake, BudgetLedger(max_tokens=1_000_000, max_usd=10.0))
        cands = [
            {"draft_text": "a" * 2000, "proposals": [{}]},
            {"draft_text": "b" * 2000, "proposals": [{}]},
        ]
        r = select_best(cands, gateway=gw, chapter_goal="入门")
        assert r["winner"] == 1
        assert r["judge_used"]
        assert r["scores"][1] == 8.0
        assert r["reason"] == "钩子更强"

    def test_judge_failure_falls_back_to_longest(self):
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.fake_provider import FakeProvider
        from novelforge.control_plane.llm.gateway import LLMGateway

        fake = FakeProvider(responses=["这不是 JSON"])
        gw = LLMGateway(fake, BudgetLedger(max_tokens=1_000_000, max_usd=10.0))
        cands = [
            {"draft_text": "a" * 3000, "proposals": [{}]},
            {"draft_text": "b" * 2000, "proposals": [{}]},
        ]
        r = select_best(cands, gateway=gw)
        assert r["winner"] == 0
        assert r["reason"] == "judge_unavailable_pick_longest"

    def test_parse_verdict_rescues_wrapped_json(self):
        assert _parse_verdict('评委结论：{"winner": 0, "scores": [9], "reason": "好"}') == (0, [9], "好")
        assert _parse_verdict("没有 JSON") is None
        assert _parse_verdict('{"no_winner": 1}') is None


# ── 流水线级集成 ──────────────────────────────────────────────────────────────

class TestPipelineWithCandidates:
    def _build_orch(self, project, n_candidates, draft_responses, judge_response=None):
        from novelforge.config import NovelForgeConfig
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.fake_provider import FakeProvider
        from novelforge.control_plane.llm.gateway import LLMGateway
        from novelforge.control_plane.orchestrator import Orchestrator
        from novelforge.control_plane.skill_registry import SkillRegistry
        from novelforge.skills import register_default_skills

        drafts = list(draft_responses)

        # 并行候选生成后调用顺序不再确定，按温度映射候选序号（i → 1.0 - i*0.15）
        def factory(messages, model="", temperature=1.0):
            user = str(messages[-1].content) if messages else ""
            if "本章任务" in user:
                idx = round((1.0 - temperature) / 0.15)
                if 0 <= idx < len(drafts):
                    return drafts[idx]
                return _draft_response("兜底" * 600)
            if "### 候选" in user:
                return judge_response or '{"winner": 0, "scores": [7], "reason": "默认"}'
            if "一致性问题" in user:
                # revise 调用：保留胜者正文标记（fake beats 为空必触发 beat_contract block）
                marker = "候选一号正文" if "候选一号正文" in user else "单稿正文"
                return f"修订后：{marker}。" * 200
            if "章正文：" in user:
                return "本章摘要。"
            return "[]"

        fake = FakeProvider(factory=factory)
        gw = LLMGateway(fake, BudgetLedger(max_tokens=10_000_000, max_usd=100.0,
                                           max_revise_rounds=100))
        reg = SkillRegistry()
        register_default_skills(reg)
        cfg = NovelForgeConfig(project_id=project)
        cfg.provider.provider = "fake"
        cfg.candidates.n_candidates = n_candidates
        cfg.settle.enabled = False   # M3 只测候选机制；伏笔结算见 test_p1_core
        return Orchestrator(gw, reg, cfg), fake

    def test_three_candidates_winner_selected_and_persisted(self, client, project):
        """3 候选：评委选 #1；胜者正文落盘、报告进 detail_json、stage 事件发出。"""
        orch, fake = self._build_orch(
            project, 3,
            draft_responses=[
                _draft_response("候选零号正文。" * 200),
                _draft_response("候选一号正文。" * 200),
                _draft_response("候选二号正文。" * 200),
            ],
            judge_response='{"winner": 1, "scores": [6, 9, 7], "reason": "节奏最佳"}',
        )
        stages: list[tuple] = []
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(
                1, conn, chapter_goal="入门",
                progress_cb=lambda s, st, d: stages.append((s, st, d)),
            )
            assert outcome.ok, outcome.error
            assert "候选一号正文" in outcome.draft_text

            row = conn.execute(
                "SELECT detail_json FROM pipeline_run WHERE run_id=?",
                (outcome.run_id,),
            ).fetchone()
        finally:
            conn.close()

        detail = json.loads(row["detail_json"])
        assert detail["winner"] == 1
        assert detail["judge_used"] is True
        assert detail["n_candidates"] == 3

        cand_events = [d for (s, _, d) in stages if s == "candidates"]
        assert cand_events and cand_events[0]["winner"] == 1

        # 3 次 draft 调用且温度梯度生效（并行下顺序不定，比对集合）
        draft_calls = [c for c in fake.calls
                       if "本章任务" in str(c["messages"][-1].content)]
        assert len(draft_calls) == 3
        temps = sorted(round(c["temperature"], 2) for c in draft_calls)
        assert temps == [0.7, 0.85, 1.0]

    def test_single_candidate_no_judge_no_detail(self, client, project):
        """n=1：行为同旧版，无候选事件、无 detail_json。"""
        orch, fake = self._build_orch(
            project, 1, draft_responses=[_draft_response("单稿正文。" * 200)],
        )
        stages: list[tuple] = []
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(
                1, conn, progress_cb=lambda s, st, d: stages.append((s, st, d)),
            )
            assert outcome.ok
            row = conn.execute(
                "SELECT detail_json FROM pipeline_run WHERE run_id=?",
                (outcome.run_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["detail_json"] is None
        assert not any(s == "candidates" for (s, _, _) in stages)
        draft_calls = [c for c in fake.calls
                       if "本章任务" in str(c["messages"][-1].content)]
        assert len(draft_calls) == 1

    def test_api_n_candidates_clamped(self, client, project):
        """API 请求带 n_candidates 时进 config（FakeProvider 下端点不崩溃）。"""
        r = client.post(f"/v1/{project}/pipeline/run", json={
            "chapter_no": 1, "chapter_goal": "测试", "n_candidates": 3,
        })
        assert r.status_code == 200
        # 超界值被 pydantic 校验拒绝
        r2 = client.post(f"/v1/{project}/pipeline/run", json={
            "chapter_no": 1, "n_candidates": 9,
        })
        assert r2.status_code == 422
