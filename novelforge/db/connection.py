"""SQLite 连接初始化、建库、PRAGMA、FTS 重建（设计 §2.1 / §2.8）。

单一真相源 = novel.db。facts_fts 是可重建派生索引：从 status='canon' 的 facts
逐行 jieba(或回退)预分词后重灌。tokenizer_version 入 meta_kv 以支持按版本重建。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import SCHEMA_VERSION
from ..tokenizer import tokenize, tokenizer_version, add_user_terms

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开连接并应用 §2.1 PRAGMA（每次开库执行）。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")     # 内存库会回落为 memory，无害
    conn.execute("PRAGMA synchronous  = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA temp_store  = MEMORY")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """建库：执行 schema.sql，写入版本元数据，返回连接。"""
    conn = connect(db_path)
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    _load_user_terms(conn)
    set_meta(conn, "schema_version", SCHEMA_VERSION)
    set_meta(conn, "tokenizer_version", tokenizer_version())
    conn.commit()
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta_kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta_kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _load_user_terms(conn: sqlite3.Connection) -> None:
    """把专名（实体规范名/别名/境界名）灌入分词器词典（§2.8 关键工程点3）。"""
    terms: list[str] = []
    for tbl, col in (
        ("entities", "canonical_name"),
        ("entity_aliases", "alias"),
        ("power_ranks", "rank_name"),
    ):
        try:
            terms += [r[0] for r in conn.execute(f"SELECT {col} FROM {tbl}")]
        except sqlite3.Error:
            pass
    add_user_terms(terms)


def rebuild_facts_fts(conn: sqlite3.Connection) -> int:
    """从 status='canon' 的 facts 重建 facts_fts（派生索引，可丢可重放）。返回索引行数。"""
    _load_user_terms(conn)  # 词典随实体增长，重建时刷新
    conn.execute("DELETE FROM facts_fts")
    rows = conn.execute(
        "SELECT id, subject, predicate, object, COALESCE(detail_json,'') AS detail "
        "FROM facts WHERE status='canon'"
    ).fetchall()
    n = 0
    for r in rows:
        conn.execute(
            "INSERT INTO facts_fts(fact_id, subject_tok, predicate_tok, object_tok, detail_tok) "
            "VALUES(?, ?, ?, ?, ?)",
            (r["id"], tokenize(r["subject"]), tokenize(r["predicate"]),
             tokenize(r["object"]), tokenize(r["detail"])),
        )
        n += 1
    conn.commit()
    return n
