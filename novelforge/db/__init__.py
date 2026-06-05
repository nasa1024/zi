"""数据库层：连接初始化、建库、PRAGMA、FTS 重建。"""
from .connection import connect, init_db, rebuild_facts_fts, set_meta, get_meta

__all__ = ["connect", "init_db", "rebuild_facts_fts", "set_meta", "get_meta"]
