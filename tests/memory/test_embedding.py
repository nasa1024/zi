"""离线 HashEmbedder 测试（§06.4.3 向量召回的 embedding 侧）。

纯 Python、无 numpy、无网络、无新依赖：分词→hash 分桶→tf 加权→L2 归一。
"""
from __future__ import annotations

import math

from novelforge.memory.embedding import HashEmbedder, cosine, get_default_embedder


class TestHashEmbedder:
    def test_dim_and_unit_norm(self):
        """输出维度 = dim，且非空文本向量为单位向量（L2 范数≈1）。"""
        emb = HashEmbedder(dim=64)
        [v] = emb.embed(["叶凡在荒古禁地觉醒了不死之身"])
        assert len(v) == 64
        norm = math.sqrt(sum(x * x for x in v))
        assert abs(norm - 1.0) < 1e-6

    def test_deterministic(self):
        """同一文本两次 embed 完全一致（确定性，跨进程可复现）。"""
        emb = HashEmbedder(dim=128)
        a = emb.embed(["重复的句子用于确定性校验"])[0]
        b = emb.embed(["重复的句子用于确定性校验"])[0]
        assert a == b

    def test_empty_text_zero_vector(self):
        """空文本 → 全零向量（不报错，cosine 退化为 0）。"""
        emb = HashEmbedder(dim=32)
        [v] = emb.embed([""])
        assert len(v) == 32
        assert all(x == 0.0 for x in v)

    def test_similar_text_closer_than_unrelated(self):
        """相似文本余弦相似度 > 不相关文本（向量召回的根本前提）。"""
        emb = HashEmbedder(dim=512)
        base = emb.embed(["叶凡修炼五行经突破到仙台境界"])[0]
        similar = emb.embed(["叶凡突破仙台境界，五行经大成"])[0]
        unrelated = emb.embed(["都市白领周末去咖啡馆看电影"])[0]
        assert cosine(base, similar) > cosine(base, unrelated)

    def test_batch_embed(self):
        """批量 embed 返回与输入等长的列表。"""
        emb = HashEmbedder(dim=32)
        out = emb.embed(["甲", "乙", "丙"])
        assert len(out) == 3
        assert all(len(v) == 32 for v in out)

    def test_default_embedder_has_name_and_version(self):
        """get_default_embedder 返回带 name/version 标识的 embedder（落库可追溯）。"""
        emb = get_default_embedder(dim=128)
        assert emb.dim == 128
        assert isinstance(emb.name, str) and emb.name
        assert isinstance(emb.version, str) and emb.version


class TestCosine:
    def test_identical_is_one(self):
        v = HashEmbedder(dim=64).embed(["一样的向量"])[0]
        assert abs(cosine(v, v) - 1.0) < 1e-6

    def test_zero_vector_is_zero(self):
        assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_orthogonal_is_zero(self):
        assert abs(cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9
