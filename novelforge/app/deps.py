"""FastAPI 依赖注入：project_id → novel.db 连接 + 项目注册表。"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Path as FPath

from ..config import NovelForgeConfig
from ..db.connection import init_db_from_conn
from ..ids import new_id


_DATA_ROOT = Path(os.environ.get("NOVELFORGE_DATA", "data"))
_REGISTRY_FILE = _DATA_ROOT / "projects.json"


@dataclass
class ProjectMeta:
    project_id: str
    name: str
    genre: str
    db_path: str
    created_at: str
    archived: bool = False


class ProjectRegistry:
    def __init__(self) -> None:
        self._projects: dict[str, ProjectMeta] = {}
        self._load()

    def _load(self) -> None:
        if _REGISTRY_FILE.exists():
            data = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
            for d in data.get("projects", []):
                pm = ProjectMeta(**d)
                self._projects[pm.project_id] = pm

    def _save(self) -> None:
        _DATA_ROOT.mkdir(parents=True, exist_ok=True)
        _REGISTRY_FILE.write_text(
            json.dumps(
                {"projects": [vars(p) for p in self._projects.values()]},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    def create(self, name: str, genre: str = "xuanhuan") -> ProjectMeta:
        pid = new_id("prj")
        db_dir = _DATA_ROOT / pid
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = str(db_dir / "novel.db")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        init_db_from_conn(conn)
        conn.close()

        pm = ProjectMeta(
            project_id=pid, name=name, genre=genre,
            db_path=db_path, created_at=datetime.utcnow().isoformat(),
        )
        self._projects[pid] = pm
        self._save()
        return pm

    def get(self, project_id: str) -> Optional[ProjectMeta]:
        return self._projects.get(project_id)

    def list_all(self) -> list[ProjectMeta]:
        return [p for p in self._projects.values() if not p.archived]

    def archive(self, project_id: str) -> bool:
        pm = self._projects.get(project_id)
        if not pm:
            return False
        pm.archived = True
        self._save()
        return True

    def open_conn(self, project_id: str) -> sqlite3.Connection:
        pm = self.get(project_id)
        if pm is None:
            raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
        conn = sqlite3.connect(pm.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


# 全局单例，由 lifespan 初始化
_registry: Optional[ProjectRegistry] = None


def get_registry() -> ProjectRegistry:
    global _registry
    if _registry is None:
        _registry = ProjectRegistry()
    return _registry


def project_conn(
    project_id: str = FPath(..., description="项目 ID"),
    registry: ProjectRegistry = Depends(get_registry),
):
    """FastAPI 依赖：自动关闭 conn 的生成器。"""
    conn = registry.open_conn(project_id)
    try:
        yield conn
    finally:
        conn.close()


ProjectConn = Annotated[sqlite3.Connection, Depends(project_conn)]
