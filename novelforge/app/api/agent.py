"""Choice-driven Author Agent API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..agent_runtime import AgentRuntime
from ..deps import ProjectRegistry, get_registry
from ...planning.repository import DecisionConflictError
from ..models import AgentChoiceRequest, AgentSessionResponse, AgentStartRequest
from ..security import sanitize_user_text


router = APIRouter(tags=["agent"])


@router.post("/{project_id}/agent/start", response_model=AgentSessionResponse, status_code=201)
def start_agent(
    project_id: str,
    req: AgentStartRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    try:
        return AgentRuntime(registry).start(
            project_id,
            interaction_mode=req.interaction_mode,
            actor=req.actor,
            governance_mode=req.governance_mode,
            hints=(sanitize_user_text(req.hints, max_length=2000) if req.hints else None),
            planning_horizon=req.planning_horizon,
            auto_chapters=req.auto_chapters,
        )
    except Exception as exc:
        raise HTTPException(500, f"Agent 启动失败: {exc}") from exc


@router.get("/{project_id}/agent/sessions", response_model=list[AgentSessionResponse])
def list_agent_sessions(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    return AgentRuntime(registry).list_sessions(project_id)


@router.get("/{project_id}/agent/{session_id}", response_model=AgentSessionResponse)
def get_agent(
    project_id: str,
    session_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    try:
        return AgentRuntime(registry).get(project_id, session_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post(
    "/{project_id}/agent/{session_id}/choose",
    response_model=AgentSessionResponse,
)
def choose_agent_option(
    project_id: str,
    session_id: str,
    req: AgentChoiceRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    if req.action == "select" and not req.option_id:
        raise HTTPException(422, "select action requires option_id")
    try:
        return AgentRuntime(registry).resume(
            project_id,
            session_id,
            action=req.action,
            option_id=req.option_id,
            hints=(sanitize_user_text(req.hints, max_length=2000) if req.hints else None),
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except DecisionConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{project_id}/story-map")
def get_story_map(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    from ...planning.repository import story_map

    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    conn = registry.open_conn(project_id)
    try:
        return story_map(conn)
    finally:
        conn.close()
