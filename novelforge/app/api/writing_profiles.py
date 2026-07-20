"""Read and append project writing-profile versions."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..deps import ProjectConn
from ..models import WritingProfilePatchRequest
from ...writing_profiles import (
    WritingProfileConflict,
    WritingProfileError,
    get_project_writing_profile,
    list_project_profile_history,
    update_project_writing_profile,
)


router = APIRouter(tags=["writing-profile"])


def _envelope(conn) -> dict:
    return {
        "current": get_project_writing_profile(conn),
        "history": list_project_profile_history(conn),
    }


@router.get("/{project_id}/writing-profile")
def get_writing_profile(project_id: str, conn: ProjectConn):
    return _envelope(conn)


@router.patch("/{project_id}/writing-profile")
def patch_writing_profile(
    project_id: str,
    req: WritingProfilePatchRequest,
    conn: ProjectConn,
):
    values = req.model_dump(exclude_unset=True)
    effective = values.pop("effective_from_chapter", None)
    confirm = bool(values.pop("confirm_high_risk", False))
    reason = values.pop("change_reason", None)
    actor = values.pop("changed_by", "user")
    try:
        update_project_writing_profile(
            conn,
            values,
            changed_by=actor,
            change_reason=reason,
            confirm_high_risk=confirm,
            effective_from_chapter=effective,
        )
    except WritingProfileConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc), "params": exc.params},
        ) from exc
    except WritingProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _envelope(conn)
