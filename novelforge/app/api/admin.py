"""管理端点：备份 + FTS 重建（Group 12 / AUDIT F3/F5）。

F3: 备份单元 = db + l0/ + config（原来只有 novel.db）
F5: jieba 词典变更后触发增量 add_word + 可按需全量重建 FTS5
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..deps import ProjectRegistry, get_registry

router = APIRouter(tags=["admin"])


# ── 数据模型 ──────────────────────────────────────────────────────────────────

class BackupResponse(BaseModel):
    backup_id: str
    db_backup_path: str
    l0_backup_path: Optional[str] = None
    db_size_bytes: int
    l0_files_copied: int
    timestamp: str


class RebuildFtsResponse(BaseModel):
    indexed_facts: int
    tokenizer_version: str


# ── 备份端点 ──────────────────────────────────────────────────────────────────

@router.post("/{project_id}/admin/backup", response_model=BackupResponse)
def create_backup(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    """创建项目完整备份：novel.db（热备） + l0/ 目录（F3）。

    备份存储到 data/{project_id}/backups/{timestamp}/ 下。
    """
    pm = registry.get(project_id)
    if pm is None:
        raise HTTPException(404, f"项目不存在: {project_id}")

    db_path = Path(pm.db_path)
    project_dir = db_path.parent
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = project_dir / "backups" / ts
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 1. 热备 novel.db（使用 SQLite .backup API）
    db_backup_path = backup_dir / "novel.db"
    conn = registry.open_conn(project_id)
    try:
        with sqlite3.connect(str(db_backup_path)) as bck:
            conn.backup(bck)
    finally:
        conn.close()

    db_size = db_backup_path.stat().st_size

    # 2. 复制 l0/ 目录
    l0_src = project_dir / "l0"
    l0_files_copied = 0
    l0_backup_path: Optional[str] = None
    if l0_src.exists():
        l0_dst = backup_dir / "l0"
        shutil.copytree(str(l0_src), str(l0_dst))
        l0_files_copied = sum(1 for _ in l0_dst.iterdir() if _.is_file())
        l0_backup_path = str(l0_dst)

    return BackupResponse(
        backup_id=ts,
        db_backup_path=str(db_backup_path),
        l0_backup_path=l0_backup_path,
        db_size_bytes=db_size,
        l0_files_copied=l0_files_copied,
        timestamp=ts,
    )


# ── FTS 重建 + jieba 词典更新端点 ──────────────────────────────────────────────

@router.post("/{project_id}/admin/rebuild_fts", response_model=RebuildFtsResponse)
def rebuild_fts(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    """全量重建 facts_fts 索引 + 刷新 jieba 词典（F5）。

    触发场景：批量添加实体 / 升级 jieba 版本 / 迁移后词典漂移。
    返回索引行数和当前 tokenizer_version。
    """
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")

    conn = registry.open_conn(project_id)
    try:
        from ...db.connection import rebuild_facts_fts
        from ...tokenizer import tokenizer_version
        from ...db.connection import set_meta

        n = rebuild_facts_fts(conn)
        tv = tokenizer_version()
        set_meta(conn, "tokenizer_version", tv)
        conn.commit()
        return RebuildFtsResponse(indexed_facts=n, tokenizer_version=tv)
    finally:
        conn.close()


# ── 增量 jieba 词典热更新（轻量版）─────────────────────────────────────────────

class AddTermsRequest(BaseModel):
    terms: list[str]


class AddTermsResponse(BaseModel):
    added: int
    tokenizer_version: str


@router.post("/{project_id}/admin/add_terms", response_model=AddTermsResponse)
def add_user_terms(
    project_id: str,
    req: AddTermsRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    """增量向 jieba 词典追加专名（F5 增量 add_word 止血）。

    不触发全量 FTS 重建，适合少量新实体即时生效场景。
    """
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")

    from ...tokenizer import add_user_terms as _add, tokenizer_version

    _add(req.terms)
    return AddTermsResponse(added=len(req.terms), tokenizer_version=tokenizer_version())
