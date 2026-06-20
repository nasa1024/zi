"""orchestrator 接线 L2/L3 测试（§06 settle 阶段触发）。

单元层：直接测 _run_l2/_run_l3/_is_volume_end/_load_chapter_text。
集成层：跑完整 generate_chapter，验证 settle 门控按 cfg.memory 开关生效（默认关 no-op）。
全程 FakeProvider，无网络。
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from novelforge.config import NovelForgeConfig
from novelforge.control_plane.budget import BudgetLedger
from novelforge.control_plane.llm.fake_provider import FakeProvider
from novelforge.control_plane.llm.gateway import LLMGateway
from novelforge.control_plane.orchestrator import (
    _is_volume_end,
    _load_chapter_text,
    _proposals_to_candidates,
    _run_l2,
    _run_l3,
)
from novelforge.db.connection import connect, init_db_from_conn


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db_from_conn(c)
    yield c
    c.close()


def _gw(provider=None):
    return LLMGateway(provider or FakeProvider(),
                      BudgetLedger(max_tokens=10_000_000, max_usd=100.0))


# ── 单元：辅助函数 ────────────────────────────────────────────────────────────

class TestHelpers:
    def test_is_volume_end(self, conn):
        conn.execute(
            "INSERT INTO volumes(id, volume_no, title, start_chapter, end_chapter)"
            " VALUES('v1',1,'第一卷',1,5)")
        conn.commit()
        assert _is_volume_end(conn, 5) is True
        assert _is_volume_end(conn, 4) is False

    def test_load_chapter_text(self, conn, tmp_path):
        f = tmp_path / "ch1.md"
        f.write_text("第一章的正文内容。", encoding="utf-8")
        conn.execute(
            "INSERT INTO draft_index(id, chapter, revision_round, file_path, sha256, status)"
            " VALUES('d1',1,0,?, 'x','committed')", (str(f),))
        conn.commit()
        assert _load_chapter_text(conn, 1) == "第一章的正文内容。"
        assert _load_chapter_text(conn, 99) is None


# ── 单元：_run_l2 触发与写库 ─────────────────────────────────────────────────

class TestRunL2:
    def test_triggers_and_writes(self, conn):
        cfg = NovelForgeConfig()
        cfg.memory.enable_l2 = True
        cfg.memory.l2_every_n_chapters = 1     # 每章触发
        _run_l2(conn, _gw(), cfg, 1, "开场写得足够长足够长。\n***\n转折写得足够长足够长。")
        n = conn.execute("SELECT COUNT(*) AS c FROM l2_scenes WHERE chapter=1").fetchone()["c"]
        assert n == 2

    def test_no_trigger_is_noop(self, conn):
        cfg = NovelForgeConfig()
        cfg.memory.enable_l2 = True
        cfg.memory.l2_every_n_chapters = 4     # 第 2 章不在触发点、非卷末
        _run_l2(conn, _gw(), cfg, 2, "正文足够长足够长足够长。")
        n = conn.execute("SELECT COUNT(*) AS c FROM l2_scenes WHERE chapter=2").fetchone()["c"]
        assert n == 0

    def test_volume_end_triggers(self, conn):
        conn.execute(
            "INSERT INTO volumes(id, volume_no, title, start_chapter, end_chapter)"
            " VALUES('v1',1,'卷一',1,3)")
        conn.commit()
        cfg = NovelForgeConfig()
        cfg.memory.enable_l2 = True
        cfg.memory.l2_every_n_chapters = 10    # 非每章触发
        _run_l2(conn, _gw(), cfg, 3, "卷末章正文足够长足够长足够长。")  # 第 3 章=卷末
        n = conn.execute("SELECT COUNT(*) AS c FROM l2_scenes WHERE chapter=3").fetchone()["c"]
        assert n >= 1

    def test_vector_indexed_when_enabled(self, conn):
        cfg = NovelForgeConfig()
        cfg.memory.enable_l2 = True
        cfg.memory.l2_every_n_chapters = 1
        cfg.recall.enable_vector = True
        _run_l2(conn, _gw(), cfg, 1, "施法场景足够长足够长。\n***\n战斗场景足够长足够长。")
        nvec = conn.execute("SELECT COUNT(*) AS c FROM scene_vec WHERE chapter=1").fetchone()["c"]
        assert nvec == 2


# ── 单元：_run_l3 入 staging ──────────────────────────────────────────────────

class TestRunL3:
    def test_proposals_enter_staging(self, conn):
        import json
        text = "自灵气复苏，天地多了一条法则。长老严令剑诀绝不得外传。"
        atoms = [{"type": "world_rule", "name": None,
                  "statement": "灵气复苏后凡人可修炼", "evidence": "灵气复苏"}]
        cfg = NovelForgeConfig()
        gw = _gw(FakeProvider(responses=[json.dumps(atoms, ensure_ascii=False)]))
        _run_l3(conn, gw, cfg, 5, text)
        rows = conn.execute(
            "SELECT fact_type, status, source_chapter FROM fact_candidates"
            " WHERE source_chapter=5").fetchall()
        assert len(rows) == 1
        assert rows[0]["fact_type"] == "world_rule"
        assert rows[0]["status"] == "proposed"

    def test_no_signal_no_candidate(self, conn):
        cfg = NovelForgeConfig()
        _run_l3(conn, _gw(), cfg, 6, "他挥剑斩出，妖兽轰然倒地。")
        n = conn.execute("SELECT COUNT(*) AS c FROM fact_candidates WHERE source_chapter=6").fetchone()["c"]
        assert n == 0

    def test_candidate_insert_failure_is_not_swallowed(self, conn):
        """候选写入违反 DB 约束时应暴露错误，不能返回一个库里不存在的 candidate。"""
        with pytest.raises(sqlite3.IntegrityError):
            _proposals_to_candidates(
                [{"op": "bad", "fact_type": "world_rule",
                  "new": {"statement": "x"}, "valid_from_chapter": 7}],
                7,
                conn,
            )
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM fact_candidates WHERE source_chapter=7"
        ).fetchone()["c"]
        assert n == 0

    def test_fact_type_aliases_are_normalized_before_staging(self, conn):
        """LLM/旧提示别名不能原样进入 staging，否则 approve 时会撞 facts.fact_type CHECK。"""
        cands = _proposals_to_candidates(
            [
                {"op": "add", "fact_type": "appearance",
                 "new": {"subject": "主角", "predicate": "appears", "object": "chapter_1"}},
                {"op": "add", "fact_type": "timeline",
                 "new": {"subject": "事件", "predicate": "happens", "object": "大战"}},
            ],
            9,
            conn,
        )
        assert [c.fact_type for c in cands] == ["misc", "event"]
        rows = conn.execute(
            "SELECT fact_type, proposal_json FROM fact_candidates"
            " WHERE source_chapter=9 ORDER BY rowid"
        ).fetchall()
        assert [r["fact_type"] for r in rows] == ["misc", "event"]
        payloads = [json.loads(r["proposal_json"]) for r in rows]
        assert payloads[0]["new"]["canon_kind"] == "appearance"
        assert payloads[1]["new"]["canon_kind"] == "timeline"


# ── 集成：settle 门控按开关生效 ──────────────────────────────────────────────

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
    r = client.post("/v1/projects", json={"name": "记忆接线", "genre": "xuanhuan"})
    assert r.status_code == 201
    return r.json()["project_id"]


def _open_conn(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


def _draft_response(body: str) -> str:
    return (
        f"```draft\n{body}\n```\n"
        "```proposals\n[]\n```"
    )


def _build_orch(project, *, enable_l2, every_n=1):
    from novelforge.control_plane.orchestrator import Orchestrator
    from novelforge.control_plane.skill_registry import SkillRegistry
    from novelforge.skills import register_default_skills

    def factory(messages, model="", temperature=1.0):
        user = str(messages[-1].content) if messages else ""
        if "本章任务" in user:
            return _draft_response("单稿正文写得足够长。" * 200)
        if "场景原文" in user:
            return "场景摘要。"
        if "一致性问题" in user:
            return "修订后：单稿正文写得足够长。" * 200
        if "章正文：" in user:
            return "本章摘要。"
        return "[]"

    gw = LLMGateway(FakeProvider(factory=factory),
                    BudgetLedger(max_tokens=10_000_000, max_usd=100.0, max_revise_rounds=100))
    reg = SkillRegistry()
    register_default_skills(reg)
    cfg = NovelForgeConfig(project_id=project)
    cfg.provider.provider = "fake"
    cfg.settle.enabled = False        # 只测 L2/L3 门控
    cfg.memory.enable_l2 = enable_l2
    cfg.memory.l2_every_n_chapters = every_n
    return Orchestrator(gw, reg, cfg)


class TestSettleGate:
    def test_l2_disabled_by_default(self, client, project):
        orch = _build_orch(project, enable_l2=False)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="入门")
            assert outcome.ok, outcome.error
            n = conn.execute("SELECT COUNT(*) AS c FROM l2_scenes").fetchone()["c"]
            assert n == 0          # 默认关 → 不产生场景
        finally:
            conn.close()

    def test_l2_enabled_populates_scenes(self, client, project):
        orch = _build_orch(project, enable_l2=True, every_n=1)
        conn = _open_conn(project)
        try:
            outcome = orch.generate_chapter(1, conn, chapter_goal="入门")
            assert outcome.ok, outcome.error
            n = conn.execute("SELECT COUNT(*) AS c FROM l2_scenes WHERE chapter=1").fetchone()["c"]
            assert n >= 1          # 开启 → settle 跑出场景
        finally:
            conn.close()
