"""卷/分支管理端点（§9.4）。

路由：
  POST   /{project_id}/volumes                  → 201 VolumeResponse
  GET    /{project_id}/volumes                  → list[VolumeResponse]
  GET    /{project_id}/volumes/{volume_no}      → VolumeResponse
  PATCH  /{project_id}/volumes/{volume_no}      → VolumeResponse
  DELETE /{project_id}/volumes/{volume_no}      → 204

  POST   /{project_id}/branches                 → 201 BranchResponse
  GET    /{project_id}/branches                 → list[BranchResponse]
  GET    /{project_id}/branches/{branch_id}     → BranchResponse
  PATCH  /{project_id}/branches/{branch_id}     → BranchResponse
  DELETE /{project_id}/branches/{branch_id}     → 204
"""
from __future__ import annotations

import uuid
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Response

from ..deps import ProjectRegistry, get_registry
from ..models import (
    BranchCreateRequest, BranchResponse, BranchUpdateRequest,
    VolumeCreateRequest, VolumeResponse, VolumeUpdateRequest,
)

router = APIRouter(tags=["volumes"])


# ── helpers ───────────────────────────────────────────────────────────────────

def _volume_row_to_resp(row) -> VolumeResponse:
    return VolumeResponse(
        id=row["id"],
        volume_no=row["volume_no"],
        title=row["title"],
        synopsis=row["synopsis"],
        start_chapter=row["start_chapter"],
        end_chapter=row["end_chapter"],
        status=row["status"],
        created_at=row["created_at"],
    )


def _branch_row_to_resp(row) -> BranchResponse:
    return BranchResponse(
        id=row["id"],
        branch_name=row["branch_name"],
        fork_chapter=row["fork_chapter"],
        base_branch_id=row["base_branch_id"],
        description=row["description"],
        status=row["status"],
        created_at=row["created_at"],
    )


# ── Volumes ───────────────────────────────────────────────────────────────────

@router.post("/{project_id}/volumes", status_code=201)
def create_volume(
    project_id: str,
    req: VolumeCreateRequest,
    registry: ProjectRegistry = Depends(get_registry),
) -> VolumeResponse:
    conn = registry.open_conn(project_id)
    try:
        vol_id = str(uuid.uuid4())
        try:
            conn.execute(
                """INSERT INTO volumes(id, volume_no, title, synopsis,
                                       start_chapter, end_chapter)
                   VALUES (?,?,?,?,?,?)""",
                (vol_id, req.volume_no, req.title, req.synopsis,
                 req.start_chapter, req.end_chapter),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, f"volume_no={req.volume_no} 已存在: {e}")
        row = conn.execute("SELECT * FROM volumes WHERE id=?", (vol_id,)).fetchone()
        return _volume_row_to_resp(row)
    finally:
        conn.close()


@router.get("/{project_id}/volumes")
def list_volumes(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
) -> list[VolumeResponse]:
    conn = registry.open_conn(project_id)
    try:
        rows = conn.execute(
            "SELECT * FROM volumes ORDER BY volume_no"
        ).fetchall()
        return [_volume_row_to_resp(r) for r in rows]
    finally:
        conn.close()


@router.get("/{project_id}/volumes/{volume_no}")
def get_volume(
    project_id: str,
    volume_no: int,
    registry: ProjectRegistry = Depends(get_registry),
) -> VolumeResponse:
    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT * FROM volumes WHERE volume_no=?", (volume_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"volume_no={volume_no} 不存在")
        return _volume_row_to_resp(row)
    finally:
        conn.close()


@router.patch("/{project_id}/volumes/{volume_no}")
def update_volume(
    project_id: str,
    volume_no: int,
    req: VolumeUpdateRequest,
    registry: ProjectRegistry = Depends(get_registry),
) -> VolumeResponse:
    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT * FROM volumes WHERE volume_no=?", (volume_no,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"volume_no={volume_no} 不存在")

        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        if not updates:
            return _volume_row_to_resp(row)

        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE volumes SET {set_clause} WHERE volume_no=?",
            list(updates.values()) + [volume_no],
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM volumes WHERE volume_no=?", (volume_no,)
        ).fetchone()
        return _volume_row_to_resp(row)
    finally:
        conn.close()


@router.delete("/{project_id}/volumes/{volume_no}", status_code=204)
def delete_volume(
    project_id: str,
    volume_no: int,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        result = conn.execute(
            "DELETE FROM volumes WHERE volume_no=?", (volume_no,)
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(404, f"volume_no={volume_no} 不存在")
        return Response(status_code=204)
    finally:
        conn.close()


# ── Branches ──────────────────────────────────────────────────────────────────

@router.post("/{project_id}/branches", status_code=201)
def create_branch(
    project_id: str,
    req: BranchCreateRequest,
    registry: ProjectRegistry = Depends(get_registry),
) -> BranchResponse:
    conn = registry.open_conn(project_id)
    try:
        if req.base_branch_id:
            exists = conn.execute(
                "SELECT id FROM branches WHERE id=?", (req.base_branch_id,)
            ).fetchone()
            if not exists:
                raise HTTPException(404, f"base_branch_id={req.base_branch_id} 不存在")

        br_id = str(uuid.uuid4())
        try:
            conn.execute(
                """INSERT INTO branches(id, branch_name, fork_chapter,
                                        base_branch_id, description)
                   VALUES (?,?,?,?,?)""",
                (br_id, req.branch_name, req.fork_chapter,
                 req.base_branch_id, req.description),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, f"branch_name={req.branch_name!r} 已存在: {e}")
        row = conn.execute("SELECT * FROM branches WHERE id=?", (br_id,)).fetchone()
        return _branch_row_to_resp(row)
    finally:
        conn.close()


@router.get("/{project_id}/branches")
def list_branches(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
) -> list[BranchResponse]:
    conn = registry.open_conn(project_id)
    try:
        rows = conn.execute(
            "SELECT * FROM branches ORDER BY created_at"
        ).fetchall()
        return [_branch_row_to_resp(r) for r in rows]
    finally:
        conn.close()


@router.get("/{project_id}/branches/{branch_id}")
def get_branch(
    project_id: str,
    branch_id: str,
    registry: ProjectRegistry = Depends(get_registry),
) -> BranchResponse:
    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT * FROM branches WHERE id=?", (branch_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"branch_id={branch_id} 不存在")
        return _branch_row_to_resp(row)
    finally:
        conn.close()


@router.patch("/{project_id}/branches/{branch_id}")
def update_branch(
    project_id: str,
    branch_id: str,
    req: BranchUpdateRequest,
    registry: ProjectRegistry = Depends(get_registry),
) -> BranchResponse:
    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT * FROM branches WHERE id=?", (branch_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"branch_id={branch_id} 不存在")

        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        if not updates:
            return _branch_row_to_resp(row)

        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE branches SET {set_clause} WHERE id=?",
            list(updates.values()) + [branch_id],
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM branches WHERE id=?", (branch_id,)
        ).fetchone()
        return _branch_row_to_resp(row)
    finally:
        conn.close()


@router.delete("/{project_id}/branches/{branch_id}", status_code=204)
def delete_branch(
    project_id: str,
    branch_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        result = conn.execute(
            "DELETE FROM branches WHERE id=?", (branch_id,)
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(404, f"branch_id={branch_id} 不存在")
        return Response(status_code=204)
    finally:
        conn.close()
