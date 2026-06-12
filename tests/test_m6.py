"""M6 测试：候选 3 选 1 人工换稿。全部 FakeProvider，无网络。"""
from __future__ import annotations

import json

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
    resp = client.post("/v1/projects", json={"name": "M6测试", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _open_conn(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


def _draft_response(body: str, obj: str) -> str:
    return (
        f"```draft\n{body}\n```\n"
        "```proposals\n"
        f'[{{"op":"add","fact_type":"power_rank","entity":"陆天",'
        f'"new":{{"subject":"陆天","predicate":"境界","object":"{obj}"}},'
        f'"valid_from_chapter":1}}]\n'
        "```"
    )


@pytest.fixture
def multi_candidate_run(client, project):
    """跑一次 3 候选生成，返回 run_id。评委选 #1。"""
    from novelforge.config import NovelForgeConfig
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.fake_provider import FakeProvider
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.orchestrator import Orchestrator
    from novelforge.control_plane.skill_registry import SkillRegistry
    from novelforge.skills import register_default_skills

    drafts = [
        _draft_response("候选零号正文。" * 200, "炼气一层"),
        _draft_response("候选一号正文。" * 200, "炼气二层"),
        _draft_response("候选二号正文。" * 200, "炼气三层"),
    ]

    def factory(messages, model=""):
        user = str(messages[-1].content) if messages else ""
        if "本章任务" in user:
            return drafts.pop(0) if drafts else "```draft\n兜底\n```"
        if "### 候选" in user:
            return '{"winner": 1, "scores": [6, 9, 7], "reason": "节奏最佳"}'
        if "一致性问题" in user:
            marker = "候选一号正文" if "候选一号正文" in user else "候选某号正文"
            return f"修订后：{marker}。" * 150
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
    cfg.candidates.n_candidates = 3
    cfg.recall.enable_summaries = False
    orch = Orchestrator(gw, reg, cfg)

    conn = _open_conn(project)
    try:
        outcome = orch.generate_chapter(1, conn, chapter_goal="入门")
        assert outcome.ok, outcome.error
    finally:
        conn.close()
    return outcome.run_id


class TestRunDetailCandidates:
    def test_detail_returns_full_candidates(self, client, project, multi_candidate_run):
        r = client.get(f"/v1/{project}/pipeline/runs/{multi_candidate_run}")
        assert r.status_code == 200
        body = r.json()
        assert len(body["candidates"]) == 3
        assert body["winner_index"] == 1
        assert body["selected_by"] == "auto"
        winner = body["candidates"][1]
        assert winner["is_winner"] is True
        assert winner["score"] == 9
        assert "候选一号正文" in winner["draft_text"]
        assert body["candidates"][0]["proposal_count"] == 1

    def test_single_run_has_no_candidates(self, client, project):
        conn = _open_conn(project)
        conn.execute(
            "INSERT INTO pipeline_run(run_id, chapter, project_id, status)"
            " VALUES('run_single', 9, ?, 'completed')", (project,),
        )
        conn.commit()
        conn.close()
        r = client.get(f"/v1/{project}/pipeline/runs/run_single")
        assert r.status_code == 200
        assert r.json()["candidates"] == []


class TestSelectCandidate:
    def test_swap_to_loser_persists_new_revision(self, client, project, multi_candidate_run):
        conn = _open_conn(project)
        before_revs = conn.execute(
            "SELECT COUNT(*) AS n FROM draft_index WHERE chapter=1").fetchone()["n"]
        before_cands = conn.execute(
            "SELECT COUNT(*) AS n FROM fact_candidates").fetchone()["n"]
        conn.close()

        r = client.post(
            f"/v1/{project}/pipeline/runs/{multi_candidate_run}/select-candidate",
            json={"candidate_index": 2},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["winner_index"] == 2
        assert body["selected_by"] == "human"
        assert "候选二号正文" in body["draft_text"]   # run 现在指向新落盘的稿

        conn = _open_conn(project)
        after_revs = conn.execute(
            "SELECT COUNT(*) AS n FROM draft_index WHERE chapter=1").fetchone()["n"]
        after_cands = conn.execute(
            "SELECT COUNT(*) AS n FROM fact_candidates").fetchone()["n"]
        # 选中稿提案入 staging
        new_cand = conn.execute(
            "SELECT proposal_json FROM fact_candidates ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert after_revs == before_revs + 1          # 新修订落盘
        assert after_cands == before_cands + 1        # 提案入 staging
        assert "炼气三层" in new_cand["proposal_json"]

    def test_idempotent_reselect(self, client, project, multi_candidate_run):
        client.post(f"/v1/{project}/pipeline/runs/{multi_candidate_run}/select-candidate",
                    json={"candidate_index": 2})
        conn = _open_conn(project)
        revs = conn.execute(
            "SELECT COUNT(*) AS n FROM draft_index WHERE chapter=1").fetchone()["n"]
        conn.close()

        r2 = client.post(
            f"/v1/{project}/pipeline/runs/{multi_candidate_run}/select-candidate",
            json={"candidate_index": 2},
        )
        assert r2.status_code == 200
        conn = _open_conn(project)
        revs2 = conn.execute(
            "SELECT COUNT(*) AS n FROM draft_index WHERE chapter=1").fetchone()["n"]
        conn.close()
        assert revs2 == revs                          # 幂等：不再落新修订

    def test_select_auto_winner_marks_human_no_new_revision(self, client, project, multi_candidate_run):
        conn = _open_conn(project)
        revs = conn.execute(
            "SELECT COUNT(*) AS n FROM draft_index WHERE chapter=1").fetchone()["n"]
        conn.close()
        r = client.post(
            f"/v1/{project}/pipeline/runs/{multi_candidate_run}/select-candidate",
            json={"candidate_index": 1},               # 本来就是自动胜者
        )
        assert r.status_code == 200
        body = r.json()
        assert body["selected_by"] == "human"
        conn = _open_conn(project)
        revs2 = conn.execute(
            "SELECT COUNT(*) AS n FROM draft_index WHERE chapter=1").fetchone()["n"]
        conn.close()
        assert revs2 == revs                          # 同稿确认：不落新修订

    def test_oob_and_missing(self, client, project, multi_candidate_run):
        r = client.post(
            f"/v1/{project}/pipeline/runs/{multi_candidate_run}/select-candidate",
            json={"candidate_index": 2 + 7},
        )
        assert r.status_code == 422                   # pydantic le=2 校验

        conn = _open_conn(project)
        conn.execute(
            "INSERT INTO pipeline_run(run_id, chapter, project_id, status)"
            " VALUES('run_nocand', 8, ?, 'completed')", (project,),
        )
        conn.commit()
        conn.close()
        r2 = client.post(f"/v1/{project}/pipeline/runs/run_nocand/select-candidate",
                         json={"candidate_index": 0})
        assert r2.status_code == 422                  # 无候选

        r3 = client.post(f"/v1/{project}/pipeline/runs/run_nope/select-candidate",
                         json={"candidate_index": 0})
        assert r3.status_code == 404


# ── 质量趋势统计 ──────────────────────────────────────────────────────────────

class TestPipelineStats:
    def _seed_run(self, conn, project, run_id, chapter, score, words, started):
        conn.execute(
            "INSERT INTO draft_index(id, chapter, revision_round, file_path, sha256, word_count)"
            " VALUES(?, ?, ?, ?, 'x', ?)",
            (f"d_{run_id}", chapter, int(started[-1]), f"l0/{run_id}.txt", words),
        )
        conn.execute(
            "INSERT INTO pipeline_run(run_id, chapter, project_id, status, draft_id,"
            " quality_score, started_at)"
            " VALUES(?, ?, ?, 'completed', ?, ?, ?)",
            (run_id, chapter, project, f"d_{run_id}", score, started),
        )

    def test_stats_latest_per_chapter_and_aggregates(self, client, project):
        conn = _open_conn(project)
        self._seed_run(conn, project, "r1", 1, 8.0, 3000, "2026-06-01T00:00:01")
        self._seed_run(conn, project, "r2", 2, 5.0, 2800, "2026-06-01T00:00:02")
        # 第 2 章重跑（更晚）→ 应取这条
        self._seed_run(conn, project, "r3", 2, 7.0, 3200, "2026-06-02T00:00:03")
        # running 的不计
        conn.execute(
            "INSERT INTO pipeline_run(run_id, chapter, project_id, status)"
            " VALUES('r4', 3, ?, 'running')", (project,),
        )
        conn.commit()
        conn.close()

        r = client.get(f"/v1/{project}/pipeline/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["chapters_completed"] == 2
        assert [s["chapter"] for s in body["series"]] == [1, 2]
        assert body["series"][1]["quality_score"] == 7.0   # 取最新一次
        assert body["total_words"] == 3000 + 3200
        assert body["avg_quality_score"] == 7.5
        assert body["low_quality_count"] == 0

    def test_stats_empty_project(self, client, project):
        r = client.get(f"/v1/{project}/pipeline/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["chapters_completed"] == 0
        assert body["avg_quality_score"] is None
