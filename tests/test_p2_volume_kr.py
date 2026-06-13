"""P2#13：卷级 Objective + KR 结算（settle_volume_kr）。

设计：docs/superpowers/specs/2026-06-13-p2-objective-kr-tier-upgrade-design.md
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from novelforge.control_plane.budget import BudgetLedger
from novelforge.control_plane.llm.fake_provider import FakeProvider
from novelforge.control_plane.llm.gateway import LLMGateway


@pytest.fixture
def conn():
    from novelforge.db.connection import init_db_from_conn
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db_from_conn(c)
    yield c
    c.close()


def _seed_volume(conn, *, objective="主角揭穿幕后黑手并夺回家族基业",
                 key_results=None, with_summary=True):
    krs = key_results if key_results is not None else [
        {"id": "kr1", "text": "揭穿幕后黑手身份", "status": "pending", "evidence": ""},
        {"id": "kr2", "text": "夺回家族产业", "status": "pending", "evidence": ""},
    ]
    conn.execute(
        "INSERT INTO volumes(id, volume_no, title, objective, key_results,"
        " rolling_summary, start_chapter, end_chapter, status)"
        " VALUES('v1', 1, '风云卷', ?, ?, ?, 1, 10, 'writing')",
        (objective, json.dumps(krs, ensure_ascii=False),
         "主角历经磨难，于第8章当众揭穿黑手即三长老，并在第10章夺回祖宅。"
         if with_summary else None))
    conn.commit()


def _gw(verdict: dict):
    def factory(messages, model="", temperature=1.0):
        return json.dumps(verdict, ensure_ascii=False)
    return LLMGateway(FakeProvider(factory=factory),
                      BudgetLedger(max_tokens=1_000_000, max_usd=10.0))


class TestSettleVolumeKr:
    def test_met_and_missed_written_back(self, conn):
        from novelforge.craft.volume_kr import settle_volume_kr
        _seed_volume(conn)
        gw = _gw({"results": [
            {"id": "kr1", "status": "met", "evidence": "第8章当众揭穿三长老"},
            {"id": "kr2", "status": "missed", "evidence": ""},
        ]})
        report = settle_volume_kr(gw, conn, 1)
        assert report["settled"] is True
        assert report["met"] == 1 and report["missed"] == 1
        row = conn.execute("SELECT key_results FROM volumes WHERE volume_no=1").fetchone()
        krs = {k["id"]: k for k in json.loads(row["key_results"])}
        assert krs["kr1"]["status"] == "met"
        assert "三长老" in krs["kr1"]["evidence"]
        assert krs["kr2"]["status"] == "missed"

    def test_met_without_evidence_downgraded_to_partial(self, conn):
        """LLM 判 met 但无证据 → 降 partial（防虚报，同伏笔防假回收）。"""
        from novelforge.craft.volume_kr import settle_volume_kr
        _seed_volume(conn)
        gw = _gw({"results": [
            {"id": "kr1", "status": "met", "evidence": ""},
            {"id": "kr2", "status": "partial", "evidence": "部分夺回"},
        ]})
        report = settle_volume_kr(gw, conn, 1)
        krs = {k["id"]: k for k in json.loads(
            conn.execute("SELECT key_results FROM volumes WHERE volume_no=1")
            .fetchone()["key_results"])}
        assert krs["kr1"]["status"] == "partial"   # met→partial
        assert report["partial"] == 2

    def test_illegal_status_stays_pending(self, conn):
        from novelforge.craft.volume_kr import settle_volume_kr
        _seed_volume(conn)
        gw = _gw({"results": [
            {"id": "kr1", "status": "完成啦", "evidence": "x"},
            {"id": "kr2", "status": "met", "evidence": "夺回祖宅"},
        ]})
        settle_volume_kr(gw, conn, 1)
        krs = {k["id"]: k for k in json.loads(
            conn.execute("SELECT key_results FROM volumes WHERE volume_no=1")
            .fetchone()["key_results"])}
        assert krs["kr1"]["status"] == "pending"   # 非法 → 不动

    def test_no_objective_not_settled(self, conn):
        from novelforge.craft.volume_kr import settle_volume_kr
        _seed_volume(conn, objective=None)
        gw = _gw({"results": []})
        report = settle_volume_kr(gw, conn, 1)
        assert report["settled"] is False
        assert "objective" in report["reason"]

    def test_no_key_results_not_settled(self, conn):
        from novelforge.craft.volume_kr import settle_volume_kr
        _seed_volume(conn, key_results=[])
        gw = _gw({"results": []})
        report = settle_volume_kr(gw, conn, 1)
        assert report["settled"] is False

    def test_unknown_volume_not_settled(self, conn):
        from novelforge.craft.volume_kr import settle_volume_kr
        gw = _gw({"results": []})
        report = settle_volume_kr(gw, conn, 99)
        assert report["settled"] is False

    def test_malformed_json_escalates_then_settles(self, conn):
        """FAST 返畸形、MID 返合法 → 靠 generate_validated 升级救活。"""
        from novelforge.craft.volume_kr import settle_volume_kr
        _seed_volume(conn)
        good = json.dumps({"results": [
            {"id": "kr1", "status": "met", "evidence": "揭穿三长老"},
            {"id": "kr2", "status": "met", "evidence": "夺回祖宅"}]},
            ensure_ascii=False)

        def factory(messages, model="", temperature=1.0):
            return "坏json{{{" if "haiku" in model else good
        gw = LLMGateway(FakeProvider(factory=factory),
                        BudgetLedger(max_tokens=1_000_000, max_usd=10.0))
        report = settle_volume_kr(gw, conn, 1, tier="fast")
        assert report["settled"] is True and report["met"] == 2

    def test_all_malformed_returns_unsettled(self, conn):
        from novelforge.craft.volume_kr import settle_volume_kr
        _seed_volume(conn)
        gw = LLMGateway(
            FakeProvider(factory=lambda messages, model="", temperature=1.0: "坏json"),
            BudgetLedger(max_tokens=1_000_000, max_usd=10.0))
        report = settle_volume_kr(gw, conn, 1, tier="fast")
        assert report["settled"] is False
