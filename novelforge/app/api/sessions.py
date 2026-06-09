"""会话/turn 模型 + SSE 流式端点（§13.2）。

sessions：一段连续工作的审计载体（CLI / Web / Chat 等）
turns：会话内每次交互，可同步（200 JSON）或异步（SSE event-stream）
turn_events：SSE 事件持久化，支持 Last-Event-ID 断线续传
"""
from __future__ import annotations

import json
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..deps import ProjectRegistry, get_registry
from ..models import (
    SessionCreateRequest, SessionEndRequest, SessionResponse,
    TurnCreateRequest, TurnEventItem, TurnResponse,
)
from ...ids import new_id

router = APIRouter(tags=["sessions"])


# ── 辅助 ──────────────────────────────────────────────────────────────────────

def _row_to_session(row) -> SessionResponse:
    return SessionResponse(
        session_id=row["id"],
        client=row["client"],
        mode=row["mode"],
        actor=row["actor"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        budget_spent_tokens=row["budget_spent_tokens"] or 0,
        budget_spent_usd=row["budget_spent_usd"] or 0.0,
    )


def _row_to_turn(row) -> TurnResponse:
    result = None
    if row["result_json"]:
        try:
            result = json.loads(row["result_json"])
        except Exception:
            result = {"raw": row["result_json"]}
    return TurnResponse(
        turn_id=row["id"],
        session_id=row["session_id"],
        seq=row["seq"],
        kind=row["kind"],
        intent=row["intent"],
        routed_endpoint=row["routed_endpoint"],
        status=row["status"],
        stream=bool(row["stream"]),
        result=result,
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _append_turn_event(conn, turn_id: str, event_type: str, data: dict) -> int:
    cursor = conn.execute(
        "INSERT INTO turn_events(turn_id, event_type, data_json)"
        " VALUES(?,?,?)",
        (turn_id, event_type, json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()
    return cursor.lastrowid


# ── Sessions CRUD ─────────────────────────────────────────────────────────────

@router.post("/{project_id}/sessions", response_model=SessionResponse, status_code=201)
def create_session(
    project_id: str,
    req: SessionCreateRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    sid = new_id("sess")
    conn = registry.open_conn(project_id)
    try:
        conn.execute(
            "INSERT INTO sessions(id, client, mode, actor) VALUES(?,?,?,?)",
            (sid, req.client, req.mode, req.actor),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return _row_to_session(row)
    finally:
        conn.close()


@router.get("/{project_id}/sessions/{session_id}", response_model=SessionResponse)
def get_session(
    project_id: str,
    session_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    conn = registry.open_conn(project_id)
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        return _row_to_session(row)
    finally:
        conn.close()


@router.get("/{project_id}/sessions", response_model=list[SessionResponse])
def list_sessions(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    conn = registry.open_conn(project_id)
    try:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 50"
        ).fetchall()
        return [_row_to_session(r) for r in rows]
    finally:
        conn.close()


@router.post("/{project_id}/sessions/{session_id}/end", response_model=SessionResponse)
def end_session(
    project_id: str,
    session_id: str,
    req: SessionEndRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    conn = registry.open_conn(project_id)
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        conn.execute(
            "UPDATE sessions SET ended_at=datetime('now'), summary=? WHERE id=?",
            (req.summary, session_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return _row_to_session(row)
    finally:
        conn.close()


# ── Turns ──────────────────────────────────────────────────────────────────────

@router.post(
    "/{project_id}/sessions/{session_id}/turns",
    status_code=201,
)
def create_turn(
    project_id: str,
    session_id: str,
    req: TurnCreateRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    """创建 turn。stream=False 同步执行；stream=True 返回 turn_id 供 SSE 端点订阅。"""
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    conn = registry.open_conn(project_id)
    try:
        sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if sess is None:
            raise HTTPException(404, f"会话不存在: {session_id}")

        # 分配序号
        row = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM turns WHERE session_id=?",
            (session_id,),
        ).fetchone()
        seq = row["next_seq"]

        turn_id = new_id("turn")
        request_json = json.dumps(req.payload, ensure_ascii=False)
        conn.execute(
            "INSERT INTO turns(id, session_id, seq, kind, intent, request_json, stream)"
            " VALUES(?,?,?,?,?,?,?)",
            (turn_id, session_id, seq, req.kind, req.intent, request_json, int(req.stream)),
        )
        conn.commit()

        if req.stream:
            # 异步模式：立即返回 turn_id，客户端用 /stream 端点订阅
            return {"turn_id": turn_id, "session_id": session_id, "seq": seq, "stream": True}

        # 同步模式：路由到目标端点逻辑（简化：将 payload 原样返回）
        routed_ep = req.intent or "passthrough"
        result = {"payload_echo": req.payload, "note": "同步 turn 已记录"}
        conn.execute(
            "UPDATE turns SET status='done', routed_endpoint=?, result_json=?, finished_at=datetime('now')"
            " WHERE id=?",
            (routed_ep, json.dumps(result, ensure_ascii=False), turn_id),
        )
        # 写入事件
        _append_turn_event(conn, turn_id, "result", result)
        conn.commit()

        row = conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        return _row_to_turn(row)

    finally:
        conn.close()


@router.get("/{project_id}/sessions/{session_id}/turns")
def list_turns(
    project_id: str,
    session_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    conn = registry.open_conn(project_id)
    try:
        rows = conn.execute(
            "SELECT * FROM turns WHERE session_id=? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [_row_to_turn(r) for r in rows]
    finally:
        conn.close()


# ── SSE 流式（断线续传）────────────────────────────────────────────────────────

@router.get("/{project_id}/sessions/{session_id}/turns/{turn_id}/stream")
async def turn_stream(
    project_id: str,
    session_id: str,
    turn_id: str,
    request: Request,
    last_event_id: Optional[int] = None,
    registry: ProjectRegistry = Depends(get_registry),
):
    """SSE 流：返回 turn 已持久化的 turn_events 列表，支持 Last-Event-ID 断线续传。

    Content-Type: text/event-stream
    客户端连接后立即获得该 turn 目前所有事件；后续事件暂不推送（polling 模式）。
    """
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")

    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT * FROM turns WHERE id=? AND session_id=?",
            (turn_id, session_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"turn 不存在: {turn_id}")

        # 从 Last-Event-ID 之后补发
        since_id = last_event_id or 0
        events = conn.execute(
            "SELECT id, event_type, data_json, created_at"
            " FROM turn_events WHERE turn_id=? AND id>? ORDER BY id",
            (turn_id, since_id),
        ).fetchall()
        event_list = [
            {"id": r["id"], "event_type": r["event_type"],
             "data": json.loads(r["data_json"]), "created_at": r["created_at"]}
            for r in events
        ]
    finally:
        conn.close()

    async def _gen() -> AsyncGenerator[str, None]:
        for evt in event_list:
            eid = evt["id"]
            etype = evt["event_type"]
            data = json.dumps(evt["data"], ensure_ascii=False)
            yield f"id: {eid}\nevent: {etype}\ndata: {data}\n\n"
        # 流结束信号
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Turn events 查询（REST 版，用于调试）──────────────────────────────────────

@router.get(
    "/{project_id}/sessions/{session_id}/turns/{turn_id}/events",
    response_model=list[TurnEventItem],
)
def get_turn_events(
    project_id: str,
    session_id: str,
    turn_id: str,
    since_id: int = 0,
    registry: ProjectRegistry = Depends(get_registry),
):
    """返回 turn_events（REST 形式，用于调试；SSE 版用 /stream）。"""
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    conn = registry.open_conn(project_id)
    try:
        rows = conn.execute(
            "SELECT id, event_type, data_json, created_at"
            " FROM turn_events WHERE turn_id=? AND id>? ORDER BY id",
            (turn_id, since_id),
        ).fetchall()
        return [
            TurnEventItem(
                id=r["id"],
                event_type=r["event_type"],
                data=json.loads(r["data_json"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]
    finally:
        conn.close()
