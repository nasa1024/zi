"""L3 事件驱动 canon 提案测试（§06.2.3 + §06.3.3）。

确定性信号扫描（保不漏）→ 有信号才 LLM 归一化 → 证据校验 → 去重 → BibleChangeProposal。
FakeProvider 注入 JSON，无网络。
"""
from __future__ import annotations

import json

import pytest

from novelforge.control_plane.budget import BudgetLedger
from novelforge.control_plane.llm.fake_provider import FakeProvider
from novelforge.control_plane.llm.gateway import LLMGateway
from novelforge.db.connection import connect, init_db_from_conn
from novelforge.memory.l3_events import (
    CANON_TYPES,
    extract_canon_events,
    scan_canon_signals,
)


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db_from_conn(c)
    yield c
    c.close()


def _gw(provider):
    return LLMGateway(provider, BudgetLedger(max_tokens=200_000, max_usd=5.0))


# ── scan_canon_signals ───────────────────────────────────────────────────────

class TestScan:
    def test_no_signals(self):
        """纯动作场景无 canon 信号。"""
        text = "他挥剑斩出，妖兽轰然倒地，四周一片狼藉。"
        assert scan_canon_signals(text, known_entity_names=set()) == []

    def test_world_rule_signal(self):
        text = "自灵气复苏，天地间多了一条铁律：凡人亦可逆天改命。"
        sigs = scan_canon_signals(text, known_entity_names=set())
        assert any(s["type"] == "world_rule" for s in sigs)

    def test_foreshadow_signal(self):
        text = "石碑上的谶语隐约预示着，三百年后必有一场大劫降临人间。"
        sigs = scan_canon_signals(text, known_entity_names=set())
        assert any(s["type"] == "foreshadowing" for s in sigs)

    def test_constraint_signal(self):
        text = "长老严令：本门弟子绝不得将剑诀外传，违者逐出师门。"
        sigs = scan_canon_signals(text, known_entity_names=set())
        assert any(s["type"] == "constraint" for s in sigs)

    def test_new_entity_signal(self):
        """书名号/引号内、且不在已知实体集中的专名 → 候选新实体。"""
        text = "远方崛起了一个名为《苍冥剑宗》的庞然大物。"
        sigs = scan_canon_signals(text, known_entity_names={"叶凡"})
        assert any(s["type"] == "new_entity" and "苍冥剑宗" in s["anchor"] for s in sigs)

    def test_known_entity_not_flagged(self):
        text = "《青云门》的弟子又来挑衅了。"
        sigs = scan_canon_signals(text, known_entity_names={"青云门"})
        assert not any(s["type"] == "new_entity" for s in sigs)


# ── extract_canon_events ─────────────────────────────────────────────────────

class TestExtract:
    def test_no_signal_no_llm_call(self, conn):
        """无信号 → 直接返回 []，且不调用 LLM（零成本，绝大多数章走这里）。"""
        provider = FakeProvider(responses=["[]"])
        text = "他挥剑斩出，妖兽轰然倒地。"
        out = extract_canon_events(conn, _gw(provider), 1, text)
        assert out == []
        assert provider.calls == []        # 未触达 LLM

    def test_signal_produces_proposals(self, conn):
        text = "自灵气复苏，天地多了一条法则。长老严令剑诀绝不得外传。"
        atoms = [
            {"type": "world_rule", "name": None,
             "statement": "灵气复苏后凡人可修炼", "evidence": "灵气复苏"},
            {"type": "constraint", "name": None,
             "statement": "剑诀不得外传", "evidence": "绝不得外传"},
        ]
        provider = FakeProvider(responses=[json.dumps(atoms, ensure_ascii=False)])
        out = extract_canon_events(conn, _gw(provider), 7, text)
        assert len(out) == 2
        types = {p["fact_type"] for p in out}
        assert types == {"world_rule", "constraint"}
        for p in out:
            assert p["op"] == "add"
            assert p["valid_from_chapter"] == 7
            assert p["evidence_refs"] and p["evidence_refs"][0]["chapter"] == 7

    def test_evidence_must_appear_in_text(self, conn):
        """证据 quote 不在正文中的原子被丢弃（§06.3.1 防幻觉）。"""
        text = "自灵气复苏，天地多了一条法则。"
        atoms = [
            {"type": "world_rule", "name": None,
             "statement": "真实的", "evidence": "灵气复苏"},
            {"type": "world_rule", "name": None,
             "statement": "编造的", "evidence": "魔神降临血洗大陆"},   # 不在正文
        ]
        provider = FakeProvider(responses=[json.dumps(atoms, ensure_ascii=False)])
        out = extract_canon_events(conn, _gw(provider), 2, text)
        assert len(out) == 1
        assert out[0]["new"]["statement"] == "真实的"

    def test_dedup_against_existing_candidate(self, conn):
        """本章已有同 (fact_type, entity) 候选（出稿期已抽）→ L3 跳过，不重复入库。"""
        text = "崛起了《苍冥剑宗》，威震一方。"
        # 兼容旧实现：旧 L3 曾把 new_entity 写成 fact_type='entity'。
        conn.execute(
            "INSERT INTO fact_candidates"
            "(candidate_id, op, entity_id, fact_type, proposal_json, status, risk_tier, source_chapter)"
            " VALUES('c_pre','add',NULL,'entity',?, 'proposed','low',3)",
            (json.dumps({"fact_type": "entity", "entity": "苍冥剑宗"}, ensure_ascii=False),),
        )
        conn.commit()
        atoms = [{"type": "new_entity", "name": "苍冥剑宗",
                  "statement": "新宗门", "evidence": "苍冥剑宗"}]
        provider = FakeProvider(responses=[json.dumps(atoms, ensure_ascii=False)])
        out = extract_canon_events(conn, _gw(provider), 3, text)
        assert out == []        # 去重

    def test_new_entity_and_foreshadow_are_schema_legal_misc(self, conn):
        """new_entity/foreshadowing 不在 facts.fact_type 白名单内，L3 应转成 misc 叙事 fact。"""
        text = "崛起了《苍冥剑宗》，石碑谶语预示三百年后大劫降临。"
        atoms = [
            {"type": "new_entity", "name": "苍冥剑宗",
             "statement": "苍冥剑宗首次登场", "evidence": "苍冥剑宗"},
            {"type": "foreshadowing", "name": None,
             "statement": "三百年后会有大劫", "evidence": "三百年后大劫"},
        ]
        provider = FakeProvider(responses=[json.dumps(atoms, ensure_ascii=False)])
        out = extract_canon_events(conn, _gw(provider), 8, text)
        assert len(out) == 2
        assert {p["fact_type"] for p in out} == {"misc"}
        assert {p["new"]["canon_kind"] for p in out} == {"new_entity", "foreshadowing"}

    def test_bad_llm_output_degrades(self, conn):
        """LLM 返回不可解析（如 echo）→ 返回 []，不抛。"""
        text = "自灵气复苏，天地多了一条法则。"
        provider = FakeProvider()        # 默认 echo，非 JSON
        out = extract_canon_events(conn, _gw(provider), 4, text)
        assert out == []

    def test_canon_types_constant(self):
        assert CANON_TYPES == {"world_rule", "new_entity", "foreshadowing", "constraint"}
