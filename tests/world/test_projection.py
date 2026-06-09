"""Tests for projection applier: §16.5 apply_state_transition, §16.6 commit_canon,
§16.8 reproject_affected, §16.9 replay_*.

All use :memory: SQLite fixture — zero LLM, zero network.
"""
import json
import sqlite3
import pytest

from novelforge.db.connection import connect, init_db_from_conn
from novelforge.ids import new_id
from novelforge.contracts import BibleChangeProposal, FactCandidate, RunContext
from novelforge.world.projection import (
    apply_state_transition,
    resolve_entity_id,
    ProjectionError,
    reproject_affected,
)
from novelforge.world.replay import get_world_state, replay_power, replay_knowledge, replay_numeric
from novelforge.governance.commit import commit_canon
from novelforge.governance.gate import Route, GateOutcome, apply_gate_routes


# ── fixture ──────────────────────────────────────────────────────────────────

@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db_from_conn(c)
    yield c
    c.close()


def _seed_entity(conn, name="叶凡", etype="character"):
    eid = new_id("ent")
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type) VALUES(?,?,?)",
        (eid, name, etype),
    )
    conn.commit()
    return eid


def _stub_fact(conn, fact_id=None, chapter=1):
    """Insert a minimal canon facts row so source_fact_id FK is satisfied in direct tests."""
    fid = fact_id or new_id("fact")
    conn.execute(
        "INSERT INTO facts(id, fact_type, subject, predicate, object, status,"
        " valid_from_chapter, current_revision_id, version)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (fid, "misc", "stub", "stub", "stub", "canon", chapter, "rev_stub", 0),
    )
    conn.commit()
    return fid


def _seed_rank(conn, system="练气体系", name="金丹·初期", order=30):
    rid = new_id("prk")
    conn.execute(
        "INSERT INTO power_ranks(id, system_name, rank_name, rank_order) VALUES(?,?,?,?)",
        (rid, system, name, order),
    )
    conn.commit()
    return rid


def _make_power_cand(conn, eid, system, rank_name, chapter=5):
    prop = BibleChangeProposal(
        op="add", fact_type="power_system", entity=eid,
        new={"facet": "power", "object": rank_name, "system_name": system,
             "rank_name": rank_name, "change_type": "breakthrough"},
        valid_from_chapter=chapter,
    )
    cid = new_id("cand")
    conn.execute(
        "INSERT INTO fact_candidates(candidate_id,entity_id,fact_type,proposal_json,op,status,risk_tier,source_chapter)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (cid, eid, "power_system", prop.model_dump_json(), "add", "proposed", "low", chapter),
    )
    conn.commit()
    return FactCandidate(
        candidate_id=cid, entity_id=eid, fact_type="power_system",
        proposal_json=prop.model_dump_json(), status="proposed", risk_tier="low",
        source_chapter=chapter,
    )


# ── 端到端核心验收 ─────────────────────────────────────────────────────────────

class TestCommitPowerLandsInLog:
    def test_commit_power_lands_in_log_and_reads_back(self, conn):
        """commit 金丹 → character_power_log+1 → get_world_state(5) 读到；as_of=4 读不到。"""
        eid = _seed_entity(conn)
        _seed_rank(conn, order=30)
        cand = _make_power_cand(conn, eid, "练气体系", "金丹·初期", chapter=5)

        fact_id = commit_canon(cand, conn, policy_mode="human_gate", actor="human:test")

        row = conn.execute(
            "SELECT rank_order, source_fact_id FROM character_power_log WHERE entity_id=?",
            (eid,),
        ).fetchone()
        assert row is not None
        assert row["source_fact_id"] == fact_id
        assert row["rank_order"] == 30

        ws5 = get_world_state(5, conn)
        hist = ws5.power_history(eid)
        assert len(hist) == 1
        assert hist[0]["rank_order"] == 30

        ws4 = get_world_state(4, conn)
        assert ws4.power_history(eid) == []

    def test_replay_power_filters_retconned_without_reproject(self, conn):
        """手工把 fact 置 retconned 但不跑级联 → replay 仍不读入（lazy 兜底）。"""
        eid = _seed_entity(conn)
        _seed_rank(conn, order=30)
        cand = _make_power_cand(conn, eid, "练气体系", "金丹·初期", chapter=3)
        fact_id = commit_canon(cand, conn, policy_mode="human_gate", actor="human:test")

        # manually mark fact retconned (skip reproject cascade)
        conn.execute("UPDATE facts SET status='retconned' WHERE id=?", (fact_id,))
        conn.commit()

        rp = replay_power(conn, 10)
        assert eid not in rp, "retconned fact should be filtered by replay"


# ── 各 facet 正例 ──────────────────────────────────────────────────────────────

class TestFacetKnowledge:
    def test_apply_knowledge_inserts_edge(self, conn):
        from novelforge.contracts import StateTransition
        eid = _seed_entity(conn)
        sfid = _stub_fact(conn, chapter=3)

        t = StateTransition(
            entity_id=eid, facet="knowledge",
            to_value="反派真实身份",
            at_chapter=3,
            payload={"facet": "knowledge", "secret_key": "反派真实身份",
                     "knowledge_state": "knows"},
        )
        with conn:
            apply_state_transition(t, sfid, conn)

        row = conn.execute(
            "SELECT knowledge_state FROM knowledge_edges WHERE knower_entity_id=?",
            (eid,),
        ).fetchone()
        assert row is not None
        assert row["knowledge_state"] == "knows"

        ws = get_world_state(3, conn)
        assert "反派真实身份" in ws.knowledge_set(eid, 3)


class TestFacetNumeric:
    def test_apply_numeric_records_value_unit(self, conn):
        from novelforge.contracts import StateTransition
        eid = _seed_entity(conn)
        sfid = _stub_fact(conn, chapter=4)

        t = StateTransition(
            entity_id=eid, facet="numeric",
            to_value="1200",
            at_chapter=4,
            payload={"facet": "numeric", "metric_key": "灵石", "value": 1200,
                     "unit": "枚", "monotonic": "none"},
        )
        with conn:
            apply_state_transition(t, sfid, conn)

        ws = get_world_state(4, conn)
        ns = ws.numeric_state(eid, "灵石")
        assert ns is not None
        assert ns["value"] == 1200
        assert ns["unit"] == "枚"


class TestFacetItem:
    def test_apply_item_updates_ownership_cursor(self, conn):
        from novelforge.contracts import StateTransition
        owner_eid = _seed_entity(conn, "叶凡")
        item_eid = _seed_entity(conn, "灵石", "item")
        sfid = _stub_fact(conn, chapter=2)

        t = StateTransition(
            entity_id=item_eid, facet="item",
            to_value="叶凡获得灵石×3",
            at_chapter=2,
            payload={"facet": "item", "item_entity": item_eid,
                     "from_owner": None, "to_owner": owner_eid,
                     "quantity_delta": 3, "change_type": "acquire"},
        )
        with conn:
            apply_state_transition(t, sfid, conn)

        ws = get_world_state(2, conn)
        assert ws.item_qty(owner_eid, item_eid) == 3

    def test_apply_item_double_spend_raises(self, conn):
        from novelforge.contracts import StateTransition
        owner_eid = _seed_entity(conn, "叶凡")
        item_eid = _seed_entity(conn, "灵石", "item")

        sfid1 = _stub_fact(conn, chapter=1)
        t_acquire = StateTransition(
            entity_id=item_eid, facet="item",
            to_value="acquire",
            at_chapter=1,
            payload={"facet": "item", "item_entity": item_eid,
                     "from_owner": None, "to_owner": owner_eid,
                     "quantity_delta": 1, "change_type": "acquire"},
        )
        with conn:
            apply_state_transition(t_acquire, sfid1, conn)

        sfid2 = _stub_fact(conn, chapter=2)
        t_consume = StateTransition(
            entity_id=item_eid, facet="item",
            to_value="consume",
            at_chapter=2,
            payload={"facet": "item", "item_entity": item_eid,
                     "from_owner": owner_eid, "to_owner": None,
                     "quantity_delta": 5, "change_type": "consume"},
        )
        with pytest.raises(ProjectionError, match="ITEM_DOUBLE_SPEND"):
            with conn:
                apply_state_transition(t_consume, sfid2, conn)


# ── 软记忆不投影 ───────────────────────────────────────────────────────────────

class TestPureLoreFact:
    def test_pure_lore_fact_no_log(self, conn):
        """world_rule fact → 0 行 *_log。"""
        eid = _seed_entity(conn)
        prop = BibleChangeProposal(
            op="add", fact_type="world_rule", entity=None,
            new={"object": "弑父禁忌", "predicate": "world_rule"},
            valid_from_chapter=1,
        )
        cid = new_id("cand")
        conn.execute(
            "INSERT INTO fact_candidates(candidate_id,fact_type,proposal_json,op,status,risk_tier,source_chapter)"
            " VALUES(?,?,?,?,?,?,?)",
            (cid, "world_rule", prop.model_dump_json(), "add", "proposed", "low", 1),
        )
        conn.commit()
        cand = FactCandidate(
            candidate_id=cid, entity_id=None, fact_type="world_rule",
            proposal_json=prop.model_dump_json(), status="proposed", risk_tier="low",
            source_chapter=1,
        )
        commit_canon(cand, conn, policy_mode="human_gate", actor="human:test")

        # no *_log rows written (world_rule is narrative, not projected)
        assert conn.execute("SELECT COUNT(*) FROM character_power_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM knowledge_edges").fetchone()[0] == 0


# ── retcon 级联 ──────────────────────────────────────────────────────────────

class TestRetcon:
    def test_retcon_marks_old_retconned_appends_new(self, conn):
        """retcon: 旧 fact status=retconned（object 不变），新 fact canon。"""
        eid = _seed_entity(conn)
        _seed_rank(conn, name="筑基·初期", order=20)
        _seed_rank(conn, name="金丹·初期", order=30)

        # original: 筑基
        cand1 = _make_power_cand(conn, eid, "练气体系", "筑基·初期", chapter=3)
        old_fact_id = commit_canon(cand1, conn, policy_mode="human_gate", actor="human:test")

        # retcon: actually 金丹 all along
        retcon_prop = BibleChangeProposal(
            op="retcon", fact_type="power_system", entity=eid,
            new={"facet": "power", "object": "金丹·初期", "system_name": "练气体系",
                 "rank_name": "金丹·初期", "change_type": "breakthrough"},
            valid_from_chapter=3,
            target_fact_id=old_fact_id,
        )
        rcid = new_id("cand")
        conn.execute(
            "INSERT INTO fact_candidates(candidate_id,entity_id,fact_type,proposal_json,op,target_fact_id,status,risk_tier,source_chapter)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (rcid, eid, "power_system", retcon_prop.model_dump_json(), "retcon",
             old_fact_id, "proposed", "low", 3),
        )
        conn.commit()
        rc_cand = FactCandidate(
            candidate_id=rcid, entity_id=eid, fact_type="power_system",
            proposal_json=retcon_prop.model_dump_json(), status="proposed",
            risk_tier="low", source_chapter=3, target_fact_id=old_fact_id,
        )
        new_fact_id = commit_canon(rc_cand, conn, policy_mode="human_gate", actor="human:test")

        old = conn.execute("SELECT status FROM facts WHERE id=?", (old_fact_id,)).fetchone()
        new = conn.execute("SELECT status FROM facts WHERE id=?", (new_fact_id,)).fetchone()
        assert old["status"] == "retconned"
        assert new["status"] == "canon"

    def test_retcon_reprojects_world_state(self, conn):
        """retcon 筑基→金丹 后 get_world_state 读金丹（rank_order=30）不读筑基（20）。"""
        eid = _seed_entity(conn)
        _seed_rank(conn, name="筑基·初期", order=20)
        _seed_rank(conn, name="金丹·初期", order=30)

        cand1 = _make_power_cand(conn, eid, "练气体系", "筑基·初期", chapter=3)
        old_fact_id = commit_canon(cand1, conn, policy_mode="human_gate", actor="human:test")

        retcon_prop = BibleChangeProposal(
            op="retcon", fact_type="power_system", entity=eid,
            new={"facet": "power", "object": "金丹·初期", "system_name": "练气体系",
                 "rank_name": "金丹·初期", "change_type": "breakthrough"},
            valid_from_chapter=3,
            target_fact_id=old_fact_id,
        )
        rcid = new_id("cand")
        conn.execute(
            "INSERT INTO fact_candidates(candidate_id,entity_id,fact_type,proposal_json,op,target_fact_id,status,risk_tier,source_chapter)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (rcid, eid, "power_system", retcon_prop.model_dump_json(), "retcon",
             old_fact_id, "proposed", "low", 3),
        )
        conn.commit()
        rc_cand = FactCandidate(
            candidate_id=rcid, entity_id=eid, fact_type="power_system",
            proposal_json=retcon_prop.model_dump_json(), status="proposed",
            risk_tier="low", source_chapter=3, target_fact_id=old_fact_id,
        )
        commit_canon(rc_cand, conn, policy_mode="human_gate", actor="human:test")

        # after retcon: only 金丹(30) in power_log (projected by reproject_affected)
        rows = conn.execute(
            "SELECT rank_order FROM character_power_log WHERE entity_id=?", (eid,)
        ).fetchall()
        orders = [r["rank_order"] for r in rows]
        assert 30 in orders
        assert 20 not in orders, f"旧 筑基 log 行应已被重投影删除, got {orders}"

        ws = get_world_state(3, conn)
        hist = ws.power_history(eid)
        assert any(h["rank_order"] == 30 for h in hist)
        assert all(h["rank_order"] != 20 for h in hist)


# ── apply_gate_routes 路由 ────────────────────────────────────────────────────

class _FakeGate:
    def __init__(self, routes):
        self.routes = routes


class TestGateRoutes:
    def test_route_commit_lands_canon_and_log(self, conn):
        eid = _seed_entity(conn)
        _seed_rank(conn, order=30)
        cand = _make_power_cand(conn, eid, "练气体系", "金丹·初期", chapter=5)
        ctx = RunContext(conn=conn, policy_mode="auto_promote", actor="system:pipeline")
        gate = _FakeGate([(cand, Route.COMMIT)])
        outcome = apply_gate_routes(ctx, gate, {})
        assert len(outcome.committed) == 1
        fact_id = outcome.committed[0][1]
        row = conn.execute("SELECT source_fact_id FROM character_power_log WHERE entity_id=?", (eid,)).fetchone()
        assert row["source_fact_id"] == fact_id

    def test_route_reject_marks_rejected(self, conn):
        eid = _seed_entity(conn)
        cand = _make_power_cand(conn, eid, "练气体系", "金丹·初期", chapter=1)
        ctx = RunContext(conn=conn, policy_mode="human_gate", actor="human:test")
        gate = _FakeGate([(cand, Route.REJECT)])
        outcome = apply_gate_routes(ctx, gate, {})
        assert cand.candidate_id in outcome.rejected
        row = conn.execute(
            "SELECT status FROM fact_candidates WHERE candidate_id=?", (cand.candidate_id,)
        ).fetchone()
        assert row["status"] == "rejected"

    def test_apply_gate_routes_idempotent_on_rerun(self, conn):
        """已 promoted 候选重跑跳过（幂等）。"""
        eid = _seed_entity(conn)
        _seed_rank(conn, order=30)
        cand = _make_power_cand(conn, eid, "练气体系", "金丹·初期", chapter=5)
        ctx = RunContext(conn=conn, policy_mode="auto_promote", actor="system:pipeline")
        gate = _FakeGate([(cand, Route.COMMIT)])
        apply_gate_routes(ctx, gate, {})

        # simulate re-run: candidate is now promoted, not 'proposed'
        cand.status = "promoted"
        gate2 = _FakeGate([(cand, Route.COMMIT)])
        outcome2 = apply_gate_routes(ctx, gate2, {})
        assert len(outcome2.committed) == 0  # skipped

    def test_promotion_log_notnull_columns_filled(self, conn):
        """decision/policy_mode/risk_tier/reason/actor 均非空。"""
        eid = _seed_entity(conn)
        _seed_rank(conn, order=30)
        cand = _make_power_cand(conn, eid, "练气体系", "金丹·初期", chapter=5)
        ctx = RunContext(conn=conn, policy_mode="human_gate", actor="human:tester")
        gate = _FakeGate([(cand, Route.COMMIT)])
        apply_gate_routes(ctx, gate, {})

        row = conn.execute("SELECT * FROM promotion_log ORDER BY created_at DESC LIMIT 1").fetchone()
        assert row["decision"] is not None
        assert row["policy_mode"] == "human_gate"
        assert row["risk_tier"] == "low"
        assert row["reason"] is not None
        assert row["actor"] == "human:tester"


# ── resolve helpers ───────────────────────────────────────────────────────────

class TestResolveEntity:
    def test_resolve_entity_by_id_name_alias(self, conn):
        eid = _seed_entity(conn, "叶凡")
        aid = new_id("alias")
        conn.execute(
            "INSERT INTO entity_aliases(id, entity_id, alias) VALUES(?,?,?)",
            (aid, eid, "小凡"),
        )
        conn.commit()

        assert resolve_entity_id(eid, conn) == eid
        assert resolve_entity_id("叶凡", conn) == eid
        assert resolve_entity_id("小凡", conn) == eid

    def test_unknown_entity_raises(self, conn):
        with pytest.raises(ProjectionError, match="未知实体"):
            resolve_entity_id("不存在的人", conn)
