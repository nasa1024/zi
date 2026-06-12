"""P2#12 接线测试：_run_hard_validators 真跑确定性校验（修复沉睡的死代码）。

历史问题（见 spec §0）：import 名错 + extract_claims_rule 缺参 + severity 域不匹配，
三个 validator 在管线里从未跑过。本文件锁定修复后的行为。
"""
from __future__ import annotations

import json

import pytest

from novelforge.db.connection import connect, init_db_from_conn
from novelforge.ids import new_id
from novelforge.validators.types import WorldState


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db_from_conn(c)
    yield c
    c.close()


def _seed_entity(conn, name, etype="character"):
    eid = new_id("ent")
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type) VALUES(?,?,?)",
        (eid, name, etype))
    conn.commit()
    return eid


def _seed_ranks(conn):
    for name, order in (("炼气", 1), ("筑基", 2), ("金丹", 3)):
        conn.execute(
            "INSERT INTO power_ranks(id, system_name, rank_name, rank_order)"
            " VALUES(?,?,?,?)", (new_id("pr"), "修真", name, order))
    conn.commit()


def _make_ctx(conn, draft_text, chapter=10):
    from novelforge.control_plane.skill_base import SkillContext
    from novelforge.control_plane.budget import BudgetLedger
    from novelforge.control_plane.llm.gateway import LLMGateway
    from novelforge.control_plane.llm.fake_provider import FakeProvider

    gw = LLMGateway(FakeProvider(responses=["[]"]), BudgetLedger())
    return SkillContext(
        "proj", chapter, "auto_promote", chapter - 1, BudgetLedger(), gw, conn,
        workspace={"draft_text": draft_text, "proposals": []},
    )


class TestHardValidatorWiring:
    def _run(self, conn, draft_text, chapter=10):
        from novelforge.skills.continuity_check_skill import _run_hard_validators
        ctx = _make_ctx(conn, draft_text, chapter)
        world = WorldState(as_of=chapter - 1, conn=conn)
        return _run_hard_validators([], world, ctx)

    def test_knowledge_leak_is_block_finding(self, conn):
        """精炼后的 KNOWLEDGE claim 无知情边 → KNOWLEDGE_LEAK，critical→block。"""
        _seed_entity(conn, "叶凡")
        other = _seed_entity(conn, "萧炎")
        conn.execute(
            "INSERT INTO knowledge_edges(id, knower_entity_id, secret_key,"
            " knowledge_state, learned_chapter) VALUES(?,?,?,'knows',3)",
            (new_id("ke"), other, "血衣门密谋"))
        conn.commit()
        findings = self._run(conn, "叶凡知道了血衣门密谋。")
        leaks = [f for f in findings if f["category"] == "KNOWLEDGE_LEAK"]
        assert leaks and leaks[0]["severity"] == "block"
        assert leaks[0]["source"] == "validator"

    def test_no_presence_is_warn_finding(self, conn):
        """在场名单缺主语 → KNOWLEDGE_NO_PRESENCE，major→warn。"""
        _seed_entity(conn, "叶凡")
        other = _seed_entity(conn, "萧炎")
        conn.execute(
            "INSERT INTO knowledge_edges(id, knower_entity_id, secret_key,"
            " knowledge_state, learned_chapter) VALUES(?,?,?,'knows',3)",
            (new_id("ke"), other, "血衣门密谋"))
        conn.execute(
            "INSERT INTO timeline_events(id, title, chapter, story_time_start,"
            " story_time_end, participants) VALUES(?,?,3,0,1,?)",
            (new_id("tl"), "血衣门密谋", json.dumps([other])))
        conn.commit()
        findings = self._run(conn, "叶凡知道了血衣门密谋。")
        np_ = [f for f in findings if f["category"] == "KNOWLEDGE_NO_PRESENCE"]
        assert np_ and np_[0]["severity"] == "warn"
        assert "不在场" in np_[0]["issue"]

    def test_power_regression_is_block(self, conn):
        """境界倒退（无 injury 记录）→ POWER_* critical→block。"""
        _seed_ranks(conn)
        eid = _seed_entity(conn, "陆天")
        rank_id = conn.execute(
            "SELECT id FROM power_ranks WHERE rank_name='金丹'").fetchone()[0]
        conn.execute(
            "INSERT INTO character_power_log(id, entity_id, system_name, rank_id,"
            " rank_order, change_chapter, change_type)"
            " VALUES(?,?,'修真',?,3,5,'breakthrough')", (new_id("cpl"), eid, rank_id))
        conn.commit()
        findings = self._run(conn, "陆天的修为跌回炼气。")
        assert any(f["severity"] == "block" and f["category"].startswith("POWER")
                   for f in findings)

    def test_clean_draft_no_findings(self, conn):
        """干净草稿（无账本冲突）→ 无 finding（激活校验不带来背景噪声）。"""
        _seed_entity(conn, "叶凡")
        findings = self._run(conn, "叶凡走进山门，与师兄寒暄几句。")
        assert findings == []

    def test_evidence_carries_claim_span(self, conn):
        """finding.evidence 带 claim 原文 span（锚点补丁可用）。"""
        _seed_entity(conn, "叶凡")
        other = _seed_entity(conn, "萧炎")
        conn.execute(
            "INSERT INTO knowledge_edges(id, knower_entity_id, secret_key,"
            " knowledge_state, learned_chapter) VALUES(?,?,?,'knows',3)",
            (new_id("ke"), other, "血衣门密谋"))
        conn.commit()
        findings = self._run(conn, "叶凡知道了血衣门密谋。")
        assert any("血衣门密谋" in (f.get("evidence") or "") for f in findings)

    def test_world_none_skips(self, conn):
        from novelforge.skills.continuity_check_skill import _run_hard_validators
        ctx = _make_ctx(conn, "叶凡知道了血衣门密谋。")
        assert _run_hard_validators([], None, ctx) == []


class TestCandidateJudgeHardBlocks:
    def test_count_hard_blocks_detects_power_regression(self, conn):
        from novelforge.craft.candidate_judge import _count_hard_blocks
        _seed_ranks(conn)
        eid = _seed_entity(conn, "陆天")
        rank_id = conn.execute(
            "SELECT id FROM power_ranks WHERE rank_name='金丹'").fetchone()[0]
        conn.execute(
            "INSERT INTO character_power_log(id, entity_id, system_name, rank_id,"
            " rank_order, change_chapter, change_type)"
            " VALUES(?,?,'修真',?,3,5,'breakthrough')", (new_id("cpl"), eid, rank_id))
        conn.commit()
        world = WorldState(as_of=9, conn=conn)
        assert _count_hard_blocks("陆天的修为跌回炼气。", world) > 0

    def test_count_zero_for_clean_draft(self, conn):
        from novelforge.craft.candidate_judge import _count_hard_blocks
        _seed_ranks(conn)
        world = WorldState(as_of=9, conn=conn)
        assert _count_hard_blocks("平静的一章。", world) == 0
