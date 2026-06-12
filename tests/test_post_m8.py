"""Post-M8 改进测试：候选并行生成 / 预算账本线程安全 / 逐章成本入库 / 质量分维度化。

全部 FakeProvider，无网络。
"""
from __future__ import annotations

import json
import re
import threading

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
    resp = client.post("/v1/projects", json={"name": "PostM8测试", "genre": "xuanhuan"})
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


def _build_orch(project, factory, *, n_candidates=1, quality=False):
    from novelforge.config import NovelForgeConfig
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.orchestrator import Orchestrator
    from novelforge.control_plane.skill_registry import SkillRegistry
    from novelforge.skills import register_default_skills

    fake = FakeProvider(factory=factory)
    gw = LLMGateway(fake, BudgetLedger(max_tokens=10_000_000, max_usd=100.0,
                                       max_revise_rounds=100))
    reg = SkillRegistry()
    register_default_skills(reg)
    cfg = NovelForgeConfig(project_id=project)
    cfg.provider.provider = "fake"
    cfg.candidates.n_candidates = n_candidates
    cfg.quality.enabled = quality
    cfg.recall.enable_summaries = False
    return Orchestrator(gw, reg, cfg), fake


# ── 候选并行生成 ──────────────────────────────────────────────────────────────

class TestParallelCandidates:
    def test_three_candidates_run_concurrently(self, client, project):
        """draft 调用在 Barrier(3) 处会合：串行永远凑不齐 3 方 → 超时失败；
        只有三个候选真正并行时才能全部通过。"""
        barrier = threading.Barrier(3, timeout=15)

        def factory(messages, model="", temperature=1.0):
            user = str(messages[-1].content) if messages else ""
            if "本章任务" in user:
                barrier.wait()  # 串行 → BrokenBarrierError → 候选全灭 → outcome 失败
                idx = round((1.0 - temperature) / 0.15)
                return _draft_response(f"并行候选{idx}正文。" * 200)
            if "### 候选" in user:
                return '{"winner": 0, "scores": [7, 7, 7], "reason": "默认"}'
            if "一致性问题" in user:
                return "修订后正文。" * 200
            if "章正文：" in user:
                return "本章摘要。"
            return "[]"

        orch, fake = _build_orch(project, factory, n_candidates=3)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="入门")
        finally:
            conn.close()
        assert outcome.ok, f"并行候选生成失败（疑似回退串行）: {outcome.error}"
        draft_calls = [c for c in fake.calls
                       if "本章任务" in str(c["messages"][-1].content)]
        assert len(draft_calls) == 3

    def test_one_failed_candidate_does_not_kill_others(self, client, project):
        """单个候选抛异常 → 该候选按失败计，其余候选照常参与择优。"""
        def factory(messages, model="", temperature=1.0):
            user = str(messages[-1].content) if messages else ""
            if "本章任务" in user:
                idx = round((1.0 - temperature) / 0.15)
                if idx == 1:
                    raise RuntimeError("候选 1 编造的网络错误")
                return _draft_response(f"幸存候选{idx}正文。" * 200)
            if "### 候选" in user:
                return '{"winner": 0, "scores": [8, 7], "reason": "默认"}'
            if "一致性问题" in user:
                # revise 调用：保留胜者正文标记，便于断言胜者身份
                m = re.search(r"幸存候选\d", user)
                return f"修订后：{m.group(0) if m else '无标记'}正文。" * 150
            if "章正文：" in user:
                return "本章摘要。"
            return "[]"

        orch, _ = _build_orch(project, factory, n_candidates=3)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="入门")
        finally:
            conn.close()
        assert outcome.ok, outcome.error
        assert "幸存候选" in outcome.draft_text


# ── 预算账本线程安全 ──────────────────────────────────────────────────────────

class TestLedgerThreadSafety:
    def test_concurrent_charges_are_atomic(self):
        from novelforge.control_plane.budget import BudgetLedger

        class _U:
            input = 7
            output = 13
            model = ""
            cache_read = 3

        ledger = BudgetLedger(max_tokens=10**9, max_usd=10**9)
        n_threads, n_charges = 8, 500

        def worker():
            for _ in range(n_charges):
                ledger.charge(_U())
                ledger.charge_revise_round()

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = n_threads * n_charges
        assert ledger.tokens_spent == total * 20
        assert ledger.cache_read_tokens == total * 3
        assert ledger.revise_rounds == total


# ── 逐章成本入库 ──────────────────────────────────────────────────────────────

class TestCostPersisted:
    def _run_chapter(self, project, chapter=1):
        def factory(messages, model="", temperature=1.0):
            user = str(messages[-1].content) if messages else ""
            if "本章任务" in user:
                return _draft_response("单稿正文。" * 200)
            if "一致性问题" in user:
                return "修订后：单稿正文。" * 150
            if "章正文：" in user:
                return "本章摘要。"
            return "[]"

        orch, _ = _build_orch(project, factory)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(chapter, conn, chapter_goal="入门")
            assert outcome.ok, outcome.error
        finally:
            conn.close()
        return outcome

    def test_tokens_and_usd_written_to_pipeline_run(self, client, project):
        outcome = self._run_chapter(project)
        conn = _open_conn(project)
        try:
            row = conn.execute(
                "SELECT tokens_spent, usd_spent FROM pipeline_run WHERE run_id=?",
                (outcome.run_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["tokens_spent"] == outcome.usage_tokens > 0
        assert row["usd_spent"] is not None and row["usd_spent"] > 0

    def test_cost_in_runs_list_and_stats(self, client, project):
        self._run_chapter(project, chapter=1)
        runs = client.get(f"/v1/{project}/pipeline/runs").json()
        completed = [r for r in runs if r["status"] == "completed"]
        assert completed and completed[0]["tokens_spent"] > 0
        assert completed[0]["usd_spent"] > 0

        stats = client.get(f"/v1/{project}/pipeline/stats").json()
        assert stats["total_tokens_spent"] > 0
        assert stats["total_usd_spent"] > 0
        assert stats["series"][0]["tokens_spent"] == completed[0]["tokens_spent"]


# ── 质量分维度化 ──────────────────────────────────────────────────────────────

class TestDimensionalScore:
    def _gw(self, responses):
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.fake_provider import FakeProvider
        from novelforge.control_plane.llm.gateway import LLMGateway
        return LLMGateway(FakeProvider(responses=responses),
                          BudgetLedger(max_tokens=10**7, max_usd=100.0))

    def test_detailed_parses_dimensions_and_clamps(self):
        from novelforge.craft.candidate_judge import score_chapter_detailed

        gw = self._gw(['{"score": 7.5, "dimensions": {"hook": 8, "pacing": 7,'
                       ' "character": 11.0, "prose": 6, "junk": 99}, "reason": "ok"}'])
        d = score_chapter_detailed(gw, "mid", "目标", "正文" * 500)
        assert d["score"] == 7.5
        assert d["dimensions"] == {"hook": 8.0, "pacing": 7.0,
                                   "character": 10.0, "prose": 6.0}  # 截到 10，junk 忽略
        assert d["reason"] == "ok"

    def test_missing_score_falls_back_to_dimension_average(self):
        from novelforge.craft.candidate_judge import score_chapter_detailed

        gw = self._gw(['{"dimensions": {"hook": 8, "pacing": 6, "character": 7, "prose": 7}}'])
        d = score_chapter_detailed(gw, "mid", "目标", "正文" * 500)
        assert d["score"] == 7.0

    def test_legacy_score_only_still_works(self):
        from novelforge.craft.candidate_judge import score_chapter

        gw = self._gw(['{"score": 6.5, "reason": "无维度旧格式"}'])
        assert score_chapter(gw, "mid", "目标", "正文" * 500) == 6.5

    def test_dimensions_persisted_and_exposed_via_api(self, client, project):
        def factory(messages, model="", temperature=1.0):
            user = str(messages[-1].content) if messages else ""
            if "本章任务" in user:
                return _draft_response("单稿正文。" * 200)
            if "一致性问题" in user:
                return "修订后：单稿正文。" * 150
            if "本章目标：" in user:  # 评分调用（_SCORE_SYSTEM 的 user 前缀）
                return ('{"score": 8.5, "dimensions": {"hook": 9, "pacing": 8,'
                        ' "character": 8.5, "prose": 8}, "reason": "钩子抓人"}')
            if "章正文：" in user:
                return "本章摘要。"
            return "[]"

        orch, _ = _build_orch(project, factory, quality=True)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="入门")
            assert outcome.ok, outcome.error
            assert outcome.quality_score == 8.5
            assert outcome.quality_dimensions == {"hook": 9.0, "pacing": 8.0,
                                                  "character": 8.5, "prose": 8.0}
            row = conn.execute(
                "SELECT detail_json FROM pipeline_run WHERE run_id=?",
                (outcome.run_id,),
            ).fetchone()
        finally:
            conn.close()
        detail = json.loads(row["detail_json"])
        assert detail["quality_dimensions"]["hook"] == 9.0

        body = client.get(f"/v1/{project}/pipeline/runs/{outcome.run_id}").json()
        assert body["quality_dimensions"] == {"hook": 9.0, "pacing": 8.0,
                                              "character": 8.5, "prose": 8.0}
