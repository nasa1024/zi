"""FastAPI 端点共享 Pydantic 模型（§8.2）。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── 通用 ──────────────────────────────────────────────────────────────────────

class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


# ── Projects ──────────────────────────────────────────────────────────────────

class ProjectCreateRequest(BaseModel):
    name: str
    genre: str = "xuanhuan"
    power_system: Optional[str] = None
    config_overrides: Optional[dict] = None


class ProjectResponse(BaseModel):
    project_id: str
    name: str
    genre: str
    db_path: str
    created_at: str
    chapter_count: int = 0
    canon_fact_count: int = 0
    archived: bool = False


# ── Capture ───────────────────────────────────────────────────────────────────

class ProposalItem(BaseModel):
    op: Literal["add", "update", "deprecate", "retcon"] = "add"
    fact_type: str
    entity: Optional[str] = None
    new: Optional[dict] = None
    valid_from_chapter: int = 0
    target_fact_id: Optional[str] = None
    risk_tier: Optional[str] = "low"


class CaptureRequest(BaseModel):
    source_chapter: int
    source_kind: Literal["draft", "canon_text", "manual"] = "manual"
    proposals: list[ProposalItem]


class CaptureResponse(BaseModel):
    candidate_ids: list[str]
    indexed: dict = Field(default_factory=dict)


# ── Recall ────────────────────────────────────────────────────────────────────

class RecallRequest(BaseModel):
    entities: list[str] = []
    as_of_chapter: int
    keyword_query: Optional[str] = None
    top_k: int = 20


class RecallItem(BaseModel):
    source: str
    fact_id: Optional[str] = None
    content: str
    entity: Optional[str] = None
    valid_from_chapter: int = 0
    score: Optional[float] = None


class RecallResponse(BaseModel):
    items: list[RecallItem]
    world_state_snapshot: dict


# ── State ─────────────────────────────────────────────────────────────────────

class StateQueryRequest(BaseModel):
    as_of_chapter: int
    entity_filter: Optional[list[str]] = None


class WorldStateSnapshot(BaseModel):
    as_of_chapter: int
    power_ranks: dict = Field(default_factory=dict)
    knowledge_edges: list = Field(default_factory=list)
    timeline_events: list = Field(default_factory=list)
    item_ownership: dict = Field(default_factory=dict)
    gimmick_rules: list = Field(default_factory=list)
    numeric_facts: dict = Field(default_factory=dict)


# ── Bible ─────────────────────────────────────────────────────────────────────

class BibleRenderResponse(BaseModel):
    content: str
    rendered_from: dict
    is_readonly: Literal[True] = True


# ── Reviews ───────────────────────────────────────────────────────────────────

class ReviewQueueItem(BaseModel):
    candidate_id: str
    fact_type: str
    risk_tier: str
    status: str
    reason: Optional[str] = None
    proposal_json: str
    source_chapter: int
    created_at: Optional[str] = None


class ApproveRequest(BaseModel):
    actor: str
    note: Optional[str] = None
    valid_from_chapter_override: Optional[int] = None


class ApproveResponse(BaseModel):
    candidate_id: str
    fact_id: str
    new_status: Literal["canon"] = "canon"


class RejectRequest(BaseModel):
    actor: str
    reason: str = "rejected_by_reviewer"


class BatchApproveRequest(BaseModel):
    candidate_ids: list[str]
    actor: str
    require_no_conflict: bool = True


class BatchApproveResponse(BaseModel):
    approved: list[ApproveResponse]
    skipped: list[dict]


# ── Revert ────────────────────────────────────────────────────────────────────

class RevertRequest(BaseModel):
    actor: str
    reason: str
    revert_to_revision_id: Optional[str] = None


class RevertResponse(BaseModel):
    fact_id: str
    reverted_to: str
    promotion_log_id: str


# ── Pipeline ──────────────────────────────────────────────────────────────────

class PipelineRunRequest(BaseModel):
    chapter_no: int
    chapter_goal: str = ""
    entity_ids: Optional[list[str]] = None
    keyword_query: Optional[str] = None
    mode: Optional[Literal["human_gate", "auto_promote", "hybrid"]] = None
    budget_max_tokens: Optional[int] = None
    budget_max_usd: Optional[float] = None
    n_candidates: Optional[int] = Field(default=None, ge=1, le=3)  # M3-①: 多候选择优
    quality_check: Optional[bool] = None  # M5-⑦: 质量评分 + 低分润色


class StageResult(BaseModel):
    stage: str
    status: Literal["ok", "blocked", "skipped", "circuit_broken"]
    detail: dict = Field(default_factory=dict)


class BudgetSpent(BaseModel):
    tokens: int
    usd: float
    revise_rounds: int = 0


class PipelineRunResponse(BaseModel):
    run_id: str
    chapter_no: int
    stages: list[StageResult]
    final_gate: str
    draft_text: str = ""
    budget_spent: BudgetSpent
    circuit_breaker_tripped: bool = False
    quality_score: Optional[float] = None  # M5-⑦
    error: Optional[str] = None


class PipelineRunRecord(BaseModel):
    """pipeline_run 历史列表条目（不含正文）。"""
    run_id: str
    chapter: int
    status: str
    started_at: str
    finished_at: Optional[str] = None
    word_count: Optional[int] = None
    quality_score: Optional[float] = None  # M5-⑦


class PipelineRunDetail(PipelineRunRecord):
    """pipeline_run 详情（含完整正文）。"""
    draft_text: str = ""


# ── Foreshadow health（M5-⑧ 伏笔回收健康度，inkos hookAgenda 思路）────────────

class ForeshadowHealth(BaseModel):
    open_count: int = 0          # 未回收（planted/reinforced/misled/overdue）
    overdue_count: int = 0
    oldest_overdue_chapter: Optional[int] = None   # 最早到期且仍未回收的章号
    due_soon: list[dict] = Field(default_factory=list)  # 3 章内到期 [{label, due_chapter}]
    status: str = "green"        # green(无逾期) / yellow(≤2) / red(>2)


# ── Volume plan（M4-④ 卷级批量预规划）─────────────────────────────────────────

class VolumePlanRequest(BaseModel):
    from_chapter: Optional[int] = None   # 缺省 = max(卷起始章, 已完成最大章+1)
    to_chapter: Optional[int] = None     # 缺省 = min(卷末章, from+9)；单次 ≤10 章


class PlannedBeat(BaseModel):
    seq: int
    beat_type: str
    summary: str
    value_axis: Optional[str] = None


class ChapterCardModel(BaseModel):
    chapter: int
    title: Optional[str] = None
    goal: Optional[str] = None
    hook_text: Optional[str] = None
    status: str = "planned"
    beats: list[PlannedBeat] = Field(default_factory=list)


class VolumePlanResponse(BaseModel):
    volume_no: int
    from_chapter: int
    to_chapter: int
    planned: list[ChapterCardModel]
    skipped: list[int] = Field(default_factory=list)  # 已 drafted/committed 受保护的章
    error: Optional[str] = None


class ChapterCardUpdateRequest(BaseModel):
    title: Optional[str] = None
    goal: Optional[str] = None
    hook_text: Optional[str] = None


class NextChapterSuggestion(BaseModel):
    """「下一章」自动建议（GET /pipeline/next）。

    next_chapter = 已完成生成的最大章节号 + 1；
    suggested_goal 由章节卡 / 上一章钩子 / 卷大纲 / 待回收伏笔 / 已计划节拍拼装，
    sources 标注每段建议的来源，便于前端展示依据。
    """
    next_chapter: int
    last_completed_chapter: int = 0
    suggested_goal: str = ""
    sources: list[str] = Field(default_factory=list)


# ── Check ─────────────────────────────────────────────────────────────────────

class CheckRequest(BaseModel):
    draft_text: str
    chapter_no: int
    beats: list[dict] = Field(default_factory=list)
    proposals: list[dict] = Field(default_factory=list)


# ── Autopilot ─────────────────────────────────────────────────────────────────

class AutopilotStartRequest(BaseModel):
    from_chapter: int
    to_chapter: int
    chapter_goals: dict = Field(default_factory=dict)  # {chapter_no: goal_str}
    mode: Literal["auto_promote", "hybrid"] = "auto_promote"
    budget_max_tokens_per_chapter: Optional[int] = None
    budget_max_usd_per_chapter: Optional[float] = None
    budget_session_max_tokens: Optional[int] = None   # E4: 会话级跨章 token 封顶
    budget_session_max_usd: Optional[float] = None    # E4: 会话级跨章 USD 封顶
    auto_degrade_after_consecutive_issues: int = 2  # 连续 N 章有 hard issue → 降级
    quality_check: bool = False  # M5-⑦: 逐章质量评分；连续低分计入降级计数


class AutopilotSessionInfo(BaseModel):
    session_id: str
    project_id: str
    from_chapter: int
    to_chapter: int
    current_chapter: int
    status: str  # running / degraded / circuit_broken / completed / error
    policy_mode: str
    chapters_done: int
    chapters_total: int
    budget_tokens_total: int = 0
    budget_usd_total: float = 0.0
    pending_reviews: int = 0
    consecutive_hard_issues: int = 0
    last_error: Optional[str] = None
    started_at: str
    finished_at: Optional[str] = None


class AutopilotDegradeRequest(BaseModel):
    reason: str = "manual_degrade"


# ── Seed ──────────────────────────────────────────────────────────────────────

class SeedProposal(BaseModel):
    op: Literal["add", "update", "deprecate", "retcon"] = "add"
    fact_type: str
    entity: Optional[str] = None
    new: Optional[dict] = None
    valid_from_chapter: int = 0
    risk_tier: str = "low"


class SeedRequest(BaseModel):
    """Bible seed：批量录入世界观 facts 进 staging，可选自动批准低风险条目。"""
    proposals: list[SeedProposal]
    auto_approve_low_risk: bool = False
    actor: str = "seed"


class SeedResponse(BaseModel):
    candidate_ids: list[str]
    auto_approved: list[str]
    queued: list[str]


# ── Volumes ───────────────────────────────────────────────────────────────────

class VolumeCreateRequest(BaseModel):
    volume_no: int = Field(..., ge=1)
    title: str = Field(..., min_length=1)
    synopsis: Optional[str] = None
    start_chapter: Optional[int] = Field(None, ge=1)
    end_chapter: Optional[int] = Field(None, ge=1)


class VolumeUpdateRequest(BaseModel):
    title: Optional[str] = None
    synopsis: Optional[str] = None
    start_chapter: Optional[int] = Field(None, ge=1)
    end_chapter: Optional[int] = Field(None, ge=1)
    status: Optional[Literal["writing", "completed", "archived"]] = None


class VolumeResponse(BaseModel):
    id: str
    volume_no: int
    title: str
    synopsis: Optional[str] = None
    start_chapter: Optional[int] = None
    end_chapter: Optional[int] = None
    status: str
    created_at: str


# ── Branches ──────────────────────────────────────────────────────────────────

class BranchCreateRequest(BaseModel):
    branch_name: str = Field(..., min_length=1)
    fork_chapter: int = Field(..., ge=1)
    base_branch_id: Optional[str] = None
    description: Optional[str] = None


class BranchUpdateRequest(BaseModel):
    description: Optional[str] = None
    status: Optional[Literal["active", "merged", "abandoned"]] = None


class BranchResponse(BaseModel):
    id: str
    branch_name: str
    fork_chapter: int
    base_branch_id: Optional[str] = None
    description: Optional[str] = None
    status: str
    created_at: str


# ── Cold Start ────────────────────────────────────────────────────────────────

class ColdStartChapter(BaseModel):
    chapter_no: int = Field(..., ge=1)
    text: str = Field(..., min_length=1)


class ColdStartRequest(BaseModel):
    """从已有正文中反向抽取 fact_candidates（全部进 staging，永不自动 canon）。"""
    chapters: list[ColdStartChapter] = Field(..., min_length=1)
    actor: str = "cold_start"


class ColdStartResponse(BaseModel):
    candidate_ids: list[str]
    atom_ids: list[str]
    chapters_processed: int


# ── Consistency Exemptions ────────────────────────────────────────────────────

class ExemptionCreateRequest(BaseModel):
    scope: Literal["fact", "entity", "chapter", "global"] = "fact"
    scope_ref: str = Field(..., description="scope 对应的引用 ID（fact_id / entity_id / chapter_no / '*'）")
    exempt_tag: str = Field(..., description="豁免标签，如 'power_decrease' / 'timeline_jump'")
    rule_codes: Optional[list[str]] = None
    reason: str = Field(..., min_length=1)
    valid_from_chapter: Optional[int] = Field(None, ge=0)
    valid_to_chapter: Optional[int] = Field(None, ge=0)
    created_by: str = "author"


class ExemptionResponse(BaseModel):
    id: int
    scope: str
    scope_ref: str
    exempt_tag: str
    rule_codes: Optional[list[str]] = None
    reason: str
    valid_from_chapter: Optional[int] = None
    valid_to_chapter: Optional[int] = None
    created_by: str
    created_at: str


# ── Foreshadow ────────────────────────────────────────────────────────────────

class ForeshadowCreateRequest(BaseModel):
    label: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    planted_chapter: int = Field(..., ge=1)
    due_chapter: Optional[int] = Field(None, ge=1)
    related_entity_id: Optional[str] = None
    importance: int = Field(3, ge=1, le=5)


class ForeshadowUpdateRequest(BaseModel):
    state: Optional[Literal["planted", "reinforced", "misled", "paid_off", "overdue"]] = None
    due_chapter: Optional[int] = Field(None, ge=1)
    paid_off_chapter: Optional[int] = Field(None, ge=1)
    importance: Optional[int] = Field(None, ge=1, le=5)
    description: Optional[str] = None


class ForeshadowResponse(BaseModel):
    id: str
    label: str
    description: str
    state: str
    planted_chapter: int
    due_chapter: Optional[int] = None
    paid_off_chapter: Optional[int] = None
    related_entity_id: Optional[str] = None
    importance: int
    updated_at: str


# ── Sessions / Turns / SSE ────────────────────────────────────────────────────

class SessionCreateRequest(BaseModel):
    client: Literal["cli", "web", "chat", "api"] = "api"
    actor: str = "user"
    mode: Optional[Literal["human_gate", "auto_promote", "hybrid"]] = None


class SessionResponse(BaseModel):
    session_id: str
    client: str
    mode: Optional[str] = None
    actor: str
    started_at: str
    ended_at: Optional[str] = None
    budget_spent_tokens: int = 0
    budget_spent_usd: float = 0.0


class SessionEndRequest(BaseModel):
    summary: Optional[str] = None


class TurnCreateRequest(BaseModel):
    kind: Literal["command", "chat", "long_task"] = "command"
    intent: Optional[str] = None
    payload: dict = Field(default_factory=dict)
    stream: bool = False


class TurnResponse(BaseModel):
    turn_id: str
    session_id: str
    seq: int
    kind: str
    intent: Optional[str] = None
    routed_endpoint: Optional[str] = None
    status: str
    stream: bool
    result: Optional[dict] = None
    started_at: str
    finished_at: Optional[str] = None


class TurnEventItem(BaseModel):
    id: int
    event_type: str
    data: dict
    created_at: str
