"""多章连写编排（v22/F6）：一次长输出调用起草连续多章，再逐章走完整流水线。

产品方向："不是一章一章写，而是尽可能多写几章"。长输出模型（Gemini 4 long decode，
``CapabilitySet.long_output``）一次写出 N 章；每章随后**按顺序**调用
``Orchestrator.generate_chapter(predrafted_text=…)``，所以 as-of 召回、确定性
连续性/工艺/计划校验、修订、质量门控、落库、actuals、结算依旧逐章生效。

研究约束（docs/NovelForge/23 §五，Book-Writing：计划遵循率 73.1%→50.8%）：
- 每章在连写 prompt 中各自携带完整契约 + 目标 + 字数 + Beat Sheet；
- 输出按章节标记切分并逐章校验；
- 某章失败（未落库）→ 丢弃其后的依赖章、从该章重写；某章落库但仍有 block →
  保留该章、丢弃其后、从下一章重写；连续无进展的重写次数受 ``cfg.batch.max_regenerations``
  限制（窗口接受 ≥1 章即清零）。

未处理/被丢弃的正文只留在 ``draft_batch_chapters``，从不进入 ``draft_index``，
因此 ``next_chapter_no`` 与 autopilot 断点续写保持正确。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..craft.extensions import build_draft_briefs
from ..craft.pacing import PacingController
from ..ids import new_id
from ..planning.context import gather_plan_context
from ..skills.chapter_batch_draft_skill import (
    per_chapter_output_tokens,
    provider_capabilities,
)
from ..world.replay import get_world_state
from ..writing_profiles import get_project_writing_profile
from .budget import CircuitTripped
from .llm.tiers import ModelTier
from .orchestrator import (
    ChapterOutcome,
    Orchestrator,
    _profile_pacing_hint,
    compose_stable_context,
    gather_recall_pack,
)
from .skill_base import SkillContext

logger = logging.getLogger(__name__)

# make_orchestrator(budget_scale=1) -> Orchestrator：每次返回带**全新** gateway/ledger 的
# 编排器。budget_scale=N 时单章预算上限 ×N（供一次起草 N 章的连写调用；熔断器按
# max_tokens//2 预留，单章额度不够）。
OrchestratorFactory = Callable[..., Orchestrator]
ProgressCallback = Callable[[dict], None]


@dataclass
class BatchOutcome:
    batch_id: Optional[str]                 # 首个 draft_batches 行（顺序模式为 None）
    mode: str                               # "batch" | "sequential"
    requested: list[int]
    accepted: list[ChapterOutcome] = field(default_factory=list)
    rejected: list[ChapterOutcome] = field(default_factory=list)
    outcomes: list[ChapterOutcome] = field(default_factory=list)   # 处理顺序（含失败章）
    discarded: list[dict] = field(default_factory=list)  # {chapter, batch_id, reason}
    regenerations: int = 0
    status: str = "completed"               # completed | partial | failed | canceled
    error: Optional[str] = None
    # circuit_broken | exception | regenerations | chapter_failed（顺序模式）
    error_kind: Optional[str] = None
    batch_ids: list[str] = field(default_factory=list)
    chapter_batch: dict[int, str] = field(default_factory=dict)   # chapter -> draft batch id
    batch_tokens: int = 0                   # 全部连写调用（含补规划）消耗
    batch_usd: float = 0.0
    capacity: int = 1
    # batch_* 中未能摊入任何接受章的部分（整窗无接受章时）；usage_* 总成本含此项
    unapportioned_tokens: int = field(default=0, repr=False)
    unapportioned_usd: float = field(default=0.0, repr=False)

    @property
    def next_chapter(self) -> int:
        """第一个未被接受的请求章（全部完成时为末章 +1）。"""
        done = {o.chapter for o in self.accepted}
        for chapter in self.requested:
            if chapter not in done:
                return chapter
        return (self.requested[-1] + 1) if self.requested else 1

    @property
    def usage_tokens(self) -> int:
        return sum(o.usage_tokens for o in self.outcomes) + self.unapportioned_tokens

    @property
    def usage_usd(self) -> float:
        return sum(o.usage_usd for o in self.outcomes) + self.unapportioned_usd

    def chapter_summary(self, outcome: ChapterOutcome) -> dict:
        return chapter_summary(outcome, batch_id=self.chapter_batch.get(outcome.chapter))

    def to_summary(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "batch_ids": list(self.batch_ids),
            "mode": self.mode,
            "status": self.status,
            "requested": list(self.requested),
            "chapters": [self.chapter_summary(o) for o in self.outcomes],
            "discarded": [dict(d) for d in self.discarded],
            "regenerations": self.regenerations,
            "next_chapter": self.next_chapter,
            "tokens": self.usage_tokens,
            "usd": round(self.usage_usd, 6),
            "batch_tokens": self.batch_tokens,
            "batch_usd": round(self.batch_usd, 6),
            "capacity": self.capacity,
            "error": self.error,
        }


def chapter_summary(outcome: ChapterOutcome, *, batch_id: Optional[str] = None) -> dict:
    blocks = [i for i in (outcome.issues or []) if i.get("severity") == "block"]
    return {
        "chapter": outcome.chapter,
        "ok": outcome.ok,
        "run_id": outcome.run_id or None,
        "batch_id": batch_id,
        "issues": len(outcome.issues or []),
        "blocks": len(blocks),
        "quality_score": outcome.quality_score,
        "tokens": outcome.usage_tokens,
        "usd": round(outcome.usage_usd, 6),
        "state_degraded": bool(outcome.state_degraded),
        "error": outcome.error,
    }


# ── 资格判定 ──────────────────────────────────────────────────────────────────

def batch_capacity(gateway: Any, cfg: Any, profile: dict) -> int:
    """一次连写调用最多起草的章数；1 = 走单章流程。

    关闭 / gateway 缺失或不可探测 / STRONG 模型未声明 ``long_output``（且
    ``require_long_output``）→ 1。否则 ``min(max_chapters, max_tokens_out // 单章预算)``。
    """
    bcfg = getattr(cfg, "batch", None)
    if bcfg is None or not getattr(bcfg, "enabled", False):
        return 1
    caps = provider_capabilities(gateway)
    if caps is None:
        return 1
    if getattr(bcfg, "require_long_output", True) and not getattr(caps, "long_output", False):
        return 1
    per_chapter = per_chapter_output_tokens(
        profile, int(getattr(cfg, "draft_target_chars", 3000) or 3000),
    )
    try:
        by_tokens = int(caps.max_tokens_out) // max(1, per_chapter)
    except (TypeError, ValueError):
        return 1
    return max(1, min(int(bcfg.max_chapters), by_tokens))


def batch_threshold(cfg: Any) -> int:
    """进入连写模式所需的最小容量（至少 2：单章连写没有意义）。"""
    return max(2, int(getattr(getattr(cfg, "batch", None), "min_chapters", 2) or 2))


# ── draft_batches 持久化（老库缺表时静默降级为纯内存）─────────────────────────

def _db(conn: sqlite3.Connection, sql: str, params: tuple) -> None:
    try:
        conn.execute(sql, params)
        conn.commit()
    except sqlite3.Error:
        logger.exception("draft batch bookkeeping failed: %s", sql.split()[0:3])
        try:
            conn.rollback()
        except sqlite3.Error:
            pass


def _insert_batch(conn, batch_id: str, window: list[int], *, origin: str,
                  model_id: Optional[str], detail: dict) -> None:
    _db(conn,
        "INSERT INTO draft_batches(id, start_chapter, end_chapter, status, origin,"
        " model_id, detail_json) VALUES(?, ?, ?, 'running', ?, ?, ?)",
        (batch_id, window[0], window[-1], origin, model_id,
         json.dumps(detail, ensure_ascii=False)))


def _insert_batch_chapter(conn, batch_id: str, chapter: int, seq: int, text: str,
                          status: str, reason: Optional[str]) -> None:
    _db(conn,
        "INSERT OR REPLACE INTO draft_batch_chapters(id, batch_id, chapter, seq,"
        " draft_text, status, reason) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (new_id("bch"), batch_id, chapter, seq, text, status, reason))


def _mark_batch_chapter(conn, batch_id: str, chapter: int, status: str, *,
                        run_id: Optional[str] = None, reason: Optional[str] = None) -> None:
    _db(conn,
        "UPDATE draft_batch_chapters SET status=?, run_id=COALESCE(?, run_id),"
        " reason=COALESCE(?, reason), updated_at=datetime('now')"
        " WHERE batch_id=? AND chapter=?",
        (status, run_id, reason, batch_id, chapter))


def _finalize_batch(conn, batch_id: str, status: str, *, tokens: int, usd: float,
                    detail: dict) -> None:
    _db(conn,
        "UPDATE draft_batches SET status=?, tokens_spent=?, usd_spent=?, detail_json=?,"
        " updated_at=datetime('now') WHERE id=?",
        (status, int(tokens), round(float(usd), 6),
         json.dumps(detail, ensure_ascii=False), batch_id))


# ── 起草一个窗口 ─────────────────────────────────────────────────────────────

@dataclass
class _WindowDraft:
    drafts: dict[int, str]
    rejects: dict[int, str]
    beats: dict[int, list[dict]]
    tokens: int
    usd: float


def _resolve_goal(conn, chapter: int, chapter_goals: Optional[dict]) -> str:
    if chapter_goals:
        goal = chapter_goals.get(str(chapter)) or chapter_goals.get(chapter)
        if goal:
            return str(goal)
    try:
        from ..app.chapter_suggest import assemble_chapter_goal
        return assemble_chapter_goal(conn, chapter)[0]
    except Exception:
        return ""


def _skill_ctx(orch: Orchestrator, conn, chapter: int, workspace: dict, profile: dict,
               *, as_of_chapter: int) -> SkillContext:
    cfg = orch.config
    quality_rules = profile.get("quality_rules") or {}
    target_range = quality_rules.get("chapter_word_range") or [1500, 2500]
    return SkillContext(
        project_id=cfg.project_id,
        target_chapter=chapter,
        mode=cfg.governance.mode,
        as_of_chapter=as_of_chapter,
        budget=orch.gateway.ledger,
        llm=orch.gateway,
        conn=conn,
        workspace=workspace,
        extra={
            "draft_target_chars": cfg.draft_target_chars,
            "draft_target_words": int((int(target_range[0]) + int(target_range[1])) / 2),
            "writing_profile": profile,
        },
    )


_BRIEF_NUMBER_RE = re.compile(r"\d+")


def _brief_key(line: str) -> str:
    """去重键：数字归一（F1"已 N 章未提及"、短线"约 N 字"在窗口内逐章递增，仍是同一条提醒）。"""
    return _BRIEF_NUMBER_RE.sub("#", line.strip())


def _brief_lines(brief: str) -> set[str]:
    return {_brief_key(line) for line in str(brief or "").splitlines()
            if line.strip() and not line.lstrip().startswith("#")}


def _fresh_brief(brief: str, seen: set[str]) -> str:
    """窗口内后续章的 brief 只保留此前各章 brief 未出现过的条目（同一条只说一次，如窗口
    开始前就欠下的爽点反应、到期承诺提醒——仅数字不同视为同一条）；整块无新条目时连标题一起省略。"""
    blocks: list[str] = []
    for block in str(brief or "").split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip()]
        head = lines[:1] if lines and lines[0].lstrip().startswith("#") else []
        body = lines[len(head):]
        fresh = [line for line in body if _brief_key(line) not in seen]
        seen.update(_brief_key(line) for line in body)
        if fresh:
            blocks.append("\n".join(head + fresh))
    return "\n\n".join(blocks)


def _draft_window(conn, window: list[int], goals: dict[int, str], *,
                  orch: Orchestrator, profile: dict) -> _WindowDraft:
    """为窗口内各章准备契约/节拍并发起一次连写调用（起始章上下文 as_of(start-1)）。"""
    cfg = orch.config
    ledger = orch.gateway.ledger
    tokens0, usd0 = ledger.tokens_spent, ledger.usd_spent
    start = window[0]
    language = str(profile.get("content_language") or "zh-CN")

    # 起始章上下文：与 generate_chapter 的 RECALL 段同源（稳定前缀逐字节一致）
    recall_pack = gather_recall_pack(cfg, conn, start, chapter_goal=goals[start])
    start_plan = gather_plan_context(conn, start, lookahead_chapters=5)
    pacing = PacingController()
    pacing_state = pacing.get_state(   # 与 generate_chapter 的 RECALL 段同口径（F3 阈值）
        conn, max_low_streak=getattr(getattr(cfg, "polarity", None), "max_low_streak", None),
    )
    hint = _profile_pacing_hint(profile, pacing, pacing_state)
    start_goal = (goals[start] + "\n" + hint).strip() if hint else goals[start]
    base_ws: dict = {
        "chapter_goal": start_goal,
        "writing_profile": profile,
        "profile_cache_key": profile.get("cache_key"),
        "recall_pack": recall_pack,
        "world_state": get_world_state(start - 1, conn),
        "stable_context": compose_stable_context(profile, recall_pack),
        "dynamic_context": recall_pack.to_dynamic_context_str(language),
        "plan_context": start_plan,
        "future_plan_context": start_plan.render_for_planner(language),
        "future_guardrails": start_plan.render_guardrails_for_draft(language),
        "chapter_contract_context": start_plan.render_contract_for_draft(language),
        "pacing_state": pacing_state,
    }
    base_ws["craft_brief"] = build_draft_briefs(
        conn, start, base_ws, language=language, cfg=cfg,
    )

    specs: list[dict] = []
    beats_by_chapter: dict[int, list[dict]] = {}
    seen_brief_lines = _brief_lines(base_ws["craft_brief"])
    for chapter in window:
        plan = start_plan if chapter == start else gather_plan_context(
            conn, chapter, lookahead_chapters=5,
        )
        goal = start_goal if chapter == start else goals[chapter]
        contract_text = plan.render_contract_for_draft(language)
        guardrails = plan.render_guardrails_for_draft(language)
        contract = contract_text or guardrails
        if chapter == start:
            brief, section_brief = base_ws["craft_brief"], ""   # 起始章 brief 已在 prompt 顶部
        else:
            # 逐章动态提示（F1 到期承诺提醒 / F3 上一章计划高潮后的余韵 / F5 爽点三段式…）：
            # 纯 DB 读、确定性，账本为 as_of(起始章-1) 的近似（与各章契约同口径）。
            # 否则窗口内后续章永远看不到"本章该提醒的承诺"。同窗口内已出现过的条目只说一次。
            brief = build_draft_briefs(
                conn, chapter, {
                    **base_ws,
                    "chapter_goal": goal,
                    "plan_context": plan,
                    "future_plan_context": plan.render_for_planner(language),
                    "future_guardrails": guardrails,
                    "chapter_contract_context": contract_text,
                },
                language=language, cfg=cfg,
            )
            section_brief = _fresh_brief(brief, seen_brief_lines)
        beats = [dict(b) for b in plan.selected_beats]
        if not beats:
            # 无已选/已计划节拍：内存内跑一次 planner（as_of 起始章的近似，见设计说明）
            plan_ws = {
                "chapter_goal": goal,
                "writing_profile": profile,
                "recall_pack": recall_pack,
                "stable_context": base_ws["stable_context"],
                "dynamic_context": base_ws["dynamic_context"],
                "future_plan_context": plan.render_for_planner(language),
                "craft_brief": brief,
            }
            result = orch.registry.invoke(
                "planner",
                _skill_ctx(orch, conn, chapter, plan_ws, profile, as_of_chapter=start - 1),
            )
            beats = list(plan_ws.get("beats") or []) if result.ok else []
        beats_by_chapter[chapter] = beats
        specs.append({
            "chapter": chapter,
            "goal": goal,
            "contract": contract,
            "brief": section_brief,
            "beats": beats,
        })

    ws = {**base_ws, "batch_chapters": specs}
    result = orch.registry.invoke(
        "chapter_batch_draft",
        _skill_ctx(orch, conn, start, ws, profile, as_of_chapter=start - 1),
    )
    if "batch_drafts" not in ws:
        # skill 未注册/未产出（不同于"产出了但切分失败"）：致命，重写无意义
        raise RuntimeError(result.error or "chapter_batch_draft produced no output")
    return _WindowDraft(
        drafts=dict(ws.get("batch_drafts") or {}),
        rejects=dict(ws.get("batch_rejects") or {}),
        beats=beats_by_chapter,
        tokens=ledger.tokens_spent - tokens0,
        usd=ledger.usd_spent - usd0,
    )


def _apportion(conn, outcomes: list[ChapterOutcome], tokens: int, usd: float) -> None:
    """把一次连写调用的成本平摊到本窗口接受的各章（ChapterOutcome + pipeline_run）。"""
    if not outcomes:
        return
    n = len(outcomes)
    base, remainder = divmod(max(0, int(tokens)), n)
    share_usd = max(0.0, float(usd)) / n
    for i, outcome in enumerate(outcomes):
        share = base + (1 if i < remainder else 0)
        outcome.usage_tokens += share
        outcome.usage_usd += share_usd
        if outcome.run_id:
            _db(conn,
                "UPDATE pipeline_run SET tokens_spent=COALESCE(tokens_spent, 0)+?,"
                " usd_spent=ROUND(COALESCE(usd_spent, 0)+?, 6) WHERE run_id=?",
                (share, share_usd, outcome.run_id))


# ── 主入口 ────────────────────────────────────────────────────────────────────

def run_batch(
    conn: sqlite3.Connection,
    *,
    start_chapter: int,
    count: int,
    make_orchestrator: OrchestratorFactory,
    chapter_goals: Optional[dict] = None,
    origin: str = "api",
    progress_cb: Optional[ProgressCallback] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    interaction_mode: Optional[str] = None,
    chapter_cb: Optional[Callable[[int, ChapterOutcome], None]] = None,
    batch_size: Optional[int] = None,
) -> BatchOutcome:
    """连续写 ``start_chapter .. start_chapter+count-1``。

    ``progress_cb(event)`` 收到可直接 JSON 序列化的事件：``batch_start``、
    ``batch_draft``（每次连写调用后，含该次成本）、``stage``（带 ``chapter``）、
    ``chapter_done``、``batch_discarded``、``batch_regenerate``、``batch_done``。
    ``chapter_cb(chapter, outcome)`` 在每章处理完后同步调用（此时 outcome 只含本章
    流水线成本，连写调用成本在窗口结束时才摊入）。``cancel_cb()`` 在每章与每次
    连写调用之前检查，返回 True 即干净停止。``batch_size`` 进一步限制单次连写章数。
    """
    requested = list(range(int(start_chapter), int(start_chapter) + max(0, int(count))))

    def emit(event: dict) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(event)
        except Exception:
            logger.exception("batch progress callback failed")

    def canceled() -> bool:
        if cancel_cb is None:
            return False
        try:
            return bool(cancel_cb())
        except Exception:
            logger.exception("batch cancel callback failed")
            return False

    def stage_cb(chapter: int):
        def _cb(stage: str, status: str, detail: dict) -> None:
            emit({"event": "stage", "chapter": chapter, "stage": stage,
                  "status": status, "detail": detail})
        return _cb

    probe = make_orchestrator()
    cfg = probe.config
    if not requested:
        out = BatchOutcome(batch_id=None, mode="sequential", requested=[], status="completed")
        emit({"event": "batch_done", **out.to_summary()})
        return out
    profile = get_project_writing_profile(conn, chapter=requested[0])
    capacity = batch_capacity(probe.gateway, cfg, profile)
    if batch_size:
        capacity = min(capacity, max(1, int(batch_size)))
    mode = "batch" if capacity >= batch_threshold(cfg) else "sequential"
    out = BatchOutcome(batch_id=None, mode=mode, requested=requested, capacity=capacity)

    if mode == "sequential":
        _run_sequential(conn, out, make_orchestrator, chapter_goals, interaction_mode,
                        emit, canceled, stage_cb, chapter_cb, first_orch=probe)
    else:
        _run_windows(conn, out, make_orchestrator, chapter_goals, interaction_mode, origin,
                     emit, canceled, stage_cb, chapter_cb,
                     max_regenerations=max(0, int(cfg.batch.max_regenerations)))

    accepted = {o.chapter for o in out.accepted}
    if out.status != "canceled":
        if accepted and len(accepted) == len(requested):
            out.status = "completed"
        elif accepted:
            out.status = "partial"
        else:
            out.status = "failed"
    emit({"event": "batch_done", **out.to_summary()})
    return out


def _record(out: BatchOutcome, outcome: ChapterOutcome, batch_id: Optional[str]) -> None:
    out.outcomes.append(outcome)
    (out.accepted if outcome.ok else out.rejected).append(outcome)
    if batch_id:
        out.chapter_batch[outcome.chapter] = batch_id


def _run_sequential(conn, out: BatchOutcome, make_orchestrator, chapter_goals,
                    interaction_mode, emit, canceled, stage_cb, chapter_cb,
                    *, first_orch: Orchestrator) -> None:
    """非长输出模型：逐章调用 generate_chapter（依旧"一次写多章"，只是每章一次调用）。"""
    emit({"event": "batch_start", "batch_id": None, "mode": "sequential",
          "chapters": list(out.requested), "capacity": out.capacity})
    orch: Optional[Orchestrator] = first_orch
    for chapter in out.requested:
        if canceled():
            out.status = "canceled"
            return
        orch = orch or make_orchestrator()
        try:
            outcome = orch.generate_chapter(
                chapter, conn,
                chapter_goal=_resolve_goal(conn, chapter, chapter_goals),
                interaction_mode=interaction_mode,
                progress_cb=stage_cb(chapter),
            )
        except Exception as exc:   # generate_chapter 自身兜底；这里只防编排器构造/回调异常
            out.error, out.error_kind = str(exc), _error_kind(exc)
            return
        orch = None   # 每章全新 gateway/ledger（防 revise_rounds 累计熔断与预算串章）
        _record(out, outcome, None)
        emit({"event": "chapter_done", **out.chapter_summary(outcome)})
        callback_error = _notify(chapter_cb, chapter, outcome)
        if callback_error is not None:
            out.error, out.error_kind = callback_error, "exception"
            return
        if not outcome.ok:
            out.error = outcome.error or f"chapter {chapter} failed"
            out.error_kind = "chapter_failed"
            return


def _notify(chapter_cb, chapter: int, outcome: ChapterOutcome) -> Optional[str]:
    """调用方逐章回调；异常不得让 draft_batches 行停在 running → 转为致命停止。"""
    if chapter_cb is None:
        return None
    try:
        chapter_cb(chapter, outcome)
    except Exception as exc:
        logger.exception("batch chapter callback failed")
        return f"chapter callback failed: {exc}"
    return None


def _draft_rowid(conn) -> Optional[int]:
    try:
        return int(conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM draft_index").fetchone()[0])
    except sqlite3.Error:
        return None


def _persisted_since(conn, chapter: int, mark: Optional[int]) -> bool:
    """``mark``（处理本章前的 draft_index 最大 rowid）之后本章是否已有新修订落库。"""
    if mark is None:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM draft_index WHERE chapter=? AND rowid>? LIMIT 1", (chapter, mark),
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _error_kind(exc: BaseException) -> str:
    return "circuit_broken" if isinstance(exc, CircuitTripped) else "exception"


def _run_windows(conn, out: BatchOutcome, make_orchestrator, chapter_goals,
                 interaction_mode, origin, emit, canceled, stage_cb, chapter_cb,
                 *, max_regenerations: int) -> None:
    end = out.requested[-1]
    next_chapter = out.requested[0]
    kind = "initial"
    # 上限按"连续无进展的重写"计：窗口接受 ≥1 章即清零。挂机把整段会话当作一次 run，
    # 若按全程累计，长会话里第 N+1 次互不相干的偶发失败（某章过短/某章残留 block）
    # 就会让整个会话报错停止；out.regenerations 仍报告全程总次数。
    regen_streak = 0
    while next_chapter <= end:
        if canceled():
            out.status = "canceled"
            return
        if kind == "regenerate":
            out.regenerations += 1          # 只有真正发起重写才计数（取消不算）
            regen_streak += 1
        profile = get_project_writing_profile(conn, chapter=next_chapter)
        window = [next_chapter]
        for chapter in range(next_chapter + 1, min(end, next_chapter + out.capacity - 1) + 1):
            # 写作配置在窗口内切换（effective_from_chapter）→ 窗口在切换前截断
            if (get_project_writing_profile(conn, chapter=chapter).get("cache_key")
                    != profile.get("cache_key")):
                break
            window.append(chapter)

        orch = make_orchestrator(budget_scale=len(window))
        try:
            model_id = orch.gateway.model_for(ModelTier.STRONG)
        except Exception:
            model_id = None
        batch_id = new_id("batch")
        out.batch_ids.append(batch_id)
        if out.batch_id is None:
            out.batch_id = batch_id
        detail: dict = {"root_batch_id": out.batch_id, "kind": kind,
                        "attempt": out.regenerations, "requested": window}
        _insert_batch(conn, batch_id, window, origin=origin, model_id=model_id, detail=detail)
        if kind == "initial":
            emit({"event": "batch_start", "batch_id": batch_id, "mode": "batch",
                  "chapters": list(out.requested), "capacity": out.capacity})
        elif kind == "regenerate":
            emit({"event": "batch_regenerate", "batch_id": batch_id,
                  "from_chapter": window[0], "attempt": out.regenerations})

        goals = {chapter: _resolve_goal(conn, chapter, chapter_goals) for chapter in window}
        try:
            draft = _draft_window(conn, window, goals, orch=orch, profile=profile)
        except Exception as exc:
            spent_tokens = orch.gateway.ledger.tokens_spent
            spent_usd = orch.gateway.ledger.usd_spent
            out.batch_tokens += spent_tokens
            out.batch_usd += spent_usd
            out.unapportioned_tokens += spent_tokens
            out.unapportioned_usd += spent_usd
            out.error, out.error_kind = str(exc), _error_kind(exc)
            _finalize_batch(conn, batch_id, "failed", tokens=spent_tokens, usd=spent_usd,
                            detail={**detail, "error": str(exc)[:500]})
            # 失败调用同样已花钱：照常通报成本（autopilot 据此累计会话预算）
            emit({"event": "batch_draft", "batch_id": batch_id, "from_chapter": window[0],
                  "to_chapter": window[-1], "parsed": [], "rejected": {},
                  "tokens": spent_tokens, "usd": spent_usd, "error": str(exc)[:500]})
            return
        out.batch_tokens += draft.tokens
        out.batch_usd += draft.usd

        # 可用前缀 = 从窗口首章起连续通过校验的章；第一个缺口章 rejected，其后全部作废
        usable: list[int] = []
        for chapter in window:
            if chapter not in draft.drafts:
                break
            usable.append(chapter)
        gap = window[len(usable)] if len(usable) < len(window) else None
        reasons: dict[int, str] = {}
        for seq, chapter in enumerate(window, 1):
            if chapter in usable:
                row_status, reason = "pending", None
            elif chapter == gap:
                row_status, reason = "rejected", draft.rejects.get(chapter, "missing")
            else:
                row_status, reason = "discarded", f"after_rejected:{gap}"
            if reason:
                reasons[chapter] = reason
            _insert_batch_chapter(conn, batch_id, chapter, seq,
                                  draft.drafts.get(chapter, ""), row_status, reason)
        emit({"event": "batch_draft", "batch_id": batch_id, "from_chapter": window[0],
              "to_chapter": window[-1], "parsed": sorted(draft.drafts),
              "rejected": {str(k): v for k, v in sorted(draft.rejects.items())},
              "tokens": draft.tokens, "usd": draft.usd})

        # 逐章校验提交（严格按序；每章全新 gateway/ledger）
        window_accepted: list[ChapterOutcome] = []
        stop: Optional[str] = None          # canceled | fatal | chapter_failed | unresolved_blocks
        stop_chapter: Optional[int] = None
        for chapter in usable:
            if canceled():
                stop = "canceled"
                break
            draft_mark = _draft_rowid(conn)
            try:
                outcome = make_orchestrator().generate_chapter(
                    chapter, conn,
                    chapter_goal=goals[chapter],
                    interaction_mode=interaction_mode,
                    progress_cb=stage_cb(chapter),
                    predrafted_text=draft.drafts[chapter],
                    predrafted_beats=draft.beats.get(chapter) or [],
                    batch_id=batch_id,
                )
            except Exception as exc:     # generate_chapter 自身兜底；这里只防构造/回调异常
                out.error, out.error_kind = str(exc), _error_kind(exc)
                _mark_batch_chapter(conn, batch_id, chapter, "rejected", reason=str(exc)[:300])
                reasons[chapter] = "exception"
                stop, stop_chapter = "fatal", chapter
                break
            accepted_reason = None
            if not outcome.ok and _persisted_since(conn, chapter, draft_mark):
                # 正文落库之后才抛的异常（catch-all 返回 ok=False，但本章已进 draft_index）：
                # 从本章重写会产生重复修订轮并二次结算。按结算降级处理（P1#11 正文已落袋）。
                # 该章 pipeline_run 仍是 crashed、结算未跑：在批次行写明原因，供人工修复。
                outcome.ok = True
                outcome.state_degraded = True
                accepted_reason = f"post_persist_error: {outcome.error or 'exception'}"[:300]
            _record(out, outcome, batch_id)
            if outcome.ok:
                _mark_batch_chapter(conn, batch_id, chapter, "accepted", run_id=outcome.run_id,
                                    reason=accepted_reason)
                window_accepted.append(outcome)
            else:
                _mark_batch_chapter(conn, batch_id, chapter, "rejected", run_id=outcome.run_id,
                                    reason=(outcome.error or "chapter_failed")[:300])
            emit({"event": "chapter_done", **out.chapter_summary(outcome)})
            callback_error = _notify(chapter_cb, chapter, outcome)
            if callback_error is not None:
                out.error, out.error_kind = callback_error, "exception"
                stop, stop_chapter = "fatal", chapter
                break
            if not outcome.ok:
                stop, stop_chapter = "chapter_failed", chapter
                break
            if any(i.get("severity") == "block" for i in (outcome.issues or [])):
                # 已落库但仍有硬问题：后续章是照着修订前的本章写的 → 作废、从下一章重写
                stop, stop_chapter = "unresolved_blocks", chapter
                break

        _apportion(conn, window_accepted, draft.tokens, draft.usd)
        if not window_accepted:
            out.unapportioned_tokens += draft.tokens
            out.unapportioned_usd += draft.usd

        # 窗口内未被接受的章（必然是后缀）全部作废；未处理的 pending 行标 discarded
        accepted_now = [o.chapter for o in window_accepted]
        thrown = window[len(accepted_now):]
        tail_reason = {
            "canceled": "canceled",
            "fatal": "aborted",
            "chapter_failed": f"after_failed:{stop_chapter}",
            "unresolved_blocks": f"after_blocked:{stop_chapter}",
        }.get(stop or "", f"after_rejected:{gap}")
        processed = {o.chapter for o in out.outcomes if out.chapter_batch.get(o.chapter) == batch_id}
        for chapter in thrown:
            if chapter in processed:
                continue            # 流水线失败章：在 outcomes 里（ok=False）
            if chapter not in reasons:
                reasons[chapter] = tail_reason
                _mark_batch_chapter(conn, batch_id, chapter, "discarded", reason=tail_reason)
            out.discarded.append({"chapter": chapter, "batch_id": batch_id,
                                  "reason": reasons[chapter]})
        failure_reason = stop or (f"draft_{reasons.get(gap, 'missing')}" if gap else None)
        if stop == "canceled":
            window_status = "canceled"
        elif len(accepted_now) == len(window):
            window_status = "completed"
        elif accepted_now:
            window_status = "partial"
        else:
            window_status = "failed"
        _finalize_batch(conn, batch_id, window_status, tokens=draft.tokens, usd=draft.usd,
                        detail={
                            **detail,
                            "accepted": accepted_now,
                            "parse_rejects": {str(k): v for k, v in sorted(draft.rejects.items())},
                            "thrown": thrown,
                            "failure_reason": failure_reason,
                        })
        if thrown:
            emit({"event": "batch_discarded", "batch_id": batch_id,
                  "from_chapter": thrown[0], "chapters": thrown, "reason": failure_reason})

        if stop == "canceled":
            out.status = "canceled"
            return
        if stop == "fatal":
            return
        if window_accepted:
            regen_streak = 0                # 有进展：之前的重写已解决问题
        if not thrown:
            next_chapter = window[-1] + 1
            kind = "continue"
            continue
        next_chapter = thrown[0]
        if regen_streak >= max_regenerations:
            out.error = (
                f"max regenerations ({max_regenerations}) reached without progress; "
                f"stopped before chapter {next_chapter} ({failure_reason})"
            )
            out.error_kind = "regenerations"
            return
        kind = "regenerate"
