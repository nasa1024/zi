"""项目 CRUD 端点 /v1/projects。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import ProjectRegistry, get_registry
from ..models import ProjectCreateRequest, ProjectResponse

router = APIRouter(prefix="/projects", tags=["projects"])


def _project_stats(registry: ProjectRegistry, pid: str) -> tuple[int, int]:
    """返回 (chapter_count, canon_fact_count)，打开临时连接读取。"""
    try:
        conn = registry.open_conn(pid)
        chapters = conn.execute(
            "SELECT COUNT(DISTINCT chapter) FROM draft_index"
        ).fetchone()[0] or 0
        facts = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE status='canon'"
        ).fetchone()[0] or 0
        conn.close()
        return chapters, facts
    except Exception:
        return 0, 0


@router.post("", status_code=201, response_model=ProjectResponse)
def create_project(
    req: ProjectCreateRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    pm = registry.create(name=req.name, genre=req.genre)
    chapters, facts = _project_stats(registry, pm.project_id)
    return ProjectResponse(
        project_id=pm.project_id, name=pm.name, genre=pm.genre,
        db_path=pm.db_path, created_at=pm.created_at,
        chapter_count=chapters, canon_fact_count=facts,
    )


@router.get("", response_model=list[ProjectResponse])
def list_projects(registry: ProjectRegistry = Depends(get_registry)):
    out = []
    for pm in registry.list_all():
        chapters, facts = _project_stats(registry, pm.project_id)
        out.append(ProjectResponse(
            project_id=pm.project_id, name=pm.name, genre=pm.genre,
            db_path=pm.db_path, created_at=pm.created_at,
            chapter_count=chapters, canon_fact_count=facts,
        ))
    return out


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    pm = registry.get(project_id)
    if pm is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    chapters, facts = _project_stats(registry, pm.project_id)
    return ProjectResponse(
        project_id=pm.project_id, name=pm.name, genre=pm.genre,
        db_path=pm.db_path, created_at=pm.created_at,
        chapter_count=chapters, canon_fact_count=facts,
        archived=pm.archived,
    )


@router.delete("/{project_id}", status_code=204)
def archive_project(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    ok = registry.archive(project_id)
    if not ok:
        raise HTTPException(404, f"项目不存在: {project_id}")
