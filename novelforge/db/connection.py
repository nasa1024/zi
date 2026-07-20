"""SQLite 连接初始化、建库、PRAGMA、FTS 重建（设计 §2.1 / §2.8）。

单一真相源 = novel.db。facts_fts 是可重建派生索引：从 status='canon' 的 facts
逐行 jieba(或回退)预分词后重灌。tokenizer_version 入 meta_kv 以支持按版本重建。
"""
from __future__ import annotations

import json
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
    """建库或升级现有库：执行 schema.sql（新建库），或运行迁移（已有库）。"""
    db_path = Path(db_path)
    is_new = not db_path.exists() or db_path.stat().st_size == 0
    conn = connect(db_path)
    if is_new:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        language, terms = project_tokenizer_context(conn)
        set_meta(conn, "schema_version", SCHEMA_VERSION)
        set_meta(conn, "tokenizer_version", tokenizer_version(language, terms))
        conn.commit()
    else:
        from .migrations import check_and_migrate
        check_and_migrate(conn)
        # 启动期 sweep：清理崩溃写入残留（F6/F7）
        from .l0 import sweep_orphans, sweep_crashed_runs
        sweep_orphans(conn, db_path.parent / "l0")
        sweep_crashed_runs(conn)
    return conn


def init_db_from_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    """建库：对已有连接执行 schema.sql，适用于 :memory: 测试 fixture。
    调用方负责 connect() + PRAGMA 设置；本函数只执行 DDL + 元数据写入。"""
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    language, terms = project_tokenizer_context(conn)
    set_meta(conn, "schema_version", SCHEMA_VERSION)
    set_meta(conn, "tokenizer_version", tokenizer_version(language, terms))
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


def project_content_language(conn: sqlite3.Connection) -> str:
    """Return the project's language without importing writing_profiles.

    ``writing_profiles`` itself imports this module, so this small meta lookup
    is intentionally kept here to avoid an import cycle.  Missing metadata is
    the legacy Chinese project contract.
    """
    try:
        return (get_meta(conn, "content_language") or "zh-CN").strip() or "zh-CN"
    except sqlite3.Error:
        return "zh-CN"


def _load_user_terms(conn: sqlite3.Connection, language: str | None = None) -> tuple[str, ...]:
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
    try:
        custom = json.loads(get_meta(conn, "tokenizer_user_terms_json") or "[]")
        if isinstance(custom, list):
            terms.extend(str(term) for term in custom)
    except (json.JSONDecodeError, sqlite3.Error):
        pass
    # Stable, project-scoped term snapshot.  add_user_terms remains for jieba
    # compatibility, while tokenize/version below receive this explicit set.
    normalized = tuple(sorted({str(term).strip() for term in terms if str(term).strip()}))
    add_user_terms(normalized, language or project_content_language(conn))
    return normalized


def project_tokenizer_context(conn: sqlite3.Connection) -> tuple[str, tuple[str, ...]]:
    """Return ``(language, project terms)`` for indexing and query parity."""
    language = project_content_language(conn)
    return language, _load_user_terms(conn, language)


def project_tokenizer_version(conn: sqlite3.Connection) -> str:
    language, terms = project_tokenizer_context(conn)
    return tokenizer_version(language, terms)


def rebuild_facts_fts(conn: sqlite3.Connection) -> int:
    """从 status='canon' 的 facts 重建 facts_fts（派生索引，可丢可重放）。返回索引行数。"""
    language, terms = project_tokenizer_context(conn)  # 词典随实体增长，重建时刷新
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
            (r["id"], tokenize(r["subject"], language, user_terms=terms),
             tokenize(r["predicate"], language, user_terms=terms),
             tokenize(r["object"], language, user_terms=terms),
             tokenize(r["detail"], language, user_terms=terms)),
        )
        n += 1
    conn.commit()
    return n


def _iter_draft_chunks(text: str):
    """按空行切成带 1-based 行号的段落，供 L0 FTS 与 source_ref 共用。"""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start: int | None = None
    buf: list[str] = []
    for idx, line in enumerate(lines, start=1):
        if line.strip():
            if start is None:
                start = idx
            buf.append(line)
            continue
        if start is not None and buf:
            yield start, idx - 1, "\n".join(buf).strip()
        start, buf = None, []
    if start is not None and buf:
        yield start, len(lines), "\n".join(buf).strip()


def index_draft_fts(
    conn: sqlite3.Connection,
    draft_id: str,
    chapter: int,
    text: str,
) -> int:
    """把一章最新 L0 正文写入 drafts_fts；调用方控制事务。"""
    language, terms = project_tokenizer_context(conn)
    # Recall 只应看到当前修订，换稿时移除同章旧版本索引，避免过期内容混入。
    conn.execute("DELETE FROM drafts_fts WHERE chapter_no=?", (chapter,))
    count = 0
    for line_start, line_end, body in _iter_draft_chunks(text):
        tokenized = tokenize(body, language, user_terms=terms)
        if not tokenized:
            continue
        conn.execute(
            "INSERT INTO drafts_fts(draft_id, chapter_no, line_start, line_end, body)"
            " VALUES(?,?,?,?,?)",
            (draft_id, chapter, line_start, line_end, tokenized),
        )
        count += 1
    try:
        conn.execute(
            "UPDATE draft_index SET fts_indexed_at=datetime('now') WHERE id=?",
            (draft_id,),
        )
    except sqlite3.Error:
        # 兼容尚未执行 v18 的独立旧库连接。
        pass
    return count


def rebuild_drafts_fts(conn: sqlite3.Connection) -> dict[str, int]:
    """从每章最新 L0 文件全量重建 drafts_fts。"""
    from .l0 import load_draft_text_by_id

    conn.execute("DELETE FROM drafts_fts")
    try:
        conn.execute("UPDATE draft_index SET fts_indexed_at=NULL")
    except sqlite3.Error:
        pass
    rows = conn.execute(
        "SELECT di.id, di.chapter FROM draft_index di"
        " JOIN (SELECT chapter, MAX(revision_round) AS revision_round"
        "       FROM draft_index GROUP BY chapter) latest"
        "   ON latest.chapter=di.chapter AND latest.revision_round=di.revision_round"
        " ORDER BY di.chapter"
    ).fetchall()
    drafts = chunks = missing = 0
    for row in rows:
        text = load_draft_text_by_id(conn, row["id"])
        if text is None:
            missing += 1
            continue
        chunks += index_draft_fts(conn, row["id"], row["chapter"], text)
        drafts += 1
    conn.commit()
    return {"drafts": drafts, "chunks": chunks, "missing_files": missing}
