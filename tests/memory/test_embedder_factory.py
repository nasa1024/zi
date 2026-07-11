"""第三方 embedding 接入测试：build_embedder 工厂 + OpenAIEmbedder（/v1/embeddings）。

OpenAIEmbedder 用注入的 fake client，无网络。默认 provider=hash 时行为不变。
"""
from __future__ import annotations

import pytest

from novelforge.config import MemoryConfig
from novelforge.db.connection import connect, init_db_from_conn
from novelforge.memory.embedding import (
    HashEmbedder,
    OpenAIEmbedder,
    build_embedder,
)
from novelforge.memory.vector_store import SceneVectorStore


class _FakeEmbeddings:
    """模拟 openai client.embeddings.create。"""
    def __init__(self):
        self.calls = []

    def create(self, *, model, input, **kw):
        self.calls.append({"model": model, "input": list(input), "kw": kw})
        # 维度=3 的确定性假向量（与文本长度相关）
        data = [type("D", (), {"embedding": [float(len(t)), 1.0, 2.0]})() for t in input]
        return type("R", (), {"data": data})()


class _FakeClient:
    def __init__(self):
        self.embeddings = _FakeEmbeddings()


# ── build_embedder 分发 ──────────────────────────────────────────────────────

class TestBuildEmbedder:
    def test_default_is_hash(self):
        emb = build_embedder(MemoryConfig())          # provider 默认 "hash"
        assert isinstance(emb, HashEmbedder)
        assert emb.name == "hash"

    def test_hash_honors_dim(self):
        emb = build_embedder(MemoryConfig(embedding_dim=128))
        assert isinstance(emb, HashEmbedder) and emb.dim == 128

    def test_openai_provider_returns_openai_embedder(self):
        cfg = MemoryConfig(embedding_provider="openai",
                           embedding_model="text-embedding-3-small")
        emb = build_embedder(cfg)
        assert isinstance(emb, OpenAIEmbedder)
        assert emb.name == "openai"
        assert emb.version == "text-embedding-3-small"   # 落库可追溯具体模型

    def test_unknown_provider_falls_back_to_hash(self):
        emb = build_embedder(MemoryConfig(embedding_provider="bogus"))
        assert isinstance(emb, HashEmbedder)


# ── OpenAIEmbedder 行为（fake client）────────────────────────────────────────

class TestOpenAIEmbedder:
    def test_embed_returns_vectors_and_discovers_dim(self):
        emb = OpenAIEmbedder(model="m", client=_FakeClient())
        out = emb.embed(["a", "bb"])
        assert out == [[1.0, 1.0, 2.0], [2.0, 1.0, 2.0]]
        assert emb.dim == 3            # 首次调用后发现维度

    def test_empty_input_no_call(self):
        client = _FakeClient()
        emb = OpenAIEmbedder(model="m", client=client)
        assert emb.embed([]) == []
        assert client.embeddings.calls == []

    def test_name_version_for_traceability(self):
        emb = OpenAIEmbedder(model="text-embedding-3-large", client=_FakeClient())
        assert emb.name == "openai"
        assert emb.version == "text-embedding-3-large"

    def test_passes_model_to_client(self):
        client = _FakeClient()
        emb = OpenAIEmbedder(model="bge-m3", client=client)
        emb.embed(["x"])
        assert client.embeddings.calls[0]["model"] == "bge-m3"

    def test_works_with_vector_store(self):
        conn = connect(":memory:")
        init_db_from_conn(conn)
        conn.execute(
            "INSERT INTO l2_scenes(id, chapter, scene_seq, scene_text, summary)"
            " VALUES('l2_a',1,0,'场景','场景')")
        conn.commit()
        emb = OpenAIEmbedder(model="text-embedding-3-small", client=_FakeClient())
        store = SceneVectorStore(conn, emb)
        store.upsert("l2_a", 1, "场景文本")
        row = conn.execute("SELECT model, version, dim FROM scene_vec WHERE scene_id='l2_a'").fetchone()
        assert row["model"] == "openai"
        assert row["version"] == "text-embedding-3-small"
        assert row["dim"] == 3
        conn.close()

    def test_l2_aggregate_records_discovered_dimension_on_first_scene(self):
        from novelforge.memory.l2_aggregate import aggregate_chapter_scenes

        conn = connect(":memory:")
        init_db_from_conn(conn)
        emb = OpenAIEmbedder(model="text-embedding-3-small", client=_FakeClient())
        aggregate_chapter_scenes(
            conn, None, 1, "首次聚合时维度尚未预先发现。", embedder=emb
        )
        scene = conn.execute(
            "SELECT embedding_model, embedding_version, embedding_dim"
            " FROM l2_scenes WHERE chapter=1"
        ).fetchone()
        vec = conn.execute("SELECT dim FROM scene_vec").fetchone()
        assert scene["embedding_model"] == "openai"
        assert scene["embedding_version"] == "text-embedding-3-small"
        assert scene["embedding_dim"] == vec["dim"] == 3
        conn.close()
