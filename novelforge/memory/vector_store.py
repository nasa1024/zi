"""scene_vec 向量存取 + 暴力余弦检索（§06.4.3 向量召回回退实现）。

§06 设计指向 sqlite-vec(vec0) 虚表；但它是可选加载扩展，未必安装。本实现用
v15 的 scene_vec 普通表把 embedding 以 JSON 存，召回时把候选行 embedding 取回
内存做暴力余弦——单库场景量有界（百~数千），暴力足够且零依赖。

未来快路替换：若 sqlite-vec 可用，可在 upsert/search 内改走 vec0 KNN，
scene_vec 表契约与调用方签名保持不变。
"""
from __future__ import annotations

import json
import sqlite3

from .embedding import Embedder, cosine


class SceneVectorStore:
    """L2 场景摘要的向量索引。embedder 负责文本→向量；本类只管存取与检索。"""

    def __init__(self, conn: sqlite3.Connection, embedder: Embedder) -> None:
        self._conn = conn
        self._emb = embedder

    def upsert(self, scene_id: str, chapter: int, text: str) -> None:
        """为某场景写入/更新 embedding（PK=scene_id，重复即覆盖）。"""
        vec = self._emb.embed([text])[0]
        self._conn.execute(
            "INSERT INTO scene_vec(scene_id, chapter, dim, vec_json, model, version)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(scene_id) DO UPDATE SET"
            "   chapter=excluded.chapter, dim=excluded.dim, vec_json=excluded.vec_json,"
            "   model=excluded.model, version=excluded.version,"
            "   created_at=datetime('now')",
            (scene_id, chapter, self._emb.dim, json.dumps(vec),
             self._emb.name, self._emb.version),
        )
        self._conn.commit()

    def search(self, query: str, as_of: int, limit: int = 8) -> list[tuple[str, float]]:
        """返回 [(scene_id, cosine_score), ...]，按分数降序，最多 limit 条。

        只检索 chapter<=as_of 的场景（防剧透，与硬召回 as-of 语义一致）。
        维度不匹配的旧向量（换过 embedder）跳过，不污染结果。
        """
        qvec = self._emb.embed([query])[0]
        rows = self._conn.execute(
            "SELECT scene_id, dim, vec_json FROM scene_vec WHERE chapter<=?",
            (as_of,),
        ).fetchall()
        scored: list[tuple[str, float]] = []
        for r in rows:
            if r["dim"] != self._emb.dim:
                continue
            try:
                vec = json.loads(r["vec_json"])
            except Exception:
                continue
            scored.append((r["scene_id"], cosine(qvec, vec)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]
