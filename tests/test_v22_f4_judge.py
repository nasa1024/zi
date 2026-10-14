"""v22/F4 评委去偏：正反两序判优 + 分维度质量门控。全部 FakeProvider，无网络。"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from novelforge.config import QualityConfig
from novelforge.control_plane.budget import BudgetLedger
from novelforge.control_plane.llm.fake_provider import FakeProvider
from novelforge.control_plane.llm.gateway import LLMGateway
from novelforge.craft.candidate_judge import select_best
from novelforge.craft.quality_gate import (
    MODE_DIMENSIONS,
    MODE_TOTAL,
    MODE_TOTAL_FALLBACK,
    MODE_UNSCORED,
    QualityVerdict,
    evaluate_quality,
    is_improvement,
    verdict_detail,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    import novelforge.app.autopilot_manager as ap_mod
    import novelforge.app.deps as deps_mod
    monkeypatch.setattr(deps_mod, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(deps_mod, "_REGISTRY_FILE", tmp_path / "projects.json")
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
    resp = client.post("/v1/projects", json={"name": "F4测试", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _open_conn(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


def _gw(provider) -> LLMGateway:
    return LLMGateway(provider, BudgetLedger(max_tokens=10_000_000, max_usd=100.0,
                                             max_revise_rounds=100))


def _draft_response(body: str) -> str:
    return (
        f"```draft\n{body}\n```\n"
        "```proposals\n"
        '[{"op":"add","fact_type":"power_rank","entity":"陆天",'
        '"new":{"subject":"陆天","predicate":"境界","object":"炼气一层"},'
        '"valid_from_chapter":1}]\n'
        "```"
    )


def _judge_calls(fake: FakeProvider) -> list[dict]:
    return [c for c in fake.calls
            if "### 候选" in str(c["messages"][-1].content)
            or "### Candidate" in str(c["messages"][-1].content)]


_TWO = [
    {"draft_text": "甲" * 2000, "proposals": []},
    {"draft_text": "乙" * 2100, "proposals": []},
]


# ── 1. select_best：正反两序 ──────────────────────────────────────────────────

class TestBothOrderJudge:
    def test_agree_accepts_and_averages_scores(self):
        fake = FakeProvider(responses=[
            '{"winner": 0, "scores": [8, 6], "reason": "钩子更强"}',
            # 逆序 [1, 0]：原 #0 在位置 1
            '{"winner": 1, "scores": [5, 9], "reason": "逆序也选它"}',
        ])
        r = select_best(_TWO, gateway=_gw(fake), chapter_goal="入门")
        assert r["winner"] == 0
        assert r["judge_used"] is True
        assert r["judge_disagreement"] is False
        assert r["reason"] == "钩子更强"
        assert r["scores"] == [8.5, 5.5]          # 两序均值，已映射回原下标
        assert r["quality_scores"] == [8.5, 5.5]
        first, second = r["judge_verdicts"]
        assert first["order"] == [0, 1] and first["winner"] == 0
        assert second["order"] == [1, 0] and second["winner"] == 0
        assert second["scores"] == [9.0, 5.0]
        # 第二次调用真的把候选倒序展示
        calls = _judge_calls(fake)
        assert len(calls) == 2
        prompt2 = str(calls[1]["messages"][-1].content)
        assert prompt2.index("### 候选 0\n乙") < prompt2.index("### 候选 1\n甲")

    def test_disagreement_falls_back_deterministically(self):
        # 两次都说"位置 1"：正序选 #1，逆序选 #0 → 位置偏差，不采信
        fake = FakeProvider(responses=[
            '{"winner": 1, "scores": [6, 8], "reason": "a"}',
            '{"winner": 1, "scores": [6, 8], "reason": "b"}',
        ])
        cands = [
            {"draft_text": "甲" * 2600, "proposals": []},
            {"draft_text": "乙" * 2000, "proposals": []},
        ]
        r = select_best(cands, gateway=_gw(fake))
        assert r["judge_disagreement"] is True
        assert r["reason"] == "judge_order_disagreement"
        assert r["judge_used"] is False
        assert r["winner"] == 0                     # 中文兜底：契合度并列 → 最长稿
        assert r["scores"] == [None, None]          # 不一致的评委分数不采信
        assert [v["winner"] for v in r["judge_verdicts"]] == [1, 0]

    def test_unparseable_second_call_is_disagreement(self):
        fake = FakeProvider(responses=[
            '{"winner": 1, "scores": [6, 8], "reason": "a"}',
            "评委走神了，没有 JSON",
        ])
        r = select_best(_TWO, gateway=_gw(fake))
        assert r["judge_disagreement"] is True
        assert r["reason"] == "judge_order_disagreement"
        assert r["judge_used"] is False
        assert r["winner"] == 1                     # 确定性兜底（乙更长）
        assert r["judge_verdicts"][1]["valid"] is False

    def test_unparseable_first_call_skips_second(self):
        fake = FakeProvider(responses=["不是 JSON", '{"winner": 0, "scores": [9, 1]}'])
        r = select_best(_TWO, gateway=_gw(fake))
        assert len(_judge_calls(fake)) == 1        # 单侧结论无法校验：不再浪费逆序调用
        assert r["judge_disagreement"] is None
        assert r["reason"] == "judge_unavailable_pick_longest"
        assert r["winner"] == 1

    def test_out_of_range_winner_counts_as_invalid(self):
        fake = FakeProvider(responses=['{"winner": 7, "scores": [6, 8]}'])
        r = select_best(_TWO, gateway=_gw(fake))
        assert len(_judge_calls(fake)) == 1
        assert r["judge_disagreement"] is None
        assert r["reason"] == "judge_unavailable_pick_longest"

    def test_second_call_error_is_unavailable_not_disagreement(self):
        state = {"n": 0}

        def factory(messages, model=""):
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("provider exploded")
            return '{"winner": 0, "scores": [9, 6], "reason": "a"}'

        r = select_best(_TWO, gateway=_gw(FakeProvider(factory=factory)))
        assert r["judge_disagreement"] is None      # 基础设施失败不计入不一致率
        assert r["judge_used"] is False
        assert r["reason"] == "judge_unavailable_pick_longest"
        assert r["judge_verdicts"][1]["error"] == "RuntimeError"

    def test_single_finalist_makes_no_llm_call(self):
        fake = FakeProvider(responses=['{"winner": 0, "scores": [9]}'] * 2)
        r = select_best([{"draft_text": "x" * 2000, "proposals": []}], gateway=_gw(fake))
        assert r["reason"] == "single_candidate"
        cands = [{"draft_text": "短", "proposals": []},
                 {"draft_text": "y" * 2000, "proposals": []}]
        r2 = select_best(cands, gateway=_gw(fake))
        assert r2["reason"] == "prescreen_single_survivor"
        assert fake.calls == []
        assert r["judge_disagreement"] is None and r2["judge_disagreement"] is None

    def test_both_orders_disabled_is_legacy_single_call(self):
        fake = FakeProvider(responses=['{"winner": 0, "scores": [8, 6], "reason": "旧行为"}'])
        r = select_best(_TWO, gateway=_gw(fake), judge_both_orders=False)
        assert len(_judge_calls(fake)) == 1
        assert r["winner"] == 0
        assert r["judge_used"] is True
        assert r["judge_disagreement"] is None
        assert r["reason"] == "旧行为"
        assert r["scores"] == [8.0, 6.0]

    def test_three_finalists_mapping(self):
        # 固定"位置 1"：倒序后中间候选仍在中间 → 一致；分数按原下标取均值
        fake = FakeProvider(responses=['{"winner": 1, "scores": [6, 9, 7], "reason": "节奏"}'] * 2)
        cands = [{"draft_text": c * 2000, "proposals": []} for c in "甲乙丙"]
        r = select_best(cands, gateway=_gw(fake))
        assert r["winner"] == 1
        assert r["judge_disagreement"] is False
        assert r["scores"] == [6.5, 9.0, 6.5]

    def test_scores_coerced_and_clamped(self):
        fake = FakeProvider(responses=[
            '{"winner": 0, "scores": [15, "abc"], "reason": "a"}',
            '{"winner": 1, "scores": [null, 9], "reason": "b"}',
        ])
        r = select_best(_TWO, gateway=_gw(fake))
        assert r["winner"] == 0
        assert r["scores"] == [9.5, None]           # (10 + 9) / 2；非数值 → None

    def test_english_disagreement_uses_stable_profile_fit_fallback(self):
        profile = {
            "content_language": "en-US",
            "writing_profile_id": "en_progression_serial_v1",
            "writing_profile_version": 1,
            "prompt_rules": {"pov": "third_person_limited", "tense": "past"},
            "quality_rules": {
                "chapter_word_range": [20, 40], "hard_min_words": 10,
                "max_pov_shifts": 0, "tense_consistency": True,
                "requires_progress_event": False, "content_warnings": [],
                "readability_min_words": 100,
            },
        }
        cands = [{"draft_text": " ".join(["river"] * 25), "proposals": []},
                 {"draft_text": " ".join(["stone"] * 25), "proposals": []}]
        fake = FakeProvider(responses=[
            '{"winner": 1, "scores": [6, 8], "reason": "a"}',
            '{"winner": 1, "scores": [6, 8], "reason": "b"}',
        ])
        r = select_best(cands, gateway=_gw(fake), writing_profile=profile)
        assert r["judge_disagreement"] is True
        assert r["reason"] == "judge_order_disagreement"
        assert r["winner"] == 0                     # 英文：稳定下标，不回到"最长稿"
        prompts = [str(c["messages"][-1].content) for c in _judge_calls(fake)]
        assert len(prompts) == 2
        assert all("### Candidate 0" in p for p in prompts)
        assert not any("候选" in p for p in prompts)


# ── 2. 质量门判定 ────────────────────────────────────────────────────────────

class TestQualityGate:
    def test_dimension_mode_flags_low_dimension_even_with_high_total(self):
        v = evaluate_quality(8.5, {"hook": 3.0, "pacing": 9, "character": 9, "prose": 9},
                             QualityConfig())
        assert v.low is True
        assert v.mode == MODE_DIMENSIONS
        assert v.reasons == ["hook 3<5"]

    def test_dimension_mode_passes_low_total_when_dimensions_pass(self):
        v = evaluate_quality(5.5, {"hook": 5.5, "pacing": 5.5, "character": 5.5, "prose": 5.5},
                             QualityConfig())
        assert v == QualityVerdict(low=False, reasons=[], mode=MODE_DIMENSIONS)

    def test_dimension_mode_without_dimensions_falls_back_to_total(self):
        assert evaluate_quality(4.0, {}, QualityConfig()) == QualityVerdict(
            low=True, reasons=["score 4<6"], mode=MODE_TOTAL_FALLBACK)
        assert evaluate_quality(7.0, None, QualityConfig()).low is False
        # 维度全是无下限/非数值 → 同样回落总分
        v = evaluate_quality(4.0, {"tension": 1, "hook": "n/a"}, QualityConfig())
        assert v.mode == MODE_TOTAL_FALLBACK and v.low is True

    def test_total_mode_ignores_dimensions(self):
        qcfg = QualityConfig(gate_mode="total")
        assert evaluate_quality(8.5, {"hook": 1.0}, qcfg) == QualityVerdict(
            low=False, reasons=[], mode=MODE_TOTAL)
        assert evaluate_quality(5.9, {"hook": 9.0}, qcfg).low is True

    def test_unknown_mode_is_legacy_total(self):
        v = evaluate_quality(5.0, {"hook": 9.0}, QualityConfig(gate_mode="vibes"))
        assert v.mode == MODE_TOTAL and v.low is True

    def test_unscored_is_never_low(self):
        assert evaluate_quality(None, None, QualityConfig()) == QualityVerdict(
            low=False, reasons=[], mode=MODE_UNSCORED)

    def test_custom_thresholds_and_partial_dimensions(self):
        qcfg = QualityConfig(dimension_min={"hook": 7.0, "prose": 4.0})
        v = evaluate_quality(9.0, {"hook": 6.5, "pacing": 1.0}, qcfg)
        assert v.reasons == ["hook 6.5<7"]          # pacing 无下限、prose 缺分 → 不判

    def test_stored_detail_round_trips_as_config(self):
        qcfg = QualityConfig(gate_mode="dimensions", min_score=7.0,
                             dimension_min={"hook": 6.0})
        stored = verdict_detail(evaluate_quality(8.0, {"hook": 5.0}, qcfg), qcfg)
        assert stored == {"low": True, "reasons": ["hook 5<6"], "mode": "dimensions",
                          "gate_mode": "dimensions", "min_score": 7.0,
                          "dimension_min": {"hook": 6.0}}
        assert json.loads(json.dumps(stored)) == stored
        # 复算：同样的阈值口径（dict 也能当 qcfg 用）
        assert evaluate_quality(8.0, {"hook": 6.5}, stored).low is False
        assert evaluate_quality(6.5, None, stored).low is True   # 回落 min_score=7

    def test_is_improvement(self):
        qcfg = QualityConfig()
        low_hook = evaluate_quality(8.0, {"hook": 3, "pacing": 9}, qcfg)
        fixed = evaluate_quality(7.5, {"hook": 6, "pacing": 8}, qcfg)
        assert is_improvement(8.0, low_hook, 7.5, fixed)        # 修好低分维度，总分略降也保留
        assert not is_improvement(7.5, fixed, 8.0, low_hook)
        same = evaluate_quality(7.0, {"hook": 6, "pacing": 7}, qcfg)
        assert is_improvement(7.0, same, 7.0, same)
        assert not is_improvement(7.5, fixed, 7.0, same)
        assert not is_improvement(7.0, same, None, same)
        # 总分口径：等价于旧规则 new >= old
        t = QualityConfig(gate_mode="total")
        assert is_improvement(4.0, evaluate_quality(4.0, None, t), 5.0, evaluate_quality(5.0, None, t))
        assert not is_improvement(4.0, evaluate_quality(4.0, None, t), 3.0, evaluate_quality(3.0, None, t))


# ── 3. 流水线：_quality_pass 按维度门控润色 ─────────────────────────────────

def _quality_orch(project, *, scores, judge=None, n_candidates=1, gate_mode="dimensions"):
    from novelforge.config import NovelForgeConfig
    from novelforge.control_plane.orchestrator import Orchestrator
    from novelforge.control_plane.skill_registry import SkillRegistry
    from novelforge.skills import register_default_skills

    queue = list(scores)

    def factory(messages, model="", temperature=1.0):
        user = str(messages[-1].content) if messages else ""
        if "## 规划任务" in user:
            return ('[{"beat_type":"setup","summary":"铺垫","value_axis":"平静→紧张"},'
                    '{"beat_type":"hook","summary":"悬念","value_axis":"紧张→悬念"}]')
        if "本章任务" in user:
            idx = round((1.0 - temperature) / 0.15)
            return _draft_response(f"初稿{idx}号正文，节奏平平。" * 100)
        if "### 候选" in user:
            return judge or '{"winner": 1, "scores": [6, 9, 7], "reason": "节奏"}'
        if "一致性问题" in user:
            return "修订稿正文。" * 150
        if "工艺问题" in user:
            return "润色后正文，钩子拉满。" * 100
        if "本章目标" in user:
            return queue.pop(0) if queue else '{"score": 5}'
        if "未解伏笔" in user:
            return '{"settlements": [], "new_hooks": []}'
        if "章正文：" in user:
            return "本章摘要。"
        return "[]"

    fake = FakeProvider(factory=factory)
    cfg = NovelForgeConfig(project_id=project)
    cfg.provider.provider = "fake"
    cfg.recall.enable_summaries = False
    cfg.settle.enabled = False
    cfg.quality.enabled = True
    cfg.quality.gate_mode = gate_mode
    cfg.candidates.n_candidates = n_candidates
    reg = SkillRegistry()
    register_default_skills(reg)
    return Orchestrator(_gw(fake), reg, cfg), fake


def _polish_calls(fake):
    return [c for c in fake.calls if "工艺问题" in str(c["messages"][-1].content)]


def _score_calls(fake):
    return [c for c in fake.calls if c["system"] and "打分" in c["system"]]


_LOW_HOOK = ('{"score": 8.5, "dimensions": {"hook": 3, "pacing": 9, "character": 9,'
             ' "prose": 9}, "reason": "结尾平"}')
_ALL_SEVEN = ('{"score": 7.0, "dimensions": {"hook": 7, "pacing": 7, "character": 7,'
              ' "prose": 7}, "reason": "均衡"}')
_ALL_MID = ('{"score": 5.5, "dimensions": {"hook": 5.5, "pacing": 5.5, "character": 5.5,'
            ' "prose": 5.5}, "reason": "及格"}')


class TestQualityPassGate:
    def _run(self, orch):
        stages: list[tuple] = []
        conn = _open_conn(orch._cfg.project_id)
        try:
            outcome = orch.generate_chapter(
                1, conn, chapter_goal="入门",
                progress_cb=lambda s, st, d: stages.append((s, st, d)),
            )
            assert outcome.ok, outcome.error
            row = conn.execute("SELECT detail_json, quality_score FROM pipeline_run"
                               " WHERE run_id=?", (outcome.run_id,)).fetchone()
        finally:
            conn.close()
        return outcome, json.loads(row["detail_json"]), row, stages

    def test_low_dimension_triggers_polish_despite_high_total(self, client, project):
        orch, fake = _quality_orch(project, scores=[_LOW_HOOK, _ALL_SEVEN])
        outcome, detail, row, stages = self._run(orch)
        assert _polish_calls(fake)
        # 润色修好了 hook（总分 8.5→7.0 略降）→ 按"未过线项更少"保留润色稿
        assert "润色后正文" in outcome.draft_text
        assert outcome.quality_score == 7.0 == row["quality_score"]
        assert outcome.quality_dimensions == {"hook": 7.0, "pacing": 7.0,
                                              "character": 7.0, "prose": 7.0}
        gate = detail["quality_gate"]
        assert gate["low"] is False and gate["mode"] == "dimensions"
        assert gate["dimension_min"]["hook"] == 5.0
        quality_event = next(d for (s, _, d) in stages if s == "quality")
        assert quality_event["gate"]["low"] is False

    def test_all_dimensions_pass_no_polish_even_with_low_total(self, client, project):
        orch, fake = _quality_orch(project, scores=[_ALL_MID])
        outcome, detail, _, _ = self._run(orch)
        assert not _polish_calls(fake)
        assert outcome.quality_score == 5.5
        assert detail["quality_gate"]["low"] is False
        assert detail["quality_gate"]["reasons"] == []

    def test_total_mode_keeps_legacy_threshold(self, client, project):
        orch, fake = _quality_orch(project, scores=[_ALL_MID, _ALL_SEVEN], gate_mode="total")
        outcome, detail, _, _ = self._run(orch)
        assert _polish_calls(fake)                  # 5.5 < 6.0 → 旧口径润色
        assert detail["quality_gate"]["mode"] == "total"

    def test_polish_still_failing_gate_and_worse_reverts(self, client, project):
        worse = ('{"score": 8.0, "dimensions": {"hook": 2, "pacing": 4, "character": 9,'
                 ' "prose": 9}}')
        orch, fake = _quality_orch(project, scores=[_LOW_HOOK, worse])
        outcome, detail, _, _ = self._run(orch)
        assert _polish_calls(fake)
        assert "润色后正文" not in outcome.draft_text
        assert outcome.quality_dimensions["hook"] == 3.0
        assert detail["quality_gate"]["low"] is True
        assert detail["quality_gate"]["reasons"] == ["hook 3<5"]

    def test_multi_candidate_dimension_mode_scores_winner_once(self, client, project):
        orch, fake = _quality_orch(project, scores=[_ALL_SEVEN], n_candidates=3)
        outcome, detail, _, stages = self._run(orch)
        assert detail["judge_disagreement"] is False
        assert len(_judge_calls(fake)) == 2
        assert len(_score_calls(fake)) == 1         # 评委无维度 → 补一次分维度评分
        assert outcome.quality_dimensions["hook"] == 7.0
        assert outcome.quality_score == 7.0
        assert detail["quality_gate"]["mode"] == "dimensions"
        cand_event = next(d for (s, _, d) in stages if s == "candidates")
        assert cand_event["judge_disagreement"] is False

    def test_multi_candidate_total_mode_reuses_judge_score(self, client, project):
        orch, fake = _quality_orch(project, scores=[], n_candidates=3, gate_mode="total")
        outcome, detail, _, _ = self._run(orch)
        assert _score_calls(fake) == []             # 旧口径：复用评委（两序均值）分
        assert outcome.quality_score == 9.0
        assert detail["quality_gate"]["mode"] == "total"

    def test_multi_candidate_disagreement_recorded(self, client, project):
        orch, fake = _quality_orch(project, scores=[_ALL_SEVEN], n_candidates=2,
                                   judge='{"winner": 0, "scores": [8, 6], "reason": "x"}')
        _, detail, _, _ = self._run(orch)
        assert detail["judge_disagreement"] is True
        assert detail["reason"] == "judge_order_disagreement"
        assert len(detail["judge_verdicts"]) == 2


# ── 4. autopilot 低质量计数 ─────────────────────────────────────────────────

def _wait_terminal(client, project, sid, timeout=15.0):
    deadline = time.time() + timeout
    s = {}
    while time.time() < deadline:
        s = client.get(f"/v1/{project}/autopilot/{sid}").json()
        if s["status"] not in ("running", "degraded"):
            return s
        time.sleep(0.05)
    return s


class TestAutopilotQualityGate:
    def _patch(self, monkeypatch, *, score, dims):
        from novelforge.control_plane.orchestrator import ChapterOutcome, Orchestrator

        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        monkeypatch.setenv("NOVELFORGE_PROVIDER", "fake")

        def fake_generate(self, chapter, conn, **kw):
            return ChapterOutcome(chapter=chapter, ok=True, run_id=f"run-{chapter}",
                                  draft_text="x" * 100, issues=[],
                                  usage_tokens=10, usage_usd=0.001,
                                  quality_score=score, quality_dimensions=dims)
        monkeypatch.setattr(Orchestrator, "generate_chapter", fake_generate)

    def _start(self, client, project):
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 3, "mode": "auto_promote",
            "auto_degrade_after_consecutive_issues": 2, "quality_check": True,
        })
        assert r.status_code == 202, r.text
        return _wait_terminal(client, project, r.json()["session_id"])

    def test_low_dimension_counts_as_low_quality(self, client, project, monkeypatch):
        self._patch(monkeypatch, score=8.5,
                    dims={"hook": 3.0, "pacing": 9.0, "character": 9.0, "prose": 9.0})
        s = self._start(client, project)
        assert s["status"] == "paused"
        assert s["policy_mode"] == "human_gate"
        assert s["chapters_done"] == 2

    def test_passing_dimensions_with_low_total_not_counted(self, client, project, monkeypatch):
        self._patch(monkeypatch, score=5.5,
                    dims={"hook": 5.5, "pacing": 5.5, "character": 5.5, "prose": 5.5})
        s = self._start(client, project)
        assert s["status"] == "completed"
        assert s["policy_mode"] == "auto_promote"
        assert s["chapters_done"] == 3

    def test_no_dimensions_falls_back_to_total(self, client, project, monkeypatch):
        self._patch(monkeypatch, score=4.0, dims=None)
        s = self._start(client, project)
        assert s["status"] == "paused"
        assert s["chapters_done"] == 2


# ── 5. 看板：不一致率 + 分维度低质量计数 ─────────────────────────────────────

class TestStats:
    def _seed(self, conn, project, run_id, chapter, score, detail):
        conn.execute(
            "INSERT INTO pipeline_run(run_id, chapter, project_id, status, quality_score,"
            " detail_json, started_at) VALUES(?, ?, ?, 'completed', ?, ?, ?)",
            (run_id, chapter, project, score,
             json.dumps(detail, ensure_ascii=False) if detail is not None else None,
             f"2026-06-01T00:00:{chapter:02d}"),
        )

    def test_disagreement_rate_and_dimension_low_count(self, client, project):
        conn = _open_conn(project)
        stored_gate = verdict_detail(
            evaluate_quality(8.5, {"hook": 3.0}, QualityConfig()), QualityConfig())
        self._seed(conn, project, "r1", 1, 8.5,
                   {"judge_disagreement": False, "quality_dimensions": {"hook": 3.0},
                    "quality_gate": stored_gate})
        self._seed(conn, project, "r2", 2, 5.5,
                   {"judge_disagreement": True,
                    "quality_dimensions": {"hook": 5.5, "pacing": 5.5}})
        self._seed(conn, project, "r3", 3, 4.0, {"judge_disagreement": True})
        self._seed(conn, project, "r4", 4, 7.0, None)            # 旧数据：无 detail
        self._seed(conn, project, "r5", 5, None, {"judge_disagreement": None})
        # 存储的阈值优先于默认值：hook 5.5 在默认下限 5 之上，但该 run 当时要求 6
        strict = {"low": False, "reasons": [], "mode": "dimensions",
                  "gate_mode": "dimensions", "min_score": 6.0, "dimension_min": {"hook": 6.0}}
        self._seed(conn, project, "r6", 6, 9.0,
                   {"quality_dimensions": {"hook": 5.5}, "quality_gate": strict})
        conn.commit()
        conn.close()

        body = client.get(f"/v1/{project}/pipeline/stats").json()
        by_ch = {s["chapter"]: s for s in body["series"]}
        assert body["judge_runs"] == 3
        assert body["judge_disagreements"] == 2
        assert body["judge_disagreement_rate"] == pytest.approx(0.667)
        assert [by_ch[c]["judge_disagreement"] for c in range(1, 7)] == [
            False, True, True, None, None, None]
        # 1: hook 3 低；2: 维度达标（总分 5.5 也不算低）；3: 无维度 → 4<6 低；
        # 4: 旧口径 7 ≥ 6；5: 未评分；6: 落库阈值 hook≥6 → 低
        assert [by_ch[c]["quality_low"] for c in range(1, 7)] == [
            True, False, True, False, None, True]
        assert by_ch[1]["quality_gate_reasons"] == ["hook 3<5"]
        assert by_ch[6]["quality_gate_reasons"] == ["hook 5.5<6"]
        assert body["low_quality_count"] == 3

    def test_empty_project_has_no_rate(self, client, project):
        body = client.get(f"/v1/{project}/pipeline/stats").json()
        assert body["judge_runs"] == 0
        assert body["judge_disagreement_rate"] is None


# ── 6. 人工换稿后质量门随之复算 ─────────────────────────────────────────────

class TestSelectCandidateRefreshesGate:
    def test_swap_recomputes_quality_gate(self, client, project, monkeypatch):
        orch, _ = _quality_orch(project, scores=[_ALL_SEVEN], n_candidates=3)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="入门")
            assert outcome.ok, outcome.error
        finally:
            conn.close()

        def scorer(messages, model="", temperature=1.0):
            user = str(messages[-1].content) if messages else ""
            if "本章目标" in user:
                return _LOW_HOOK
            if "章正文：" in user:
                return "本章摘要。"
            return "[]"

        import novelforge.control_plane.llm.runtime as runtime_mod
        monkeypatch.setattr(runtime_mod, "build_project_gateway",
                            lambda *a, **k: _gw(FakeProvider(factory=scorer)))
        r = client.post(f"/v1/{project}/pipeline/runs/{outcome.run_id}/select-candidate",
                        json={"candidate_index": 2})
        assert r.status_code == 200, r.text
        conn = _open_conn(project)
        try:
            detail = json.loads(conn.execute(
                "SELECT detail_json FROM pipeline_run WHERE run_id=?", (outcome.run_id,),
            ).fetchone()["detail_json"])
        finally:
            conn.close()
        assert detail["quality_gate"]["low"] is True
        assert detail["quality_gate"]["reasons"] == ["hook 3<5"]
        body = client.get(f"/v1/{project}/pipeline/stats").json()
        assert body["series"][0]["quality_low"] is True
        assert body["low_quality_count"] == 1


# ── 7. 决策评委（AI 代选）同样正反两序 ──────────────────────────────────────

class TestDecisionJudgeBothOrders:
    def _decision(self):
        return {
            "stage": "story_plan", "question": "选择路线",
            "recommended_option_id": "opt0", "context": {},
            "options": [
                {"id": f"opt{i}", "option_index": i, "title": f"方案{i}",
                 "summary": "摘要", "payload": {}, "impact": {}, "score": 8 - i}
                for i in range(3)
            ],
        }

    def _run(self, monkeypatch, responses, **kw):
        import novelforge.planning.generation as generation

        monkeypatch.delenv("NOVELFORGE_PROVIDER", raising=False)
        fake = FakeProvider(responses=responses)
        monkeypatch.setattr(generation, "_gateway", lambda *a, **k: _gw(fake))
        option_id, meta = generation.recommend_decision_option(
            self._decision(), project_id="p", db_path=":memory:", **kw,
        )
        return option_id, meta, fake

    def test_disagreement_falls_back_to_scores(self, monkeypatch):
        # 两次都选"位置 2"：正序→opt2，逆序→opt0，不一致
        option_id, meta, fake = self._run(monkeypatch, [
            '{"option_index": 2, "reason": "a"}', '{"option_index": 2, "reason": "b"}',
        ])
        assert len(fake.calls) == 2
        assert option_id == "opt0"                  # 确定性：分数最高
        assert meta["source"] == "deterministic_fallback"
        assert meta["judge_disagreement"] is True
        assert [v["option_index"] for v in meta["judge_verdicts"]] == [2, 0]
        # 逆序调用里选项按倒序展示且重新编号
        payload = json.loads(str(fake.calls[1]["messages"][-1].content))
        assert [o["title"] for o in payload["options"]] == ["方案2", "方案1", "方案0"]
        assert [o["option_index"] for o in payload["options"]] == [0, 1, 2]

    def test_disabled_is_single_call(self, monkeypatch):
        option_id, meta, fake = self._run(
            monkeypatch, ['{"option_index": 1, "reason": "a"}'], judge_both_orders=False,
        )
        assert len(fake.calls) == 1
        assert option_id == "opt1"
        assert meta["source"] == "llm_judge"
        assert meta["judge_disagreement"] is None
