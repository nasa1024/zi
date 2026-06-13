"""P2#14：generate_validated——机械校验失败自动升一档重试。

设计：docs/superpowers/specs/2026-06-13-p2-objective-kr-tier-upgrade-design.md
"""
from __future__ import annotations

import json

import pytest

from novelforge.control_plane.budget import BudgetLedger
from novelforge.control_plane.llm.fake_provider import FakeProvider
from novelforge.control_plane.llm.gateway import LLMGateway
from novelforge.control_plane.llm.provider import Message
from novelforge.control_plane.llm.tiers import ModelTier


def _gw(factory=None, responses=None):
    return LLMGateway(FakeProvider(factory=factory, responses=responses), BudgetLedger())


def _parse_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


class TestGenerateValidated:
    def test_start_tier_success_no_escalation(self):
        gw = _gw(responses=['{"ok": 1}'])
        r = gw.generate_validated(
            ModelTier.FAST, [Message(role="user", content="x")], parse=_parse_json)
        assert r.value == {"ok": 1}
        assert r.tier_used == "fast"
        assert r.escalated is False

    def test_escalates_to_mid_on_parse_failure(self):
        def factory(messages, model="", **kw):
            return "坏json{{{" if "haiku" in model else '{"ok": 2}'
        gw = _gw(factory=factory)
        r = gw.generate_validated(
            ModelTier.FAST, [Message(role="user", content="x")], parse=_parse_json)
        assert r.value == {"ok": 2}
        assert r.tier_used == "mid"
        assert r.escalated is True

    def test_all_tiers_fail_returns_none(self):
        gw = _gw(factory=lambda messages, model="", **kw: "全是坏json")
        r = gw.generate_validated(
            ModelTier.FAST, [Message(role="user", content="x")], parse=_parse_json)
        assert r.value is None
        assert r.tier_used == "strong"   # 升到 max_tier 仍失败

    def test_max_tier_caps_escalation(self):
        """max_tier=FAST 时不升级（等价旧行为）。"""
        calls = []
        def factory(messages, model="", **kw):
            calls.append(model)
            return "坏json"
        gw = _gw(factory=factory)
        r = gw.generate_validated(
            ModelTier.FAST, [Message(role="user", content="x")],
            parse=_parse_json, max_tier=ModelTier.FAST)
        assert r.value is None
        assert len(calls) == 1   # 只调一次，不升级

    def test_each_call_charged_to_ledger(self):
        def factory(messages, model="", **kw):
            return "坏json" if "haiku" in model else '{"ok": 3}'
        gw = _gw(factory=factory)
        r = gw.generate_validated(
            ModelTier.FAST, [Message(role="user", content="x")], parse=_parse_json)
        # 两次调用（FAST 失败 + MID 成功）都计入 ledger
        assert gw.ledger.tokens_spent > 0
        assert r.escalated is True

    def test_mid_start_escalates_to_strong(self):
        def factory(messages, model="", **kw):
            return '{"ok": 4}' if "opus" in model else "坏json"
        gw = _gw(factory=factory)
        r = gw.generate_validated(
            ModelTier.MID, [Message(role="user", content="x")], parse=_parse_json)
        assert r.value == {"ok": 4}
        assert r.tier_used == "strong"


class TestForeshadowSettleUpgrade:
    """FAST 返畸形 JSON、MID 返合法 → 结算靠升级救活（P2#14 接入 settle）。"""

    def _conn(self):
        import sqlite3
        from novelforge.db.connection import init_db_from_conn
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        init_db_from_conn(c)
        return c

    def test_settle_recovers_via_mid(self):
        from novelforge.craft.foreshadow_settle import settle_foreshadow
        from novelforge.ids import new_id
        conn = self._conn()
        conn.execute(
            "INSERT INTO foreshadow(id, label, description, planted_chapter, state)"
            " VALUES(?,?,?,?, 'planted')",
            (new_id("fs"), "断剑来历", "十年前血案凶器", 1))
        conn.commit()
        good = json.dumps({"settlements": [], "new_hooks": []}, ensure_ascii=False)

        def factory(messages, model="", temperature=1.0):
            return "坏json{{{" if "haiku" in model else good

        gw = _gw(factory=factory)
        # tier='fast'：FAST 解析失败 → 升 MID 成功，不再 raise
        report = settle_foreshadow(gw, "fast", conn, 5, "正文。")
        assert report["mentions"] == 0   # 空 settlements，但没崩
        conn.close()
