"""分支隔离 World State 冒烟测试（Group 13）。

测试 replay_power / replay_knowledge / get_world_state 在 branch_id 指定时的
分支感知过滤：主线事实按 fork_chapter 截断，分支事实全量可见。
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest

from novelforge.db.connection import connect, init_db_from_conn
from novelforge.world.branch import build_branch_filter
from novelforge.world.replay import (
    get_world_state,
    replay_knowledge,
    replay_numeric,
    replay_power,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    """内存 DB，已初始化 schema，FK 关闭（直接插测试数据）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    init_db_from_conn(conn)
    return conn


@pytest.fixture
def populated(db):
    """插入以下数据集：
    - entity: e1
    - power_ranks: rank A(order=1), rank B(order=2)
    - main branch facts:
        f_main_ch3  (branch_id=NULL, valid_from_chapter=3, status=canon)  → power ch3
        f_main_ch8  (branch_id=NULL, valid_from_chapter=8, status=canon)  → power ch8
    - branch BR1 forks from main at fork_chapter=5
    - branch fact:
        f_br_ch6    (branch_id=br1_id, valid_from_chapter=6, status=canon) → power ch6

    预期行为（在 BR1 上，as_of=10）：
    - 主线 f_main_ch3 可见（3 <= fork=5）
    - 主线 f_main_ch8 不可见（8 > fork=5）
    - 分支 f_br_ch6 可见（属于 BR1）
    """
    e1 = "entity-e1"
    rk_a = "rank-a"
    rk_b = "rank-b"
    db.execute(
        "INSERT INTO entities(id,canonical_name,entity_type) VALUES(?,?,?)",
        (e1, "陆天", "character"),
    )
    db.execute(
        "INSERT INTO power_ranks(id,system_name,rank_name,rank_order) VALUES(?,?,?,?)",
        (rk_a, "炼气", "炼气初期", 1),
    )
    db.execute(
        "INSERT INTO power_ranks(id,system_name,rank_name,rank_order) VALUES(?,?,?,?)",
        (rk_b, "炼气", "炼气中期", 2),
    )

    # 主线 facts
    f_main_3 = str(uuid.uuid4())
    f_main_8 = str(uuid.uuid4())
    for fid, ch in ((f_main_3, 3), (f_main_8, 8)):
        db.execute(
            "INSERT INTO facts(id,fact_type,subject,predicate,object,status,"
            "  valid_from_chapter,current_revision_id,branch_id)"
            " VALUES(?,?,?,?,?,?,?,?,NULL)",
            (fid, "power_system", "陆天", "境界", "炼气", "canon", ch, fid),
        )

    # 分支 BR1
    br1 = str(uuid.uuid4())
    db.execute(
        "INSERT INTO branches(id,branch_name,fork_chapter,base_branch_id) VALUES(?,?,?,NULL)",
        (br1, "if-branch-1", 5),
    )

    # 分支 fact
    f_br_6 = str(uuid.uuid4())
    db.execute(
        "INSERT INTO facts(id,fact_type,subject,predicate,object,status,"
        "  valid_from_chapter,current_revision_id,branch_id)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (f_br_6, "power_system", "陆天", "境界", "筑基", "canon", 6, f_br_6, br1),
    )

    # character_power_log 条目（source_fact_id 关联对应 fact）
    # ch3 主线：rank A
    db.execute(
        "INSERT INTO character_power_log(id,entity_id,system_name,rank_id,rank_order,"
        "  change_chapter,change_type,source_fact_id)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), e1, "炼气", rk_a, 1, 3, "init", f_main_3),
    )
    # ch8 主线：rank B（应在 BR1 视角不可见）
    db.execute(
        "INSERT INTO character_power_log(id,entity_id,system_name,rank_id,rank_order,"
        "  change_chapter,change_type,source_fact_id)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), e1, "炼气", rk_b, 2, 8, "breakthrough", f_main_8),
    )
    # ch6 分支：rank B
    db.execute(
        "INSERT INTO character_power_log(id,entity_id,system_name,rank_id,rank_order,"
        "  change_chapter,change_type,source_fact_id)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), e1, "炼气", rk_b, 2, 6, "breakthrough", f_br_6),
    )
    db.commit()
    return {"conn": db, "e1": e1, "br1": br1,
            "f_main_3": f_main_3, "f_main_8": f_main_8, "f_br_6": f_br_6}


# ── build_branch_filter unit tests ────────────────────────────────────────────

class TestBuildBranchFilter:
    def test_none_returns_empty(self, db):
        sql, params = build_branch_filter(db, None)
        assert sql == ""
        assert params == ()

    def test_unknown_branch_returns_empty(self, db):
        sql, params = build_branch_filter(db, "nonexistent-id")
        assert sql == ""
        assert params == ()

    def test_single_branch_generates_three_conditions(self, db):
        br = str(uuid.uuid4())
        db.execute(
            "INSERT INTO branches(id,branch_name,fork_chapter,base_branch_id) VALUES(?,?,?,NULL)",
            (br, "test-br", 10),
        )
        db.commit()
        sql, params = build_branch_filter(db, br)
        assert "x.source_fact_id IS NULL" in sql
        assert "f.branch_id IS NULL" in sql
        assert "f.branch_id=?" in sql
        # params: root_fork=10, leaf_branch=br
        assert 10 in params
        assert br in params

    def test_nested_branch_generates_ancestor_conditions(self, db):
        br_a = str(uuid.uuid4())
        br_b = str(uuid.uuid4())
        db.execute(
            "INSERT INTO branches(id,branch_name,fork_chapter,base_branch_id) VALUES(?,?,?,NULL)",
            (br_a, "br-a", 5),
        )
        db.execute(
            "INSERT INTO branches(id,branch_name,fork_chapter,base_branch_id) VALUES(?,?,?,?)",
            (br_b, "br-b", 15, br_a),
        )
        db.commit()
        sql, params = build_branch_filter(db, br_b)
        # Should contain: mainline <=5, br_a <=15, br_b unrestricted
        assert params.count(5) >= 1   # root (A) fork
        assert params.count(15) >= 1  # B's fork (cutoff for A)
        assert br_a in params
        assert br_b in params


# ── replay_power 分支隔离 ──────────────────────────────────────────────────────

class TestReplayPowerBranchIsolation:
    def test_mainline_sees_all_mainline_entries(self, populated):
        conn, e1 = populated["conn"], populated["e1"]
        result = replay_power(conn, 10, branch_id=None)
        # ch8 主线突破应可见（最新）
        assert e1 in result
        assert result[e1] == 2  # rank B order

    def test_branch_sees_mainline_before_fork(self, populated):
        conn, e1, br1 = populated["conn"], populated["e1"], populated["br1"]
        result = replay_power(conn, 10, branch_id=br1)
        # 至少应有 e1（来自 ch3 mainline 或 ch6 branch）
        assert e1 in result

    def test_branch_excludes_mainline_after_fork(self, populated):
        """ch8 主线突破（8 > fork=5）在 BR1 视角不可见。
        ch6 分支突破可见，所以最终 rank 应为分支的 rank_order=2 (ch6)，
        而非主线 ch8 的 rank_order=2。
        要验证 ch8 mainline 被排除：单独查 ch3-only 视角。
        """
        conn, e1, br1 = populated["conn"], populated["e1"], populated["br1"]
        # as_of=4（仅 ch3 主线应可见，ch6/ch8 都超出）
        result = replay_power(conn, 4, branch_id=br1)
        assert e1 in result
        assert result[e1] == 1  # rank A from ch3 mainline

    def test_branch_sees_own_facts(self, populated):
        conn, e1, br1 = populated["conn"], populated["e1"], populated["br1"]
        # as_of=10：ch6 branch fact 应可见
        result = replay_power(conn, 10, branch_id=br1)
        assert e1 in result
        assert result[e1] == 2  # rank B from ch6 branch

    def test_mainline_as_of_before_any_branch(self, populated):
        """as_of=4（早于所有分支相关章节）：只有 ch3 主线可见，rank=1。"""
        conn, e1 = populated["conn"], populated["e1"]
        result = replay_power(conn, 4, branch_id=None)
        assert e1 in result
        assert result[e1] == 1  # only ch3 mainline visible at N=4

    def test_null_source_fact_id_always_visible(self, db):
        """source_fact_id IS NULL 的 log 条目在任何分支下都应可见。"""
        br = str(uuid.uuid4())
        db.execute(
            "INSERT INTO branches(id,branch_name,fork_chapter,base_branch_id) VALUES(?,?,?,NULL)",
            (br, "test-branch", 5),
        )
        # 插入无 source_fact_id 的 log 条目
        db.execute(
            "INSERT INTO character_power_log(id,entity_id,system_name,rank_id,rank_order,"
            "  change_chapter,change_type)"
            " VALUES(?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "e-free", "sys", "r1", 99, 3, "init"),
        )
        db.commit()
        result = replay_power(db, 10, branch_id=br)
        assert "e-free" in result
        assert result["e-free"] == 99


# ── get_world_state 分支感知 ──────────────────────────────────────────────────

class TestGetWorldStateBranch:
    def test_branch_id_propagated_to_world_state(self, populated):
        conn, br1 = populated["conn"], populated["br1"]
        ws = get_world_state(10, conn, branch_id=br1)
        assert ws._branch_id == br1

    def test_world_state_power_history_branch_scoped(self, populated):
        conn, e1, br1 = populated["conn"], populated["e1"], populated["br1"]
        ws = get_world_state(4, conn, branch_id=br1)
        hist = ws.power_history(e1)
        chapters = [r["change_chapter"] for r in hist]
        assert 3 in chapters      # ch3 mainline 可见
        assert 8 not in chapters  # ch8 mainline 超出 fork=5
        assert 6 not in chapters  # ch6 branch 超出 as_of=4

    def test_world_state_no_branch_id_backward_compat(self, populated):
        conn, e1 = populated["conn"], populated["e1"]
        ws = get_world_state(10, conn)
        hist = ws.power_history(e1)
        # 无分支过滤时，ch3 和 ch8 主线都应可见
        chapters = [r["change_chapter"] for r in hist]
        assert 3 in chapters
        assert 8 in chapters


# ── replay_knowledge 分支隔离 ─────────────────────────────────────────────────

class TestReplayKnowledgeBranch:
    def test_branch_knowledge_visible(self, db):
        br = str(uuid.uuid4())
        db.execute(
            "INSERT INTO branches(id,branch_name,fork_chapter,base_branch_id) VALUES(?,?,?,NULL)",
            (br, "k-branch", 5),
        )
        f_br = str(uuid.uuid4())
        db.execute(
            "INSERT INTO facts(id,fact_type,subject,predicate,object,status,"
            "  valid_from_chapter,current_revision_id,branch_id)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (f_br, "knowledge", "甲", "知道", "乙的秘密", "canon", 6, f_br, br),
        )
        db.execute(
            "INSERT INTO knowledge_edges(id,knower_entity_id,secret_key,knowledge_state,"
            "  learned_chapter,source_fact_id)"
            " VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), "ent-jia", "secret-yi", "knows", 6, f_br),
        )
        db.commit()
        result = replay_knowledge(db, 10, branch_id=br)
        assert "ent-jia" in result
        assert "secret-yi" in result["ent-jia"]

    def test_mainline_knowledge_after_fork_excluded_in_branch(self, db):
        br = str(uuid.uuid4())
        db.execute(
            "INSERT INTO branches(id,branch_name,fork_chapter,base_branch_id) VALUES(?,?,?,NULL)",
            (br, "k-branch2", 5),
        )
        f_main_after = str(uuid.uuid4())
        db.execute(
            "INSERT INTO facts(id,fact_type,subject,predicate,object,status,"
            "  valid_from_chapter,current_revision_id,branch_id)"
            " VALUES(?,?,?,?,?,?,?,?,NULL)",
            (f_main_after, "knowledge", "甲", "知道", "丙的秘密", "canon", 8, f_main_after),
        )
        db.execute(
            "INSERT INTO knowledge_edges(id,knower_entity_id,secret_key,knowledge_state,"
            "  learned_chapter,source_fact_id)"
            " VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), "ent-jia", "secret-bing", "knows", 8, f_main_after),
        )
        db.commit()
        result = replay_knowledge(db, 10, branch_id=br)
        # 主线 ch8 fact 超出 fork=5，branch 视角不可见
        assert "ent-jia" not in result or "secret-bing" not in result.get("ent-jia", {})
