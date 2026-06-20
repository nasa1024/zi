"""scene_vec 向量存取 + 暴力余弦检索测试（§06.4.3）。

:memory: SQLite，纯 Python，无 numpy/网络。
"""
from __future__ import annotations

import pytest

from novelforge.db.connection import connect, init_db_from_conn
from novelforge.memory.embedding import HashEmbedder
from novelforge.memory.vector_store import SceneVectorStore


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db_from_conn(c)
    yield c
    c.close()


def _seed_scene(conn, scene_id: str, chapter: int, seq: int, text: str):
    conn.execute(
        "INSERT INTO l2_scenes(id, chapter, scene_seq, scene_text, summary)"
        " VALUES(?,?,?,?,?)",
        (scene_id, chapter, seq, text, text),
    )
    conn.commit()


class TestSceneVectorStore:
    def test_upsert_and_count(self, conn):
        emb = HashEmbedder(dim=128)
        store = SceneVectorStore(conn, emb)
        _seed_scene(conn, "l2_a", 1, 0, "叶凡突破仙台境界")
        store.upsert("l2_a", 1, "叶凡突破仙台境界")
        row = conn.execute("SELECT scene_id, chapter, dim, model, version FROM scene_vec").fetchone()
        assert row["scene_id"] == "l2_a"
        assert row["chapter"] == 1
        assert row["dim"] == 128
        assert row["model"] == "hash" and row["version"] == "v1"

    def test_upsert_idempotent(self, conn):
        """同 scene_id 重复 upsert 不产生重复行（PK 冲突走更新）。"""
        emb = HashEmbedder(dim=64)
        store = SceneVectorStore(conn, emb)
        _seed_scene(conn, "l2_a", 1, 0, "原文")
        store.upsert("l2_a", 1, "原文")
        store.upsert("l2_a", 1, "改了的原文")
        n = conn.execute("SELECT COUNT(*) AS c FROM scene_vec").fetchone()["c"]
        assert n == 1

    def test_search_returns_most_similar_first(self, conn):
        emb = HashEmbedder(dim=512)
        store = SceneVectorStore(conn, emb)
        _seed_scene(conn, "l2_fire", 1, 0, "叶凡施展焚天火焰击败妖兽")
        _seed_scene(conn, "l2_city", 2, 0, "周末都市白领在咖啡馆闲聊")
        store.upsert("l2_fire", 1, "叶凡施展焚天火焰击败妖兽")
        store.upsert("l2_city", 2, "周末都市白领在咖啡馆闲聊")

        hits = store.search("叶凡用火焰焚天之力", as_of=99, limit=2)
        assert hits[0][0] == "l2_fire"      # (scene_id, score)
        assert hits[0][1] >= hits[-1][1]    # 分数降序

    def test_search_respects_as_of(self, conn):
        """只召回 chapter<=as_of 的场景（防剧透，与硬召回一致语义）。"""
        emb = HashEmbedder(dim=256)
        store = SceneVectorStore(conn, emb)
        _seed_scene(conn, "l2_early", 1, 0, "主角觉醒血脉")
        _seed_scene(conn, "l2_late", 10, 0, "主角觉醒血脉")
        store.upsert("l2_early", 1, "主角觉醒血脉")
        store.upsert("l2_late", 10, "主角觉醒血脉")

        hits = store.search("血脉觉醒", as_of=5, limit=10)
        ids = [h[0] for h in hits]
        assert "l2_early" in ids
        assert "l2_late" not in ids

    def test_search_limit(self, conn):
        emb = HashEmbedder(dim=128)
        store = SceneVectorStore(conn, emb)
        for i in range(5):
            sid = f"l2_{i}"
            _seed_scene(conn, sid, i + 1, 0, f"场景内容编号{i}")
            store.upsert(sid, i + 1, f"场景内容编号{i}")
        hits = store.search("场景内容", as_of=99, limit=3)
        assert len(hits) == 3

    def test_search_empty_store(self, conn):
        emb = HashEmbedder(dim=64)
        store = SceneVectorStore(conn, emb)
        assert store.search("任意查询", as_of=99, limit=5) == []
