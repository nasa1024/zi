"""项目 CRUD 端点 /v1/projects。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import ProjectRegistry, get_registry
from ..models import (
    PlotSetting, ProjectCreateRequest, ProjectResponse,
    SettingSuggestRequest, SettingSuggestResponse,
)
from ...writing_profiles import (
    WritingProfileError,
    get_project_writing_profile,
    normalize_writing_selection,
)

router = APIRouter(prefix="/projects", tags=["projects"])

_EXAMPLE_PREFIXES = ("（示例）", "[Example]")


def _strip_degraded_examples(setting: dict | None) -> dict | None:
    """Never persist deterministic no-key sentinels as real story canon."""
    if setting is None:
        return None
    cleaned = dict(setting)
    for key, value in list(cleaned.items()):
        if isinstance(value, str) and value.startswith(_EXAMPLE_PREFIXES):
            cleaned[key] = None
    return cleaned


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
    "fantasy": "Untitled Fantasy",
    "science_fiction": "Untitled Science Fiction",
    "romance": "Untitled Romance",
    "mystery": "Untitled Mystery",
    "thriller": "Untitled Thriller",
    "young_adult": "Untitled Story",
    "speculative_fiction": "Untitled Speculative Fiction",
}


def _project_name(
    name: str, genre: str, setting: dict | None, *, content_language: str = "zh-CN",
) -> str:
    """Return a usable title for both form-based and zero-text Agent starts."""
    explicit = (name or "").strip()
    if explicit:
        return explicit
    setting = setting or {}
    premise = str(setting.get("premise") or "").strip()
    if premise:
        # A concise premise-derived working title remains editable later while
        # avoiding an empty project chip in the Studio.
        compact = " ".join(premise.split()) if content_language.startswith("en") else "".join(premise.split())
        for separator in ("。", "，", ",", "；", ";", "：", ":", ".", "!", "?"):
            compact = compact.split(separator, 1)[0]
        if compact:
            return compact[:60] if content_language.startswith("en") else compact[:14]
    if content_language.startswith("en"):
        return _GENRE_AUTO_TITLES.get(genre, "Untitled Story")
    return _GENRE_AUTO_TITLES.get(genre, _GENRE_AUTO_TITLES["default"])


def _project_view(registry: ProjectRegistry, pid: str) -> dict:
    """Return project counters, setting echoes and resolved writing profile.

    回显字段统一从 meta_kv 读回，使 create / list / get 三个接口一致
    （而非只有 create 响应带回显）。
    """
    view: dict = {
        "chapter_count": 0, "canon_fact_count": 0,
        "power_system": None, "premise": None,
    }
    conn = registry.open_conn(pid)
    try:
        view["chapter_count"] = conn.execute(
            "SELECT COUNT(DISTINCT chapter) FROM draft_index"
        ).fetchone()[0] or 0
        view["canon_fact_count"] = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE status='canon'"
        ).fetchone()[0] or 0
        from ...db.connection import get_meta
        view["power_system"] = get_meta(conn, "power_system")
        view["premise"] = get_meta(conn, "premise")
        writing = get_project_writing_profile(conn)
        view.update({
            "content_language": writing["content_language"],
            "target_market": writing["target_market"],
            "publication_format": writing["publication_format"],
            "primary_genre": writing.get("primary_genre"),
            "story_tags": writing.get("story_tags") or [],
            "audience_rating": writing.get("audience_rating") or "general",
            "content_warnings": writing.get("content_warnings") or [],
            "writing_profile_id": writing["writing_profile_id"],
            "writing_profile_version": writing["writing_profile_version"],
            "writing_profile_effective_from_chapter": writing["effective_from_chapter"],
            "writing_profile_summary": {
                "id": writing["writing_profile_id"],
                "display_name": writing["display_name"],
                "version": writing["writing_profile_version"],
                "content_language": writing["content_language"],
            },
        })
    finally:
        conn.close()
    return view


def _response(registry: ProjectRegistry, pm) -> ProjectResponse:
    view = _project_view(registry, pm.project_id)
    view.setdefault("primary_genre", pm.genre)
    return ProjectResponse(
        project_id=pm.project_id, name=pm.name, genre=pm.genre,
        db_path=pm.db_path, created_at=pm.created_at, archived=pm.archived,
        **view,
    )


@router.post("", status_code=201, response_model=ProjectResponse)
def create_project(
    req: ProjectCreateRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    setting = _strip_degraded_examples(req.setting.model_dump() if req.setting else None)
    try:
        writing = normalize_writing_selection(req.model_dump())
    except WritingProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    genre = writing.get("primary_genre") or req.genre
    if req.setting and "genre" in req.setting.model_fields_set:
        if (
            writing["content_language"].startswith("en")
            and req.setting.genre != genre
        ):
            raise HTTPException(
                status_code=422,
                detail="setting.genre must match primary_genre for English projects",
            )
        if not writing["content_language"].startswith("en"):
            genre = req.setting.genre
    # 向后兼容：旧调用方只传 power_system（无 setting）时，也写进 meta_kv
    if setting is None and req.power_system:
        setting = {"power_system": req.power_system}
    pm = registry.create(
        name=_project_name(
            req.name, genre, setting, content_language=writing["content_language"],
        ),
        genre=genre,
        setting=setting,
        writing_config=writing,
    )
    return _response(registry, pm)


# 注意：静态段路由 /setup/suggest 必须声明在所有 /{project_id} 动态路由之前，
# 避免被路径参数误捕获（当前 /{project_id} 仅 GET/DELETE，但保持此顺序约定）。
@router.post("/setup/suggest", response_model=SettingSuggestResponse)
def suggest_setting(req: SettingSuggestRequest):
    """生成 N 个情节设定候选（建项目前，无 project DB）。候选不入库，瞬态返回。"""
    import sqlite3

    from ...config import NovelForgeConfig
    from ...control_plane.budget import BudgetLedger, CircuitTripped
    from ...control_plane.llm.runtime import apply_provider_env, build_configured_gateway
    from ...control_plane.skill_base import SkillContext
    from ...skills.setting_gen_skill import SettingGenSkill

    try:
        writing = normalize_writing_selection(req.model_dump())
    except WritingProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    cfg = NovelForgeConfig(project_id="setup", db_path="")
    apply_provider_env(cfg)
    degraded = False
    if cfg.provider.provider == "fake" or not cfg.provider.api_key:
        cfg.provider.provider = "fake"      # 无 key 降级（同 cold_start）
        degraded = True

    # 独立小预算：建项目前的探索操作，不共享章节预算
    ledger = BudgetLedger(max_tokens=20_000, max_usd=0.20)
    gw = build_configured_gateway(cfg, ledger=ledger)

    # 占位 conn 满足 SkillContext.conn 非 Optional 契约；SettingGenSkill 自律不读它
    placeholder_conn = sqlite3.connect(":memory:")
    try:
        ws: dict = {
            "name": req.name, "genre": req.genre, "hints": req.hints, "n": req.n,
            "locked": req.locked.model_dump() if req.locked else None,
            "locked_fields": req.locked_fields, "field": req.field, "avoid": req.avoid,
            **writing,
        }
        ws["writing_profile"] = {
            **writing,
            "profile_id": writing["writing_profile_id"],
            "profile_version": 1,
            "writing_profile_version": 1,
            "cache_key": (
                f"{writing['content_language']}:{writing['writing_profile_id']}:"
                f"1:{writing['prompt_version']}"
            ),
        }
        if not ws.get("genre"):
            ws["genre"] = writing.get("primary_genre")
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
    return [_response(registry, pm) for pm in registry.list_all()]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    pm = registry.get(project_id)
    if pm is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    return _response(registry, pm)


@router.delete("/{project_id}", status_code=204)
def archive_project(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    ok = registry.archive(project_id)
    if not ok:
        raise HTTPException(404, f"项目不存在: {project_id}")
