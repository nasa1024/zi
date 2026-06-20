"""向量召回通道 + RRF 融合测试（§06.4.3 / §06.4.5）。

向量召回为"增强、非主路、可关闭"：默认 enable_vector=False → similar_scenes 恒空，
旧行为不变。开启时与词法排名 RRF 融合（k=60，只用排名）。:memory:，无网络。
"""
from __future__ import annotations

import pytest

from novelforge.db.connection import connect, init_db_from_conn
from novelforge.memory.embedding import HashEmbedder
from novelforge.memory.recall import (
    gather_hard_context,
    rrf_fuse,
    search_similar_scenes,
)
from novelforge.memory.vector_store import SceneVectorStore


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db_from_conn(c)
    yield c
    c.close()


def _seed_scene(conn, emb, scene_id, chapter, seq, text):
    conn.execute(
        "INSERT INTO l2_scenes(id, chapter, scene_seq, scene_text, summary)"
        " VALUES(?,?,?,?,?)",
        (scene_id, chapter, seq, text, text),
    )
    SceneVectorStore(conn, emb).upsert(scene_id, chapter, text)
    conn.commit()


# ── rrf_fuse ─────────────────────────────────────────────────────────────────

class TestRRF:
    def test_empty(self):
        assert rrf_fuse([]) == []
        assert rrf_fuse([[], []]) == []

    def test_item_in_both_lists_ranks_first(self):
        # b 在两个列表都靠前 → 融合分最高
        fused = rrf_fuse([["a", "b", "c"], ["b", "x", "y"]], k=60)
        ids = [d for d, _ in fused]
        assert ids[0] == "b"

    def test_scores_descending(self):
        fused = rrf_fuse([["a", "b"], ["a", "c"]], k=60)
        scores = [s for _, s in fused]
        assert scores == sorted(scores, reverse=True)

    def test_single_list(self):
        fused = rrf_fuse([["a", "b", "c"]], k=60)
        assert [d for d, _ in fused] == ["a", "b", "c"]


# ── search_similar_scenes ────────────────────────────────────────────────────

class TestSimilarScenes:
    def test_returns_relevant_first(self, conn):
        emb = HashEmbedder(dim=512)
        _seed_scene(conn, emb, "l2_fire", 1, 0, "叶凡施展焚天火焰焚烧妖兽群")
        _seed_scene(conn, emb, "l2_talk", 2, 0, "众人围坐茶楼闲谈家长里短")
        out = search_similar_scenes(conn, emb, "焚天火焰焚烧", as_of=9, top_k=2)
        assert out and out[0]["scene_id"] == "l2_fire"
        assert "chapter" in out[0] and "summary" in out[0]

    def test_respects_as_of(self, conn):
        emb = HashEmbedder(dim=256)
        _seed_scene(conn, emb, "l2_early", 1, 0, "血脉觉醒之章")
        _seed_scene(conn, emb, "l2_late", 12, 0, "血脉觉醒之章")
        out = search_similar_scenes(conn, emb, "血脉觉醒", as_of=5, top_k=5)
        ids = [o["scene_id"] for o in out]
        assert "l2_early" in ids and "l2_late" not in ids

    def test_empty_when_no_scenes(self, conn):
        emb = HashEmbedder(dim=128)
        assert search_similar_scenes(conn, emb, "任意", as_of=9, top_k=5) == []


# ── gather_hard_context 集成 ──────────────────────────────────────────────────

class TestGatherIntegration:
    def test_disabled_by_default(self, conn):
        """不传 embedder / enable_vector=False → similar_scenes 恒空（旧行为）。"""
        pack = gather_hard_context([], as_of=9, conn=conn)
        assert pack.similar_scenes == []

    def test_enabled_populates_similar_scenes(self, conn):
        emb = HashEmbedder(dim=512)
        _seed_scene(conn, emb, "l2_fire", 1, 0, "叶凡施展焚天火焰焚烧妖兽群")
        pack = gather_hard_context(
            [], as_of=9, conn=conn,
            embedder=emb, enable_vector=True, scene_query="焚天火焰焚烧妖兽",
        )
        assert pack.similar_scenes
        assert pack.similar_scenes[0]["scene_id"] == "l2_fire"

    def test_enabled_but_no_query_stays_empty(self, conn):
        emb = HashEmbedder(dim=128)
        pack = gather_hard_context([], as_of=9, conn=conn,
                                   embedder=emb, enable_vector=True, scene_query=None)
        assert pack.similar_scenes == []

    def test_dynamic_context_renders_similar_scenes(self, conn):
        emb = HashEmbedder(dim=512)
        _seed_scene(conn, emb, "l2_fire", 1, 0, "叶凡施展焚天火焰焚烧妖兽群")
        pack = gather_hard_context(
            [], as_of=9, conn=conn,
            embedder=emb, enable_vector=True, scene_query="焚天火焰",
        )
        rendered = pack.to_dynamic_context_str()
        assert "相似" in rendered      # 相似桥段通道出现在动态层
