"""Migration v10 → v11: pipeline_run 加 tokens_spent + usd_spent（逐章成本入库，质量趋势看板成本曲线）。"""
from __future__ import annotations
import sqlite3
from . import register, column_exists


@register("11")
def migrate_v11(conn: sqlite3.Connection) -> None:
    if not column_exists(conn, "pipeline_run", "tokens_spent"):
        conn.execute("ALTER TABLE pipeline_run ADD COLUMN tokens_spent INTEGER")
    if not column_exists(conn, "pipeline_run", "usd_spent"):
        conn.execute("ALTER TABLE pipeline_run ADD COLUMN usd_spent REAL")
