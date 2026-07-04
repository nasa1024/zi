"""冷启动反向抽取端点（§9.4）。

POST /{project_id}/cold_start
  从已有章节正文中用 LLM 抽取 BibleChangeProposal，
  全部写入 fact_candidates（staging），永不自动 canon，由人工审核后再 promote。
"""
from __future__ import annotations

import json
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException

from ..deps import ProjectRegistry, get_registry
from ..models import (
    ColdStartImportRequest, ColdStartJobInfo, ColdStartRequest,
    ColdStartResponse, ColdStartSplitPreview,
)

router = APIRouter(tags=["cold_start"])


# ── 批量导入（POST-v15 P1#4）──────────────────────────────────────────────────

@router.post("/{project_id}/cold_start/import", status_code=202)
def cold_start_import(
    project_id: str,
    req: ColdStartImportRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    """整本存量正文导入：自动切章 → 并发反向抽取 → 去噪聚类 → 分层晋升。

    dry_run=True 返回切章预览（不启动抽取）；否则启动后台 job，
    经 GET /cold_start/jobs/{job_id} 轮询进度。
    """
    from ...coldstart import get_cold_start_manager, split_manuscript

    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")

    if req.full_text:
        chapters = split_manuscript(req.full_text)
    elif req.chapters:
        chapters = [{"chapter_no": c.chapter_no, "title": f"第 {c.chapter_no} 章",
                     "text": c.text} for c in req.chapters]
    else:
        raise HTTPException(422, "full_text 与 chapters 必须提供其一")
    if not chapters:
        raise HTTPException(422, "切章结果为空——检查正文内容")

    if req.dry_run:
        return ColdStartSplitPreview(
            chapters_total=len(chapters),
            chapters=[{"chapter_no": c["chapter_no"], "title": c["title"],
                       "chars": len(c["text"])} for c in chapters],
        )

    mgr = get_cold_start_manager()
    api_key = os.environ.get("NOVELFORGE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    job = mgr.start(
        project_id, chapters, registry,
        auto_promote_low_risk=req.auto_promote_low_risk,
        max_concurrency=req.max_concurrency,
        api_key=api_key,
    )
    return ColdStartJobInfo(**job.snapshot())


@router.get("/{project_id}/cold_start/jobs", response_model=list[ColdStartJobInfo])
def cold_start_jobs(
    project_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    from ...coldstart import get_cold_start_manager

    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")
    mgr = get_cold_start_manager()
    return [ColdStartJobInfo(**j.snapshot()) for j in mgr.list_for_project(project_id)]


@router.get("/{project_id}/cold_start/jobs/{job_id}", response_model=ColdStartJobInfo)
def cold_start_job_status(
    project_id: str,
    job_id: str,
    registry: ProjectRegistry = Depends(get_registry),
):
    from ...coldstart import get_cold_start_manager

    mgr = get_cold_start_manager()
    job = mgr.get(job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(404, f"job 不存在: {job_id}")
    return ColdStartJobInfo(**job.snapshot())


@router.post("/{project_id}/cold_start", response_model=ColdStartResponse, status_code=202)
def cold_start(
    project_id: str,
    req: ColdStartRequest,
    registry: ProjectRegistry = Depends(get_registry),
):
    """从已有正文中反向抽取世界状态 facts → staging（永不自动 canon）。"""
    if registry.get(project_id) is None:
        raise HTTPException(404, f"项目不存在: {project_id}")

    conn = registry.open_conn(project_id)
    try:
        from ...config import NovelForgeConfig
        from ...control_plane.skill_base import SkillContext
        from ...control_plane.budget import BudgetLedger
        from ...control_plane.llm.factory import build_gateway
        from ...skills.cold_extract_skill import ColdExtractSkill

        api_key = os.environ.get("NOVELFORGE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")

        cfg = NovelForgeConfig(project_id=project_id, db_path="")
        if api_key:
            cfg.provider.api_key = api_key
            cfg.provider.provider = os.environ.get("NOVELFORGE_PROVIDER", "deepseek")
        else:
            cfg.provider.provider = "fake"

        skill = ColdExtractSkill()
        all_candidate_ids: list[str] = []
        all_atom_ids: list[str] = []

        for chapter_item in req.chapters:
            ledger = BudgetLedger()
            gw = build_gateway(cfg, ledger=ledger)
            ws: dict = {
                "source_text": chapter_item.text,
                "chapter_no": chapter_item.chapter_no,
            }
            ctx = SkillContext(
                project_id=project_id,
                target_chapter=chapter_item.chapter_no,
                mode="human_gate",
                as_of_chapter=chapter_item.chapter_no - 1,
                budget=ledger,
                llm=gw,
                conn=conn,
                workspace=ws,
            )
            result = skill.run(ctx)
            proposals: list[dict] = ws.get("proposals", [])

            # 写入 l1_atoms（冷启动原子，cold_start=1）
            atom_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO l1_atoms(id, chapter, atom_text, extracted_by, cold_start)"
                " VALUES(?,?,?,?,1)",
                (atom_id, chapter_item.chapter_no,
                 chapter_item.text[:500],
                 "cold_extract"),
            )
            all_atom_ids.append(atom_id)

            # 写入 fact_candidates（全部 proposed，不自动 canon）
            for prop in proposals:
                cid = str(uuid.uuid4())
                fact_type = prop.get("fact_type", "misc")
                op = prop.get("op", "add")
                risk_tier = prop.get("risk_tier", "low")
                valid_from = prop.get("valid_from_chapter", chapter_item.chapter_no)
                # 构建标准化的 proposal_json
                prop_dict = {
                    "op": op,
                    "fact_type": fact_type,
                    "new": prop.get("new", {}),
                    "valid_from_chapter": valid_from,
                    "source": "cold_start",
                }
                if prop.get("entity"):
                    prop_dict["entity"] = prop["entity"]
                conn.execute(
                    "INSERT INTO fact_candidates"
                    "(candidate_id, op, entity_id, fact_type, proposal_json,"
                    " status, risk_tier, source_chapter, source_skill)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (cid, op, None, fact_type,
                     json.dumps(prop_dict, ensure_ascii=False),
                     "proposed", risk_tier, chapter_item.chapter_no,
                     "cold_extract"),
                )
                all_candidate_ids.append(cid)

        conn.commit()
        return ColdStartResponse(
            candidate_ids=all_candidate_ids,
            atom_ids=all_atom_ids,
            chapters_processed=len(req.chapters),
        )
    finally:
        conn.close()
