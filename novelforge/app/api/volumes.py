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
    ChapterCardModel, ChapterCardUpdateRequest, PlannedBeat,
    VolumeCreateRequest, VolumePlanRequest, VolumePlanResponse,
    VolumeResponse, VolumeUpdateRequest,
    VolumeWizardRequest, VolumeWizardResponse,
)

router = APIRouter(tags=["volumes"])


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_kr(raw) -> list[dict]:
    if not raw:
        return []
    try:
        import json
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _volume_row_to_resp(row) -> VolumeResponse:
    keys = row.keys() if hasattr(row, "keys") else []
    return VolumeResponse(
        id=row["id"],
        volume_no=row["volume_no"],
        title=row["title"],
        synopsis=row["synopsis"],
        start_chapter=row["start_chapter"],
        end_chapter=row["end_chapter"],
        status=row["status"],
        created_at=row["created_at"],
        objective=row["objective"] if "objective" in keys else None,
        key_results=_parse_kr(row["key_results"] if "key_results" in keys else None),
    )


def _build_kr_gateway(project_id: str, registry):
    """构建 LLM 网关供 KR 结算用（与 plan_volume 同款 provider 装配）。"""
    import os
    from ...config import NovelForgeConfig
    from ...control_plane.llm import factory as llm_factory

    cfg = NovelForgeConfig(project_id=project_id,
                           db_path=registry.get(project_id).db_path)
    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        cfg.provider.api_key = api_key
        cfg.provider.provider = os.environ.get("NOVELFORGE_PROVIDER", "deepseek")
    return llm_factory.build_gateway(cfg)


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
        import json as _json
        from ...craft.volume_kr import normalize_key_results
        kr_json = (_json.dumps(normalize_key_results(req.key_results),
                               ensure_ascii=False)
                   if req.key_results is not None else None)
        try:
            conn.execute(
                """INSERT INTO volumes(id, volume_no, title, synopsis,
                                       start_chapter, end_chapter,
                                       objective, key_results)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (vol_id, req.volume_no, req.title, req.synopsis,
                 req.start_chapter, req.end_chapter, req.objective, kr_json),
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


# ── Setting → Volume 向导（POST-v15 P2#6）────────────────────────────────────
# 注意：必须注册在 /volumes/{volume_no} 之前（wizard 不是 int，避免 422）。

_WIZARD_SYSTEM = """\
你是 NovelForge 的卷大纲策划。根据给定的整套情节设定（世界观/力量体系/主角/
主线冲突/开篇钩子），为下一卷生成卷级大纲。只输出一个 JSON 对象：

{
  "title": "卷名（不含『第N卷』前缀）",
  "synopsis": "本卷剧情概要，150-300字：起点局势、核心冲突推进、卷末落点与钩子",
  "objective": "本卷可验证目标（一句话，如『李云探索秘境并立足外门』）",
  "key_results": ["可判定的关键结果1（如『获得天道之眼』）", "关键结果2", "关键结果3"]
}

要求：synopsis 与设定的主线冲突、力量阶梯一致；objective/KR 必须可从剧情
判定兑现与否。只输出 JSON，不要其他说明。"""


def _load_plot_setting(conn) -> dict:
    """读取建项目时写入 meta_kv 的整套情节设定散件。"""
    from ...db.connection import get_meta
    keys = ("premise", "world_setting", "power_system", "protagonist",
            "main_conflict", "opening_hook", "target_audience", "tone")
    setting = {k: get_meta(conn, k) for k in keys}
    ranks = conn.execute(
        "SELECT rank_name FROM power_ranks ORDER BY rank_order").fetchall()
    setting["power_ranks"] = [r["rank_name"] for r in ranks]
    return setting


def _parse_wizard_json(text: str):
    import json as _json
    import re as _re
    m = _re.search(r"\{.*\}", text, _re.DOTALL)
    if not m:
        return None
    try:
        data = _json.loads(m.group(0))
    except _json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if not (data.get("title") and data.get("synopsis")):
        return None
    return data


@router.post("/{project_id}/volumes/wizard")
def volume_wizard(
    project_id: str,
    req: VolumeWizardRequest,
    registry: ProjectRegistry = Depends(get_registry),
) -> VolumeWizardResponse:
    """设定→卷大纲端到端向导：锁定的情节设定 → 建卷（大纲+OKR）→ 预填前几章细纲。"""
    import json as _json
    import os

    from ...config import NovelForgeConfig
    from ...control_plane.llm import factory as llm_factory
    from ...control_plane.llm.provider import Message
    from ...control_plane.llm.tiers import ModelTier
    from ...craft.volume_kr import normalize_key_results
    from ..chapter_suggest import next_chapter_no

    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    conn = registry.open_conn(project_id)
    try:
        setting = _load_plot_setting(conn)
        if not any(setting.get(k) for k in
                   ("premise", "main_conflict", "world_setting", "protagonist")):
            raise HTTPException(
                422, "项目无情节设定（meta_kv 空）——先在建项目时锁定设定，或手动建卷")

        # 卷号与章区间：接在已有卷之后
        row = conn.execute(
            "SELECT MAX(volume_no) AS mv, MAX(end_chapter) AS me FROM volumes"
        ).fetchone()
        volume_no = (row["mv"] or 0) + 1
        next_ch, _ = next_chapter_no(conn, project_id)
        start_chapter = max(next_ch, (row["me"] or 0) + 1)
        end_chapter = start_chapter + req.volume_span - 1

        # ── LLM 生成卷大纲（MID 起跑、失败升 STRONG；无 key → 确定性兜底）──
        cfg = NovelForgeConfig(project_id=project_id,
                               db_path=registry.get(project_id).db_path)
        api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            cfg.provider.api_key = api_key
            cfg.provider.provider = os.environ.get("NOVELFORGE_PROVIDER", "deepseek")
        degraded = False
        tier_used = None
        data = None
        try:
            gw = llm_factory.build_gateway(cfg)
            lines = [f"- {k}: {v}" for k, v in setting.items()
                     if v and k != "power_ranks"]
            if setting.get("power_ranks"):
                lines.append(f"- 力量阶梯(弱→强): {'→'.join(setting['power_ranks'])}")
            prev = conn.execute(
                "SELECT volume_no, title, rolling_summary FROM volumes"
                " WHERE rolling_summary IS NOT NULL ORDER BY volume_no DESC LIMIT 1"
            ).fetchone()
            user = (
                "## 情节设定\n" + "\n".join(lines)
                + (f"\n\n## 上一卷概要\n第{prev['volume_no']}卷《{prev['title']}》："
                   f"{prev['rolling_summary']}" if prev else "")
                + f"\n\n## 任务\n为第 {volume_no} 卷"
                  f"（第 {start_chapter}-{end_chapter} 章）生成卷大纲。"
                + (f"\n方向提示：{req.hints}" if req.hints else "")
            )
            result = gw.generate_validated(
                ModelTier.MID, [Message(role="user", content=user)],
                parse=_parse_wizard_json, system=_WIZARD_SYSTEM,
                max_tokens=1500, max_tier=ModelTier.STRONG)
            data = result.value
            tier_used = result.tier_used
        except Exception:
            data = None
        if data is None:
            degraded = True
            data = {
                "title": (setting.get("premise") or "启程")[:20],
                "synopsis": "；".join(
                    v for v in (setting.get("premise"),
                                setting.get("main_conflict"),
                                setting.get("opening_hook")) if v) or "（待补充）",
                "objective": setting.get("main_conflict"),
                "key_results": [],
            }

        vol_id = str(uuid.uuid4())
        kr_json = _json.dumps(
            normalize_key_results(data.get("key_results") or []), ensure_ascii=False)
        try:
            conn.execute(
                """INSERT INTO volumes(id, volume_no, title, synopsis,
                                       start_chapter, end_chapter,
                                       objective, key_results)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (vol_id, volume_no, str(data["title"])[:100], data["synopsis"],
                 start_chapter, end_chapter, data.get("objective"), kr_json),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, f"volume_no={volume_no} 冲突: {e}")
        vol_row = conn.execute("SELECT * FROM volumes WHERE id=?", (vol_id,)).fetchone()
        volume = _volume_row_to_resp(vol_row)
    finally:
        conn.close()

    # ── 复用卷规划管线预填前几章细纲（独立 conn；失败不阻断建卷结果）──────
    plan = None
    if not degraded:
        try:
            plan = plan_volume(
                project_id, volume_no,
                VolumePlanRequest(
                    from_chapter=start_chapter,
                    to_chapter=start_chapter + req.plan_chapters - 1),
                registry)
        except Exception:
            plan = None

    return VolumeWizardResponse(volume=volume, plan=plan,
                                tier_used=tier_used, degraded=degraded)


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

        # P2#13：key_results 列表 → JSON 串入库（归一补全 id/status）
        if "key_results" in updates:
            import json as _json
            from ...craft.volume_kr import normalize_key_results
            updates["key_results"] = _json.dumps(
                normalize_key_results(updates["key_results"]), ensure_ascii=False)

        set_clause = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE volumes SET {set_clause} WHERE volume_no=?",
            list(updates.values()) + [volume_no],
        )
        conn.commit()

        # P2#13：卷 status→completed 时自动结算 KR（best-effort，失败不阻断状态更新）
        if updates.get("status") == "completed":
            try:
                from ...craft.volume_kr import settle_volume_kr
                gw = _build_kr_gateway(project_id, registry)
                if gw is not None:
                    settle_volume_kr(gw, conn, volume_no)
            except Exception:
                pass

        row = conn.execute(
            "SELECT * FROM volumes WHERE volume_no=?", (volume_no,)
        ).fetchone()
        return _volume_row_to_resp(row)
    finally:
        conn.close()


@router.post("/{project_id}/volumes/{volume_no}/settle-kr")
def settle_volume_kr_endpoint(
    project_id: str,
    volume_no: int,
    registry: ProjectRegistry = Depends(get_registry),
):
    """P2#13：手动触发本卷 KR 结算（对照卷情节判每条 KR 兑现度并写回）。"""
    from ...craft.volume_kr import settle_volume_kr

    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    conn = registry.open_conn(project_id)
    try:
        if conn.execute("SELECT 1 FROM volumes WHERE volume_no=?",
                        (volume_no,)).fetchone() is None:
            raise HTTPException(404, f"volume_no={volume_no} 不存在")
        gw = _build_kr_gateway(project_id, registry)
        if gw is None:
            raise HTTPException(503, "LLM 网关不可用（缺 API key）")
        return settle_volume_kr(gw, conn, volume_no)
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


# ── Volume plan（M4-④ 批量预规划）──────────────────────────────────────────────

def _load_chapter_card(conn, chapter: int) -> ChapterCardModel | None:
    row = conn.execute(
        "SELECT chapter, title, goal, hook_text, status,"
        "       target_emotion, opening_hook_type, hook_type, expectation_score"
        " FROM chapter_cards WHERE chapter=?",
        (chapter,),
    ).fetchone()
    if row is None:
        return None
    beats = conn.execute(
        "SELECT seq, beat_type, summary, value_axis FROM beats"
        " WHERE chapter=? AND status='planned' ORDER BY seq",
        (chapter,),
    ).fetchall()
    return ChapterCardModel(
        chapter=row["chapter"], title=row["title"], goal=row["goal"],
        hook_text=row["hook_text"], status=row["status"],
        target_emotion=row["target_emotion"],
        opening_hook_type=row["opening_hook_type"],
        hook_type=row["hook_type"],
        expectation_score=row["expectation_score"],
        beats=[PlannedBeat(seq=b["seq"], beat_type=b["beat_type"],
                           summary=b["summary"], value_axis=b["value_axis"]) for b in beats],
    )


@router.post("/{project_id}/volumes/{volume_no}/plan", response_model=VolumePlanResponse)
def plan_volume(
    project_id: str,
    volume_no: int,
    req: VolumePlanRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    """按卷大纲批量生成逐章章节卡 + planned beats（单次 ≤10 章）。

    只覆盖 status='planned' 且尚无草稿的章——已 drafted/committed 的章跳过，
    保护既成事实。生成结果是 /pipeline/next「最优建议」的直接数据源。
    """
    import os
    from ...config import (
        NovelForgeConfig,
        apply_project_memory_settings,
        apply_runtime_env,
    )
    from ...control_plane.skill_base import SkillContext
    from ...control_plane.skill_registry import SkillRegistry
    from ...ids import new_id
    from ...skills import register_default_skills
    from ..chapter_suggest import next_chapter_no

    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    conn = registry.open_conn(project_id)
    try:
        project = registry.get(project_id)
        cfg = NovelForgeConfig(
            project_id=project_id,
            db_path=project.db_path if project else "novel.db",
        )
        apply_project_memory_settings(cfg, conn)
        apply_runtime_env(cfg)

        vol = conn.execute(
            "SELECT volume_no, title, synopsis, start_chapter, end_chapter, rolling_summary"
            " FROM volumes WHERE volume_no=?", (volume_no,),
        ).fetchone()
        if vol is None:
            raise HTTPException(404, f"volume_no={volume_no} 不存在")
        if vol["start_chapter"] is None:
            raise HTTPException(422, "该卷未设置 start_chapter，无法规划")

        next_ch, _ = next_chapter_no(conn, project_id)
        plan_from = req.from_chapter or max(vol["start_chapter"], next_ch)
        default_to = vol["end_chapter"] if vol["end_chapter"] is not None else plan_from + 9
        plan_to = min(req.to_chapter or default_to, plan_from + 9)   # 单次 ≤10 章防截断
        if vol["end_chapter"] is not None:
            plan_to = min(plan_to, vol["end_chapter"])
        if plan_from > plan_to:
            raise HTTPException(422, f"规划范围为空: from={plan_from} > to={plan_to}")

        # ── 组装 skill 输入 ────────────────────────────────────────────────────
        from ...memory.recall import gather_hard_context
        ent_rows = conn.execute("SELECT id FROM entities LIMIT 10").fetchall()
        pack = gather_hard_context(
            [r["id"] for r in ent_rows],
            plan_from - 1,
            conn,
            summary_window=cfg.recall.summary_window_chapters,
            enable_summaries=cfg.recall.enable_summaries,
        )
        _stable = pack.to_stable_context_str()
        fs_rows = conn.execute(
            "SELECT label, due_chapter FROM foreshadow"
            " WHERE state IN ('planted','reinforced','misled','overdue')"
            "   AND due_chapter IS NOT NULL AND due_chapter<=?"
            " ORDER BY due_chapter LIMIT 10",
            (plan_to,),
        ).fetchall()

        # ── 构建 LLM 网关并调用 skill ─────────────────────────────────────────
        from ...control_plane.llm import factory as llm_factory
        api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            cfg.provider.api_key = api_key
            cfg.provider.provider = os.environ.get("NOVELFORGE_PROVIDER", "deepseek")

        try:
            gw = llm_factory.build_gateway(cfg)
            reg = SkillRegistry()
            register_default_skills(reg)
            ctx = SkillContext(
                project_id=project_id,
                target_chapter=plan_from,
                mode="human_gate",
                as_of_chapter=plan_from - 1,
                budget=gw.ledger if gw else None,
                llm=gw,
                conn=conn,
                workspace={
                    "volume_brief": {
                        "volume_no": vol["volume_no"], "title": vol["title"],
                        "synopsis": vol["synopsis"],
                        "rolling_summary": vol["rolling_summary"],
                    },
                    "plan_from": plan_from,
                    "plan_to": plan_to,
                    "foreshadow_due": [dict(r) for r in fs_rows],
                    "stable_context": f"## 世界设定（稳定）\n{_stable}" if _stable else "",
                },
            )
            result = reg.invoke("volume_plan", ctx)
            plans: list[dict] = ctx.workspace.get("chapter_plans", [])
            error = None if result.ok else (result.error or "规划无产出")
        except Exception as e:
            plans, error = [], str(e)

        # ── 落库：只覆盖 planned，保护既成事实 ────────────────────────────────
        planned: list[ChapterCardModel] = []
        skipped: list[int] = []
        for p in plans:
            ch = p["chapter"]
            drafted = conn.execute(
                "SELECT 1 FROM draft_index WHERE chapter=? LIMIT 1", (ch,)
            ).fetchone()
            card = conn.execute(
                "SELECT status FROM chapter_cards WHERE chapter=?", (ch,)
            ).fetchone()
            if drafted or (card and card["status"] != "planned"):
                skipped.append(ch)
                continue
            conn.execute(
                "INSERT INTO chapter_cards(id, chapter, title, goal, hook_text,"
                " target_emotion, opening_hook_type, hook_type, expectation_score, status)"
                " VALUES(?,?,?,?,?,?,?,?,?,'planned')"
                " ON CONFLICT(chapter) DO UPDATE SET"
                "   title=excluded.title, goal=excluded.goal,"
                "   hook_text=excluded.hook_text,"
                "   target_emotion=excluded.target_emotion,"
                "   opening_hook_type=excluded.opening_hook_type,"
                "   hook_type=excluded.hook_type,"
                "   expectation_score=excluded.expectation_score,"
                "   status='planned'",
                (new_id("card"), ch, p.get("title"), p.get("goal"), p.get("hook_text"),
                 p.get("target_emotion"), p.get("opening_hook_type"),
                 p.get("hook_type"), p.get("expectation_score")),
            )
            conn.execute("DELETE FROM beats WHERE chapter=? AND status='planned'", (ch,))
            for b in p.get("beats", []):
                conn.execute(
                    "INSERT OR IGNORE INTO beats(id, chapter, seq, beat_type, summary, value_axis, status)"
                    " VALUES(?,?,?,?,?,?,'planned')",
                    (new_id("beat"), ch, b["seq"], b["beat_type"], b["summary"], b.get("value_axis")),
                )
            conn.commit()
            loaded = _load_chapter_card(conn, ch)
            if loaded:
                planned.append(loaded)

        return VolumePlanResponse(
            volume_no=volume_no, from_chapter=plan_from, to_chapter=plan_to,
            planned=planned, skipped=skipped, error=error,
        )
    finally:
        conn.close()


@router.get("/{project_id}/chapter-cards")
def list_chapter_cards(
    project_id: str,
    from_chapter: int = 1,
    to_chapter: int = 9999,
    registry: ProjectRegistry = Depends(get_registry),
) -> list[ChapterCardModel]:
    conn = registry.open_conn(project_id)
    try:
        rows = conn.execute(
            "SELECT chapter FROM chapter_cards WHERE chapter BETWEEN ? AND ?"
            " ORDER BY chapter",
            (from_chapter, to_chapter),
        ).fetchall()
        return [c for r in rows if (c := _load_chapter_card(conn, r["chapter"]))]
    finally:
        conn.close()


@router.patch("/{project_id}/chapter-cards/{chapter}")
def update_chapter_card(
    project_id: str,
    chapter: int,
    req: ChapterCardUpdateRequest,
    registry: ProjectRegistry = Depends(get_registry),
) -> ChapterCardModel:
    """人审/手改章节卡（human_gate 精神在规划层的延伸）。"""
    conn = registry.open_conn(project_id)
    try:
        row = conn.execute(
            "SELECT chapter FROM chapter_cards WHERE chapter=?", (chapter,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"chapter_cards 无第 {chapter} 章")
        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE chapter_cards SET {set_clause} WHERE chapter=?",
                list(updates.values()) + [chapter],
            )
            conn.commit()
        return _load_chapter_card(conn, chapter)
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


# ── Branch world state / merge（POST-v15 P1#3）────────────────────────────────

@router.get("/{project_id}/branches/{branch_id}/world-state")
def branch_world_state(
    project_id: str,
    branch_id: str,
    as_of_chapter: int = 99999,
    registry: ProjectRegistry = Depends(get_registry),
):
    """分支沙盒投影：沿分支祖先链过滤的 world state 快照。

    分支 A 的情节改动（如某人未死）只在 A 的视图里可见，不污染主线或分支 B。
    """
    from ...world.replay import branch_world_snapshot

    conn = registry.open_conn(project_id)
    try:
        if conn.execute("SELECT 1 FROM branches WHERE id=?",
                        (branch_id,)).fetchone() is None:
            raise HTTPException(404, f"branch_id={branch_id} 不存在")
        return branch_world_snapshot(conn, as_of_chapter, branch_id)
    finally:
        conn.close()


@router.get("/{project_id}/branches/{branch_id}/merge-preview")
def branch_merge_preview(
    project_id: str,
    branch_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    """并线预览：列出分支待并入 facts 与主线的确定性冲突，不写任何数据。"""
    from ...world.branch_merge import preview_branch_merge

    conn = registry.open_conn(project_id)
    try:
        result = preview_branch_merge(conn, branch_id)
        if not result.get("found"):
            raise HTTPException(404, f"branch_id={branch_id} 不存在")
        return result
    finally:
        conn.close()


@router.post("/{project_id}/branches/{branch_id}/merge", status_code=202)
def branch_merge(
    project_id: str,
    branch_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    """把分支 canon facts 降级为主线 staging 提案 + 入审队列（不直接落 canon）。

    冲突条目 op='retcon'、risk 升 high；审批走 /reviews 或 batch_approve。
    分支状态置 'merged'。
    """
    from ...world.branch_merge import merge_branch_to_mainline

    conn = registry.open_conn(project_id)
    try:
        result = merge_branch_to_mainline(conn, branch_id)
        if not result.get("merged"):
            reason = result.get("reason")
            if reason == "branch_not_found":
                raise HTTPException(404, f"branch_id={branch_id} 不存在")
            raise HTTPException(409, f"分支不可并线: {reason}")
        return result
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
