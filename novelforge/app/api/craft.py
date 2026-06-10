"""工艺层辅助端点：一致性豁免（§4.5）+ 伏笔管理（§5 foreshadow）。

路由：
  POST   /{project_id}/exemptions                  → 201 ExemptionResponse
  GET    /{project_id}/exemptions                  → list[ExemptionResponse]
  DELETE /{project_id}/exemptions/{id}             → 204

  POST   /{project_id}/foreshadow                  → 201 ForeshadowResponse
  GET    /{project_id}/foreshadow                  → list[ForeshadowResponse]
  GET    /{project_id}/foreshadow/{foreshadow_id}  → ForeshadowResponse
  PATCH  /{project_id}/foreshadow/{foreshadow_id}  → ForeshadowResponse
  DELETE /{project_id}/foreshadow/{foreshadow_id}  → 204

  GET    /{project_id}/foreshadow/overdue          → list[ForeshadowResponse]（到期未兑现）
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response

from ..deps import ProjectRegistry, get_registry
from ..models import (
    ExemptionCreateRequest, ExemptionResponse,
    ForeshadowCreateRequest, ForeshadowResponse, ForeshadowUpdateRequest,
)

router = APIRouter(tags=["craft"])


# ── helpers ───────────────────────────────────────────────────────────────────

def _row_to_exemption(row) -> ExemptionResponse:
    rule_codes = None
    if row["rule_codes"]:
        try:
            rule_codes = json.loads(row["rule_codes"])
        except (json.JSONDecodeError, TypeError):
            rule_codes = [row["rule_codes"]]
    return ExemptionResponse(
        id=row["id"],
        scope=row["scope"],
        scope_ref=row["scope_ref"],
        exempt_tag=row["exempt_tag"],
        rule_codes=rule_codes,
        reason=row["reason"],
        valid_from_chapter=row["valid_from_chapter"],
        valid_to_chapter=row["valid_to_chapter"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def _row_to_foreshadow(row) -> ForeshadowResponse:
    return ForeshadowResponse(
        id=row["id"],
        label=row["label"],
        description=row["description"],
        state=row["state"],
        planted_chapter=row["planted_chapter"],
        due_chapter=row["due_chapter"],
        paid_off_chapter=row["paid_off_chapter"],
        related_entity_id=row["related_entity_id"],
        importance=row["importance"],
        updated_at=row["updated_at"],
    )


# ── Exemptions ────────────────────────────────────────────────────────────────

@router.post("/{project_id}/exemptions", status_code=201)
def create_exemption(
    project_id: str,
    req: ExemptionCreateRequest,
    registry: ProjectRegistry = Depends(get_registry),
) -> ExemptionResponse:
    """新增一条误报豁免记录，使 validator 跳过该范围内的特定规则码。"""
    conn = registry.open_conn(project_id)
    try:
        rule_codes_json = json.dumps(req.rule_codes) if req.rule_codes else None
        conn.execute(
            """INSERT INTO consistency_exemptions
               (scope, scope_ref, exempt_tag, rule_codes, reason,
                valid_from_chapter, valid_to_chapter, created_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (req.scope, req.scope_ref, req.exempt_tag, rule_codes_json,
             req.reason, req.valid_from_chapter, req.valid_to_chapter,
             req.created_by),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM consistency_exemptions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _row_to_exemption(row)
    finally:
        conn.close()


@router.get("/{project_id}/exemptions")
def list_exemptions(
    project_id: str,
    scope: str | None = None,
    registry: ProjectRegistry = Depends(get_registry),
) -> list[ExemptionResponse]:
    """列出豁免记录，可按 scope 过滤。"""
    conn = registry.open_conn(project_id)
    try:
        if scope:
            rows = conn.execute(
                "SELECT * FROM consistency_exemptions WHERE scope=? ORDER BY id",
                (scope,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM consistency_exemptions ORDER BY id"
            ).fetchall()
        return [_row_to_exemption(r) for r in rows]
    finally:
        conn.close()


@router.delete("/{project_id}/exemptions/{exemption_id}", status_code=204)
def delete_exemption(
    project_id: str,
    exemption_id: int,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        result = conn.execute(
            "DELETE FROM consistency_exemptions WHERE id=?", (exemption_id,)
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(404, f"豁免 id={exemption_id} 不存在")
        return Response(status_code=204)
    finally:
        conn.close()


# ── Foreshadow ────────────────────────────────────────────────────────────────

@router.post("/{project_id}/foreshadow", status_code=201)
def create_foreshadow(
    project_id: str,
    req: ForeshadowCreateRequest,
    registry: ProjectRegistry = Depends(get_registry),
) -> ForeshadowResponse:
    """埋下一条伏笔（planted 状态）。"""
    conn = registry.open_conn(project_id)
    try:
        fs_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO foreshadow
               (id, label, description, planted_chapter, due_chapter,
                related_entity_id, importance)
               VALUES (?,?,?,?,?,?,?)""",
            (fs_id, req.label, req.description, req.planted_chapter,
             req.due_chapter, req.related_entity_id, req.importance),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM foreshadow WHERE id=?", (fs_id,)).fetchone()
        return _row_to_foreshadow(row)
    finally:
        conn.close()


@router.get("/{project_id}/foreshadow/overdue")
def list_overdue_foreshadow(
    project_id: str,
    as_of_chapter: int = 0,
    registry: ProjectRegistry = Depends(get_registry),
) -> list[ForeshadowResponse]:
    """返回到期（due_chapter ≤ as_of_chapter）但未兑现的伏笔。"""
    conn = registry.open_conn(project_id)
    try:
        rows = conn.execute(
            """SELECT * FROM foreshadow
               WHERE state NOT IN ('paid_off','overdue')
                 AND due_chapter IS NOT NULL
                 AND due_chapter <= ?
               ORDER BY due_chapter""",
            (as_of_chapter,),
        ).fetchall()
        return [_row_to_foreshadow(r) for r in rows]
    finally:
        conn.close()


@router.get("/{project_id}/foreshadow/health")
def foreshadow_health(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    """M5-⑧ 伏笔回收健康度（inkos hookAgenda 思路）：逾期堆积全局视图。"""
    from ..chapter_suggest import next_chapter_no
    from ..models import ForeshadowHealth

    conn = registry.open_conn(project_id)
    try:
        next_ch, _ = next_chapter_no(conn, project_id)
        open_states = ("planted", "reinforced", "misled", "overdue")
        ph = ",".join("?" * len(open_states))

        open_count = conn.execute(
            f"SELECT COUNT(*) AS n FROM foreshadow WHERE state IN ({ph})", open_states
        ).fetchone()["n"]
        # 逾期 = 显式 overdue 状态 ∪ due 已过但状态未翻转的行（容错）
        overdue_rows = conn.execute(
            f"SELECT label, due_chapter FROM foreshadow"
            f" WHERE state IN ({ph}) AND due_chapter IS NOT NULL AND due_chapter<?"
            f" ORDER BY due_chapter",
            open_states + (next_ch,),
        ).fetchall()
        due_soon = conn.execute(
            f"SELECT label, due_chapter FROM foreshadow"
            f" WHERE state IN ({ph}) AND due_chapter IS NOT NULL"
            f"   AND due_chapter>=? AND due_chapter<=?"
            f" ORDER BY due_chapter LIMIT 10",
            open_states + (next_ch, next_ch + 3),
        ).fetchall()

        overdue_count = len(overdue_rows)
        status = "green" if overdue_count == 0 else ("yellow" if overdue_count <= 2 else "red")
        return ForeshadowHealth(
            open_count=open_count,
            overdue_count=overdue_count,
            oldest_overdue_chapter=overdue_rows[0]["due_chapter"] if overdue_rows else None,
            due_soon=[dict(r) for r in due_soon],
            status=status,
        )
    finally:
        conn.close()


@router.get("/{project_id}/foreshadow")
def list_foreshadow(
    project_id: str,
    state: str | None = None,
    registry: ProjectRegistry = Depends(get_registry),
) -> list[ForeshadowResponse]:
    """列出所有伏笔，可按 state 过滤。"""
    conn = registry.open_conn(project_id)
    try:
        if state:
            rows = conn.execute(
                "SELECT * FROM foreshadow WHERE state=? ORDER BY planted_chapter",
                (state,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM foreshadow ORDER BY planted_chapter"
            ).fetchall()
        return [_row_to_foreshadow(r) for r in rows]
    finally:
        conn.close()


@router.get("/{project_id}/foreshadow/{foreshadow_id}")
def get_foreshadow(
    project_id: str,
    foreshadow_id: str,
    registry: ProjectRegistry = Depends(get_registry),
) -> ForeshadowResponse:
    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT * FROM foreshadow WHERE id=?", (foreshadow_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"foreshadow_id={foreshadow_id} 不存在")
        return _row_to_foreshadow(row)
    finally:
        conn.close()


@router.patch("/{project_id}/foreshadow/{foreshadow_id}")
def update_foreshadow(
    project_id: str,
    foreshadow_id: str,
    req: ForeshadowUpdateRequest,
    registry: ProjectRegistry = Depends(get_registry),
) -> ForeshadowResponse:
    """更新伏笔状态（state / paid_off_chapter / due_chapter / importance）。"""
    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT * FROM foreshadow WHERE id=?", (foreshadow_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"foreshadow_id={foreshadow_id} 不存在")

        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        if not updates:
            return _row_to_foreshadow(row)

        updates["updated_at"] = "datetime('now')"
        set_parts = []
        vals = []
        for k, v in updates.items():
            if k == "updated_at":
                set_parts.append("updated_at=datetime('now')")
            else:
                set_parts.append(f"{k}=?")
                vals.append(v)
        vals.append(foreshadow_id)
        conn.execute(
            f"UPDATE foreshadow SET {', '.join(set_parts)} WHERE id=?", vals
        )
        conn.commit()
        row = conn.execute("SELECT * FROM foreshadow WHERE id=?", (foreshadow_id,)).fetchone()
        return _row_to_foreshadow(row)
    finally:
        conn.close()


@router.delete("/{project_id}/foreshadow/{foreshadow_id}", status_code=204)
def delete_foreshadow(
    project_id: str,
    foreshadow_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        result = conn.execute(
            "DELETE FROM foreshadow WHERE id=?", (foreshadow_id,)
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(404, f"foreshadow_id={foreshadow_id} 不存在")
        return Response(status_code=204)
    finally:
        conn.close()
