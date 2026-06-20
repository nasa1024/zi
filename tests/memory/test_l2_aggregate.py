"""L2 场景聚合测试（§06.2.2）。

split_scenes 确定性切分；aggregate_chapter_scenes 写 l2_scenes 幂等 + 可选向量索引；
run_l2_window 回填窗口内缺 L2 的章。全程 FakeProvider，无网络。
"""
from __future__ import annotations

import pytest

from novelforge.control_plane.budget import BudgetLedger
from novelforge.control_plane.llm.fake_provider import FakeProvider
from novelforge.control_plane.llm.gateway import LLMGateway
from novelforge.db.connection import connect, init_db_from_conn
from novelforge.memory.embedding import HashEmbedder
from novelforge.memory.l2_aggregate import (
    aggregate_chapter_scenes,
    pending_l2_chapters,
    run_l2_window,
    should_run_l2,
    split_scenes,
)


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db_from_conn(c)
    yield c
    c.close()


def _gw():
    return LLMGateway(FakeProvider(), BudgetLedger(max_tokens=200_000, max_usd=5.0))


def _seed_draft(conn, chapter: int):
    """在 draft_index 登记一章（run_l2_window 据此判定 pending）。"""
    conn.execute(
        "INSERT INTO draft_index(id, chapter, revision_round, file_path, sha256, status)"
        " VALUES(?,?,?,?,?, 'committed')",
        (f"d{chapter}", chapter, 0, f"/tmp/ch{chapter}.md", "x"),
    )
    conn.commit()


# ── split_scenes ───────────────────────────────────────────────────────────────

class TestSplitScenes:
    def test_empty(self):
        assert split_scenes("") == []
        assert split_scenes("   \n  ") == []

    def test_single_block(self):
        out = split_scenes("叶凡走进荒古禁地，四下寂静无声。", max_scenes=6)
        assert out == ["叶凡走进荒古禁地，四下寂静无声。"]

    def test_split_on_markers(self):
        text = "第一幕的内容写在这里，足够长来当一个场景。\n***\n第二幕在分隔线之后，同样写得足够长。"
        out = split_scenes(text, max_scenes=6)
        assert len(out) == 2
        assert "第一幕" in out[0] and "第二幕" in out[1]

    def test_caps_at_max(self):
        parts = [
            f"这是第{i}个段落，内容写得足够长足够长足够长以避免被按长度合并进相邻段落里去，"
            "凑够四十字以上让它能独立成块参与封顶逻辑的检验。"
            for i in range(10)
        ]
        text = "\n\n".join(parts)
        out = split_scenes(text, max_scenes=4)
        assert len(out) <= 4
        # 末尾溢出段落被并入最后一个场景，不丢内容
        assert "第9个段落" in out[-1]

    def test_deterministic(self):
        text = "甲段足够长足够长足够长。\n\n乙段也足够长足够长足够长。\n\n丙段同样足够长足够长。"
        assert split_scenes(text, max_scenes=3) == split_scenes(text, max_scenes=3)

    def test_merges_short_fragments(self):
        """过短片段并入相邻场景，不单独成场景。"""
        text = "短。\n\n这是一个内容足够长的正常场景段落，应当独立成块不被合并。"
        out = split_scenes(text, max_scenes=6, min_chars=20)
        assert len(out) == 1
        assert "短" in out[0] and "正常场景" in out[0]


# ── aggregate_chapter_scenes ─────────────────────────────────────────────────

class TestAggregate:
    def test_writes_scenes(self, conn):
        text = "开场写得足够长足够长足够长。\n***\n转折写得足够长足够长足够长。"
        n = aggregate_chapter_scenes(conn, _gw(), 1, text, max_scenes=6)
        assert n == 2
        rows = conn.execute(
            "SELECT chapter, scene_seq, scene_text, summary FROM l2_scenes"
            " WHERE chapter=1 ORDER BY scene_seq"
        ).fetchall()
        assert [r["scene_seq"] for r in rows] == [0, 1]
        assert all(r["summary"] for r in rows)        # 摘要非空

    def test_idempotent(self, conn):
        text = "甲场景足够长足够长。\n***\n乙场景足够长足够长。"
        aggregate_chapter_scenes(conn, _gw(), 3, text, max_scenes=6)
        aggregate_chapter_scenes(conn, _gw(), 3, text, max_scenes=6)
        n = conn.execute("SELECT COUNT(*) AS c FROM l2_scenes WHERE chapter=3").fetchone()["c"]
        assert n == 2        # 不重复

    def test_degrade_without_gateway(self, conn):
        """无 gateway（LLM 不可用）也能写场景，摘要降级为截断原文。"""
        text = "一段没有 LLM 摘要时直接截断当摘要的正文，内容足够长足够长。"
        n = aggregate_chapter_scenes(conn, None, 5, text, max_scenes=6)
        assert n == 1
        row = conn.execute("SELECT summary FROM l2_scenes WHERE chapter=5").fetchone()
        assert row["summary"]      # 非空（截断原文）

    def test_with_embedder_indexes_vectors(self, conn):
        text = "施法场景足够长足够长。\n***\n战斗场景足够长足够长。"
        emb = HashEmbedder(dim=128)
        aggregate_chapter_scenes(conn, _gw(), 2, text, max_scenes=6, embedder=emb)
        nvec = conn.execute("SELECT COUNT(*) AS c FROM scene_vec WHERE chapter=2").fetchone()["c"]
        assert nvec == 2

    def test_empty_text_no_rows(self, conn):
        assert aggregate_chapter_scenes(conn, _gw(), 9, "", max_scenes=6) == 0
        n = conn.execute("SELECT COUNT(*) AS c FROM l2_scenes WHERE chapter=9").fetchone()["c"]
        assert n == 0


# ── run_l2_window + 触发判定 ──────────────────────────────────────────────────

class TestWindow:
    def test_should_run_l2(self):
        assert should_run_l2(4, every_n=4) is True
        assert should_run_l2(8, every_n=4) is True
        assert should_run_l2(5, every_n=4) is False
        assert should_run_l2(0, every_n=4) is False

    def test_pending_excludes_aggregated(self, conn):
        for ch in (1, 2, 3):
            _seed_draft(conn, ch)
        aggregate_chapter_scenes(conn, _gw(), 2, "已聚合的一章正文足够长足够长。", max_scenes=6)
        pending = pending_l2_chapters(conn, up_to_chapter=3)
        assert pending == [1, 3]      # 2 已聚合被排除，顺序升序

    def test_run_window_processes_pending(self, conn):
        for ch in (1, 2):
            _seed_draft(conn, ch)
        texts = {1: "第一章正文足够长足够长足够长。", 2: "第二章正文足够长足够长足够长。"}
        out = run_l2_window(conn, _gw(), up_to_chapter=2,
                            draft_loader=lambda c: texts.get(c), max_scenes=6)
        assert out["processed"] == 2
        assert out["scenes"] >= 2
        n = conn.execute("SELECT COUNT(DISTINCT chapter) AS c FROM l2_scenes").fetchone()["c"]
        assert n == 2

    def test_run_window_skips_unloadable(self, conn):
        """draft_loader 返回 None 的章跳过，不报错。"""
        for ch in (1, 2):
            _seed_draft(conn, ch)
        out = run_l2_window(conn, _gw(), up_to_chapter=2,
                            draft_loader=lambda c: "正文足够长足够长足够长。" if c == 1 else None,
                            max_scenes=6)
        assert out["processed"] == 1
