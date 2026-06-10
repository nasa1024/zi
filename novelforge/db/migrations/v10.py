"""Migration v9 → v10: pipeline_run 加 detail_json + quality_score（M3-① 候选报告 / M5-⑦ 质量分）。"""
from __future__ import annotations
import sqlite3
from . import register, column_exists


@register("10")
def migrate_v10(conn: sqlite3.Connection) -> None:
    if not column_exists(conn, "pipeline_run", "detail_json"):
        conn.execute("ALTER TABLE pipeline_run ADD COLUMN detail_json TEXT")
    if not column_exists(conn, "pipeline_run", "quality_score"):
        conn.execute("ALTER TABLE pipeline_run ADD COLUMN quality_score REAL")
