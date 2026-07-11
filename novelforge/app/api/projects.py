"""项目 CRUD 端点 /v1/projects。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import ProjectRegistry, get_registry
from ..models import (
    PlotSetting, ProjectCreateRequest, ProjectResponse,
    SettingSuggestRequest, SettingSuggestResponse,
)

router = APIRouter(prefix="/projects", tags=["projects"])


_GENRE_AUTO_TITLES = {
    "xuanhuan": "万界初章",
    "xianxia": "问道长生",
    "dushi": "城市异闻录",
    "kehuan": "群星之外",
    "lishi": "山河新局",
    "wuxia": "江湖未远",
    "yanqing": "与你相逢",
    "xuanyi": "迷雾来信",
    "default": "未完待续",
}


def _project_name(name: str, genre: str, setting: dict | None) -> str:
    """Return a usable title for both form-based and zero-text Agent starts."""
    explicit = (name or "").strip()
    if explicit:
        return explicit
    setting = setting or {}
    premise = str(setting.get("premise") or "").strip()
    if premise:
        # A concise premise-derived working title remains editable later while
        # avoiding an empty project chip in the Studio.
        compact = "".join(premise.split())
        for separator in ("。", "，", ",", "；", ";", "：", ":"):
            compact = compact.split(separator, 1)[0]
        if compact:
            return compact[:14]
    return _GENRE_AUTO_TITLES.get(genre, _GENRE_AUTO_TITLES["default"])


def _project_view(registry: ProjectRegistry, pid: str):
    """返回 (chapter_count, canon_fact_count, power_system, premise)。

    回显字段统一从 meta_kv 读回，使 create / list / get 三个接口一致
    （而非只有 create 响应带回显）。
    """
    chapters = facts = 0
    power_system = premise = None
    try:
        conn = registry.open_conn(pid)
        try:
            chapters = conn.execute(
                "SELECT COUNT(DISTINCT chapter) FROM draft_index"
            ).fetchone()[0] or 0
            facts = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE status='canon'"
            ).fetchone()[0] or 0
            from ...db.connection import get_meta
            power_system = get_meta(conn, "power_system")
            premise = get_meta(conn, "premise")
        finally:
            conn.close()
    except Exception:
        pass
    return chapters, facts, power_system, premise


@router.post("", status_code=201, response_model=ProjectResponse)
def create_project(
    req: ProjectCreateRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    setting = req.setting.model_dump() if req.setting else None
    genre = req.genre
    if req.setting and "genre" in req.setting.model_fields_set:
        genre = req.setting.genre
    # 向后兼容：旧调用方只传 power_system（无 setting）时，也写进 meta_kv
    if setting is None and req.power_system:
        setting = {"power_system": req.power_system}
    pm = registry.create(
        name=_project_name(req.name, genre, setting),
        genre=genre,
        setting=setting,
    )
    chapters, facts, power_system, premise = _project_view(registry, pm.project_id)
    return ProjectResponse(
        project_id=pm.project_id, name=pm.name, genre=pm.genre,
        db_path=pm.db_path, created_at=pm.created_at,
        chapter_count=chapters, canon_fact_count=facts,
        power_system=power_system, premise=premise,
    )


# 注意：静态段路由 /setup/suggest 必须声明在所有 /{project_id} 动态路由之前，
# 避免被路径参数误捕获（当前 /{project_id} 仅 GET/DELETE，但保持此顺序约定）。
@router.post("/setup/suggest", response_model=SettingSuggestResponse)
def suggest_setting(req: SettingSuggestRequest):
    """生成 N 个情节设定候选（建项目前，无 project DB）。候选不入库，瞬态返回。"""
    import os
    import sqlite3

    from ...config import NovelForgeConfig
    from ...control_plane.budget import BudgetLedger, CircuitTripped
    from ...control_plane.llm.factory import build_gateway
    from ...control_plane.skill_base import SkillContext
    from ...skills.setting_gen_skill import SettingGenSkill

    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    cfg = NovelForgeConfig(project_id="setup", db_path="")
    degraded = False
    if api_key:
        cfg.provider.api_key = api_key
        cfg.provider.provider = os.environ.get("NOVELFORGE_PROVIDER", "deepseek")
    else:
        cfg.provider.provider = "fake"      # 无 key 降级（同 cold_start）
        degraded = True

    # 独立小预算：建项目前的探索操作，不共享章节预算
    ledger = BudgetLedger(max_tokens=20_000, max_usd=0.20)
    gw = build_gateway(cfg, ledger=ledger)

    # 占位 conn 满足 SkillContext.conn 非 Optional 契约；SettingGenSkill 自律不读它
    placeholder_conn = sqlite3.connect(":memory:")
    try:
        ws: dict = {
            "name": req.name, "genre": req.genre, "hints": req.hints, "n": req.n,
            "locked": req.locked.model_dump() if req.locked else None,
            "locked_fields": req.locked_fields, "field": req.field, "avoid": req.avoid,
        }
        ctx = SkillContext(
            project_id="setup", target_chapter=0, mode="human_gate",
            as_of_chapter=0, budget=ledger, llm=gw, conn=placeholder_conn, workspace=ws,
        )
        try:
            SettingGenSkill().run(ctx)
        except CircuitTripped:
            raise HTTPException(429, "预算/熔断触发，请稍后再试")
        candidates = ws.get("candidates", [])
        return SettingSuggestResponse(
            candidates=[PlotSetting(**c) for c in candidates],
            degraded=degraded or bool(ws.get("degraded")),
            tier_used=ws.get("tier_used"),
        )
    finally:
        placeholder_conn.close()


@router.get("", response_model=list[ProjectResponse])
def list_projects(registry: ProjectRegistry = Depends(get_registry)):
    out = []
    for pm in registry.list_all():
        chapters, facts, power_system, premise = _project_view(registry, pm.project_id)
        out.append(ProjectResponse(
            project_id=pm.project_id, name=pm.name, genre=pm.genre,
            db_path=pm.db_path, created_at=pm.created_at,
            chapter_count=chapters, canon_fact_count=facts,
            power_system=power_system, premise=premise,
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
    chapters, facts, power_system, premise = _project_view(registry, pm.project_id)
    return ProjectResponse(
        project_id=pm.project_id, name=pm.name, genre=pm.genre,
        db_path=pm.db_path, created_at=pm.created_at,
        chapter_count=chapters, canon_fact_count=facts,
        archived=pm.archived,
        power_system=power_system, premise=premise,
    )


@router.delete("/{project_id}", status_code=204)
def archive_project(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    ok = registry.archive(project_id)
    if not ok:
        raise HTTPException(404, f"项目不存在: {project_id}")
