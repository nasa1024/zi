"""Orchestrator：generate_chapter() 主循环（§07.4 / §12）。

流程：0.init → 1.RECALL+PACING → 2.PLAN → 3.DRAFT → 4.CHECK → 5.REVISE(loop) → 6.DEDUP+CONFLICT+GATE → 7.COMMIT
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..config import NovelForgeConfig
from ..contracts import FactCandidate, RunContext, normalize_proposal_fact_type
from ..craft.extensions import build_draft_briefs, settle_steps
from ..craft.pacing import PacingController
from ..dedup.dedup_engine import DeduplicationEngine
from ..governance.conflict import ConflictSet, classify_risk, detect_conflict
from ..governance.gate import GateOutcome, Route, apply_gate_routes
from ..governance.promotion_policy import GateDecision, PromotionPolicy
from ..ids import new_id
from ..memory.recall import gather_hard_context
from ..planning.alignment import (
    check_plan_alignment,
    evaluate_and_persist_alignment,
    reconcile_plan,
)
from ..planning.context import gather_plan_context
from ..world.replay import get_world_state
from .budget import BudgetLedger, CircuitTripped
from .llm.gateway import LLMGateway
from .llm.tiers import ModelTier
from .skill_base import SkillContext, SkillResult
from .skill_registry import SkillRegistry, get_registry
from ..writing_profiles import (
    compile_system_prompt,
    get_project_writing_profile,
    profile_prompt_block,
)


class DraftRevisionConflict(RuntimeError):
    """The requested base draft is no longer the chapter's latest revision."""

    def __init__(self, expected_id: str, current_id: str | None) -> None:
        self.expected_id = expected_id
        self.current_id = current_id
        super().__init__(
            f"draft base revision changed: expected={expected_id}, current={current_id}"
        )


@dataclass
class ChapterOutcome:
    chapter: int
    ok: bool
    run_id: str = ""
    draft_text: str = ""
    fact_ids_committed: list[str] = field(default_factory=list)
    candidates_queued: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    gate: Optional[GateOutcome] = None
    error: Optional[str] = None
    usage_tokens: int = 0
    usage_usd: float = 0.0
    cache_read_tokens: int = 0
    quality_score: Optional[float] = None
    quality_dimensions: Optional[dict] = None  # {hook,pacing,character,prose: 0-10}
    state_degraded: bool = False   # P1#11: 结算块失败但正文已落袋（连载继续，留修复入口）
    alignment_report: Optional[dict] = None
    recommended_plan_action: Optional[str] = None

    def summary(self) -> str:
        if not self.ok:
            return f"章节 {self.chapter} 失败: {self.error}"
        return (
            f"章节 {self.chapter} 完成: committed={len(self.fact_ids_committed)}"
            f" queued={len(self.candidates_queued)}"
            f" tokens={self.usage_tokens}"
        )


def _ensure_final_proposals(
    registry: SkillRegistry,
    skill_ctx: SkillContext,
) -> bool:
    """Ensure proposals correspond to the currently selected draft text.

    Returns ``True`` when the selected text changed and extraction was refreshed.
    Empty output is a valid, memoized result and is not repeatedly re-extracted.
    """
    workspace = skill_ctx.workspace
    draft_text = str(workspace.get("draft_text") or "")
    if workspace.get("proposal_source_text") == draft_text:
        return False

    # Compatibility: an older chapter-draft model may already have returned
    # proposals alongside the selected candidate.  They belong to this text.
    if workspace.get("proposal_source_text") is None and workspace.get("proposals"):
        workspace["proposal_source_text"] = draft_text
        workspace["final_proposals_extracted"] = True
        return True

    previous_proposals = list(workspace.get("proposals") or [])
    workspace["proposals"] = []
    if draft_text:
        workspace["source_text"] = draft_text
        workspace["chapter_no"] = skill_ctx.target_chapter
        try:
            registry.invoke("cold_extract", skill_ctx)
        except Exception as exc:
            workspace["proposals"] = []
            workspace["actuals_extractor_error"] = str(exc)
    if not workspace.get("proposals") and previous_proposals:
        # A revision may leave an already extracted state change untouched.
        # Retain only proposals whose explicit evidence/value still occurs in
        # the accepted text; removed events must not leak into settlement.
        workspace["proposals"] = [
            proposal for proposal in previous_proposals
            if _proposal_supported_by_text(proposal, draft_text)
        ]
    workspace["proposal_source_text"] = draft_text
    workspace["final_proposals_extracted"] = True
    return True


def _proposal_supported_by_text(proposal: dict, draft_text: str) -> bool:
    refs = proposal.get("evidence_refs") or []
    quotes = [
        str(ref.get("quote") or "").strip()
        for ref in refs if isinstance(ref, dict)
    ]
    quotes = [quote for quote in quotes if quote]
    if quotes:
        return any(quote in draft_text for quote in quotes)
    new = proposal.get("new") if isinstance(proposal.get("new"), dict) else {}
    values = [
        new.get("object"),
        new.get("value"),
        new.get("after"),
        new.get("statement"),
        new.get("detail"),
    ]
    targets = [str(value).strip() for value in values if value is not None]
    return any(len(target) >= 2 and target in draft_text for target in targets)


def validate_final_candidate(
    skill_ctx: SkillContext,
    draft_text: str,
    *,
    plan_context=None,
    proposals: Optional[list[dict]] = None,
    registry: Optional[SkillRegistry] = None,
    score_quality: bool = True,
    judge_tier: str = "mid",
) -> dict:
    """Validate a generated or manually selected final candidate.

    The order is intentional and shared by the manual replacement API:
    continuity → plan alignment → craft.  Validation runs in a copied workspace
    so inspecting a candidate cannot overwrite the caller's selected draft.
    """
    workspace = dict(skill_ctx.workspace)
    workspace["draft_text"] = draft_text
    workspace["proposals"] = list(proposals or [])
    writing_profile = (
        workspace.get("writing_profile")
        or skill_ctx.extra.get("writing_profile")
        or get_project_writing_profile(
            skill_ctx.conn, chapter=skill_ctx.target_chapter,
        )
    )
    workspace["writing_profile"] = writing_profile
    context = plan_context or workspace.get("plan_context")
    if context is None:
        context = gather_plan_context(skill_ctx.conn, skill_ctx.target_chapter)
    workspace["plan_context"] = context
    if not workspace.get("beats") and context is not None:
        workspace["beats"] = list(context.selected_beats)
    if workspace.get("world_state") is None:
        workspace["world_state"] = get_world_state(
            skill_ctx.as_of_chapter, skill_ctx.conn
        )

    check_ctx = SkillContext(
        project_id=skill_ctx.project_id,
        target_chapter=skill_ctx.target_chapter,
        mode=skill_ctx.mode,
        as_of_chapter=skill_ctx.as_of_chapter,
        budget=skill_ctx.budget,
        llm=skill_ctx.llm,
        conn=skill_ctx.conn,
        workspace=workspace,
        extra={**skill_ctx.extra, "writing_profile": writing_profile},
    )
    reg = registry or get_registry()

    # Pure candidates intentionally carry no proposals.  Extract state changes
    # once for the selected candidate before continuity/craft checks that may
    # require payoff evidence.
    _ensure_final_proposals(reg, check_ctx)

    reg.invoke("continuity_check", check_ctx)
    continuity_issues = list(workspace.get("continuity_issues") or [])

    from ..planning.scoring import evaluate_candidate_against_plan
    plan_evaluation = evaluate_candidate_against_plan(
        draft_text,
        chapter_contract=getattr(context, "current_contract", None),
        plan_context=context,
    )
    plan_issues = list(plan_evaluation["plan_blocks"])

    reg.invoke("craft_check", check_ctx)
    raw_craft_issues = list(workspace.get("craft_issues") or [])
    if not workspace.get("beats"):
        # Legacy runs did not persist dynamically generated planner beats in
        # detail_json.  Absence is then "unknown", not proof of a broken beat
        # contract; retain all other craft checks.
        raw_craft_issues = [
            issue for issue in raw_craft_issues
            if issue.get("check") != "beat_contract"
        ]
    craft_issues = [
        issue if issue.get("source") else {"source": "craft", **issue}
        for issue in raw_craft_issues
    ]
    issues = continuity_issues + plan_issues + craft_issues
    hard_blocks = [issue for issue in issues if issue.get("severity") == "block"]

    quality = None
    if score_quality and draft_text:
        from ..craft.candidate_judge import score_chapter_detailed
        quality = score_chapter_detailed(
            skill_ctx.llm,
            judge_tier,
            workspace.get("chapter_goal", ""),
            draft_text,
            writing_profile,
        )
    return {
        "ok": not hard_blocks,
        "issues": issues,
        "hard_blocks": hard_blocks,
        "hard_block_count": len(hard_blocks),
        "quality": quality,
        "quality_score": quality.get("score") if quality else None,
        "proposals": list(workspace.get("proposals") or []),
        "continuity_issues": continuity_issues,
        "plan_issues": plan_issues,
        "craft_issues": craft_issues,
        "plan_evaluation": plan_evaluation,
    }


class Orchestrator:
    def __init__(
        self,
        gateway: LLMGateway,
        registry: Optional[SkillRegistry] = None,
        cfg: Optional[NovelForgeConfig] = None,
    ) -> None:
        self._gw = gateway
        self._reg = registry or get_registry()
        self._cfg = cfg or NovelForgeConfig()

    def generate_chapter(
        self,
        chapter: int,
        conn: sqlite3.Connection,
        *,
        chapter_goal: str = "",
        entity_ids: Optional[list[str]] = None,
        keyword_query: Optional[str] = None,
        interaction_mode: Optional[str] = None,
        progress_cb=None,  # Optional[Callable[[str, str, dict], None]]
    ) -> ChapterOutcome:
        cfg = self._cfg
        ledger = self._gw.ledger
        writing_profile = get_project_writing_profile(conn, chapter=chapter)
        # 成本快照：落库本章净消耗（调用方复用 ledger 跨章时差值依然正确）
        _tokens0, _usd0 = ledger.tokens_spent, ledger.usd_spent
        _cache0 = getattr(ledger, "cache_read_tokens", 0)
        resolved_interaction_mode = interaction_mode or {
            "auto_promote": "autonomous",
            "hybrid": "copilot",
        }.get(cfg.governance.mode, "guided")

        # ── 0. init workspace + pipeline_run 状态机 ──────────────────────────
        run_id = new_id("run")
        try:
            generation_model = self._gw.model_for(ModelTier.STRONG)
        except Exception:
            generation_model = None
        _begin_pipeline_run(
            conn, chapter, cfg.project_id, run_id,
            writing_profile=writing_profile, model_id=generation_model,
        )
        workspace: dict = {
            "chapter_goal": chapter_goal,
            "writing_profile": writing_profile,
            "profile_cache_key": writing_profile["cache_key"],
        }
        quality_rules = writing_profile.get("quality_rules") or {}
        target_range = quality_rules.get("chapter_word_range") or [1500, 2500]
        target_words = int((int(target_range[0]) + int(target_range[1])) / 2)
        skill_ctx = SkillContext(
            project_id=cfg.project_id,
            target_chapter=chapter,
            mode=cfg.governance.mode,
            as_of_chapter=chapter - 1,
            budget=ledger,
            llm=self._gw,
            conn=conn,
            workspace=workspace,
            extra={
                "draft_target_chars": cfg.draft_target_chars,
                "draft_target_words": target_words,
                "writing_profile": writing_profile,
            },
        )

        pacing = PacingController()

        try:
            # ── 1. RECALL + PACING ────────────────────────────────────────────
            entity_ids = entity_ids or _infer_entity_ids(conn, chapter)
            mcfg = getattr(cfg, "memory", None)
            _vec_on = getattr(cfg.recall, "enable_vector", False) and mcfg is not None
            _embedder = None
            if _vec_on:
                from ..memory.embedding import build_embedder
                _embedder = build_embedder(mcfg, dim=mcfg.embedding_dim)
            recall_pack = gather_hard_context(
                entity_ids,
                chapter - 1,
                conn,
                keyword_query=keyword_query,
                max_keywords=cfg.recall.max_keywords,
                context_window=cfg.recall.context_window_chapters,
                summary_window=getattr(cfg.recall, "summary_window_chapters", 5),
                enable_summaries=getattr(cfg.recall, "enable_summaries", True),
                embedder=_embedder,
                enable_vector=_vec_on,
                scene_query=keyword_query or chapter_goal,
                vector_top_k=getattr(mcfg, "vector_top_k", 8) if mcfg else 8,
                rrf_k=getattr(mcfg, "rrf_k", 60) if mcfg else 60,
            )
            world_state = get_world_state(chapter - 1, conn)
            workspace["recall_pack"] = recall_pack
            workspace["world_state"] = world_state
            # M1-⑥：稳定前缀一次构建（含标题头，保证各 skill 的 user 消息从第 0 字节
            # 起完全一致），draft/check/revise 共享，吃 provider 前缀缓存
            content_language = writing_profile["content_language"]
            _stable = recall_pack.to_stable_context_str(content_language)
            _world_stable = (
                (f"## Stable world context\n{_stable}" if content_language.startswith("en")
                 else f"## 世界设定（稳定）\n{_stable}")
                if _stable else ""
            )
            workspace["stable_context"] = "\n\n".join(
                part for part in (profile_prompt_block(writing_profile), _world_stable)
                if part
            )
            workspace["dynamic_context"] = recall_pack.to_dynamic_context_str(
                content_language,
            )

            # v16：历史事实继续严格 as_of(N-1)；未来路线走独立 PlanContext，
            # 只作为作者意图/创作护栏，绝不进入 World State 或 canon。
            plan_context = gather_plan_context(conn, chapter, lookahead_chapters=5)
            workspace["plan_context"] = plan_context
            workspace["future_plan_context"] = plan_context.render_for_planner(
                content_language,
            )
            workspace["future_guardrails"] = plan_context.render_guardrails_for_draft(
                content_language,
            )
            workspace["chapter_contract_context"] = plan_context.render_contract_for_draft(
                content_language,
            )

            # P1#9：文风锚点 few-shot——按本章细纲目标情绪选段，注入动态段。
            # 无情绪/无匹配锚点 → 空块（fail-fast 不瞎编）；不碰稳定前缀，缓存无损。
            from ..craft.style_anchor import pick_style_anchors, render_anchor_block
            try:
                emo_row = conn.execute(
                    "SELECT target_emotion FROM chapter_cards WHERE chapter=?",
                    (chapter,),
                ).fetchone()
                target_emotion = emo_row["target_emotion"] if emo_row else None
            except sqlite3.OperationalError:
                target_emotion = None   # 老库无 v13 列
            workspace["style_anchor_block"] = render_anchor_block(
                pick_style_anchors(
                    conn,
                    target_emotion,
                    language=writing_profile["content_language"],
                    profile_tags=writing_profile.get("story_tags") or [],
                ),
                language=writing_profile["content_language"],
            )

            # PacingController：读取节拍状态，附加建议到 chapter_goal
            pacing_state = pacing.get_state(conn)
            workspace["pacing_state"] = pacing_state
            hint = _profile_pacing_hint(writing_profile, pacing, pacing_state)
            if hint:
                workspace["chapter_goal"] = (chapter_goal + "\n" + hint).strip()

            # v22：特性扩展的动态提示块（承诺提醒/转机要求/信息差机会…）。
            # 只进动态段（planner 与 draft 均在契约之后渲染），绝不进入稳定前缀。
            workspace["craft_brief"] = build_draft_briefs(
                conn, chapter, workspace, language=content_language, cfg=cfg,
            )

            if progress_cb:
                progress_cb("recall", "ok", {
                    "future_cards": len(plan_context.lookahead),
                    "active_arcs": len(plan_context.active_arcs),
                    "due_milestones": len(plan_context.due_milestones),
                    "locked_contract": plan_context.has_locked_contract,
                })

            # ── 2. PLAN ───────────────────────────────────────────────────────
            if plan_context.has_locked_contract:
                # 用户/Agent 已选细纲就是本章契约。Planner 不得静默重写。
                workspace["beats"] = plan_context.selected_beats
                workspace["planner_source"] = "selected_contract"
                if progress_cb:
                    progress_cb("plan", "ok", {
                        "source": "selected_contract",
                        "beats": len(plan_context.selected_beats),
                    })
            else:
                plan_result = self._reg.invoke("planner", skill_ctx)
                workspace["planner_source"] = "generated"
                if progress_cb:
                    progress_cb("plan", "ok" if plan_result.ok else "blocked", {
                        "source": "generated",
                    })
                if not plan_result.ok:
                    workspace["beats"] = []

            # ── 3. DRAFT（M3-①: n_candidates>1 时多候选 + 三级漏斗择优；
            #             候选间无依赖 → 线程池并行，单章等待时间 ≈ 最慢一稿）──
            n_cands = max(1, min(3, getattr(getattr(cfg, "candidates", None),
                                            "n_candidates", 1) or 1))
            if n_cands <= 1:
                draft_result = self._reg.invoke("chapter_draft", skill_ctx)
                draft_ok = draft_result.ok
            else:
                from concurrent.futures import ThreadPoolExecutor

                from ..craft.candidate_judge import select_best
                spread = getattr(cfg.candidates, "temperature_spread", 0.15)

                def _gen_candidate(i: int) -> dict:
                    # 每候选独立 workspace/extra（chapter_draft 会写 draft_text/proposals）；
                    # recall_pack 等大对象按引用共享（只读）。ledger 共享，charge 已加锁。
                    # 注意：chapter_draft 不触 conn；并行路径中的 skill 不得使用 sqlite 连接。
                    local_ctx = SkillContext(
                        project_id=skill_ctx.project_id,
                        target_chapter=skill_ctx.target_chapter,
                        mode=skill_ctx.mode,
                        as_of_chapter=skill_ctx.as_of_chapter,
                        budget=skill_ctx.budget,
                        llm=skill_ctx.llm,
                        conn=skill_ctx.conn,
                        workspace=dict(workspace),
                        extra={**skill_ctx.extra,
                               "temperature": max(0.1, 1.0 - i * spread)},
                    )
                    try:
                        r = self._reg.invoke("chapter_draft", local_ctx)
                        ok = r.ok
                    except CircuitTripped:
                        raise  # 预算熔断终止整章
                    except Exception:
                        ok = False  # 单候选失败不拖垮其余候选
                    return {
                        "draft_text": local_ctx.workspace.get("draft_text", ""),
                        "proposals": local_ctx.workspace.get("proposals", []),
                        "ok": ok,
                    }

                with ThreadPoolExecutor(max_workers=n_cands,
                                        thread_name_prefix="cand") as pool:
                    cand_list: list[dict] = list(pool.map(_gen_candidate, range(n_cands)))
                report = select_best(
                    cand_list,
                    world=workspace.get("world_state"),
                    chapter_goal=workspace.get("chapter_goal", ""),
                    chapter_contract=plan_context.current_contract,
                    plan_context=plan_context,
                    gateway=self._gw,
                    judge_tier=getattr(cfg.candidates, "judge_tier", "mid"),
                    writing_profile=writing_profile,
                )
                winner = cand_list[report["winner"]]
                workspace["draft_text"] = winner["draft_text"]
                workspace["proposals"] = winner["proposals"]
                workspace["candidate_report"] = {
                    **report,
                    "n_candidates": n_cands,
                    "lengths": [
                        _content_units(c["draft_text"], writing_profile["content_language"])
                        for c in cand_list
                    ],
                    "length_unit": (
                        "words" if writing_profile["content_language"].startswith("en")
                        else "characters"
                    ),
                    "beats": list(workspace.get("beats") or []),
                    "selected_by": "auto",
                    # 候选全文 + 提案持久化（M6: human_gate 下前端 3 选 1 换稿的数据源）
                    "candidates": [
                        {"draft_text": c["draft_text"][:20000],
                         "proposals": c["proposals"]}
                        for c in cand_list
                    ],
                }
                draft_ok = winner["ok"]
                if progress_cb:
                    progress_cb("candidates", "ok", {
                        "n": n_cands,
                        "winner": report["winner"],
                        "scores": report["scores"],
                        "reason": report["reason"],
                    })

            _draft_length = _content_units(
                workspace.get("draft_text", ""), writing_profile["content_language"]
            )
            if progress_cb:
                draft_stage_ok = bool(
                    draft_ok
                    or (
                        workspace.get("draft_text")
                        and not writing_profile["content_language"].startswith("en")
                    )
                )
                progress_cb("draft", "ok" if draft_stage_ok else "blocked",
                            {
                                "length": _draft_length,
                                "unit": "words" if writing_profile["content_language"].startswith("en") else "characters",
                            })
            if not draft_ok and not workspace.get("draft_text"):
                _fail_pipeline_run(
                    conn, run_id, "draft_empty", {"chapter": chapter},
                )
                return ChapterOutcome(
                    chapter=chapter, ok=False, run_id=run_id,
                    error="draft 失败: 所有候选均无产出",
                    usage_tokens=ledger.tokens_spent - _tokens0,
                    usage_usd=ledger.usd_spent - _usd0,
                    cache_read_tokens=(
                        getattr(ledger, "cache_read_tokens", 0) - _cache0
                    ),
                )

            # English language and hard word floor are locked profile
            # constraints, not advisory craft findings.  The drafting skill has
            # already retried invalid output; never extract or persist a last
            # invalid response merely because it contains some text.
            profile_blocks = _locked_profile_blocks(
                workspace.get("draft_text", ""), writing_profile,
            )
            if profile_blocks:
                _fail_pipeline_run(
                    conn, run_id, "writing_profile_constraints", {
                        "chapter": chapter,
                        "issues": profile_blocks,
                    },
                )
                if progress_cb:
                    progress_cb("draft", "blocked", {
                        "reason": "writing_profile_constraints",
                        "issues": profile_blocks,
                    })
                return ChapterOutcome(
                    chapter=chapter,
                    ok=False,
                    run_id=run_id,
                    draft_text=workspace.get("draft_text", ""),
                    issues=profile_blocks,
                    error="draft failed locked writing-profile constraints",
                    usage_tokens=ledger.tokens_spent - _tokens0,
                    usage_usd=ledger.usd_spent - _usd0,
                    cache_read_tokens=(
                        getattr(ledger, "cache_read_tokens", 0) - _cache0
                    ),
                )

            # Only the selected final candidate gets state-change extraction.
            # This must precede craft validation because payoff_binding uses the
            # extracted proposals as evidence that a promised payoff happened.
            _ensure_final_proposals(self._reg, skill_ctx)

            # ── 4. CHECK（continuity + craft 并行）────────────────────────────
            _check_continuity = self._reg.invoke("continuity_check", skill_ctx)
            _check_craft = self._reg.invoke("craft_check", skill_ctx)

            continuity_issues: list[dict] = workspace.get("continuity_issues", [])
            craft_issues: list[dict] = workspace.get("craft_issues", [])
            plan_issues = check_plan_alignment(
                plan_context, workspace.get("draft_text", ""), chapter)
            workspace["plan_issues"] = plan_issues
            all_issues: list[dict] = continuity_issues + [
                {"source": "craft", **i} for i in craft_issues
            ] + plan_issues
            if progress_cb:
                progress_cb("check", "ok", {"issues": len(all_issues)})

            # ── 5. REVISE loop ────────────────────────────────────────────────
            # M2-⑤ 中段加压：ConStory-Bench 实证一致性错误集中在叙事进程 40-60% 处，
            # 卷中段章节 revise 上限 +1
            revise_budget = cfg.max_revise_loops
            if getattr(cfg, "midpoint_boost", True):
                _prog = _volume_progress(conn, chapter)
                if _prog is not None and 0.4 <= _prog <= 0.6:
                    revise_budget += 1
            hard_blocks = [i for i in all_issues if i.get("severity") == "block"]
            for _iter in range(revise_budget):
                if not hard_blocks:
                    break
                if ledger and hasattr(ledger, "charge_revise_round"):
                    ledger.charge_revise_round()
                revise_result = self._revise(skill_ctx, hard_blocks)
                workspace.update(revise_result)
                _ensure_final_proposals(self._reg, skill_ctx)
                # 重跑两项 check
                self._reg.invoke("continuity_check", skill_ctx)
                self._reg.invoke("craft_check", skill_ctx)
                continuity_issues = workspace.get("continuity_issues", [])
                craft_issues = workspace.get("craft_issues", [])
                plan_issues = check_plan_alignment(
                    plan_context, workspace.get("draft_text", ""), chapter)
                workspace["plan_issues"] = plan_issues
                all_issues = continuity_issues + [
                    {"source": "craft", **i} for i in craft_issues
                ] + plan_issues
                hard_blocks = [i for i in all_issues if i.get("severity") == "block"]

            # ── 5b. 质量评分 + 软问题润色（M5-⑦，enabled=False 时零额外调用）──
            quality_score: Optional[float] = None
            qcfg = getattr(cfg, "quality", None)
            if qcfg is not None and qcfg.enabled and workspace.get("draft_text"):
                quality_score = self._quality_pass(
                    skill_ctx, cfg, hard_blocks, all_issues, progress_cb
                )
                # Polishing may have changed the accepted text.  Refresh its
                # actual state changes before the final validation snapshot.
                proposals_refreshed = _ensure_final_proposals(self._reg, skill_ctx)
                if proposals_refreshed:
                    self._reg.invoke("continuity_check", skill_ctx)
                    self._reg.invoke("craft_check", skill_ctx)
                continuity_issues = workspace.get("continuity_issues", [])
                craft_issues = workspace.get("craft_issues", [])
                plan_issues = check_plan_alignment(
                    plan_context, workspace.get("draft_text", ""), chapter)
                workspace["plan_issues"] = plan_issues
                all_issues = continuity_issues + [
                    {"source": "craft", **i} for i in craft_issues
                ] + plan_issues

            # A revise/polish pass can replace the accepted text.  Re-evaluate
            # the two locked English constraints immediately before durability
            # so no later mutation can bypass them.
            final_profile_blocks = _locked_profile_blocks(
                workspace.get("draft_text", ""), writing_profile,
            )
            if final_profile_blocks:
                all_issues = _merge_issues(all_issues, final_profile_blocks)
                _fail_pipeline_run(
                    conn, run_id, "writing_profile_constraints", {
                        "chapter": chapter,
                        "issues": final_profile_blocks,
                    },
                )
                if progress_cb:
                    progress_cb("check", "blocked", {
                        "reason": "writing_profile_constraints",
                        "issues": final_profile_blocks,
                    })
                return ChapterOutcome(
                    chapter=chapter,
                    ok=False,
                    run_id=run_id,
                    draft_text=workspace.get("draft_text", ""),
                    issues=all_issues,
                    error="revised draft failed locked writing-profile constraints",
                    usage_tokens=ledger.tokens_spent - _tokens0,
                    usage_usd=ledger.usd_spent - _usd0,
                    cache_read_tokens=(
                        getattr(ledger, "cache_read_tokens", 0) - _cache0
                    ),
                    quality_score=quality_score,
                    quality_dimensions=workspace.get("quality_dimensions"),
                )

            # ── 6. COMMIT-DRAFT（P1#11：正文先落袋，结算失败不丢章）─────────
            draft_text: str = workspace.get("draft_text", "")
            proposals: list[dict] = list(workspace.get("proposals") or [])
            draft_id = _persist_draft(
                conn, draft_text, chapter, cfg.project_id, cfg.db_path,
                writing_profile=writing_profile,
            )

            try:
                alignment = evaluate_and_persist_alignment(
                    conn,
                    plan_context,
                    draft_text,
                    chapter,
                    draft_id=draft_id,
                    seed_proposals=proposals,
                    interaction_mode=resolved_interaction_mode,
                )
                workspace["chapter_actuals"] = alignment.actuals
                workspace["alignment_report"] = alignment.report
                workspace["chapter_actuals_id"] = alignment.actuals_id
                workspace["alignment_report_id"] = alignment.report_id
                workspace["recommended_plan_action"] = alignment.report.decision
                proposals = alignment.proposals
                workspace["proposals"] = proposals
                conn.commit()
                if progress_cb:
                    progress_cb("plan_alignment", "ok", {
                        "divergence_score": alignment.report.divergence_score,
                        "divergence_level": alignment.report.divergence_level,
                        "recommended_plan_action": alignment.report.decision,
                    })
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                workspace["alignment_error"] = str(exc)

            # ── 7. SETTLE（canon 结算 + 写回类结算；失败降级不阻塞连载）──────
            settle = self._settle_chapter(
                skill_ctx, conn, chapter, draft_text, proposals, pacing,
                draft_id=draft_id,
                progress_cb=progress_cb,
                interaction_mode=resolved_interaction_mode,
            )

            plan_reconcile = settle.get("plan_reconcile") or {}
            final_alignment_report = (
                plan_reconcile.get("alignment_report")
                or (
                    workspace["alignment_report"].model_dump(mode="json")
                    if hasattr(workspace.get("alignment_report"), "model_dump")
                    else workspace.get("alignment_report")
                )
            )
            recommended_plan_action = (
                plan_reconcile.get("recommended_plan_action")
                or workspace.get("recommended_plan_action")
            )

            run_detail = workspace.get("candidate_report")
            run_detail = {
                **(run_detail or {}),
                "writing_profile": {
                    "content_language": writing_profile["content_language"],
                    "profile_id": writing_profile["writing_profile_id"],
                    "profile_version": writing_profile["writing_profile_version"],
                    "catalog_version": writing_profile["writing_profile_catalog_version"],
                    "prompt_version": writing_profile["prompt_version"],
                    "effective_from_chapter": writing_profile["effective_from_chapter"],
                },
                "model": generation_model,
            }
            if workspace.get("patch_stats"):
                run_detail = {**(run_detail or {}), "patch_stats": workspace["patch_stats"]}
            if workspace.get("quality_dimensions"):
                run_detail = {**(run_detail or {}),
                              "quality_dimensions": workspace["quality_dimensions"]}
            if settle["foreshadow_settle"] is not None:
                run_detail = {**(run_detail or {}),
                              "foreshadow_settle": settle["foreshadow_settle"]}
            if settle.get("plan_reconcile") is not None:
                run_detail = {**(run_detail or {}),
                              "plan_reconcile": settle["plan_reconcile"]}
            if settle.get("extensions"):
                run_detail = {**(run_detail or {}),
                              "extensions": settle["extensions"]}
            if final_alignment_report is not None:
                run_detail = {
                    **(run_detail or {}),
                    "alignment_report": final_alignment_report,
                    "recommended_plan_action": recommended_plan_action,
                    "chapter_actuals_id": workspace.get("chapter_actuals_id"),
                    "alignment_report_id": workspace.get("alignment_report_id"),
                }
            if workspace.get("actuals_extractor_error") or workspace.get("alignment_error"):
                run_detail = {
                    **(run_detail or {}),
                    "actuals_extractor_error": workspace.get("actuals_extractor_error"),
                    "alignment_error": workspace.get("alignment_error"),
                }
            if settle["degraded"]:
                run_detail = {**(run_detail or {}),
                              "state_degraded": True,
                              "settle_error": settle["error"],
                              "failed_steps": settle["failed_steps"],
                              # 修复入口：未结算 proposals 快照，可经 seed API 重放
                              "unsettled_proposals": proposals[:50]}
            _complete_pipeline_run(conn, run_id, draft_id,
                                   detail=run_detail,
                                   quality_score=quality_score,
                                   tokens_spent=ledger.tokens_spent - _tokens0,
                                   usd_spent=ledger.usd_spent - _usd0)

            if progress_cb:
                progress_cb("gate", "degraded" if settle["degraded"] else "ok",
                            {"committed": len(settle["committed_ids"]),
                             "queued": len(settle["queued"]),
                             "state_degraded": settle["degraded"]})
            return ChapterOutcome(
                chapter=chapter,
                ok=True,
                run_id=run_id,
                draft_text=draft_text,
                fact_ids_committed=settle["committed_ids"],
                candidates_queued=settle["queued"],
                issues=all_issues,
                gate=settle["gate_outcome"],
                error=settle["error"] if settle["degraded"] else None,
                usage_tokens=ledger.tokens_spent - _tokens0,
                usage_usd=ledger.usd_spent - _usd0,
                cache_read_tokens=(
                    getattr(ledger, "cache_read_tokens", 0) - _cache0
                ),
                quality_score=quality_score,
                quality_dimensions=workspace.get("quality_dimensions"),
                state_degraded=settle["degraded"],
                alignment_report=final_alignment_report,
                recommended_plan_action=recommended_plan_action,
            )

        except Exception as e:
            return ChapterOutcome(
                chapter=chapter, ok=False,
                error=str(e),
                usage_tokens=ledger.tokens_spent - _tokens0,
                usage_usd=ledger.usd_spent - _usd0,
                cache_read_tokens=(
                    getattr(ledger, "cache_read_tokens", 0) - _cache0
                ),
            )

    def _settle_chapter(
        self, skill_ctx: SkillContext, conn: sqlite3.Connection, chapter: int,
        draft_text: str, proposals: list[dict], pacing: PacingController,
        *, draft_id: Optional[str] = None, progress_cb=None,
        interaction_mode: str = "guided",
    ) -> dict:
        """P1#11（inkos state-degraded）：结算块降级保护——正文已落袋，结算
        任一步失败只标记降级、不丢章。

        步骤 A（canon 结算：candidates→dedup→conflict→gate）失败时清理本次
        创建的候选行后重试一次——重放安全性：候选行已删、dedup 会把与既提交
        事实重复的提案判 merge。步骤 B-E（pacing/摘要/伏笔翻转/伏笔结算）
        各自单次、单独降级，互不阻塞。含 CircuitTripped：正文已花钱生成，
        熔断时丢整章是最差结局，同样走降级。
        """
        cfg = self._cfg
        workspace = skill_ctx.workspace
        out: dict = {"degraded": False, "error": None, "failed_steps": [],
                     "gate_outcome": None, "committed_ids": [], "queued": [],
                     "foreshadow_settle": None, "plan_reconcile": None,
                     "extensions": {}}
        successful_candidates: list[FactCandidate] = []

        # ── 步骤 A：canon 结算（失败清理后重试一次）──────────────────────────
        for attempt in (1, 2):
            created: list[str] = []
            try:
                ctx = RunContext(conn=conn, policy_mode=cfg.governance.mode,
                                 actor="orchestrator")
                candidates = _proposals_to_candidates(
                    proposals, chapter, conn, source_draft_id=draft_id
                )
                successful_candidates = list(candidates)
                created = [c.candidate_id for c in candidates]
                dedup_gw = self._gw if cfg.dedup.enable_llm_arbiter else None
                dedup_engine = DeduplicationEngine(
                    bm25_gap_min=cfg.dedup.bm25_gap_min, llm_gateway=dedup_gw)
                candidates = _apply_dedup(candidates, dedup_engine, conn)

                conflict_map: dict[str, ConflictSet] = {}
                for cand in candidates:
                    cset = detect_conflict(cand, conn)
                    cand.risk_tier = classify_risk(cand, cfg)
                    if cset.has_block:
                        conflict_map[cand.candidate_id] = cset
                        try:
                            conn.execute(
                                "UPDATE fact_candidates SET risk_tier=? WHERE candidate_id=?",
                                (cand.risk_tier, cand.candidate_id))
                        except Exception:
                            pass
                conn.commit()

                world = workspace.get("world_state")
                gate_decision: GateDecision = PromotionPolicy.decide_batch(
                    candidates, world, cfg, conflict_map=conflict_map)
                gate_outcome: GateOutcome = apply_gate_routes(
                    ctx, gate_decision, {"chapter": chapter})
                out["gate_outcome"] = gate_outcome
                out["committed_ids"] = [fid for _, fid in gate_outcome.committed]
                out["queued"] = gate_outcome.queued
                break
            except Exception as e:
                successful_candidates = []
                try:
                    if created:
                        ph = ",".join("?" * len(created))
                        conn.execute(
                            f"DELETE FROM fact_candidates WHERE candidate_id IN ({ph})",
                            created)
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                if attempt == 2:
                    out["degraded"] = True
                    out["failed_steps"].append("gate")
                    out["error"] = f"gate: {e}"

        # ── 步骤 B-E：写回类结算，各自降级互不阻塞 ───────────────────────────
        def _step(name: str, fn) -> None:
            try:
                fn()
            except Exception as e:
                out["degraded"] = True
                out["failed_steps"].append(name)
                if out["error"] is None:
                    out["error"] = f"{name}: {e}"

        # L1 是 proposals 的证据/审计层，不参与 canon 裁决。复用已有提案，零额外
        # LLM；即便本章无提案也会把 draft 标为 completed，区分“未处理”和“无原子”。
        def _write_l1() -> None:
            from ..memory.l1_atoms import persist_proposal_atoms

            persist_proposal_atoms(
                conn,
                chapter=chapter,
                draft_id=draft_id,
                draft_text=draft_text,
                proposals=proposals,
                candidate_ids=[c.candidate_id for c in successful_candidates],
            )

        _step("l1_atoms", _write_l1)
        beats = workspace.get("beats", [])
        _step("pacing", lambda: pacing.update(
            chapter,
            beats,
            _content_units(
                draft_text,
                str((workspace.get("writing_profile") or {}).get("content_language") or "zh-CN"),
            ),
            conn,
            actuals=workspace.get("chapter_actuals"),
            contract=getattr(workspace.get("plan_context"), "current_contract", None),
        ))
        if getattr(cfg.recall, "enable_summaries", True) and draft_text:
            _step("summary", lambda: _persist_chapter_summary(
                conn, self._gw, chapter, draft_text))
        _step("foreshadow_flip", lambda: _flip_overdue_foreshadow(conn, chapter))
        plan_context = workspace.get("plan_context")
        has_future_plan = bool(
            plan_context is not None
            and (
                plan_context.story_plan
                or plan_context.current_contract
                or plan_context.active_arcs
                or plan_context.due_milestones
            )
        )
        if has_future_plan and draft_text:
            def _reconcile() -> None:
                out["plan_reconcile"] = reconcile_plan(
                    conn,
                    plan_context,
                    draft_text,
                    chapter,
                    actuals=workspace.get("chapter_actuals"),
                    report=workspace.get("alignment_report"),
                    report_id=workspace.get("alignment_report_id"),
                    interaction_mode=interaction_mode,
                )
                if out["plan_reconcile"]:
                    workspace["alignment_report"] = out["plan_reconcile"].get(
                        "alignment_report", workspace.get("alignment_report")
                    )
                    workspace["recommended_plan_action"] = out["plan_reconcile"].get(
                        "recommended_plan_action",
                        workspace.get("recommended_plan_action"),
                    )
            _step("plan_reconcile", _reconcile)
            if progress_cb:
                progress_cb("plan_reconcile", "ok", out["plan_reconcile"] or {})

        # §06 L2/L3 异步记忆管线（默认关；各自降级互不阻塞）。
        mcfg = getattr(cfg, "memory", None)
        if mcfg is not None and mcfg.enable_l2 and draft_text:
            _step("l2_aggregate", lambda: _run_l2(conn, self._gw, cfg, chapter, draft_text))
        if mcfg is not None and mcfg.enable_l3 and draft_text:
            _step("l3_events", lambda: _run_l3(
                conn, self._gw, cfg, chapter, draft_text, draft_id=draft_id
            ))

        scfg = getattr(cfg, "settle", None)
        if scfg is not None and scfg.enabled and draft_text:
            def _do_settle() -> None:
                from ..craft.foreshadow_settle import settle_foreshadow
                out["foreshadow_settle"] = settle_foreshadow(
                    self._gw, scfg.tier, conn, chapter, draft_text,
                    max_new_hooks=scfg.max_new_hooks)
            _step("foreshadow_settle", _do_settle)
            if progress_cb:
                progress_cb("settle",
                            "degraded" if "foreshadow_settle" in out["failed_steps"]
                            else "ok",
                            out["foreshadow_settle"] or {})

        # v22：特性扩展的结算步骤（承诺账本校验 / 实际转向入库 …），各自降级互不阻塞。
        if draft_text:
            for ext_name, ext_settle in settle_steps():
                def _run_extension(ext_name=ext_name, ext_settle=ext_settle) -> None:
                    out["extensions"][ext_name] = ext_settle(
                        conn, chapter, workspace, gateway=self._gw, cfg=cfg,
                    ) or {}
                _step(f"ext_{ext_name}", _run_extension)
        return out

    def _quality_pass(
        self, ctx: SkillContext, cfg, hard_blocks: list[dict],
        all_issues: list[dict], progress_cb=None,
    ) -> Optional[float]:
        """M5-⑦：质量评分；低分或 craft warn 堆积时做一轮润色，取分高版本。

        维度分（钩子/节奏/人物/文笔）写入 workspace["quality_dimensions"]，
        随 detail_json 落库；多候选复用评委总分的路径无维度分（省一次调用）。

        Returns 最终质量分（评分失败返回 None，等同未启用）。
        """
        from ..craft.candidate_judge import score_chapter_detailed

        qcfg = cfg.quality
        judge_tier = getattr(cfg.candidates, "judge_tier", "mid")
        chapter_goal = ctx.workspace.get("chapter_goal", "")

        # 多候选模式下评委已给胜者打过分 → 复用，避免重复调用
        score: Optional[float] = None
        report = ctx.workspace.get("candidate_report") or {}
        w = report.get("winner")
        scores = report.get("scores") or []
        if isinstance(w, int) and w < len(scores) and scores[w] is not None:
            score = float(scores[w])
        if score is None:
            detail = score_chapter_detailed(
                self._gw,
                judge_tier,
                chapter_goal,
                ctx.workspace.get("draft_text", ""),
                ctx.extra.get("writing_profile")
                or get_project_writing_profile(
                    ctx.conn, chapter=ctx.target_chapter,
                ),
            )
            if detail is not None:
                score = detail["score"]
                if detail["dimensions"]:
                    ctx.workspace["quality_dimensions"] = detail["dimensions"]
        if score is None:
            return None

        craft_warns = [i for i in ctx.workspace.get("craft_issues", [])
                       if i.get("severity") == "warn"]
        need_polish = (qcfg.polish_enabled and not hard_blocks and
                       (score < qcfg.min_score or len(craft_warns) >= 3))
        if need_polish:
            try:
                old_draft = ctx.workspace.get("draft_text", "")
                old_craft = list(ctx.workspace.get("craft_issues", []))
                if self._gw.ledger and hasattr(self._gw.ledger, "charge_revise_round"):
                    self._gw.ledger.charge_revise_round()
                polished = self._polish(ctx, craft_warns)
                if polished and polished != old_draft:
                    ctx.workspace["draft_text"] = polished
                    self._reg.invoke("craft_check", ctx)
                    new_detail = score_chapter_detailed(
                        self._gw,
                        judge_tier,
                        chapter_goal,
                        polished,
                        ctx.extra.get("writing_profile")
                        or get_project_writing_profile(
                            ctx.conn, chapter=ctx.target_chapter,
                        ),
                    )
                    if new_detail is not None and new_detail["score"] >= score:
                        score = new_detail["score"]
                        if new_detail["dimensions"]:
                            ctx.workspace["quality_dimensions"] = new_detail["dimensions"]
                    else:
                        # 润色变差 → 回退（HoLLMwood 渐进精炼 + 保底）
                        ctx.workspace["draft_text"] = old_draft
                        ctx.workspace["craft_issues"] = old_craft
            except Exception:
                pass  # 预算熔断等 → 保留原稿原分

        if progress_cb:
            progress_cb("quality", "ok", {
                "score": score, "polished": need_polish,
                "min_score": qcfg.min_score,
                "dimensions": ctx.workspace.get("quality_dimensions"),
            })
        return score

    def _polish(self, ctx: SkillContext, craft_warns: list[dict]) -> str:
        """单轮工艺润色：不改情节/人物行为/事实设定，只解决 craft warn。

        M7：优先锚点补丁（局部润色），失败回退全文润色。
        """
        from ..craft.findings import findings_to_issues_str
        draft_text: str = ctx.workspace.get("draft_text", "")
        profile = ctx.workspace.get("writing_profile") or get_project_writing_profile(
            ctx.conn, chapter=ctx.target_chapter,
        )
        is_english = profile["content_language"].startswith("en")
        warns_str = findings_to_issues_str(craft_warns) or (
            "- Improve pacing, prose clarity, and the continuation hook."
            if is_english else "- 整体打磨节奏与钩子"
        )
        stable = ctx.workspace.get("stable_context", "")

        if getattr(self._cfg, "patch_revise", True):
            from ..craft.patch_revise import apply_patches, generate_patches
            patches = generate_patches(
                self._gw, ModelTier.MID,
                stable_context=stable, draft_text=draft_text[:8000],
                issues_str=warns_str,
                task_label="Polish Patch Task" if is_english else "润色补丁任务",
                writing_profile=profile,
            )
            if patches:
                result = apply_patches(draft_text, patches)
                _record_patch_stats(ctx.workspace, "polish", result, len(patches))
                if result.applied > 0:
                    return result.text

        from .llm.provider import CacheHint, Message
        system = compile_system_prompt(
            "你是 NovelForge 润色助手。在不改变情节走向、人物行为与事实设定的前提下"
            "润色草稿，重点解决列出的工艺问题。只输出润色后的完整草稿，不要其他说明。",
            profile,
            english_system=(
                "You are zi's prose editor. Fix only the listed craft problems while preserving "
                "plot direction, character actions, facts, point of view, tense, and American "
                "English spelling. Return only the complete polished chapter."
            ),
        )
        prefix = f"{stable}\n\n" if stable else ""
        # P1#9：润色是文风敏感环节，注入锚点块（锚点补丁分支不注入——局部修复
        # 引入风格样本反而引导改写过多）
        anchor_block = ctx.workspace.get("style_anchor_block", "")
        resp = ctx.llm.generate(
            ModelTier.MID,
            [Message(role="user", content=(
                f"{prefix}"
                + (f"{anchor_block}\n\n" if anchor_block else "")
                + (
                    f"Craft issues:\n{warns_str}\n\nCurrent draft:\n{draft_text[:9000]}"
                    if is_english else
                    f"工艺问题：\n{warns_str}\n\n当前草稿：\n{draft_text[:6000]}"
                )
            ))],
            system=system,
            max_tokens=8192,
            cache_hint=CacheHint(user_prefix_chars=len(stable)) if stable else None,
        )
        return resp.text.strip()

    def _revise(self, ctx: SkillContext, hard_blocks: list[dict]) -> dict:
        from ..craft.findings import findings_to_issues_str
        draft_text: str = ctx.workspace.get("draft_text", "")
        issues_str = findings_to_issues_str(hard_blocks)
        stable = ctx.workspace.get("stable_context", "")
        profile = ctx.workspace.get("writing_profile") or get_project_writing_profile(
            ctx.conn, chapter=ctx.target_chapter,
        )
        is_english = profile["content_language"].startswith("en")

        # P0#2/P1#8（inkos repair_scope 路由）：结构性问题（OOC/主线偏离/时间线）
        # 补丁救不了，跳过锚点补丁直接全文重写；全 local 才走补丁。
        # M7：先尝试锚点补丁（输出短、不碰好段落）；锚定失败回退全文重写
        structural = any(i.get("repair_scope") == "structural" for i in hard_blocks)
        if not structural and getattr(self._cfg, "patch_revise", True):
            from ..craft.patch_revise import apply_patches, generate_patches
            patches = generate_patches(
                self._gw, ModelTier.STRONG,
                stable_context=stable, draft_text=draft_text[:8000],
                issues_str=issues_str,
                task_label="Revision Patch Task" if is_english else "修订补丁任务",
                writing_profile=profile,
            )
            if patches:
                result = apply_patches(draft_text, patches)
                _record_patch_stats(ctx.workspace, "revise", result, len(patches))
                if result.applied > 0:
                    return {"draft_text": result.text}

        from .llm.provider import CacheHint, Message
        system = compile_system_prompt(
            "你是 NovelForge 修订助手。根据以下一致性问题修改草稿。只输出修改后的完整草稿，不要其他说明。",
            profile,
            english_system=(
                "You are zi's revision editor. Correct the listed continuity and profile "
                "violations while preserving unaffected events and prose. Keep the configured "
                "point of view, tense, and American English. Return only the complete revised chapter."
            ),
        )
        prefix = f"{stable}\n\n" if stable else ""
        user_msg = (
            f"{prefix}"
            + (
                f"Issues to correct:\n{issues_str}\n\nCurrent draft:\n{draft_text[:9000]}"
                if is_english else
                f"一致性问题：\n{issues_str}\n\n当前草稿：\n{draft_text[:4000]}"
            )
        )
        resp = ctx.llm.generate(
            ModelTier.STRONG,
            [Message(role="user", content=user_msg)],
            system=system,
            max_tokens=5000,
            cache_hint=CacheHint(user_prefix_chars=len(stable)) if stable else None,
        )
        return {"draft_text": resp.text.strip()}


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def _infer_entity_ids(conn: sqlite3.Connection, chapter: int, limit: int = 10) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT entity_id FROM character_power_log"
        " WHERE change_chapter<=? ORDER BY change_chapter DESC LIMIT ?",
        (chapter, limit),
    ).fetchall()
    if rows:
        return [r["entity_id"] for r in rows]
    rows = conn.execute("SELECT id FROM entities LIMIT ?", (limit,)).fetchall()
    return [r["id"] for r in rows]


def _content_units(text: str, language: str) -> int:
    from ..text_metrics import count_content_units

    return count_content_units(text or "", language)


def _profile_pacing_hint(profile: dict, pacing: PacingController, state) -> str:
    """Keep legacy cadence for Chinese; English rules follow the profile."""
    if not str(profile.get("content_language") or "").startswith("en"):
        return pacing.recommend_beat_hint(state)
    if state.needs_cooldown:
        return (
            "Pacing note: recent chapters have stayed at high tension. Let this chapter "
            "breathe through character reaction, consequence, or relationship movement."
        )
    quality = profile.get("quality_rules") or {}
    if not quality.get("requires_progress_event"):
        return ""
    if state.needs_big_payoff and state.buildup >= 5:
        return (
            "Pacing note: deliver an earned major progress event that resolves prior setup "
            "and creates a concrete new cost or objective."
        )
    if state.needs_small_payoff:
        return (
            "Pacing note: include at least one observable, earned progress event or useful "
            "feedback loop in this chapter."
        )
    return ""


def _proposals_to_candidates(
    proposals: list[dict], chapter: int, conn: sqlite3.Connection,
    *, source_draft_id: str | None = None,
) -> list[FactCandidate]:
    candidates = []
    for p in proposals:
        p = normalize_proposal_fact_type(p)
        cid = new_id("cand")
        fact_type = p.get("fact_type", "unknown")
        entity = p.get("entity")
        entity_id = _resolve_entity(entity, conn) if entity else None
        risk_tier = p.get("risk_tier", "low")
        proposal_json = json.dumps(p, ensure_ascii=False)
        op = p.get("op", "add")
        conn.execute(
            "INSERT INTO fact_candidates"
            "(candidate_id, op, entity_id, fact_type, proposal_json, status, risk_tier,"
            " source_chapter, source_draft_id)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (cid, op, entity_id, fact_type, proposal_json, "proposed", risk_tier,
             chapter, source_draft_id),
        )
        conn.commit()
        candidates.append(FactCandidate(
            candidate_id=cid,
            entity_id=entity_id,
            fact_type=fact_type,
            proposal_json=proposal_json,
            status="proposed",
            risk_tier=risk_tier,
            source_chapter=chapter,
        ))
    return candidates


def _apply_dedup(
    candidates: list[FactCandidate],
    engine: DeduplicationEngine,
    conn: sqlite3.Connection,
) -> list[FactCandidate]:
    """去重过滤：merge/conflict 的候选从活跃列表中移除。"""
    keep: list[FactCandidate] = []
    for cand in candidates:
        verdict = engine.check(cand, conn)
        if verdict.action == "store":
            keep.append(cand)
        elif verdict.action == "merge":
            # 标记为 superseded，不进入 gate
            try:
                conn.execute(
                    "UPDATE fact_candidates SET status='superseded' WHERE candidate_id=?",
                    (cand.candidate_id,),
                )
            except Exception:
                pass
        elif verdict.action == "conflict":
            # 保留进入 gate，但会被 conflict_map 捕获
            keep.append(cand)
    return keep


def _resolve_entity(ref: str, conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        "SELECT id FROM entities WHERE id=? OR canonical_name=? LIMIT 1", (ref, ref)
    ).fetchone()
    return row["id"] if row else None


def _begin_pipeline_run(
    conn: sqlite3.Connection,
    chapter: int,
    project_id: str,
    run_id: str,
    *,
    writing_profile: Optional[dict] = None,
    model_id: Optional[str] = None,
) -> None:
    """在 pipeline_run 表插入 'running' 行（F6 状态机开始）。"""
    try:
        profile = writing_profile or {}
        try:
            conn.execute(
                "INSERT OR IGNORE INTO pipeline_run("
                "run_id, chapter, project_id, status, content_language,"
                " writing_profile_id, writing_profile_version, prompt_version, model_id)"
                " VALUES(?, ?, ?, 'running', ?, ?, ?, ?, ?)",
                (
                    run_id, chapter, project_id,
                    profile.get("content_language"), profile.get("writing_profile_id"),
                    profile.get("writing_profile_version"), profile.get("prompt_version"),
                    model_id,
                ),
            )
        except sqlite3.OperationalError:
            conn.execute(
                "INSERT OR IGNORE INTO pipeline_run(run_id, chapter, project_id, status)"
                " VALUES(?, ?, ?, 'running')",
                (run_id, chapter, project_id),
            )
        conn.commit()
    except Exception:
        pass


def _complete_pipeline_run(
    conn: sqlite3.Connection, run_id: str, draft_id: Optional[str],
    detail: Optional[dict] = None,
    quality_score: Optional[float] = None,
    tokens_spent: Optional[int] = None,
    usd_spent: Optional[float] = None,
) -> None:
    """将 pipeline_run 行更新为 'completed'（F6 状态机结束）。detail=候选择优报告等明细。"""
    try:
        detail_json = json.dumps(detail, ensure_ascii=False) if detail else None
        conn.execute(
            "UPDATE pipeline_run SET status='completed', draft_id=?, detail_json=?,"
            " quality_score=?, tokens_spent=?, usd_spent=?, finished_at=datetime('now')"
            " WHERE run_id=?",
            (draft_id, detail_json, quality_score, tokens_spent,
             round(usd_spent, 6) if usd_spent is not None else None, run_id),
        )
        conn.commit()
    except Exception:
        pass


def _fail_pipeline_run(
    conn: sqlite3.Connection,
    run_id: str,
    reason: str,
    detail: Optional[dict] = None,
) -> None:
    """Close a run rejected before draft durability using the legacy state enum."""
    try:
        payload = {"failure_reason": reason, **(detail or {})}
        conn.execute(
            "UPDATE pipeline_run SET status='crashed', detail_json=?,"
            " finished_at=datetime('now') WHERE run_id=?",
            (json.dumps(payload, ensure_ascii=False), run_id),
        )
        conn.commit()
    except Exception:
        pass


def _locked_profile_blocks(text: str, writing_profile: dict) -> list[dict]:
    """Return only non-negotiable language/length findings for English prose."""
    language = str(writing_profile.get("content_language") or "")
    if not language.startswith("en"):
        return []
    from ..craft.language_quality import check_english_quality

    blocks: list[dict] = []
    for finding in check_english_quality(text, writing_profile):
        category = str(finding.get("category") or "")
        if finding.get("severity") != "block" or category not in {
            "language_match", "word_count_range",
        }:
            continue
        blocks.append({
            "source": "craft",
            "check": category,
            **finding,
        })
    return blocks


def _merge_issues(existing: list[dict], additions: list[dict]) -> list[dict]:
    merged = list(existing)
    seen = {
        (item.get("source"), item.get("check"), item.get("detail") or item.get("issue"))
        for item in merged
    }
    for item in additions:
        key = (item.get("source"), item.get("check"), item.get("detail") or item.get("issue"))
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


def _flip_overdue_foreshadow(conn: sqlite3.Connection, chapter: int) -> None:
    """M5-⑧：把已过期未回收的伏笔翻转为 overdue（此前无任何代码翻转此状态）。"""
    try:
        conn.execute(
            "UPDATE foreshadow SET state='overdue', updated_at=datetime('now')"
            " WHERE state IN ('planted','reinforced','misled')"
            "   AND due_chapter IS NOT NULL AND due_chapter<?",
            (chapter,),
        )
        conn.commit()
    except Exception:
        pass


def _record_patch_stats(workspace: dict, kind: str, result, n_patches: int) -> None:
    """M7：累计补丁应用统计（kind = revise|polish），随 detail_json 落库供观测。"""
    stats = workspace.setdefault("patch_stats", {})
    s = stats.setdefault(kind, {"rounds": 0, "patches": 0, "applied": 0, "failed": 0})
    s["rounds"] += 1
    s["patches"] += n_patches
    s["applied"] += result.applied
    s["failed"] += result.failed


def _volume_progress(conn: sqlite3.Connection, chapter: int) -> Optional[float]:
    """章节在所属卷中的进度 [0,1]；无卷信息或卷未闭区间时返回 None。"""
    try:
        row = conn.execute(
            "SELECT start_chapter, end_chapter FROM volumes"
            " WHERE start_chapter IS NOT NULL AND end_chapter IS NOT NULL"
            "   AND start_chapter<=? AND end_chapter>=?"
            " ORDER BY volume_no LIMIT 1",
            (chapter, chapter),
        ).fetchone()
        if row is None:
            return None
        start, end = row["start_chapter"], row["end_chapter"]
        if end <= start:
            return None
        return (chapter - start) / (end - start)
    except Exception:
        return None


_SUMMARY_SYSTEM = """\
你是 NovelForge 的前情摘要助手。用不超过 250 字概括本章，必须覆盖三点：
① 发生了什么（关键事件）；② 谁的状态/关系变了；③ 章末悬念或情绪落点。
只输出摘要正文，不要标题、不要解释。"""

_SUMMARY_SYSTEM_EN = """\
You are zi's continuity summarizer. In no more than 180 words, objectively
summarize: (1) the chapter's key events, (2) meaningful state or relationship
changes, and (3) the ending question or emotional landing. Write natural
American English. Return only the summary, with no heading or commentary.
"""


def _run_l2(conn: sqlite3.Connection, gateway, cfg, chapter: int, draft_text: str) -> None:
    """§06.2.2 L2 场景聚合。触发=每 N 章 或 卷末；聚合当前章 + 回填本窗口缺 L2 的章。

    窗口上界为本章、下界为 chapter-N+1，每次触发处理量有界（≤N 章）。
    enable_vector 时同步建场景向量（scene_vec）。失败由调用方 _step 降级。
    """
    from ..memory.l2_aggregate import (
        aggregate_chapter_scenes, pending_l2_chapters, should_run_l2,
    )

    mcfg = cfg.memory
    n = max(1, mcfg.l2_every_n_chapters)
    if not (should_run_l2(chapter, every_n=n) or _is_volume_end(conn, chapter)):
        return
    embedder = None
    if getattr(cfg.recall, "enable_vector", False):
        from ..memory.embedding import build_embedder
        embedder = build_embedder(mcfg, dim=mcfg.embedding_dim)

    kw = dict(max_scenes=mcfg.l2_max_scenes_per_chapter, tier=mcfg.l2_tier, embedder=embedder)
    # 当前章用手头正文（可能尚未 flush 到 L0）
    aggregate_chapter_scenes(
        conn,
        gateway,
        chapter,
        draft_text,
        writing_profile=get_project_writing_profile(conn, chapter=chapter),
        **kw,
    )
    # 回填窗口内更早、尚无 L2 的章（从 L0 读回）
    window_start = max(1, chapter - n + 1)
    pending = set(pending_l2_chapters(conn, chapter))
    for ch in range(window_start, chapter):
        if ch not in pending:
            continue
        text = _load_chapter_text(conn, ch)
        if text:
            aggregate_chapter_scenes(
                conn,
                gateway,
                ch,
                text,
                writing_profile=get_project_writing_profile(conn, chapter=ch),
                **kw,
            )


def _run_l3(
    conn: sqlite3.Connection, gateway, cfg, chapter: int, draft_text: str,
    *, draft_id: str | None = None,
) -> None:
    """§06.2.3 L3 事件驱动 canon 提案。产出的提案进 staging（proposed，人审兜底）。"""
    from ..memory.l3_events import extract_canon_events

    proposals = extract_canon_events(
        conn,
        gateway,
        chapter,
        draft_text,
        tier=cfg.memory.l3_tier,
        writing_profile=get_project_writing_profile(conn, chapter=chapter),
    )
    if proposals:
        _proposals_to_candidates(
            proposals, chapter, conn, source_draft_id=draft_id
        )


def _is_volume_end(conn: sqlite3.Connection, chapter: int) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM volumes WHERE end_chapter=? LIMIT 1", (chapter,)
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _load_chapter_text(conn: sqlite3.Connection, chapter: int) -> Optional[str]:
    """从 draft_index 最新修订的 L0 文件读回该章正文（读不到返回 None）。"""
    from ..db.l0 import load_latest_draft_text

    return load_latest_draft_text(conn, chapter)


def _persist_chapter_summary(conn: sqlite3.Connection, gateway, chapter: int, draft_text: str) -> None:
    """M2-②：章摘要 + 每 5 章卷级滚动摘要。失败静默，不阻断主流程。"""
    from .llm.provider import Message
    from .llm.tiers import ModelTier

    try:
        profile = get_project_writing_profile(conn, chapter=chapter)
        is_english = profile["content_language"].startswith("en")
        resp = gateway.generate(
            ModelTier.FAST,
            [Message(
                role="user",
                content=(
                    f"Chapter {chapter} text:\n\n{draft_text[:9000]}"
                    if is_english else f"第 {chapter} 章正文：\n\n{draft_text[:6000]}"
                ),
            )],
            system=compile_system_prompt(
                _SUMMARY_SYSTEM, profile, english_system=_SUMMARY_SYSTEM_EN,
            ),
            max_tokens=400,
        )
        summary = resp.text.strip()
        if is_english:
            from ..text_metrics import count_words, language_matches
            if not summary or not language_matches(summary, profile["content_language"]):
                words = draft_text.split()
                summary = " ".join(words[: min(180, len(words))])
            elif count_words(summary) > 180:
                summary = " ".join(summary.split()[:180])
        if not summary:
            return

        vol_row = conn.execute(
            "SELECT volume_no FROM volumes"
            " WHERE start_chapter IS NOT NULL AND start_chapter<=?"
            "   AND (end_chapter IS NULL OR end_chapter>=?)"
            " ORDER BY volume_no LIMIT 1",
            (chapter, chapter),
        ).fetchone()
        volume_no = vol_row["volume_no"] if vol_row else None

        conn.execute(
            "INSERT INTO chapter_summaries(id, chapter, summary, volume_no)"
            " VALUES(?,?,?,?)"
            " ON CONFLICT(chapter) DO UPDATE SET"
            "   summary=excluded.summary, volume_no=excluded.volume_no,"
            "   created_at=datetime('now')",
            (new_id("csum"), chapter, summary, volume_no),
        )
        conn.commit()

        # 卷级 rollup：每 5 章或写到卷末时刷新本卷滚动摘要
        at_volume_end = False
        if volume_no is not None:
            vol = conn.execute(
                "SELECT end_chapter FROM volumes WHERE volume_no=?", (volume_no,)
            ).fetchone()
            at_volume_end = bool(vol and vol["end_chapter"] == chapter)
            if chapter % 5 == 0 or at_volume_end:
                _rollup_volume_summary(conn, gateway, volume_no)

        # 全局梗概：每 10 章或卷末刷新（M2-② 第三层，跨卷长程记忆的地基）
        if chapter % 10 == 0 or at_volume_end:
            _update_global_synopsis(conn, gateway, as_of_chapter=chapter)
    except Exception:
        pass


def _rollup_volume_summary(conn: sqlite3.Connection, gateway, volume_no: int) -> None:
    from .llm.provider import Message
    from .llm.tiers import ModelTier

    rows = conn.execute(
        "SELECT chapter, summary FROM chapter_summaries"
        " WHERE volume_no=? ORDER BY chapter",
        (volume_no,),
    ).fetchall()
    if not rows:
        return
    as_of_chapter = max(int(row["chapter"]) for row in rows)
    profile = get_project_writing_profile(conn, chapter=as_of_chapter)
    is_english = profile["content_language"].startswith("en")
    joined = "\n".join(
        (f"Chapter {r['chapter']}: {r['summary']}" if is_english
         else f"第{r['chapter']}章：{r['summary']}")
        for r in rows
    )
    resp = gateway.generate(
        ModelTier.FAST,
        [Message(role="user", content=(
            f"Chapter summaries for this volume:\n\n{joined[:10000]}"
            if is_english else f"以下是本卷各章摘要：\n\n{joined[:8000]}"
        ))],
        system=compile_system_prompt(
            "你是 NovelForge 的卷情节梳理助手。把各章摘要压缩成不超过 300 字的本卷剧情概要，"
            "保留主线推进、关键转折与当前悬而未决的冲突。只输出概要正文。",
            profile,
            english_system=(
                "You are zi's volume summarizer. Compress the chapter summaries into at most "
                "250 English words, retaining main-line movement, key turns, and unresolved "
                "pressures. Return only the summary."
            ),
        ),
        max_tokens=500,
    )
    summary = resp.text.strip()
    if is_english:
        from ..text_metrics import language_matches
        if not language_matches(summary, profile["content_language"]):
            summary = " ".join(joined.split()[:250])
    if summary:
        conn.execute(
            "UPDATE volumes SET rolling_summary=? WHERE volume_no=?",
            (summary, volume_no),
        )
        conn.commit()


def _update_global_synopsis(
    conn: sqlite3.Connection,
    gateway,
    *,
    as_of_chapter: Optional[int] = None,
) -> None:
    """M2-② 第三层：把各卷滚动摘要 + 近期章摘要压成 ≤400 字全书梗概，存 meta_kv。"""
    from .llm.provider import Message
    from .llm.tiers import ModelTier
    from ..db.connection import set_meta

    vol_rows = conn.execute(
        "SELECT volume_no, title, rolling_summary FROM volumes"
        " WHERE rolling_summary IS NOT NULL ORDER BY volume_no",
    ).fetchall()
    ch_rows = conn.execute(
        "SELECT chapter, summary FROM chapter_summaries ORDER BY chapter DESC LIMIT 5",
    ).fetchall()
    if as_of_chapter is None:
        chapter_values = [int(row["chapter"]) for row in ch_rows]
        if chapter_values:
            as_of_chapter = max(chapter_values)
        else:
            latest = conn.execute(
                "SELECT COALESCE(MAX(chapter), 1) FROM draft_index"
            ).fetchone()[0]
            as_of_chapter = int(latest or 1)
    profile = get_project_writing_profile(conn, chapter=as_of_chapter)
    is_english = profile["content_language"].startswith("en")
    parts = [
        (f"Volume {r['volume_no']} — {r['title']}: {r['rolling_summary']}"
         if is_english else f"第{r['volume_no']}卷《{r['title']}》：{r['rolling_summary']}")
        for r in vol_rows
    ]
    parts += [
        (f"Chapter {r['chapter']}: {r['summary']}"
         if is_english else f"第{r['chapter']}章：{r['summary']}")
        for r in reversed(ch_rows)
    ]
    if not parts:
        return
    resp = gateway.generate(
        ModelTier.FAST,
        [Message(role="user", content="\n".join(parts)[:8000])],
        system=compile_system_prompt(
            "你是 NovelForge 的全书梗概助手。把以下各卷概要与近期章节摘要压缩成"
            "不超过 400 字的全书至此梗概：主线推进、主要人物当前处境、最大的未解冲突。"
            "只输出梗概正文。",
            profile,
            english_system=(
                "You are zi's whole-story summarizer. Compress the volume summaries and recent "
                "chapters into at most 300 English words covering main-line movement, each major "
                "character's current position, and the largest unresolved pressure. Return only "
                "the synopsis."
            ),
        ),
        max_tokens=600,
    )
    synopsis = resp.text.strip()
    if is_english:
        from ..text_metrics import language_matches
        if not language_matches(synopsis, profile["content_language"]):
            synopsis = " ".join("\n".join(parts).split()[:300])
    if synopsis:
        set_meta(conn, "global_synopsis", synopsis)
        conn.commit()


def _persist_draft(
    conn: sqlite3.Connection,
    text: str,
    chapter: int,
    project_id: str,
    db_path: str = "novel.db",
    *,
    expected_base_revision_id: Optional[str] = None,
    writing_profile: Optional[dict] = None,
) -> Optional[str]:
    """原子写入 L0 草稿文件并在 draft_index 登记（F7）。

    流程：
    1. temp→fsync→rename 写 l0/ 文件
    2. INSERT draft_index 行（含 sha256）

    Returns:
        draft_id（draft_index.id），失败时返回 None。
    """
    from pathlib import Path
    from ..db.connection import index_draft_fts
    from ..db.l0 import atomic_write_l0, database_file

    final_path = None
    try:
        actual_db = database_file(conn)
        l0_dir = (actual_db.parent if actual_db is not None else Path(db_path).parent) / "l0"

        # revision 分配、文件名、draft_index 与 FTS 必须属于同一个写临界区。
        # BEGIN IMMEDIATE 会在读 MAX(revision_round) 前取得 SQLite 写锁，避免
        # 两个请求同时拿到同一 revision 后造成文件/行/FTS 身份分裂。
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")

        # Compare-and-swap must happen after acquiring the write lock.  A
        # preflight check in an API handler cannot prevent two writers from
        # accepting the same base revision concurrently.
        latest = conn.execute(
            "SELECT id FROM draft_index WHERE chapter=?"
            " ORDER BY revision_round DESC, created_at DESC LIMIT 1",
            (chapter,),
        ).fetchone()
        latest_id = latest["id"] if latest is not None else None
        if (
            expected_base_revision_id is not None
            and latest_id != expected_base_revision_id
        ):
            conn.rollback()
            raise DraftRevisionConflict(expected_base_revision_id, latest_id)

        # 查当前章最大 revision_round，递增
        row = conn.execute(
            "SELECT MAX(revision_round) AS max_r FROM draft_index WHERE chapter=?",
            (chapter,),
        ).fetchone()
        revision_round = (row["max_r"] + 1) if (row and row["max_r"] is not None) else 0

        draft_id = new_id("draft")
        filename = f"ch{chapter:04d}_r{revision_round:02d}_{draft_id[-8:]}.txt"
        relative_path = f"l0/{filename}"

        # 原子写文件
        final_path, sha256 = atomic_write_l0(l0_dir, filename, text)

        # ``word_count`` retains its historical column name, but now stores the
        # language-aware project metric (English words / Chinese characters).
        profile = writing_profile or get_project_writing_profile(
            conn, chapter=chapter,
        )
        word_count = _content_units(text, profile["content_language"])
        try:
            conn.execute(
                "INSERT INTO draft_index("
                "id, chapter, revision_round, file_path, sha256, word_count, status,"
                " content_language, writing_profile_id, writing_profile_version, prompt_version)"
                " VALUES(?,?,?,?,?,?,'draft',?,?,?,?)",
                (
                    draft_id, chapter, revision_round, relative_path, sha256, word_count,
                    profile["content_language"], profile["writing_profile_id"],
                    profile["writing_profile_version"], profile["prompt_version"],
                ),
            )
        except sqlite3.OperationalError:
            conn.execute(
                "INSERT INTO draft_index(id, chapter, revision_round, file_path, sha256, word_count, status)"
                " VALUES(?,?,?,?,?,?,'draft')",
                (draft_id, chapter, revision_round, relative_path, sha256, word_count),
            )
        # 新修订是新的当前证据源；旧稿原子保留用于审计，但不再声称是当前证据。
        try:
            conn.execute(
                "UPDATE l1_atoms SET status='superseded'"
                " WHERE chapter=? AND status IN ('active','unanchored')"
                " AND (draft_id IS NULL OR draft_id<>?)",
                (chapter, draft_id),
            )
        except sqlite3.Error:
            pass
        # 新修订会使该章旧 L2 场景/向量失效；删除派生索引，等待本次 settle
        # 或维护任务重建。L0 历史文件和 draft_index 修订记录仍完整保留。
        try:
            conn.execute(
                "DELETE FROM scene_vec WHERE scene_id IN"
                " (SELECT id FROM l2_scenes WHERE chapter=?)",
                (chapter,),
            )
            conn.execute("DELETE FROM l2_scenes WHERE chapter=?", (chapter,))
        except sqlite3.Error:
            pass
        # 章/卷/全书摘要同样是正文派生快照；换稿后宁可暂缺，也不能继续召回旧稿摘要。
        try:
            conn.execute("DELETE FROM chapter_summaries WHERE chapter=?", (chapter,))
            conn.execute(
                "UPDATE volumes SET rolling_summary=NULL"
                " WHERE start_chapter IS NOT NULL AND start_chapter<=?"
                " AND (end_chapter IS NULL OR end_chapter>=?)",
                (chapter, chapter),
            )
            conn.execute("DELETE FROM meta_kv WHERE key='global_synopsis'")
        except sqlite3.Error:
            pass
        # L0 FTS 与 draft_index 同事务提交；只索引最新修订。
        index_draft_fts(conn, draft_id, chapter, text)
        # 已选择的章节卡进入 drafted；未来卡仍保持 planned。
        conn.execute(
            "UPDATE chapter_cards SET"
            " status=CASE WHEN status='planned' THEN 'drafted' ELSE status END,"
            " draft_id=?, word_count=? WHERE chapter=?",
            (draft_id, word_count, chapter),
        )
        # The version referenced by the chapter projection is the contract that
        # produced this durable draft.  Mark only active→executed; historical
        # superseded/candidate versions remain untouched and auditable.
        try:
            conn.execute(
                "UPDATE chapter_plan_versions SET status='executed',"
                " activated_at=COALESCE(activated_at, datetime('now'))"
                " WHERE status='active' AND id=("
                " SELECT current_plan_version_id FROM chapter_cards WHERE chapter=?"
                " )",
                (chapter,),
            )
        except sqlite3.Error:
            pass
        conn.commit()
        return draft_id
    except DraftRevisionConflict:
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        # DB 未登记成功时删除本次新文件，避免产生不可追踪的 L0 孤儿。
        try:
            if final_path is not None and final_path.exists():
                final_path.unlink()
        except OSError:
            pass
        return None
