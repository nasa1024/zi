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
  GET    /{project_id}/foreshadow/dormant          → list[ForeshadowDormantResponse]（v22 休眠承诺）
  GET    /{project_id}/foreshadow/health           → ForeshadowHealth
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..deps import ProjectRegistry, get_registry
from ..models import (
    ExemptionCreateRequest, ExemptionResponse,
    ForeshadowCreateRequest, ForeshadowDormantResponse, ForeshadowResponse,
    ForeshadowUpdateRequest,
    StyleAnchorCreate, StyleAnchorResponse, StyleAnchorUpdate,
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


def _foreshadow_fields(row) -> dict:
    keys = set(row.keys())

    def col(name: str, default=None):
        return row[name] if name in keys and row[name] is not None else default

    return dict(
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
        last_mentioned_chapter=row["last_mentioned_chapter"],
        advance_count=row["advance_count"],
        last_advanced_chapter=row["last_advanced_chapter"],
        origin=row["origin"],
        # v22/F1：老库无这些列时按 schema 默认值回填
        promise_kind=col("promise_kind"),
        line_type=col("line_type", "long"),
        visibility=col("visibility", "reader"),
        parent_id=col("parent_id"),
        remind_every=col("remind_every"),
    )


def _row_to_foreshadow(row) -> ForeshadowResponse:
    return ForeshadowResponse(**_foreshadow_fields(row))


def _validate_parent(conn, foreshadow_id: str | None, parent_id: str) -> None:
    """v22/F1 嵌套校验：父承诺必须存在、不能是自身、不能成环。"""
    if foreshadow_id is not None and parent_id == foreshadow_id:
        raise HTTPException(422, "parent_id 不能指向自身")
    if conn.execute("SELECT 1 FROM foreshadow WHERE id=?", (parent_id,)).fetchone() is None:
        raise HTTPException(422, f"父承诺 parent_id={parent_id} 不存在")
    if foreshadow_id is None:
        return
    seen: set[str] = set()
    cursor: str | None = parent_id
    while cursor and cursor not in seen:
        if cursor == foreshadow_id:
            raise HTTPException(422, "parent_id 会形成嵌套环")
        seen.add(cursor)
        row = conn.execute("SELECT parent_id FROM foreshadow WHERE id=?", (cursor,)).fetchone()
        cursor = row["parent_id"] if row else None


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
    from ...craft.promise_ledger import normalize_promise_kind

    conn = registry.open_conn(project_id)
    try:
        if req.parent_id:
            _validate_parent(conn, None, req.parent_id)
        fs_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO foreshadow
               (id, label, description, planted_chapter, due_chapter,
                related_entity_id, importance,
                promise_kind, line_type, visibility, parent_id, remind_every)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fs_id, req.label, req.description, req.planted_chapter,
             req.due_chapter, req.related_entity_id, req.importance,
             normalize_promise_kind(req.promise_kind), req.line_type, req.visibility,
             req.parent_id or None, req.remind_every),
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


@router.get("/{project_id}/foreshadow/dormant")
def list_dormant_foreshadow(
    project_id: str,
    as_of_chapter: int | None = Query(None, ge=1),
    registry: ProjectRegistry = Depends(get_registry),
) -> list[ForeshadowDormantResponse]:
    """v22/F1：休眠承诺——读者可见、未兑现的长/短线，超过 remind_every × dormant_factor
    章未被提及/推进（默认按下一章计算），最久休眠的在前。"""
    from ...craft.promise_ledger import dormant_promises
    from ..chapter_suggest import next_chapter_no

    conn = registry.open_conn(project_id)
    try:
        chapter = as_of_chapter or next_chapter_no(conn, project_id)[0]
        return [
            ForeshadowDormantResponse(
                **_foreshadow_fields(row),
                last_touch_chapter=row["last_touch_chapter"],
                chapters_since_touch=row["chapters_since_touch"],
                effective_remind_every=row["effective_remind_every"],
                dormant_after=row["dormant_after"],
            )
            for row in dormant_promises(conn, chapter)
        ]
    finally:
        conn.close()


@router.get("/{project_id}/foreshadow/health")
def foreshadow_health(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    """M5-⑧ 伏笔回收健康度（inkos hookAgenda 思路）：逾期堆积全局视图。"""
    from ...craft.promise_ledger import dormant_promises
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
            dormant_count=len(dormant_promises(conn, next_ch)),
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


_CLEARABLE_FORESHADOW_FIELDS = frozenset({"promise_kind", "parent_id", "remind_every"})


@router.patch("/{project_id}/foreshadow/{foreshadow_id}")
def update_foreshadow(
    project_id: str,
    foreshadow_id: str,
    req: ForeshadowUpdateRequest,
    registry: ProjectRegistry = Depends(get_registry),
) -> ForeshadowResponse:
    """更新伏笔（state / paid_off_chapter / due_chapter / importance / 承诺字段）。

    未传或传 null 的字段保持不变；v22 的 promise_kind / parent_id / remind_every
    显式传 null 表示清空。"""
    from ...craft.promise_ledger import normalize_promise_kind

    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT * FROM foreshadow WHERE id=?", (foreshadow_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"foreshadow_id={foreshadow_id} 不存在")

        updates = {
            k: v for k, v in req.model_dump(exclude_unset=True).items()
            if v is not None or k in _CLEARABLE_FORESHADOW_FIELDS
        }
        if "promise_kind" in updates:
            updates["promise_kind"] = normalize_promise_kind(updates["promise_kind"])
        if "parent_id" in updates:
            updates["parent_id"] = updates["parent_id"] or None
            if updates["parent_id"]:
                _validate_parent(conn, foreshadow_id, updates["parent_id"])
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
        # v22/F1：内层承诺脱离被删的外层（否则外键阻止删除）
        conn.execute(
            "UPDATE foreshadow SET parent_id=NULL WHERE parent_id=?", (foreshadow_id,)
        )
        result = conn.execute(
            "DELETE FROM foreshadow WHERE id=?", (foreshadow_id,)
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(404, f"foreshadow_id={foreshadow_id} 不存在")
        return Response(status_code=204)
    finally:
        conn.close()


# ── Style anchors（P1#9 文风锚点）─────────────────────────────────────────────

_RIGHTS_GATED_ANCHOR_SOURCES = {"user", "official", "licensed"}


def _require_anchor_rights(source_type: str, rights_confirmed: bool) -> None:
    if source_type in _RIGHTS_GATED_ANCHOR_SOURCES and not rights_confirmed:
        raise HTTPException(
            422,
            "user/official/licensed 文风锚点必须确认拥有可使用权利",
        )


def _row_to_anchor(row) -> StyleAnchorResponse:
    keys = set(row.keys())
    try:
        profile_tags = json.loads(row["profile_tags"]) if "profile_tags" in keys else []
    except (json.JSONDecodeError, TypeError):
        profile_tags = []
    return StyleAnchorResponse(
        id=row["id"], emotion=row["emotion"], title=row["title"],
        content=row["content"], enabled=bool(row["enabled"]),
        language=row["language"] if "language" in keys else "zh-CN",
        source_type=row["source_type"] if "source_type" in keys else "legacy",
        source_note=row["source_note"] if "source_note" in keys else None,
        rights_confirmed=(
            bool(row["rights_confirmed"]) if "rights_confirmed" in keys else True
        ),
        profile_tags=profile_tags if isinstance(profile_tags, list) else [],
        created_at=row["created_at"],
    )


@router.post("/{project_id}/style-anchors", status_code=201)
def create_style_anchor(
    project_id: str,
    req: StyleAnchorCreate,
    registry: ProjectRegistry = Depends(get_registry),
) -> StyleAnchorResponse:
    conn = registry.open_conn(project_id)
    try:
        aid = f"anchor_{uuid.uuid4().hex[:12]}"
        language = req.language
        if "language" not in req.model_fields_set:
            from ...writing_profiles import get_project_writing_profile

            language = get_project_writing_profile(conn)["content_language"]
        _require_anchor_rights(req.source_type, req.rights_confirmed)
        conn.execute(
            "INSERT INTO style_anchors("
            "id, emotion, title, content, language, source_type, source_note,"
            " rights_confirmed, profile_tags) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                aid, req.emotion, req.title, req.content, language,
                req.source_type, req.source_note, int(req.rights_confirmed),
                json.dumps(req.profile_tags, ensure_ascii=False),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM style_anchors WHERE id=?", (aid,)).fetchone()
        return _row_to_anchor(row)
    finally:
        conn.close()


@router.get("/{project_id}/style-anchors")
def list_style_anchors(
    project_id: str,
    emotion: str | None = None,
    language: str | None = None,
    registry: ProjectRegistry = Depends(get_registry),
) -> list[StyleAnchorResponse]:
    conn = registry.open_conn(project_id)
    try:
        where: list[str] = []
        params: list[str] = []
        if emotion:
            where.append("emotion=?")
            params.append(emotion)
        if language:
            if language not in {"zh-CN", "en-US", "en-GB"}:
                raise HTTPException(422, f"不支持的锚点语言: {language}")
            where.append("language=?")
            params.append(language)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            f"SELECT * FROM style_anchors{clause} ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [_row_to_anchor(r) for r in rows]
    finally:
        conn.close()


@router.patch("/{project_id}/style-anchors/{anchor_id}")
def update_style_anchor(
    project_id: str,
    anchor_id: str,
    req: StyleAnchorUpdate,
    registry: ProjectRegistry = Depends(get_registry),
) -> StyleAnchorResponse:
    conn = registry.open_conn(project_id)
    try:
        current = conn.execute(
            "SELECT * FROM style_anchors WHERE id=?", (anchor_id,),
        ).fetchone()
        if current is None:
            raise HTTPException(404, f"style_anchor 不存在: {anchor_id}")
        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        source_type = updates.get("source_type", current["source_type"])
        rights_confirmed = bool(
            updates.get("rights_confirmed", current["rights_confirmed"])
        )
        _require_anchor_rights(source_type, rights_confirmed)
        if "enabled" in updates:
            updates["enabled"] = int(updates["enabled"])
        if "rights_confirmed" in updates:
            updates["rights_confirmed"] = int(updates["rights_confirmed"])
        if "profile_tags" in updates:
            updates["profile_tags"] = json.dumps(
                updates["profile_tags"], ensure_ascii=False,
            )
        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            cur = conn.execute(
                f"UPDATE style_anchors SET {set_clause} WHERE id=?",
                list(updates.values()) + [anchor_id])
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(404, f"style_anchor 不存在: {anchor_id}")
        row = conn.execute(
            "SELECT * FROM style_anchors WHERE id=?", (anchor_id,)).fetchone()
        return _row_to_anchor(row)
    finally:
        conn.close()


@router.delete("/{project_id}/style-anchors/{anchor_id}", status_code=204)
def delete_style_anchor(
    project_id: str,
    anchor_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    conn = registry.open_conn(project_id)
    try:
        cur = conn.execute("DELETE FROM style_anchors WHERE id=?", (anchor_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, f"style_anchor 不存在: {anchor_id}")
        return Response(status_code=204)
    finally:
        conn.close()
