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
from .budget import BudgetLedger
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
        ctx = RunContext(conn=conn, policy_mode=cfg.governance.mode, actor="orchestrator")

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

            # ── 3. DRAFT ──────────────────────────────────────────────────────
            draft_result = self._reg.invoke("chapter_draft", skill_ctx)
            _draft_chars = len(workspace.get("draft_text", ""))
            if progress_cb:
                progress_cb("draft", "ok" if (draft_result.ok or workspace.get("draft_text")) else "blocked",
                            {"chars": _draft_chars})
            if not draft_result.ok and not workspace.get("draft_text"):
                return ChapterOutcome(
                    chapter=chapter, ok=False, run_id=run_id,
                    error=f"draft 失败: {draft_result.error}",
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
            hard_blocks = [i for i in all_issues if i.get("severity") == "block"]
            for _iter in range(cfg.max_revise_loops):
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

            # ── 6. DEDUP + CONFLICT + GATE ────────────────────────────────────
            draft_text: str = workspace.get("draft_text", "")
            proposals: list[dict] = workspace.get("proposals", [])
            candidates = _proposals_to_candidates(proposals, chapter, conn)

            # 近邻去重
            dedup_gw = self._gw if cfg.dedup.enable_llm_arbiter else None
            dedup_engine = DeduplicationEngine(
                bm25_gap_min=cfg.dedup.bm25_gap_min,
                llm_gateway=dedup_gw,
            )
            candidates = _apply_dedup(candidates, dedup_engine, conn)

            # 冲突检测 + 风险重分类
            conflict_map: dict[str, ConflictSet] = {}
            for cand in candidates:
                cset = detect_conflict(cand, conn)
                cand.risk_tier = classify_risk(cand, cfg)
                if cset.has_block:
                    conflict_map[cand.candidate_id] = cset
                    # 更新 DB 中的 risk_tier
                    try:
                        conn.execute(
                            "UPDATE fact_candidates SET risk_tier=? WHERE candidate_id=?",
                            (cand.risk_tier, cand.candidate_id),
                        )
                    except Exception:
                        pass
            conn.commit()

            world = workspace.get("world_state")
            gate_decision: GateDecision = PromotionPolicy.decide_batch(
                candidates, world, cfg, conflict_map=conflict_map
            )
            gate_outcome: GateOutcome = apply_gate_routes(ctx, gate_decision, {"chapter": chapter})

            # ── 7. COMMIT（持久化草稿 + 更新节拍游标）────────────────────────
            draft_id = _persist_draft(conn, draft_text, chapter, cfg.project_id, cfg.db_path)
            _complete_pipeline_run(conn, run_id, draft_id)
            beats = workspace.get("beats", [])
            pacing.update(chapter, beats, len(draft_text), conn)

            committed_ids = [fid for _, fid in gate_outcome.committed]
            if progress_cb:
                progress_cb("gate", "ok", {"committed": len(committed_ids), "queued": len(gate_outcome.queued)})
            return ChapterOutcome(
                chapter=chapter,
                ok=True,
                run_id=run_id,
                draft_text=draft_text,
                fact_ids_committed=committed_ids,
                candidates_queued=gate_outcome.queued,
                issues=all_issues,
                gate=gate_outcome,
                usage_tokens=ledger.tokens_spent,
                usage_usd=ledger.usd_spent,
                cache_read_tokens=getattr(ledger, "cache_read_tokens", 0),
            )

        except Exception as e:
            return ChapterOutcome(
                chapter=chapter, ok=False,
                error=str(e),
                usage_tokens=ledger.tokens_spent,
            )

    def _revise(self, ctx: SkillContext, hard_blocks: list[dict]) -> dict:
        draft_text: str = ctx.workspace.get("draft_text", "")
        issues_str = "\n".join(f"- {i.get('desc', i)}" for i in hard_blocks)

        from .llm.provider import Message
        system = "你是 NovelForge 修订助手。根据以下一致性问题修改草稿。只输出修改后的完整草稿，不要其他说明。"
        stable = ctx.workspace.get("stable_context", "")
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


def _complete_pipeline_run(conn: sqlite3.Connection, run_id: str, draft_id: Optional[str]) -> None:
    """将 pipeline_run 行更新为 'completed'（F6 状态机结束）。"""
    try:
        conn.execute(
            "UPDATE pipeline_run SET status='completed', draft_id=?, finished_at=datetime('now')"
            " WHERE run_id=?",
            (draft_id, run_id),
        )
        conn.commit()
    except Exception:
        pass


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
