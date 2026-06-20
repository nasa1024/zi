"""离线确定性 embedding（§06.4.3 向量召回的 embedding 侧）。

为什么是 hash embedding 而非神经模型：§06 把向量召回定位为"增强、非主路、可关闭"，
且系统必须能离线/无网络跑（FakeProvider 同款立场）。HashEmbedder 用特征哈希
（feature hashing / hashing trick）把文本映射到固定维向量：纯 Python、无 numpy、
无新依赖、跨进程可复现。真实本地模型（bge-m3 等）可实现同一 Embedder 协议替换进来，
不改调用方与 scene_vec 落库契约（model/version 列可追溯换模型）。
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, runtime_checkable

# CJK 汉字区间 + ASCII 词
_CJK_RE = re.compile(r"[一-鿿]")
_ASCII_RE = re.compile(r"[a-z0-9]+")


@runtime_checkable
class Embedder(Protocol):
    """embedding 后端协议。换本地模型/在线 API 只需实现这三样。"""
    dim: int
    name: str
    version: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


def _tokens(text: str) -> list[str]:
    """分词：单汉字 + 相邻汉字 bigram + ASCII 词。

    bigram 让中文相似句共享更多特征（"仙台境界" 与 "境界仙台" 共享 bi）。
    无需 jieba（未安装）；确定性、零依赖。
    """
    text = text.strip().lower()
    if not text:
        return []
    toks: list[str] = []
    # ASCII 词
    toks.extend(_ASCII_RE.findall(text))
    # 单汉字
    cjk = _CJK_RE.findall(text)
    toks.extend(cjk)
    # 汉字 bigram（按出现顺序，相邻两字）
    for i in range(len(cjk) - 1):
        toks.append(cjk[i] + cjk[i + 1])
    return toks


def _bucket_and_sign(token: str, dim: int) -> tuple[int, float]:
    """特征哈希：token → (桶下标, 符号)。

    用 hashlib（而非内置 hash()，后者对 str 每进程加盐、不可复现）。
    符号位用同一摘要的另一 bit，做 signed hashing 抵消碰撞偏置。
    """
    digest = hashlib.md5(token.encode("utf-8")).digest()
    h = int.from_bytes(digest[:8], "big")
    bucket = h % dim
    sign = 1.0 if (digest[8] & 1) else -1.0
    return bucket, sign


class HashEmbedder:
    """特征哈希 embedder。name/version 落 scene_vec，换实现时可识别旧向量。"""

    name = "hash"
    version = "v1"

    def __init__(self, dim: int = 256) -> None:
        if dim <= 0:
            raise ValueError("dim 必须为正")
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        toks = _tokens(text)
        if not toks:
            return vec
        for tok in toks:
            bucket, sign = _bucket_and_sign(tok, self.dim)
            vec[bucket] += sign          # tf 加权（重复 token 累加）
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:                  # 罕见：正负抵消为零
            return vec
        return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。任一为零向量 → 0.0。"""
    if len(a) != len(b):
        raise ValueError("向量维度不一致")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class OpenAIEmbedder:
    """OpenAI 兼容 /v1/embeddings 后端（OpenAI / DeepSeek / vLLM / 任意兼容端点）。

    维度由模型决定（首次调用后从返回向量发现）；`name="openai"`、`version=模型 id`
    落 scene_vec，换模型时旧向量（dim/model 不符）会被检索侧自动跳过。
    依赖 `openai>=1.0`（项目已用于 OpenAICompatProvider）；构造不发起网络调用。
    """

    name = "openai"

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        client=None,
    ) -> None:
        self.model = model
        self.version = model
        self._dim = 0                 # 惰性发现：首次 embed 后填充
        self._client = client         # 测试可注入；否则首次 embed 时惰性构造
        self._base_url = base_url
        self._api_key = api_key

    def _ensure_client(self):
        """惰性构造 openai client：构造 embedder 不触发 import，仅实际 embed 才需要。"""
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:  # pragma: no cover
                raise ImportError("openai 包未安装。运行: pip install openai>=1.0") from e
            self._client = OpenAI(api_key=self._api_key or "placeholder", base_url=self._base_url)
        return self._client

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._ensure_client().embeddings.create(model=self.model, input=list(texts))
        vecs = [list(d.embedding) for d in resp.data]
        if vecs and not self._dim:
            self._dim = len(vecs[0])
        return vecs


def get_default_embedder(dim: int = 256) -> Embedder:
    """默认离线 embedder（HashEmbedder）。历史入口，保留兼容。"""
    return HashEmbedder(dim=dim)


def build_embedder(cfg, *, dim: int | None = None) -> Embedder:
    """按配置构造 embedder（唯一构造入口）。

    cfg = MemoryConfig。`embedding_provider`：
    - "hash"（默认）→ 离线 HashEmbedder（dim 取参数或 cfg.embedding_dim）；
    - "openai"      → OpenAIEmbedder（维度由模型决定，dim 参数对其无效）；
    - 其它/未知     → 回退 hash（绝不因配置笔误而崩）。
    """
    provider = (getattr(cfg, "embedding_provider", "hash") or "hash").lower()
    if provider == "openai":
        return OpenAIEmbedder(
            model=getattr(cfg, "embedding_model", "text-embedding-3-small"),
            base_url=getattr(cfg, "embedding_base_url", None),
            api_key=getattr(cfg, "embedding_api_key", None),
        )
    return HashEmbedder(dim=dim or getattr(cfg, "embedding_dim", 256))
