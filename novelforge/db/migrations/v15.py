"""Migration v14 → v15: 向量召回回退存储 scene_vec（§06.4.3）。

§06 设计里 L2 场景块走 sqlite-vec（vec0 虚表）建索引；但 sqlite-vec 是可选
加载扩展，未必安装。这里建一张纯 SQLite 回退表 scene_vec：把 L2 场景摘要的
embedding 以 JSON 数组存储，召回时做暴力余弦（场景量有界，足够）。
sqlite-vec 可作未来快路替换，不改本表契约。
"""
from __future__ import annotations
import sqlite3
from . import register, table_exists

_SCENE_VEC_DDL = """
CREATE TABLE IF NOT EXISTS scene_vec (
    scene_id    TEXT PRIMARY KEY,           -- = l2_scenes.id
    chapter     INTEGER NOT NULL,           -- 冗余存章号，便于 as-of(chapter<=) 过滤
    dim         INTEGER NOT NULL,
    vec_json    TEXT NOT NULL,              -- JSON float 数组（回退暴力检索用）
    model       TEXT,
    version     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scene_vec_chapter ON scene_vec(chapter);
"""


@register("15")
def migrate_v15(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "scene_vec"):
        conn.executescript(_SCENE_VEC_DDL)
