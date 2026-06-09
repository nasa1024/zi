"""MVP1 集成测试（无网络调用，使用 FakeProvider）。

覆盖：
  - PromotionPolicy.decide_batch()
  - BudgetLedger + CircuitBreaker
  - LLMGateway (FakeProvider)
  - SkillRegistry 注册 + 调用
  - PlannerSkill + ChapterDraftSkill + ContinuityCheckSkill (fake LLM)
  - Orchestrator.generate_chapter() 端到端
  - PipelineManager.run_l1()
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
        "INSERT INTO entities(id, canonical_name, entity_type) VALUES(?,?,?)", (eid, name, etype)
    )
    conn.commit()
    return eid


def _candidate(conn, fact_type="appearance", risk_tier="low", chapter=1, entity_id=None):
    from novelforge.ids import new_id
    cid = new_id("cand")
    prop = json.dumps({
        "op": "add", "fact_type": fact_type,
        "entity": entity_id, "new": {"subject": entity_id or "x", "predicate": "p", "object": "o"},
        "valid_from_chapter": chapter,
    }, ensure_ascii=False)
    conn.execute(
        "INSERT INTO fact_candidates(candidate_id, op, entity_id, fact_type, proposal_json, status, risk_tier, source_chapter)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (cid, "add", entity_id, fact_type, prop, "proposed", risk_tier, chapter),
    )
    conn.commit()
    from novelforge.contracts import FactCandidate
    return FactCandidate(
        candidate_id=cid, entity_id=entity_id, fact_type=fact_type,
        proposal_json=prop, status="proposed", risk_tier=risk_tier, source_chapter=chapter,
    )


# ── PromotionPolicy ───────────────────────────────────────────────────────────

class TestPromotionPolicy:
    def test_human_gate_always_review(self, conn):
        from novelforge.config import NovelForgeConfig
        from novelforge.governance.promotion_policy import PromotionPolicy
        from novelforge.governance.gate import Route
        cfg = NovelForgeConfig()
        cfg.governance.mode = "human_gate"
        cfg.governance.require_human_for = []
        cand = _candidate(conn, fact_type="appearance", risk_tier="low")
        route = PromotionPolicy.decide(cand, None, cfg)
        assert route == Route.REVIEW

    def test_auto_promote_with_evidence(self, conn):
        from novelforge.config import NovelForgeConfig
        from novelforge.governance.promotion_policy import PromotionPolicy
        from novelforge.governance.gate import Route
        from novelforge.contracts import FactCandidate
        cfg = NovelForgeConfig()
        cfg.governance.mode = "auto_promote"
        cfg.governance.require_human_for = []
        cand = _candidate(conn, risk_tier="low")
        # evidence_refs 非空 → evidence_strong
        cand_with_ev = FactCandidate(
            candidate_id=cand.candidate_id, entity_id=cand.entity_id,
            fact_type=cand.fact_type, proposal_json=cand.proposal_json,
            status="proposed", risk_tier="low", source_chapter=1,
            evidence_refs="ch1:line10",
        )
        route = PromotionPolicy.decide(cand_with_ev, None, cfg)
        assert route == Route.COMMIT

    def test_require_human_for_retcon(self, conn):
        from novelforge.config import NovelForgeConfig
        from novelforge.governance.promotion_policy import PromotionPolicy
        from novelforge.governance.gate import Route
        from novelforge.contracts import FactCandidate
        cfg = NovelForgeConfig()
        cfg.governance.mode = "auto_promote"
        cfg.governance.require_human_for = ["retcon"]
        prop = json.dumps({"op": "retcon", "fact_type": "power_rank", "valid_from_chapter": 1, "new": {}})
        cand = FactCandidate(
            candidate_id="x", entity_id=None, fact_type="power_rank",
            proposal_json=prop, status="proposed", risk_tier="low", source_chapter=1,
        )
        route = PromotionPolicy.decide(cand, None, cfg)
        assert route == Route.REVIEW

    def test_decide_batch(self, conn):
        from novelforge.config import NovelForgeConfig
        from novelforge.governance.promotion_policy import PromotionPolicy
        from novelforge.governance.gate import Route
        cfg = NovelForgeConfig()
        cfg.governance.mode = "human_gate"
        cfg.governance.require_human_for = []
        candidates = [_candidate(conn) for _ in range(3)]
        decision = PromotionPolicy.decide_batch(candidates, None, cfg)
        assert len(decision.routes) == 3
        assert all(r == Route.REVIEW for _, r in decision.routes)

    def test_hold_on_conflict(self, conn):
        from novelforge.config import NovelForgeConfig
        from novelforge.governance.promotion_policy import PromotionPolicy
        from novelforge.governance.gate import Route
        from novelforge.validators.types import WorldState

        class FakeWorld:
            def __init__(self):
                self.conflict_map = {}

        cfg = NovelForgeConfig()
        cfg.governance.mode = "auto_promote"
        cfg.governance.require_human_for = []
        cand = _candidate(conn, risk_tier="low")
        world = FakeWorld()
        world.conflict_map[cand.candidate_id] = True
        route = PromotionPolicy.decide(cand, world, cfg)
        assert route == Route.HOLD


# ── Budget + CircuitBreaker ───────────────────────────────────────────────────

class TestBudget:
    def test_charge_accumulates(self):
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.provider import Usage
        ledger = BudgetLedger(max_tokens=10_000, max_usd=1.0)
        usage = Usage(input=100, output=50, model="claude-sonnet-4-6")
        ledger.charge(usage)
        assert ledger.tokens_spent == 150
        assert ledger.usd_spent > 0

    def test_circuit_breaker_tokens(self):
        from novelforge.control_plane.budget import BudgetLedger, CircuitBreaker, CircuitTripped
        ledger = BudgetLedger(max_tokens=100, max_usd=100.0)
        cb = CircuitBreaker(ledger)
        with pytest.raises(CircuitTripped) as exc:
            cb.guard(tokens_in=80, tokens_out_est=30, model="claude-sonnet-4-6")
        assert exc.value.reason == "tokens"

    def test_circuit_breaker_usd(self):
        from novelforge.control_plane.budget import BudgetLedger, CircuitBreaker, CircuitTripped
        ledger = BudgetLedger(max_tokens=10_000_000, max_usd=0.001)
        cb = CircuitBreaker(ledger)
        with pytest.raises(CircuitTripped) as exc:
            cb.guard(tokens_in=100_000, tokens_out_est=100_000, model="claude-opus-4-8")
        assert exc.value.reason == "usd"

    def test_no_trip_within_budget(self):
        from novelforge.control_plane.budget import BudgetLedger, CircuitBreaker
        ledger = BudgetLedger(max_tokens=200_000, max_usd=5.0)
        cb = CircuitBreaker(ledger)
        cb.guard(tokens_in=100, tokens_out_est=100, model="claude-haiku-4-5-20251001")  # no raise


# ── LLMGateway + FakeProvider ─────────────────────────────────────────────────

class TestLLMGateway:
    def _make_gw(self, responses=None):
        from novelforge.control_plane.llm.fake_provider import FakeProvider
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.gateway import LLMGateway
        p = FakeProvider(responses=responses or ["hello"])
        l = BudgetLedger(max_tokens=200_000, max_usd=5.0)
        return LLMGateway(p, l), p

    def test_generate_returns_response(self):
        from novelforge.control_plane.llm.tiers import ModelTier
        from novelforge.control_plane.llm.provider import Message
        gw, _ = self._make_gw(["你好"])
        resp = gw.generate(ModelTier.MID, [Message(role="user", content="hi")])
        assert resp.text == "你好"

    def test_budget_charged_after_call(self):
        from novelforge.control_plane.llm.tiers import ModelTier
        from novelforge.control_plane.llm.provider import Message
        gw, _ = self._make_gw(["test"])
        gw.generate(ModelTier.FAST, [Message(role="user", content="x")])
        assert gw.ledger.tokens_spent > 0

    def test_circuit_trips_gateway(self):
        from novelforge.control_plane.llm.tiers import ModelTier
        from novelforge.control_plane.llm.provider import Message
        from novelforge.control_plane.budget import BudgetLedger, CircuitTripped
        from novelforge.control_plane.llm.gateway import LLMGateway
        from novelforge.control_plane.llm.fake_provider import FakeProvider
        p = FakeProvider(responses=["a"])
        l = BudgetLedger(max_tokens=1, max_usd=100.0)  # 1 token 上限
        gw = LLMGateway(p, l)
        with pytest.raises(CircuitTripped):
            gw.generate(ModelTier.MID, [Message(role="user", content="x" * 100)])


# ── SkillRegistry ─────────────────────────────────────────────────────────────

class TestSkillRegistry:
    def test_register_and_invoke(self, conn):
        from novelforge.control_plane.skill_registry import SkillRegistry
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.gateway import LLMGateway
        from novelforge.control_plane.llm.fake_provider import FakeProvider
        from novelforge.control_plane.skill_base import SkillContext

        beats_json = '[{"beat_type":"setup","summary":"intro","value_axis":"中性"},{"beat_type":"hook","summary":"悬念","value_axis":"紧张↑"}]'
        p = FakeProvider(responses=[beats_json])
        l = BudgetLedger()
        gw = LLMGateway(p, l)

        from novelforge.skills import PlannerSkill
        reg = SkillRegistry()
        reg.register(PlannerSkill())
        assert "planner" in reg.names()

        ctx = SkillContext(
            project_id="test", target_chapter=1, mode="human_gate",
            as_of_chapter=0, budget=l, llm=gw, conn=conn, workspace={},
        )
        result = reg.invoke("planner", ctx)
        assert result.ok is True
        assert len(ctx.workspace.get("beats", [])) >= 1

    def test_missing_skill_returns_error(self, conn):
        from novelforge.control_plane.skill_registry import SkillRegistry
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.gateway import LLMGateway
        from novelforge.control_plane.llm.fake_provider import FakeProvider
        from novelforge.control_plane.skill_base import SkillContext
        gw = LLMGateway(FakeProvider(), BudgetLedger())
        reg = SkillRegistry()
        ctx = SkillContext("p", 1, "human_gate", 0, BudgetLedger(), gw, conn, {})
        result = reg.invoke("nonexistent_skill", ctx)
        assert result.ok is False
        assert "not registered" in result.error


# ── PlannerSkill ─────────────────────────────────────────────────────────────

class TestPlannerSkill:
    def test_parses_beats_array(self, conn):
        from novelforge.skills import PlannerSkill
        from novelforge.control_plane.skill_base import SkillContext
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.gateway import LLMGateway
        from novelforge.control_plane.llm.fake_provider import FakeProvider

        beats_json = '[{"beat_type":"setup","summary":"开场","value_axis":"平"},{"beat_type":"hook","summary":"结尾悬念","value_axis":"紧张↑"}]'
        gw = LLMGateway(FakeProvider(responses=[beats_json]), BudgetLedger())
        ctx = SkillContext("p", 3, "human_gate", 2, BudgetLedger(), gw, conn, {})
        result = PlannerSkill().run(ctx)
        assert result.ok
        assert ctx.workspace["beats"][1]["beat_type"] == "hook"

    def test_fallback_on_malformed_json(self, conn):
        from novelforge.skills import PlannerSkill
        from novelforge.control_plane.skill_base import SkillContext
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.gateway import LLMGateway
        from novelforge.control_plane.llm.fake_provider import FakeProvider

        gw = LLMGateway(FakeProvider(responses=["not json at all"]), BudgetLedger())
        ctx = SkillContext("p", 1, "human_gate", 0, BudgetLedger(), gw, conn, {})
        result = PlannerSkill().run(ctx)
        # 不崩溃，beats 非空（兜底 setup）
        assert isinstance(ctx.workspace.get("beats"), list)
        assert len(ctx.workspace["beats"]) >= 1


# ── ChapterDraftSkill ─────────────────────────────────────────────────────────

class TestChapterDraftSkill:
    _FAKE_RESPONSE = """\
```draft
主角走出了宗门大门，踏上了前往秘境的旅途。一路上，他心中充满了期待与忐忑。
秘境中充满了危机，每一步都可能是最后一步。但他知道，这是他突破瓶颈的唯一机会。
他深吸一口气，脚步坚定地迈入了秘境的迷雾之中。那一刻，命运的齿轮悄然转动。
"这里……有什么不对劲。"他低声自语，感应到前方隐藏着一股巨大的威压。这是今天最大的考验。
```

```proposals
[
  {
    "op": "add",
    "fact_type": "appearance",
    "entity": "主角",
    "new": {"subject": "主角", "predicate": "enters", "object": "秘境"},
    "valid_from_chapter": 2,
    "risk_tier": "low"
  }
]
```
"""

    def test_parses_draft_and_proposals(self, conn):
        from novelforge.skills import ChapterDraftSkill
        from novelforge.control_plane.skill_base import SkillContext
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.gateway import LLMGateway
        from novelforge.control_plane.llm.fake_provider import FakeProvider

        gw = LLMGateway(FakeProvider(responses=[self._FAKE_RESPONSE]), BudgetLedger())
        ctx = SkillContext("p", 2, "human_gate", 1, BudgetLedger(), gw, conn,
                          workspace={"beats": []})
        result = ChapterDraftSkill().run(ctx)
        assert "主角" in ctx.workspace["draft_text"]
        assert isinstance(ctx.workspace["proposals"], list)
        assert len(ctx.workspace["proposals"]) >= 1


# ── ContinuityCheckSkill ──────────────────────────────────────────────────────

class TestContinuityCheckSkill:
    def test_no_issues_on_clean_draft(self, conn):
        from novelforge.skills import ContinuityCheckSkill
        from novelforge.control_plane.skill_base import SkillContext
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.gateway import LLMGateway
        from novelforge.control_plane.llm.fake_provider import FakeProvider

        gw = LLMGateway(FakeProvider(responses=["[]"]), BudgetLedger())
        ctx = SkillContext("p", 1, "human_gate", 0, BudgetLedger(), gw, conn,
                          workspace={"draft_text": "平静的一天。", "proposals": []})
        result = ContinuityCheckSkill().run(ctx)
        assert result.ok  # 无 block 级问题


# ── PipelineManager ───────────────────────────────────────────────────────────

class TestPipelineManager:
    def test_run_l1_no_crash(self, conn):
        from novelforge.memory.pipeline_manager import PipelineManager
        eid = _entity(conn, name="主角")
        pm = PipelineManager(conn)
        result = pm.run_l1(1, "主角踏上旅途，一路风平浪静。")
        assert result.l1_ok

    def test_l2_stub(self, conn):
        from novelforge.memory.pipeline_manager import PipelineManager
        pm = PipelineManager(conn)
        r = pm.run_l2(5)
        assert r.l2_triggered

    def test_should_run_l2_boundary(self, conn):
        from novelforge.memory.pipeline_manager import PipelineManager
        pm = PipelineManager(conn)
        assert pm.should_run_l2(5)
        assert not pm.should_run_l2(3)


# ── Orchestrator 端到端（FakeProvider）──────────────────────────────────────

class TestOrchestrator:
    _BEATS = '[{"beat_type":"setup","summary":"开场","value_axis":"平"},{"beat_type":"hook","summary":"悬念","value_axis":"紧"}]'
    _DRAFT = """\
```draft
今日，主角独自穿越了迷雾森林。
树影婆娑，鸟鸣婆娑，空气中弥漫着淡淡的药香。
他不知道，命运的转折就在前方等待。秘密，即将揭开。
```

```proposals
[{"op":"add","fact_type":"appearance","entity":null,"new":{"subject":"主角","predicate":"traverses","object":"迷雾森林"},"valid_from_chapter":1,"risk_tier":"low"}]
```
"""

    def _make_orchestrator(self, conn):
        from novelforge.control_plane.llm.fake_provider import FakeProvider
        from novelforge.control_plane.budget import BudgetLedger
        from novelforge.control_plane.llm.gateway import LLMGateway
        from novelforge.control_plane.skill_registry import SkillRegistry
        from novelforge.skills import register_default_skills
        from novelforge.config import NovelForgeConfig
        from novelforge.control_plane.orchestrator import Orchestrator

        # FakeProvider 响应顺序：1=planner beats, 2=draft, 3=continuity soft check
        p = FakeProvider(responses=[self._BEATS, self._DRAFT, "[]"])
        l = BudgetLedger(max_tokens=500_000, max_usd=50.0)
        gw = LLMGateway(p, l)
        reg = SkillRegistry()
        register_default_skills(reg)
        cfg = NovelForgeConfig()
        cfg.governance.mode = "human_gate"  # 所有候选 → REVIEW（不实际写 facts）
        cfg.governance.require_human_for = []
        return Orchestrator(gw, reg, cfg)

    def test_generate_chapter_ok(self, conn):
        orch = self._make_orchestrator(conn)
        outcome = orch.generate_chapter(1, conn, chapter_goal="主角出发")
        assert outcome.ok, outcome.error
        assert outcome.chapter == 1
        assert isinstance(outcome.draft_text, str)

    def test_generate_chapter_queues_candidates(self, conn):
        orch = self._make_orchestrator(conn)
        outcome = orch.generate_chapter(1, conn)
        # human_gate 模式 → 所有候选入 review_queue
        assert outcome.gate is not None
        assert len(outcome.fact_ids_committed) == 0  # human_gate 无自动提交

    def test_usage_tokens_recorded(self, conn):
        orch = self._make_orchestrator(conn)
        outcome = orch.generate_chapter(1, conn)
        assert outcome.usage_tokens > 0
