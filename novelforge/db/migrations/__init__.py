"""Schema migration runner for NovelForge novel.db.

migrate(conn) checks current schema_version in meta_kv and applies all pending
migrations in order. Each migration is idempotent (uses IF NOT EXISTS / IF NOT EXISTS).
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


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply all pending migrations. Returns list of applied version strings."""
    current = get_meta(conn, "schema_version") or "1"
    applied = []
    for v, fn in sorted(_MIGRATIONS.items()):
        if v > current:
            fn(conn)
            set_meta(conn, "schema_version", v)
            conn.commit()
            applied.append(v)
    return applied
