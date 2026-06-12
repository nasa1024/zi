"""Orchestrator：generate_chapter() 主循环（§07.4 / §12）。

流程：0.init → 1.RECALL+PACING → 2.PLAN → 3.DRAFT → 4.CHECK → 5.REVISE(loop) → 6.DEDUP+CONFLICT+GATE → 7.COMMIT
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..config import NovelForgeConfig
from ..contracts import FactCandidate, RunContext
from ..craft.pacing import PacingController
from ..dedup.dedup_engine import DeduplicationEngine
from ..governance.conflict import ConflictSet, classify_risk, detect_conflict
from ..governance.gate import GateOutcome, Route, apply_gate_routes
from ..governance.promotion_policy import GateDecision, PromotionPolicy
from ..ids import new_id
from ..memory.recall import gather_hard_context
from ..world.replay import get_world_state
from .budget import BudgetLedger, CircuitTripped
from .llm.gateway import LLMGateway
from .llm.tiers import ModelTier
from .skill_base import SkillContext, SkillResult
from .skill_registry import SkillRegistry, get_registry


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

    def summary(self) -> str:
        if not self.ok:
            return f"章节 {self.chapter} 失败: {self.error}"
        return (
            f"章节 {self.chapter} 完成: committed={len(self.fact_ids_committed)}"
            f" queued={len(self.candidates_queued)}"
            f" tokens={self.usage_tokens}"
        )


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
        progress_cb=None,  # Optional[Callable[[str, str, dict], None]]
    ) -> ChapterOutcome:
        cfg = self._cfg
        ledger = self._gw.ledger
        # 成本快照：落库本章净消耗（调用方复用 ledger 跨章时差值依然正确）
        _tokens0, _usd0 = ledger.tokens_spent, ledger.usd_spent

        # ── 0. init workspace + pipeline_run 状态机 ──────────────────────────
        run_id = new_id("run")
        _begin_pipeline_run(conn, chapter, cfg.project_id, run_id)
        workspace: dict = {
            "chapter_goal": chapter_goal,
        }
        skill_ctx = SkillContext(
            project_id=cfg.project_id,
            target_chapter=chapter,
            mode=cfg.governance.mode,
            as_of_chapter=chapter - 1,
            budget=ledger,
            llm=self._gw,
            conn=conn,
            workspace=workspace,
            extra={"draft_target_chars": cfg.draft_target_chars},
        )

        pacing = PacingController()

        try:
            # ── 1. RECALL + PACING ────────────────────────────────────────────
            entity_ids = entity_ids or _infer_entity_ids(conn, chapter)
            recall_pack = gather_hard_context(
                entity_ids,
                chapter - 1,
                conn,
                keyword_query=keyword_query,
                max_keywords=cfg.recall.max_keywords,
                context_window=cfg.recall.context_window_chapters,
                summary_window=getattr(cfg.recall, "summary_window_chapters", 5),
                enable_summaries=getattr(cfg.recall, "enable_summaries", True),
            )
            world_state = get_world_state(chapter - 1, conn)
            workspace["recall_pack"] = recall_pack
            workspace["world_state"] = world_state
            # M1-⑥：稳定前缀一次构建（含标题头，保证各 skill 的 user 消息从第 0 字节
            # 起完全一致），draft/check/revise 共享，吃 provider 前缀缓存
            _stable = recall_pack.to_stable_context_str()
            workspace["stable_context"] = f"## 世界设定（稳定）\n{_stable}" if _stable else ""
            workspace["dynamic_context"] = recall_pack.to_dynamic_context_str()

            # PacingController：读取节拍状态，附加建议到 chapter_goal
            pacing_state = pacing.get_state(conn)
            workspace["pacing_state"] = pacing_state
            hint = pacing.recommend_beat_hint(pacing_state)
            if hint:
                workspace["chapter_goal"] = (chapter_goal + "\n" + hint).strip()

            if progress_cb:
                progress_cb("recall", "ok", {})

            # ── 2. PLAN ───────────────────────────────────────────────────────
            plan_result = self._reg.invoke("planner", skill_ctx)
            if progress_cb:
                progress_cb("plan", "ok" if plan_result.ok else "blocked", {})
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
                    gateway=self._gw,
                    judge_tier=getattr(cfg.candidates, "judge_tier", "mid"),
                )
                winner = cand_list[report["winner"]]
                workspace["draft_text"] = winner["draft_text"]
                workspace["proposals"] = winner["proposals"]
                workspace["candidate_report"] = {
                    **report,
                    "n_candidates": n_cands,
                    "lengths": [len(c["draft_text"]) for c in cand_list],
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

            _draft_chars = len(workspace.get("draft_text", ""))
            if progress_cb:
                progress_cb("draft", "ok" if (draft_ok or workspace.get("draft_text")) else "blocked",
                            {"chars": _draft_chars})
            if not draft_ok and not workspace.get("draft_text"):
                return ChapterOutcome(
                    chapter=chapter, ok=False, run_id=run_id,
                    error="draft 失败: 所有候选均无产出",
                    usage_tokens=ledger.tokens_spent,
                )

            # ── 4. CHECK（continuity + craft 并行）────────────────────────────
            _check_continuity = self._reg.invoke("continuity_check", skill_ctx)
            _check_craft = self._reg.invoke("craft_check", skill_ctx)

            continuity_issues: list[dict] = workspace.get("continuity_issues", [])
            craft_issues: list[dict] = workspace.get("craft_issues", [])
            all_issues: list[dict] = continuity_issues + [
                {"source": "craft", **i} for i in craft_issues
            ]
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
                # 重跑两项 check
                self._reg.invoke("continuity_check", skill_ctx)
                self._reg.invoke("craft_check", skill_ctx)
                continuity_issues = workspace.get("continuity_issues", [])
                craft_issues = workspace.get("craft_issues", [])
                all_issues = continuity_issues + [
                    {"source": "craft", **i} for i in craft_issues
                ]
                hard_blocks = [i for i in all_issues if i.get("severity") == "block"]

            # ── 5b. 质量评分 + 软问题润色（M5-⑦，enabled=False 时零额外调用）──
            quality_score: Optional[float] = None
            qcfg = getattr(cfg, "quality", None)
            if qcfg is not None and qcfg.enabled and workspace.get("draft_text"):
                quality_score = self._quality_pass(
                    skill_ctx, cfg, hard_blocks, all_issues, progress_cb
                )
                continuity_issues = workspace.get("continuity_issues", [])
                craft_issues = workspace.get("craft_issues", [])
                all_issues = continuity_issues + [
                    {"source": "craft", **i} for i in craft_issues
                ]

            # ── 6. COMMIT-DRAFT（P1#11：正文先落袋，结算失败不丢章）─────────
            draft_text: str = workspace.get("draft_text", "")
            proposals: list[dict] = workspace.get("proposals", [])
            draft_id = _persist_draft(conn, draft_text, chapter, cfg.project_id, cfg.db_path)

            # ── 7. SETTLE（canon 结算 + 写回类结算；失败降级不阻塞连载）──────
            settle = self._settle_chapter(skill_ctx, conn, chapter, draft_text,
                                          proposals, pacing, progress_cb)

            run_detail = workspace.get("candidate_report")
            if workspace.get("patch_stats"):
                run_detail = {**(run_detail or {}), "patch_stats": workspace["patch_stats"]}
            if workspace.get("quality_dimensions"):
                run_detail = {**(run_detail or {}),
                              "quality_dimensions": workspace["quality_dimensions"]}
            if settle["foreshadow_settle"] is not None:
                run_detail = {**(run_detail or {}),
                              "foreshadow_settle": settle["foreshadow_settle"]}
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
                usage_tokens=ledger.tokens_spent,
                usage_usd=ledger.usd_spent,
                cache_read_tokens=getattr(ledger, "cache_read_tokens", 0),
                quality_score=quality_score,
                quality_dimensions=workspace.get("quality_dimensions"),
                state_degraded=settle["degraded"],
            )

        except Exception as e:
            return ChapterOutcome(
                chapter=chapter, ok=False,
                error=str(e),
                usage_tokens=ledger.tokens_spent,
            )

    def _settle_chapter(
        self, skill_ctx: SkillContext, conn: sqlite3.Connection, chapter: int,
        draft_text: str, proposals: list[dict], pacing: PacingController,
        progress_cb=None,
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
                     "foreshadow_settle": None}

        # ── 步骤 A：canon 结算（失败清理后重试一次）──────────────────────────
        for attempt in (1, 2):
            created: list[str] = []
            try:
                ctx = RunContext(conn=conn, policy_mode=cfg.governance.mode,
                                 actor="orchestrator")
                candidates = _proposals_to_candidates(proposals, chapter, conn)
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

        beats = workspace.get("beats", [])
        _step("pacing", lambda: pacing.update(chapter, beats, len(draft_text), conn))
        if getattr(cfg.recall, "enable_summaries", True) and draft_text:
            _step("summary", lambda: _persist_chapter_summary(
                conn, self._gw, chapter, draft_text))
        _step("foreshadow_flip", lambda: _flip_overdue_foreshadow(conn, chapter))

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
            detail = score_chapter_detailed(self._gw, judge_tier, chapter_goal,
                                            ctx.workspace.get("draft_text", ""))
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
                        self._gw, judge_tier, chapter_goal, polished)
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
        warns_str = findings_to_issues_str(craft_warns) or "- 整体打磨节奏与钩子"
        stable = ctx.workspace.get("stable_context", "")

        if getattr(self._cfg, "patch_revise", True):
            from ..craft.patch_revise import apply_patches, generate_patches
            patches = generate_patches(
                self._gw, ModelTier.MID,
                stable_context=stable, draft_text=draft_text[:8000],
                issues_str=warns_str, task_label="润色补丁任务",
            )
            if patches:
                result = apply_patches(draft_text, patches)
                _record_patch_stats(ctx.workspace, "polish", result, len(patches))
                if result.applied > 0:
                    return result.text

        from .llm.provider import CacheHint, Message
        system = ("你是 NovelForge 润色助手。在不改变情节走向、人物行为与事实设定的前提下"
                  "润色草稿，重点解决列出的工艺问题。只输出润色后的完整草稿，不要其他说明。")
        prefix = f"{stable}\n\n" if stable else ""
        resp = ctx.llm.generate(
            ModelTier.MID,
            [Message(role="user", content=(
                f"{prefix}工艺问题：\n{warns_str}\n\n当前草稿：\n{draft_text[:6000]}"
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

        # P0#2/P1#8（inkos repair_scope 路由）：结构性问题（OOC/主线偏离/时间线）
        # 补丁救不了，跳过锚点补丁直接全文重写；全 local 才走补丁。
        # M7：先尝试锚点补丁（输出短、不碰好段落）；锚定失败回退全文重写
        structural = any(i.get("repair_scope") == "structural" for i in hard_blocks)
        if not structural and getattr(self._cfg, "patch_revise", True):
            from ..craft.patch_revise import apply_patches, generate_patches
            patches = generate_patches(
                self._gw, ModelTier.STRONG,
                stable_context=stable, draft_text=draft_text[:8000],
                issues_str=issues_str, task_label="修订补丁任务",
            )
            if patches:
                result = apply_patches(draft_text, patches)
                _record_patch_stats(ctx.workspace, "revise", result, len(patches))
                if result.applied > 0:
                    return {"draft_text": result.text}

        from .llm.provider import CacheHint, Message
        system = "你是 NovelForge 修订助手。根据以下一致性问题修改草稿。只输出修改后的完整草稿，不要其他说明。"
        prefix = f"{stable}\n\n" if stable else ""
        user_msg = (
            f"{prefix}"
            f"一致性问题：\n{issues_str}\n\n"
            f"当前草稿：\n{draft_text[:4000]}"
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


def _proposals_to_candidates(
    proposals: list[dict], chapter: int, conn: sqlite3.Connection
) -> list[FactCandidate]:
    candidates = []
    for p in proposals:
        cid = new_id("cand")
        fact_type = p.get("fact_type", "unknown")
        entity = p.get("entity")
        entity_id = _resolve_entity(entity, conn) if entity else None
        risk_tier = p.get("risk_tier", "low")
        proposal_json = json.dumps(p, ensure_ascii=False)
        try:
            op = p.get("op", "add")
            conn.execute(
                "INSERT OR IGNORE INTO fact_candidates"
                "(candidate_id, op, entity_id, fact_type, proposal_json, status, risk_tier, source_chapter)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (cid, op, entity_id, fact_type, proposal_json, "proposed", risk_tier, chapter),
            )
            conn.commit()
        except Exception:
            pass
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


def _begin_pipeline_run(conn: sqlite3.Connection, chapter: int, project_id: str, run_id: str) -> None:
    """在 pipeline_run 表插入 'running' 行（F6 状态机开始）。"""
    try:
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


def _persist_chapter_summary(conn: sqlite3.Connection, gateway, chapter: int, draft_text: str) -> None:
    """M2-②：章摘要 + 每 5 章卷级滚动摘要。失败静默，不阻断主流程。"""
    from .llm.provider import Message
    from .llm.tiers import ModelTier

    try:
        resp = gateway.generate(
            ModelTier.FAST,
            [Message(role="user", content=f"第 {chapter} 章正文：\n\n{draft_text[:6000]}")],
            system=_SUMMARY_SYSTEM,
            max_tokens=400,
        )
        summary = resp.text.strip()
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
            _update_global_synopsis(conn, gateway)
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
    joined = "\n".join(f"第{r['chapter']}章：{r['summary']}" for r in rows)
    resp = gateway.generate(
        ModelTier.FAST,
        [Message(role="user", content=f"以下是本卷各章摘要：\n\n{joined[:8000]}")],
        system="你是 NovelForge 的卷情节梳理助手。把各章摘要压缩成不超过 300 字的本卷剧情概要，"
               "保留主线推进、关键转折与当前悬而未决的冲突。只输出概要正文。",
        max_tokens=500,
    )
    summary = resp.text.strip()
    if summary:
        conn.execute(
            "UPDATE volumes SET rolling_summary=? WHERE volume_no=?",
            (summary, volume_no),
        )
        conn.commit()


def _update_global_synopsis(conn: sqlite3.Connection, gateway) -> None:
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
    parts = [f"第{r['volume_no']}卷《{r['title']}》：{r['rolling_summary']}" for r in vol_rows]
    parts += [f"第{r['chapter']}章：{r['summary']}" for r in reversed(ch_rows)]
    if not parts:
        return
    resp = gateway.generate(
        ModelTier.FAST,
        [Message(role="user", content="\n".join(parts)[:8000])],
        system="你是 NovelForge 的全书梗概助手。把以下各卷概要与近期章节摘要压缩成"
               "不超过 400 字的全书至此梗概：主线推进、主要人物当前处境、最大的未解冲突。"
               "只输出梗概正文。",
        max_tokens=600,
    )
    synopsis = resp.text.strip()
    if synopsis:
        set_meta(conn, "global_synopsis", synopsis)
        conn.commit()


def _persist_draft(
    conn: sqlite3.Connection, text: str, chapter: int, project_id: str, db_path: str = "novel.db"
) -> Optional[str]:
    """原子写入 L0 草稿文件并在 draft_index 登记（F7）。

    流程：
    1. temp→fsync→rename 写 l0/ 文件
    2. INSERT draft_index 行（含 sha256）

    Returns:
        draft_id（draft_index.id），失败时返回 None。
    """
    from pathlib import Path
    from ..db.l0 import atomic_write_l0

    try:
        l0_dir = Path(db_path).parent / "l0"

        # 查当前章最大 revision_round，递增
        row = conn.execute(
            "SELECT MAX(revision_round) AS max_r FROM draft_index WHERE chapter=?",
            (chapter,),
        ).fetchone()
        revision_round = (row["max_r"] + 1) if (row and row["max_r"] is not None) else 0

        filename = f"ch{chapter:04d}_r{revision_round:02d}.txt"
        relative_path = f"l0/{filename}"

        # 原子写文件
        _, sha256 = atomic_write_l0(l0_dir, filename, text)

        # 写 draft_index
        draft_id = new_id("draft")
        word_count = len(text)
        conn.execute(
            "INSERT INTO draft_index(id, chapter, revision_round, file_path, sha256, word_count, status)"
            " VALUES(?,?,?,?,?,?,'draft')"
            " ON CONFLICT(chapter, revision_round) DO UPDATE SET"
            "   file_path=excluded.file_path, sha256=excluded.sha256,"
            "   word_count=excluded.word_count, status='draft'",
            (draft_id, chapter, revision_round, relative_path, sha256, word_count),
        )
        conn.commit()
        return draft_id
    except Exception:
        return None
