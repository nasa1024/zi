"""FastAPI 依赖注入：project_id → novel.db 连接 + 项目注册表。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
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
        self._migrated_projects: set[str] = set()
        self._migration_lock = threading.Lock()
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

    def create(
        self, name: str, genre: str = "xuanhuan",
        setting: Optional[dict] = None,
    ) -> ProjectMeta:
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
        if setting:
            self._write_setting(conn, setting)   # 必须在 close 之前
        conn.close()

        pm = ProjectMeta(
            project_id=pid, name=name, genre=genre,
            db_path=db_path, created_at=datetime.utcnow().isoformat(),
        )
        self._projects[pid] = pm
        self._migrated_projects.add(pid)
        self._save()
        return pm

    # 散件设定字段 → meta_kv（genre 单一真相源在 ProjectMeta，不在此双写）
    _SETTING_META_KEYS = (
        "premise", "world_setting", "power_system", "protagonist",
        "main_conflict", "opening_hook", "target_audience", "tone",
    )

    @staticmethod
    def _write_setting(conn: sqlite3.Connection, setting: dict) -> None:
        """整套情节设定落库：文本散件 → meta_kv；有序力量阶梯 → power_ranks 表。"""
        from ..db.connection import set_meta

        for k in ProjectRegistry._SETTING_META_KEYS:
            v = setting.get(k)
            if v:
                set_meta(conn, k, str(v))

        # 力量阶梯：弱→强写入 power_ranks 表（项目创建期的阶梯注册，非 canon 治理）。
        # rank_order 从 1 起按列表顺序递增；越大越强（与 validators/power.py 约定一致）。
        ranks = setting.get("power_ranks") or []
        system_name = (setting.get("power_system") or "").strip()
        if ranks and system_name:
            seen: set[str] = set()
            order = 0
            for rank_name in ranks:
                rn = str(rank_name).strip()
                if not rn or rn in seen:   # 跳过空 / 重复，满足 UNIQUE(system_name,rank_name)
                    continue
                seen.add(rn)
                order += 1
                conn.execute(
                    "INSERT INTO power_ranks(id, system_name, rank_name, rank_order)"
                    " VALUES(?,?,?,?)",
                    (new_id("prk"), system_name, rn, order),
                )
        conn.commit()

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
        conn.execute("PRAGMA busy_timeout=5000")
        # Projects created by an earlier app version must migrate on first
        # access. Registry connections previously bypassed db.init_db(); cache
        # only after the connection proves it reached the current schema.
        if project_id not in self._migrated_projects:
            with self._migration_lock:
                if project_id not in self._migrated_projects:
                    from .. import SCHEMA_VERSION
                    from ..db.migrations import check_and_migrate, get_db_version
                    try:
                        check_and_migrate(conn)
                        actual = get_db_version(conn)
                        if actual != SCHEMA_VERSION:
                            raise RuntimeError(
                                f"项目 {project_id} schema 版本未就绪："
                                f"当前 {actual}，期望 {SCHEMA_VERSION}"
                            )
                    except Exception:
                        conn.close()
                        raise
                    self._migrated_projects.add(project_id)
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
