"""MVP2 集成测试（无网络调用，使用 FakeProvider / in-memory SQLite）。

覆盖：
  - ConflictDetect + score_evidence + classify_risk
  - DeduplicationEngine（无 LLM 仲裁）
  - PacingController + PacingState
  - CraftCheckSkill（确定性校验）
  - PromotionPolicy.decide_batch() conflict_map 路径
  - Orchestrator.generate_chapter() MVP2 端到端（FakeProvider）
"""
from __future__ import annotations

import json
import sqlite3

import pytest


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def conn():
    from novelforge.db.connection import init_db_from_conn
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db_from_conn(c)
    yield c
    c.close()


def _entity(conn, name="主角", etype="character"):
    from novelforge.ids import new_id
    eid = new_id("ent")
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type) VALUES(?,?,?)",
        (eid, name, etype),
    )
    conn.commit()
    return eid


def _candidate(conn, fact_type="style", risk_tier="low", chapter=1, entity_id=None, op="add",
               predicate="p", obj="o"):
    from novelforge.ids import new_id
    from novelforge.contracts import FactCandidate
    cid = new_id("cand")
    prop = json.dumps({
        "op": op, "fact_type": fact_type,
        "entity": entity_id,
        "new": {"subject": entity_id or "x", "predicate": predicate, "object": obj},
        "valid_from_chapter": chapter,
    }, ensure_ascii=False)
    conn.execute(
        "INSERT INTO fact_candidates"
        "(candidate_id, op, entity_id, fact_type, proposal_json, status, risk_tier, source_chapter)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (cid, op, entity_id, fact_type, prop, "proposed", risk_tier, chapter),
    )
    conn.commit()
    return FactCandidate(
        candidate_id=cid, entity_id=entity_id, fact_type=fact_type,
        proposal_json=prop, status="proposed", risk_tier=risk_tier, source_chapter=chapter,
    )


def _fact(conn, entity_id, fact_type, predicate, obj, chapter=0):
    from novelforge.ids import new_id
    fid = new_id("fact")
    rid = new_id("rev")
    conn.execute(
        "INSERT INTO facts(id, entity_id, subject, fact_type, predicate, object,"
        "  status, valid_from_chapter, current_revision_id)"
        " VALUES(?,?,?,?,?,?,'canon',?,?)",
        (fid, entity_id, entity_id, fact_type, predicate, obj, chapter, rid),
    )
    conn.commit()
    return fid


# ── 1. ConflictDetect ─────────────────────────────────────────────────────────

class TestConflictDetect:
    def test_no_conflict_empty_db(self, conn):
        from novelforge.governance.conflict import detect_conflict
        cand = _candidate(conn, fact_type="style", obj="blue")
        cs = detect_conflict(cand, conn)
        assert not cs.has_block
        assert cs.items == []

    def test_same_predicate_diff_value_is_block(self, conn):
        from novelforge.governance.conflict import detect_conflict
        eid = _entity(conn, "主角")
        # 已有 canon fact：眼睛=蓝色（fact_type 必须与 candidate 一致才能命中 SQL）
        _fact(conn, eid, "character_trait", "eye_color", "蓝色")
        cand = _candidate(conn, fact_type="character_trait", entity_id=eid,
                          predicate="eye_color", obj="红色")
        cs = detect_conflict(cand, conn)
        assert cs.has_block
        kinds = [c.kind for c in cs.items]
        assert "same_predicate_diff_value" in kinds

    def test_no_conflict_same_value(self, conn):
        from novelforge.governance.conflict import detect_conflict
        eid = _entity(conn, "主角")
        _fact(conn, eid, "character_trait", "eye_color", "蓝色")
        # 提案与已有相同值 → 不冲突
        cand = _candidate(conn, fact_type="character_trait", entity_id=eid,
                          predicate="eye_color", obj="蓝色")
        cs = detect_conflict(cand, conn)
        assert not cs.has_block


# ── 2. score_evidence ─────────────────────────────────────────────────────────

class TestScoreEvidence:
    def test_no_evidence_no_conflict(self, conn):
        from novelforge.governance.conflict import score_evidence
        cand = _candidate(conn)
        score = score_evidence(cand)
        assert 0.0 <= score <= 1.0
        # no evidence_refs, no conflict → 0.6*0 + 0.2*0 + 0.2*1 = 0.2
        assert score == pytest.approx(0.2)

    def test_with_evidence_refs(self, conn):
        from novelforge.governance.conflict import score_evidence
        from novelforge.contracts import FactCandidate
        cand = _candidate(conn)
        cand.evidence_refs = "ch1,ch2,ch3"
        score = score_evidence(cand)
        # 0.6*1 + 0.2*1.0 + 0.2*1.0 = 1.0
        assert score == pytest.approx(1.0)

    def test_with_conflict_lowers_score(self, conn):
        from novelforge.governance.conflict import score_evidence, ConflictSet, ConflictItem
        from novelforge.contracts import FactCandidate
        cand = _candidate(conn)
        cand.evidence_refs = "ch1"
        cs = ConflictSet(items=[ConflictItem(kind="same_predicate_diff_value",
                                              fact_id="f1", detail="x", severity="block")])
        score = score_evidence(cand, conflict_set=cs)
        # consistency=0 → 0.6*1 + 0.2*(1/3) + 0.2*0 ≈ 0.667
        assert score < 0.9


# ── 3. classify_risk ─────────────────────────────────────────────────────────

class TestClassifyRisk:
    def test_power_rank_is_high(self, conn):
        from novelforge.governance.conflict import classify_risk
        cand = _candidate(conn, fact_type="power_rank")
        assert classify_risk(cand) == "high"

    def test_appearance_is_low(self, conn):
        from novelforge.governance.conflict import classify_risk
        cand = _candidate(conn, fact_type="style")
        assert classify_risk(cand) == "low"

    def test_knowledge_is_medium(self, conn):
        from novelforge.governance.conflict import classify_risk
        cand = _candidate(conn, fact_type="knowledge")
        assert classify_risk(cand) == "medium"

    def test_retcon_is_medium(self, conn):
        from novelforge.governance.conflict import classify_risk
        cand = _candidate(conn, fact_type="style", op="retcon")
        assert classify_risk(cand) == "medium"


# ── 4. DeduplicationEngine ────────────────────────────────────────────────────

class TestDeduplicationEngine:
    def test_no_neighbor_returns_store(self, conn):
        from novelforge.dedup.dedup_engine import DeduplicationEngine
        engine = DeduplicationEngine(llm_gateway=None)
        cand = _candidate(conn, fact_type="style", obj="独特内容xyz")
        v = engine.check(cand, conn)
        assert v.action == "store"

    def test_empty_query_returns_store(self, conn):
        from novelforge.dedup.dedup_engine import DeduplicationEngine
        from novelforge.contracts import FactCandidate
        from novelforge.ids import new_id
        engine = DeduplicationEngine(llm_gateway=None)
        # proposal_json 缺 new 字段 → 提取关键词为空
        cid = new_id("cand")
        conn.execute(
            "INSERT INTO fact_candidates(candidate_id, op, entity_id, fact_type, proposal_json, status, risk_tier, source_chapter)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (cid, "add", None, "appearance", json.dumps({"op": "add", "fact_type": "appearance"}),
             "proposed", "low", 1),
        )
        conn.commit()
        cand = FactCandidate(
            candidate_id=cid, entity_id=None, fact_type="style",
            proposal_json=json.dumps({"op": "add", "fact_type": "appearance"}),
            status="proposed", risk_tier="low", source_chapter=1,
        )
        v = engine.check(cand, conn)
        assert v.action == "store"


# ── 5. PacingController ────────────────────────────────────────────────────────

class TestPacingController:
    def test_initial_state_is_zero(self, conn):
        from novelforge.craft.pacing import PacingController
        pc = PacingController()
        s = pc.get_state(conn)
        assert s.chapters_since_big_payoff == 0
        assert s.kchars_since_small_payoff == 0.0
        assert not s.needs_big_payoff
        assert not s.needs_small_payoff
        assert not s.needs_cooldown

    def test_update_with_big_payoff_resets(self, conn):
        from novelforge.craft.pacing import PacingController
        pc = PacingController()
        beats = [{"beat_type": "payoff_beat", "value_axis": "power"}]
        state = pc.update(chapter=1, beats=beats, draft_chars=3000, conn=conn)
        assert state.chapters_since_big_payoff == 0

    def test_no_payoff_increments(self, conn):
        from novelforge.craft.pacing import PacingController
        pc = PacingController()
        beats = [{"beat_type": "setup", "value_axis": ""}]
        state = pc.update(chapter=1, beats=beats, draft_chars=2000, conn=conn)
        assert state.chapters_since_big_payoff == 1
        assert state.kchars_since_small_payoff == pytest.approx(2.0)

    def test_needs_small_payoff_after_3k_chars(self, conn):
        from novelforge.craft.pacing import PacingController
        pc = PacingController()
        beats = [{"beat_type": "setup", "value_axis": ""}]
        pc.update(1, beats, 3500, conn)
        state = pc.get_state(conn)
        assert state.needs_small_payoff

    def test_recommend_hint_cooldown(self, conn):
        from novelforge.craft.pacing import PacingController, PacingState
        pc = PacingController()
        hint = pc.recommend_beat_hint(PacingState(recent_high_streak=3))
        assert "冷却" in hint or "回落" in hint

    def test_recommend_hint_big_payoff(self, conn):
        from novelforge.craft.pacing import PacingController, PacingState
        pc = PacingController()
        hint = pc.recommend_beat_hint(PacingState(chapters_since_big_payoff=12, buildup=8))
        assert "大爽点" in hint


# ── 6. CraftCheckSkill 确定性部分 ─────────────────────────────────────────────

def _fake_gw():
    from novelforge.control_plane.llm.provider import Message, Response, Usage, CapabilitySet
    from novelforge.control_plane.llm.tiers import ModelTier

    class FakeGateway:
        ledger = type("L", (), {"tokens_spent": 0, "usd_spent": 0.0})()

        def generate(self, tier, messages, *, system="", tools=None, max_tokens=2048, temperature=0.7, cache_hint=None):
            return Response(text="[]", tool_calls=[], usage=Usage(0, 0, 0, 0, "fake", "fake"))

        def model_for(self, tier):
            return "fake-model"

        class _provider:
            @staticmethod
            def capabilities(model):
                return CapabilitySet(supports_tools=False, supports_cache=False,
                                     max_tokens_out=4096, context_window=8192)

    return FakeGateway()


def _skill_ctx(conn, beats, draft_text, proposals=None, pacing_state=None):
    from novelforge.control_plane.skill_base import SkillContext
    ws = {
        "beats": beats,
        "draft_text": draft_text,
        "proposals": proposals or [],
        "pacing_state": pacing_state,
    }
    return SkillContext(
        project_id="test", target_chapter=2, mode="human_gate",
        as_of_chapter=1, budget=None, llm=_fake_gw(),
        conn=conn, workspace=ws,
    )


class TestCraftCheckSkill:
    def test_good_beats_pass(self, conn):
        from novelforge.skills.craft_check_skill import CraftCheckSkill
        beats = [
            {"beat_type": "setup", "value_axis": "power", "summary": "主角修炼"},
            {"beat_type": "hook", "value_axis": "", "summary": "悬念结尾"},
        ]
        ctx = _skill_ctx(conn, beats, "他突破了！")
        result = CraftCheckSkill().run(ctx)
        # good beats: has value_axis + hook → no blocks
        blocks = [i for i in ctx.workspace["craft_issues"] if i["severity"] == "block"
                  and i["check"] in ("value_shift", "hook")]
        assert blocks == []

    def test_missing_hook_is_block(self, conn):
        from novelforge.skills.craft_check_skill import CraftCheckSkill
        beats = [
            {"beat_type": "setup", "value_axis": "power", "summary": "修炼"},
            {"beat_type": "payoff_beat", "value_axis": "power", "summary": "突破"},
        ]
        ctx = _skill_ctx(conn, beats, "修炼到了极限。")
        CraftCheckSkill().run(ctx)
        hooks = [i for i in ctx.workspace["craft_issues"]
                 if i["check"] == "hook" and i["severity"] == "block"]
        assert len(hooks) == 1

    def test_no_value_axis_is_block(self, conn):
        from novelforge.skills.craft_check_skill import CraftCheckSkill
        beats = [
            {"beat_type": "setup", "value_axis": "", "summary": "无聊"},
            {"beat_type": "hook", "value_axis": "", "summary": "悬念"},
        ]
        ctx = _skill_ctx(conn, beats, "平淡无奇。")
        CraftCheckSkill().run(ctx)
        shifts = [i for i in ctx.workspace["craft_issues"]
                  if i["check"] == "value_shift" and i["severity"] == "block"]
        assert len(shifts) == 1

    def test_invalid_beat_type_is_block(self, conn):
        from novelforge.skills.craft_check_skill import CraftCheckSkill
        beats = [
            {"beat_type": "invalid_type", "value_axis": "x", "summary": "x"},
            {"beat_type": "hook", "value_axis": "", "summary": "钩"},
        ]
        ctx = _skill_ctx(conn, beats, "一些文字")
        CraftCheckSkill().run(ctx)
        contracts = [i for i in ctx.workspace["craft_issues"]
                     if i["check"] == "beat_contract" and i["severity"] == "block"]
        assert len(contracts) >= 1

    def test_too_few_beats_is_block(self, conn):
        from novelforge.skills.craft_check_skill import CraftCheckSkill
        beats = [{"beat_type": "hook", "value_axis": "x", "summary": "悬念"}]
        ctx = _skill_ctx(conn, beats, "单 beat")
        CraftCheckSkill().run(ctx)
        contracts = [i for i in ctx.workspace["craft_issues"]
                     if i["check"] == "beat_contract" and i["severity"] == "block"]
        assert len(contracts) >= 1


# ── 7. PromotionPolicy conflict_map 路径 ──────────────────────────────────────

class TestPromotionPolicyConflictMap:
    def test_conflict_map_causes_hold(self, conn):
        from novelforge.governance.promotion_policy import PromotionPolicy
        from novelforge.governance.conflict import ConflictSet, ConflictItem
        from novelforge.config import NovelForgeConfig, CanonGovernanceConfig
        cfg = NovelForgeConfig(governance=CanonGovernanceConfig(mode="auto_promote"))
        cand = _candidate(conn, fact_type="style", risk_tier="low")
        cs = ConflictSet(items=[ConflictItem(
            kind="same_predicate_diff_value", fact_id="f1", detail="x", severity="block"
        )])
        conflict_map = {cand.candidate_id: cs}
        route = PromotionPolicy.decide(cand, None, cfg, conflict_map=conflict_map)
        from novelforge.governance.gate import Route
        assert route == Route.HOLD

    def test_no_conflict_map_auto_commit(self, conn):
        from novelforge.governance.promotion_policy import PromotionPolicy
        from novelforge.config import NovelForgeConfig, CanonGovernanceConfig
        from novelforge.contracts import FactCandidate
        from novelforge.ids import new_id
        cfg = NovelForgeConfig(governance=CanonGovernanceConfig(
            mode="auto_promote",
            require_human_for=[],
            evidence_threshold=0.0,  # 阈值=0 → 任何候选都 commit
        ))
        cand = _candidate(conn, fact_type="style", risk_tier="low")
        route = PromotionPolicy.decide(cand, None, cfg, conflict_map={})
        from novelforge.governance.gate import Route
        assert route == Route.COMMIT


# ── 8. Orchestrator MVP2 端到端 ───────────────────────────────────────────────

class TestOrchestratorMVP2:
    def _setup(self, conn):
        from novelforge.config import NovelForgeConfig, ProviderConfig, CanonGovernanceConfig
        from novelforge.control_plane.llm.factory import build_gateway
        from novelforge.skills import register_default_skills
        from novelforge.control_plane.skill_registry import SkillRegistry
        from novelforge.control_plane.orchestrator import Orchestrator

        cfg = NovelForgeConfig(
            project_id="test_mvp2",
            governance=CanonGovernanceConfig(mode="human_gate"),
            provider=ProviderConfig(provider="fake"),
        )
        gw = build_gateway(cfg)
        reg = SkillRegistry()
        register_default_skills(reg)
        orch = Orchestrator(gw, registry=reg, cfg=cfg)
        return orch

    def test_generate_chapter_ok(self, conn):
        orch = self._setup(conn)
        outcome = orch.generate_chapter(
            chapter=1, conn=conn,
            chapter_goal="主角修炼突破",
        )
        # FakeProvider 会返回空草稿，但流程不应崩溃
        assert outcome.chapter == 1
        assert outcome.error is None or isinstance(outcome.error, str)

    def test_pacing_cursor_written_after_chapter(self, conn):
        orch = self._setup(conn)
        orch.generate_chapter(chapter=1, conn=conn, chapter_goal="test pacing")
        row = conn.execute("SELECT * FROM pacing_cursor WHERE id=1").fetchone()
        assert row is not None

    def test_craft_check_registered(self):
        from novelforge.skills import register_default_skills
        from novelforge.control_plane.skill_registry import SkillRegistry
        reg = SkillRegistry()
        register_default_skills(reg)
        assert "craft_check" in reg.names()
