"""POST-v15 功能测试：P0#1 降级熔断闭环 / P0#2 失败升级全覆盖 /
P1#3 分支投影+并线 / P1#4 冷启动批量 / P2#6 设定→卷向导 / P2#7 摘要看板。

全部 FakeProvider，无网络。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from novelforge.db.connection import init_db_from_conn


# ── fixtures（与 test_autopilot / test_volumes 同款）──────────────────────────

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_DATA", str(tmp_path))
    import novelforge.app.deps as deps_mod
    import novelforge.app.autopilot_manager as ap_mod
    import novelforge.coldstart.batch as cs_mod
    deps_mod._registry = None
    ap_mod._manager = None
    cs_mod._manager = None
    yield tmp_path
    deps_mod._registry = None
    ap_mod._manager = None
    cs_mod._manager = None


@pytest.fixture
def client(tmp_data):
    from novelforge.app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project(client):
    resp = client.post("/v1/projects", json={"name": "post-v15", "genre": "xuanhuan"})
    assert resp.status_code == 201
    return resp.json()["project_id"]


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    init_db_from_conn(conn)
    return conn


def _open_conn(project_id):
    from novelforge.app.deps import get_registry
    return get_registry().open_conn(project_id)


def _wait_terminal(client, project, sid, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/v1/{project}/autopilot/{sid}").json()
        if s["status"] not in ("running", "degraded"):
            return s
        time.sleep(0.05)
    return s


# ── P0#1 降级熔断闭环 ─────────────────────────────────────────────────────────

class TestAutoDegradePause:
    def _patch_outcome(self, monkeypatch, issues):
        from novelforge.control_plane.orchestrator import ChapterOutcome, Orchestrator

        # 无 key 时默认 anthropic provider 会因 SDK 未装直接 error——
        # 走 fake provider 让会话进入正常写章循环。
        # 注意用 DEEPSEEK_API_KEY：NOVELFORGE_API_KEY 会同时打开服务端鉴权。
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        monkeypatch.setenv("NOVELFORGE_PROVIDER", "fake")

        def fake_generate(self, chapter, conn, **kw):
            return ChapterOutcome(chapter=chapter, ok=True, run_id="run-x",
                                  draft_text="x" * 100, issues=issues,
                                  usage_tokens=10, usage_usd=0.001)
        monkeypatch.setattr(Orchestrator, "generate_chapter", fake_generate)

    def test_auto_promote_consecutive_hard_pauses(self, client, project, monkeypatch):
        """auto_promote 连续 hard block → 降级 human_gate + status=paused（终态）。"""
        self._patch_outcome(monkeypatch, [{"severity": "block", "code": "X"}])
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 10, "mode": "auto_promote",
            "auto_degrade_after_consecutive_issues": 2,
        })
        assert r.status_code == 202
        sid = r.json()["session_id"]
        s = _wait_terminal(client, project, sid)
        assert s["status"] == "paused"
        assert s["policy_mode"] == "human_gate"
        assert s["chapters_done"] == 2          # 第 2 章触发熔断即停
        assert "auto-degraded" in (s["last_error"] or "")

    def test_paused_session_resumable(self, client, project, monkeypatch):
        self._patch_outcome(monkeypatch, [{"severity": "block", "code": "X"}])
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 10, "mode": "auto_promote",
            "auto_degrade_after_consecutive_issues": 1,
        })
        sid = r.json()["session_id"]
        s = _wait_terminal(client, project, sid)
        assert s["status"] == "paused"
        # 恢复：从断点开新会话（resumed_from 链回）
        r2 = client.post(f"/v1/{project}/autopilot/{sid}/resume")
        assert r2.status_code == 202, r2.text
        assert r2.json()["session_id"] != sid

    def test_pause_on_degrade_false_keeps_writing(self, client, project, monkeypatch):
        """pause_on_degrade=False → 旧行为：转 human_gate 后继续写完。"""
        self._patch_outcome(monkeypatch, [{"severity": "block", "code": "X"}])
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 4, "mode": "auto_promote",
            "auto_degrade_after_consecutive_issues": 2,
            "pause_on_degrade": False,
        })
        sid = r.json()["session_id"]
        s = _wait_terminal(client, project, sid)
        assert s["status"] == "completed"
        assert s["policy_mode"] == "human_gate"
        assert s["chapters_done"] == 4

    def test_hybrid_mode_not_paused(self, client, project, monkeypatch):
        """hybrid 连续失败仅转 human_gate（degraded→completed），不暂停。"""
        self._patch_outcome(monkeypatch, [{"severity": "block", "code": "X"}])
        r = client.post(f"/v1/{project}/autopilot/start", json={
            "from_chapter": 1, "to_chapter": 3, "mode": "hybrid",
            "auto_degrade_after_consecutive_issues": 1,
        })
        sid = r.json()["session_id"]
        s = _wait_terminal(client, project, sid)
        assert s["status"] == "completed"
        assert s["chapters_done"] == 3


# ── P0#2 generate_validated 推广 ──────────────────────────────────────────────

class TestGenerateValidatedCoverage:
    def _gw(self, responses):
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.fake_provider import FakeProvider
        from novelforge.control_plane.llm.gateway import LLMGateway
        p = FakeProvider(responses=responses)
        return LLMGateway(p, BudgetLedger()), p

    def test_attempts_per_tier_retries_same_tier(self):
        from novelforge.control_plane.llm.provider import Message
        from novelforge.control_plane.llm.tiers import ModelTier
        gw, p = self._gw(["坏的", '{"ok": 1}'])
        r = gw.generate_validated(
            ModelTier.STRONG, [Message(role="user", content="x")],
            parse=lambda t: json.loads(t) if t.startswith("{") else None,
            max_tier=ModelTier.STRONG, attempts_per_tier=2)
        assert r.value == {"ok": 1}
        assert r.tier_used == "strong"
        assert r.escalated is False
        assert len(p.calls) == 2

    def test_planner_escalates_on_malformed(self, db):
        """MID 输出畸形 → 升 STRONG 抢救出合格 beats。"""
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.skill_base import SkillContext
        from novelforge.skills import PlannerSkill
        good = ('[{"beat_type":"setup","summary":"开场","value_axis":"平"},'
                '{"beat_type":"hook","summary":"悬念","value_axis":"紧"}]')
        gw, p = self._gw(["not json", good])
        ctx = SkillContext("p", 1, "human_gate", 0, BudgetLedger(), gw, db, {})
        result = PlannerSkill().run(ctx)
        assert result.ok
        assert ctx.workspace["planner_escalated"] is True
        assert ctx.workspace["planner_tier_used"] == "strong"
        assert ctx.workspace["beats"][-1]["beat_type"] == "hook"

    def test_planner_single_call_when_valid(self, db):
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.skill_base import SkillContext
        from novelforge.skills import PlannerSkill
        good = ('[{"beat_type":"setup","summary":"开场","value_axis":"平"},'
                '{"beat_type":"hook","summary":"悬念","value_axis":"紧"}]')
        gw, p = self._gw([good])
        ctx = SkillContext("p", 1, "human_gate", 0, BudgetLedger(), gw, db, {})
        result = PlannerSkill().run(ctx)
        assert result.ok
        assert len(p.calls) == 1
        assert ctx.workspace["planner_escalated"] is False

    def test_chapter_draft_retries_on_empty(self, db):
        """草稿完全无产出（机械失败）→ 同档重采样一次抢救。"""
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.skill_base import SkillContext
        from novelforge.skills import ChapterDraftSkill
        good = "```draft\n主角踏入秘境。\n```\n\n```proposals\n[]\n```"
        gw, p = self._gw(["", good])
        ctx = SkillContext("p", 1, "human_gate", 0, BudgetLedger(), gw, db,
                           workspace={"beats": []})
        ChapterDraftSkill().run(ctx)
        assert "主角" in ctx.workspace["draft_text"]
        assert len(p.calls) == 2

    def test_chapter_draft_single_call_normal(self, db):
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.skill_base import SkillContext
        from novelforge.skills import ChapterDraftSkill
        good = "```draft\n主角踏入秘境。\n```\n\n```proposals\n[]\n```"
        gw, p = self._gw([good])
        ctx = SkillContext("p", 1, "human_gate", 0, BudgetLedger(), gw, db,
                           workspace={"beats": []})
        ChapterDraftSkill().run(ctx)
        assert len(p.calls) == 1


# ── P1#3 分支投影 + 并线 ──────────────────────────────────────────────────────

def _seed_branch_world(conn):
    """主线『陆天死亡=真』canon + 分支 BR『陆天死亡=假』canon（fork=5）。"""
    e1 = "e-lutian"
    conn.execute("INSERT INTO entities(id,canonical_name,entity_type) VALUES(?,?,?)",
                 (e1, "陆天", "character"))
    conn.execute("INSERT INTO power_ranks(id,system_name,rank_name,rank_order)"
                 " VALUES(?,?,?,?)", ("rk1", "炼气", "炼气期", 1))
    conn.execute("INSERT INTO power_ranks(id,system_name,rank_name,rank_order)"
                 " VALUES(?,?,?,?)", ("rk2", "炼气", "筑基期", 2))
    br = str(uuid.uuid4())
    conn.execute("INSERT INTO branches(id,branch_name,fork_chapter) VALUES(?,?,5)",
                 (br, "if-未死线"))
    f_main = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO facts(id,entity_id,fact_type,subject,predicate,object,status,"
        " valid_from_chapter,current_revision_id,branch_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,NULL)",
        (f_main, e1, "event", "陆天", "生死", "死亡", "canon", 7, f_main))
    f_br = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO facts(id,entity_id,fact_type,subject,predicate,object,status,"
        " valid_from_chapter,current_revision_id,branch_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f_br, e1, "event", "陆天", "生死", "存活", "canon", 6, f_br, br))
    # 分支上的境界突破（power log 走分支 fact）
    f_br2 = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO facts(id,entity_id,fact_type,subject,predicate,object,status,"
        " valid_from_chapter,current_revision_id,branch_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f_br2, e1, "power_system", "陆天", "境界", "筑基期", "canon", 6, f_br2, br))
    conn.execute(
        "INSERT INTO character_power_log(id,entity_id,system_name,rank_id,rank_order,"
        " change_chapter,change_type,source_fact_id) VALUES(?,?,?,?,?,6,?,?)",
        (str(uuid.uuid4()), e1, "炼气", "rk2", 2, "breakthrough", f_br2))
    # 主线 ch8 的境界（分支视角应不可见：8 > fork=5）
    f_main2 = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO facts(id,entity_id,fact_type,subject,predicate,object,status,"
        " valid_from_chapter,current_revision_id,branch_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,NULL)",
        (f_main2, e1, "power_system", "陆天", "境界", "炼气期", "canon", 8, f_main2))
    conn.execute(
        "INSERT INTO character_power_log(id,entity_id,system_name,rank_id,rank_order,"
        " change_chapter,change_type,source_fact_id) VALUES(?,?,?,?,?,8,?,?)",
        (str(uuid.uuid4()), e1, "炼气", "rk1", 1, "init", f_main2))
    conn.commit()
    return br


class TestBranchProjectionAndMerge:
    def test_branch_world_snapshot_isolated(self, db):
        from novelforge.world.replay import branch_world_snapshot
        br = _seed_branch_world(db)
        snap = branch_world_snapshot(db, 10, br)
        # 分支视角：ch6 分支突破可见（筑基期），主线 ch8 的炼气期不可见
        assert snap["power_ranks"].get("陆天") == "筑基期"
        main = branch_world_snapshot(db, 10, None)
        assert main["power_ranks"].get("陆天") == "炼气期"

    def test_merge_preview_detects_conflict(self, db):
        from novelforge.world.branch_merge import preview_branch_merge
        br = _seed_branch_world(db)
        p = preview_branch_merge(db, br)
        assert p["found"] and p["fact_count"] == 2
        # 生死（存活 vs 主线死亡）+ 境界（筑基期 vs 主线炼气期）均为冲突
        assert p["conflict_count"] == 2
        assert any(f["conflict"] and f["predicate"] == "生死" for f in p["facts"])

    def test_merge_creates_staging_and_review_queue(self, db):
        from novelforge.world.branch_merge import merge_branch_to_mainline
        br = _seed_branch_world(db)
        r = merge_branch_to_mainline(db, br)
        assert r["merged"] and r["fact_count"] == 2 and r["conflict_count"] == 2
        rows = db.execute(
            "SELECT * FROM fact_candidates WHERE candidate_id IN (%s)"
            % ",".join("?" * len(r["candidate_ids"])), r["candidate_ids"]).fetchall()
        assert all(row["status"] == "pending_review" for row in rows)
        # 冲突条目 op=retcon + risk high
        retcons = [row for row in rows if row["op"] == "retcon"]
        assert len(retcons) == 2 and all(x["risk_tier"] == "high" for x in retcons)
        rq = db.execute("SELECT COUNT(*) AS n FROM review_queue"
                        " WHERE status='pending'").fetchone()
        assert rq["n"] == 2
        st = db.execute("SELECT status FROM branches WHERE id=?", (br,)).fetchone()
        assert st["status"] == "merged"
        # 幂等：再次并线拒绝
        r2 = merge_branch_to_mainline(db, br)
        assert not r2["merged"] and r2["reason"] == "already_merged"

    def test_branch_endpoints_http(self, client, project):
        conn = _open_conn(project)
        br = _seed_branch_world(conn)
        conn.close()
        r = client.get(f"/v1/{project}/branches/{br}/world-state?as_of_chapter=10")
        assert r.status_code == 200
        assert r.json()["power_ranks"].get("陆天") == "筑基期"
        r = client.get(f"/v1/{project}/branches/{br}/merge-preview")
        assert r.status_code == 200 and r.json()["conflict_count"] == 2
        r = client.post(f"/v1/{project}/branches/{br}/merge")
        assert r.status_code == 202 and r.json()["merged"]
        # 并入的提案出现在暂存待审列表
        staging = client.get(f"/v1/{project}/staging").json()
        assert len(staging) >= 2
        r = client.get(f"/v1/{project}/branches/404x/world-state")
        assert r.status_code == 404


# ── P1#4 冷启动批量 ───────────────────────────────────────────────────────────

class TestColdStartBatch:
    def test_split_by_chapter_heads(self):
        from novelforge.coldstart import split_manuscript
        text = ("第一章 初入宗门\n陆天来到青云宗。\n\n"
                "第二章 试炼\n他通过了外门试炼。\n\n"
                "第十章 突破\n炼气三层！")
        chs = split_manuscript(text)
        assert [c["chapter_no"] for c in chs] == [1, 2, 10]
        assert "青云宗" in chs[0]["text"]

    def test_split_fallback_chunks(self):
        from novelforge.coldstart import split_manuscript
        chs = split_manuscript("没有章节标题的" * 800, fallback_chunk_chars=2000)
        assert len(chs) >= 2
        assert chs[0]["chapter_no"] == 1

    def test_dedup_exact_and_near(self):
        from novelforge.coldstart import dedup_proposals
        p1 = {"fact_type": "character_trait", "entity": "陆天",
              "new": {"subject": "陆天", "predicate": "境界", "object": "炼气期"}}
        p2 = dict(p1)                                        # 精确重复
        p3 = {"fact_type": "character_trait", "entity": "陆天",
              "new": {"subject": "陆天", "predicate": "境界", "object": "炼气期修士"}}  # 近邻
        p4 = {"fact_type": "character_trait", "entity": "苏雪",
              "new": {"subject": "苏雪", "predicate": "身份", "object": "青衣女子"}}
        kept, dropped = dedup_proposals([p1, p2, p3, p4])
        assert len(kept) == 2 and dropped == 2

    def test_dedup_against_canon(self, db):
        from novelforge.coldstart import dedup_proposals
        fid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO facts(id,fact_type,subject,predicate,object,status,"
            " valid_from_chapter,current_revision_id)"
            " VALUES(?,?,?,?,?,'canon',1,?)",
            (fid, "character_trait", "陆天", "境界", "炼气期", fid))
        db.commit()
        p = {"fact_type": "character_trait", "entity": "陆天",
             "new": {"subject": "陆天", "predicate": "境界", "object": "炼气期"}}
        kept, dropped = dedup_proposals([p], db)
        assert kept == [] and dropped == 1

    def test_tier_route(self, db):
        from novelforge.coldstart import tier_route_proposals
        props = [
            {"fact_type": "character_trait", "entity": "陆天", "risk_tier": "low",
             "new": {"subject": "陆天", "predicate": "性格", "object": "坚韧"},
             "valid_from_chapter": 1},
            {"fact_type": "world_rule", "risk_tier": "high",
             "new": {"subject": "世界", "predicate": "规则", "object": "灵气复苏"},
             "valid_from_chapter": 1},
        ]
        out = tier_route_proposals(db, props, auto_promote_low_risk=False)
        assert len(out["staged_low"]) == 1 and len(out["queued_review"]) == 1
        rq = db.execute("SELECT COUNT(*) AS n FROM review_queue").fetchone()
        assert rq["n"] == 1

    def test_tier_route_auto_promote_low(self, db):
        from novelforge.coldstart import tier_route_proposals
        props = [{"fact_type": "character_trait", "entity": "陆天", "risk_tier": "low",
                  "new": {"subject": "陆天", "predicate": "性格", "object": "坚韧"},
                  "valid_from_chapter": 1}]
        out = tier_route_proposals(db, props, auto_promote_low_risk=True)
        assert len(out["auto_promoted"]) == 1
        n = db.execute("SELECT COUNT(*) AS n FROM facts WHERE status='canon'").fetchone()
        assert n["n"] == 1

    def test_import_dry_run_preview(self, client, project):
        r = client.post(f"/v1/{project}/cold_start/import", json={
            "full_text": "第一章 开局\n正文A。\n第二章 发展\n正文B。",
            "dry_run": True,
        })
        assert r.status_code == 202
        body = r.json()
        assert body["chapters_total"] == 2
        assert body["chapters"][0]["chapter_no"] == 1

    def test_import_job_completes(self, client, project):
        r = client.post(f"/v1/{project}/cold_start/import", json={
            "full_text": "第一章 开局\n陆天入门。\n第二章 发展\n陆天突破。",
            "max_concurrency": 2,
        })
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        deadline = time.time() + 15
        while time.time() < deadline:
            j = client.get(f"/v1/{project}/cold_start/jobs/{job_id}").json()
            if j["status"] != "running":
                break
            time.sleep(0.05)
        assert j["status"] == "completed", j
        assert j["chapters_done"] == 2
        lst = client.get(f"/v1/{project}/cold_start/jobs").json()
        assert any(x["job_id"] == job_id for x in lst)

    def test_import_requires_text(self, client, project):
        r = client.post(f"/v1/{project}/cold_start/import", json={})
        assert r.status_code == 422


# ── P2#6 设定→卷向导 ──────────────────────────────────────────────────────────

class TestVolumeWizard:
    def test_wizard_requires_setting(self, client, project):
        r = client.post(f"/v1/{project}/volumes/wizard", json={})
        assert r.status_code == 422

    def test_wizard_degraded_fallback(self, client):
        """无 LLM key（fake 网关解析失败）→ 确定性兜底大纲，卷照建。"""
        resp = client.post("/v1/projects", json={
            "name": "向导书", "genre": "xuanhuan",
            "setting": {
                "genre": "xuanhuan", "premise": "少年得上古传承",
                "main_conflict": "宗门与魔道之争", "protagonist": "李云",
                "power_system": "灵气", "power_ranks": ["炼气", "筑基"],
            },
        })
        assert resp.status_code == 201
        pid = resp.json()["project_id"]
        r = client.post(f"/v1/{pid}/volumes/wizard",
                        json={"volume_span": 20, "plan_chapters": 3})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["degraded"] is True
        vol = body["volume"]
        assert vol["volume_no"] == 1
        assert vol["start_chapter"] == 1 and vol["end_chapter"] == 20
        assert vol["synopsis"]
        # 卷已实际落库
        vols = client.get(f"/v1/{pid}/volumes").json()
        assert len(vols) == 1


# ── P2#7 摘要看板 ─────────────────────────────────────────────────────────────

class TestSummaryBoard:
    def test_board_roundtrip(self, client, project):
        # 建卷 + 写章摘要 + 写卷摘要 + 写全局梗概
        client.post(f"/v1/{project}/volumes", json={
            "volume_no": 1, "title": "卷一", "start_chapter": 1, "end_chapter": 10})
        r = client.patch(f"/v1/{project}/summaries/chapters/3",
                         json={"summary": "陆天突破炼气三层，结识苏雪。"})
        assert r.status_code == 200
        assert r.json()["volume_no"] == 1       # 自动归卷
        r = client.patch(f"/v1/{project}/summaries/volumes/1",
                         json={"rolling_summary": "本卷主线：入门与试炼。"})
        assert r.status_code == 200
        r = client.patch(f"/v1/{project}/summaries/global",
                         json={"synopsis": "全书至此：少年入宗门。"})
        assert r.status_code == 200

        board = client.get(f"/v1/{project}/summaries").json()
        assert board["global_synopsis"] == "全书至此：少年入宗门。"
        assert board["volumes"][0]["rolling_summary"] == "本卷主线：入门与试炼。"
        assert board["chapters"][0]["chapter"] == 3
        assert "苏雪" in board["chapters"][0]["summary"]

    def test_chapter_summary_edit_overwrites(self, client, project):
        client.patch(f"/v1/{project}/summaries/chapters/1", json={"summary": "v1"})
        r = client.patch(f"/v1/{project}/summaries/chapters/1", json={"summary": "v2 修订"})
        assert r.status_code == 200 and r.json()["summary"] == "v2 修订"
        board = client.get(f"/v1/{project}/summaries").json()
        assert len([c for c in board["chapters"] if c["chapter"] == 1]) == 1

    def test_volume_summary_404(self, client, project):
        r = client.patch(f"/v1/{project}/summaries/volumes/99",
                         json={"rolling_summary": "x"})
        assert r.status_code == 404
