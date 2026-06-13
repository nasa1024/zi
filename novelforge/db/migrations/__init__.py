"""Schema migration runner for NovelForge novel.db.

migrate(conn) checks current schema_version in meta_kv and applies all pending
migrations in order. Each migration is idempotent (uses IF NOT EXISTS checks).

Usage:
    from novelforge.db.migrations import migrate, check_and_migrate
    old_v, new_v = migrate(conn)
"""
from __future__ import annotations
import sqlite3
from novelforge.db.connection import get_meta, set_meta
from novelforge import SCHEMA_VERSION

# Registry: version string → migration function
_MIGRATIONS: dict[str, callable] = {}


def register(version: str):
    def decorator(fn):
        _MIGRATIONS[version] = fn
        return fn
    return decorator


# ── 公共辅助 ──────────────────────────────────────────────────────────────────

def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def get_db_version(conn: sqlite3.Connection) -> str:
    """读取当前数据库 schema 版本（不触发迁移）。"""
    return get_meta(conn, "schema_version") or "1"


# ── 核心运行器 ────────────────────────────────────────────────────────────────

def migrate(conn: sqlite3.Connection) -> tuple[str, str]:
    """Apply all pending migrations.

    Returns:
        (old_version, new_version) — same string means nothing was applied.
    """
    # 延迟导入，确保所有版本模块已注册
    from . import v3, v4, v5, v6, v7, v8, v9, v10, v11, v12, v13, v14  # noqa: F401

    current = get_meta(conn, "schema_version") or "1"
    applied = []
    # 数值序比较：字符串序到 v10 会错（"10" < "9"）
    for v, fn in sorted(_MIGRATIONS.items(), key=lambda kv: int(kv[0])):
        if int(v) > int(current):
            try:
                fn(conn)
                set_meta(conn, "schema_version", v)
                conn.commit()
                applied.append(v)
            except Exception as exc:
                conn.rollback()
                raise RuntimeError(f"迁移 →v{v} 失败: {exc}") from exc
    new_version = get_meta(conn, "schema_version") or current
    return current, new_version


def check_and_migrate(conn: sqlite3.Connection) -> None:
    """启动期门控：如需迁移则迁移，打印日志，不中断启动。"""
    import sys
    current = get_meta(conn, "schema_version") or "1"
    if current == SCHEMA_VERSION:
        return
    try:
        old, new = migrate(conn)
        if old != new:
            print(f"[NovelForge] DB 迁移 {old} → {new}", file=sys.stderr)
    except Exception as exc:
        print(f"[NovelForge] 警告：DB 迁移失败（{exc}）", file=sys.stderr)
