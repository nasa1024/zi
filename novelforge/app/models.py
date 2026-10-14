"""FastAPI 端点共享 Pydantic 模型（§8.2）。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ── 通用 ──────────────────────────────────────────────────────────────────────

class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


# ── Projects ──────────────────────────────────────────────────────────────────

# Compatibility whitelist used by PlotSetting validation.  The user-facing
# source of truth is GET /v1/catalogs/genres; the frontend must not copy it.
GENRE_VALUES: tuple[str, ...] = (
    "xuanhuan", "xianxia", "dushi", "kehuan", "lishi",
    "wuxia", "yanqing", "xuanyi", "default",
    "fantasy", "science_fiction", "romance", "mystery", "thriller",
    "young_adult", "speculative_fiction",
)
GENRE_GENERATABLE: tuple[str, ...] = tuple(g for g in GENRE_VALUES if g != "default")


class PlotSetting(BaseModel):
    """单份情节设定（手填或生成的候选；字段均可空便于局部填充）。"""
    genre: str = "xuanhuan"
    premise: Optional[str] = None              # 一句话设定
    world_setting: Optional[str] = None        # 世界观
    power_system: Optional[str] = None         # 力量体系名
    power_ranks: list[str] = Field(default_factory=list)   # 有序阶梯（弱→强）
    protagonist: Optional[str] = None
    main_conflict: Optional[str] = None
    opening_hook: Optional[str] = None
    target_audience: Optional[str] = None
    tone: Optional[str] = None


class WritingSelectionFields(BaseModel):
    """Project writing dimensions shared by create and pre-create suggest."""

    content_language: Literal["zh-CN", "en-US", "en-GB"] = "zh-CN"
    target_market: Optional[Literal["cn_web", "global_english", "us", "uk"]] = None
    publication_format: Optional[Literal["web_serial", "novel"]] = None
    primary_genre: Optional[str] = None
    story_tags: list[str] = Field(default_factory=list, max_length=30)
    audience_rating: Optional[Literal["general", "young_adult", "adult"]] = None
    content_warnings: list[str] = Field(default_factory=list, max_length=30)
    writing_profile_id: Optional[str] = None
    # Accepted compatibility name from the product PRD.
    style_preset_id: Optional[str] = None
    writing_profile_overrides: dict = Field(default_factory=dict)


class ProjectCreateRequest(WritingSelectionFields):
    # Choice-driven Agent entry permits a zero-text start.  The projects API
    # assigns a genre-aware title when this is blank or omitted.
    name: str = ""
    genre: str = "xuanhuan"
    power_system: Optional[str] = None
    config_overrides: Optional[dict] = None
    setting: Optional[PlotSetting] = None      # 新增：最终确认的整套情节设定


class ProjectResponse(BaseModel):
    project_id: str
    name: str
    genre: str
    db_path: str
    created_at: str
    chapter_count: int = 0
    canon_fact_count: int = 0
    archived: bool = False
    power_system: Optional[str] = None         # 回显
    premise: Optional[str] = None              # 回显
    content_language: str = "zh-CN"
    target_market: str = "cn_web"
    publication_format: str = "web_serial"
    primary_genre: Optional[str] = None
    story_tags: list[str] = Field(default_factory=list)
    audience_rating: str = "general"
    content_warnings: list[str] = Field(default_factory=list)
    writing_profile_id: str = "cn_web_novel_v1"
    writing_profile_version: int = 0
    writing_profile_effective_from_chapter: int = 1
    writing_profile_summary: dict = Field(default_factory=dict)


class SettingSuggestRequest(WritingSelectionFields):
    """生成 N 个情节设定候选（建项目前，无 project DB）。

    - locked + locked_fields：锁定字段作为条件生成前提。
    - field 非空 = 只对单字段 reroll（服务端强制 n=1）。
    - avoid = 针对当前生成目标的历史值列表（去重）。
    """
    name: Optional[str] = None
    genre: Optional[str] = None
    hints: Optional[str] = None                # 用户脑洞 / Braindump
    n: int = Field(default=3, ge=1, le=5)      # 越界由 Pydantic 拒绝（422）
    locked: Optional[PlotSetting] = None       # 已锁定字段值
    locked_fields: list[str] = Field(default_factory=list)  # 显式锁定的 key 集合
    field: Optional[str] = None                # 单字段 reroll 目标（如 'power_system'）
    avoid: list[str] = Field(default_factory=list)  # 该生成目标的历史值，避免重复


class SettingSuggestResponse(BaseModel):
    candidates: list[PlotSetting]
    degraded: bool = False                     # 无 key / 解析失败 → 兜底示例，前端明示
    tier_used: Optional[str] = None            # generate_validated 实际档位


class WritingProfilePatchRequest(BaseModel):
    content_language: Optional[Literal["zh-CN", "en-US", "en-GB"]] = None
    target_market: Optional[Literal["cn_web", "global_english", "us", "uk"]] = None
    publication_format: Optional[Literal["web_serial", "novel"]] = None
    primary_genre: Optional[str] = None
    story_tags: Optional[list[str]] = Field(default=None, max_length=30)
    audience_rating: Optional[Literal["general", "young_adult", "adult"]] = None
    content_warnings: Optional[list[str]] = Field(default=None, max_length=30)
    writing_profile_id: Optional[str] = None
    writing_profile_overrides: Optional[dict] = None
    effective_from_chapter: Optional[int] = Field(default=None, ge=1)
    confirm_high_risk: bool = False
    change_reason: Optional[str] = Field(default=None, max_length=500)
    changed_by: str = Field(default="user", min_length=1, max_length=100)


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
    entities: list[str] = Field(default_factory=list, max_length=100)
    as_of_chapter: int = Field(ge=0, le=1_000_000)
    keyword_query: Optional[str] = Field(default=None, max_length=500)
    top_k: int = Field(default=20, ge=1, le=100)

    @field_validator("keyword_query")
    @classmethod
    def normalize_keyword_query(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None


class RecallItem(BaseModel):
    source: str
    fact_id: Optional[str] = None
    scene_id: Optional[str] = None
    draft_id: Optional[str] = None
    content: str
    entity: Optional[str] = None
    valid_from_chapter: int = 0
    valid_to_chapter: Optional[int] = None
    score: Optional[float] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None


class WorldStateSnapshot(BaseModel):
    as_of_chapter: int
    power_ranks: dict = Field(default_factory=dict)
    knowledge_edges: list = Field(default_factory=list)
    timeline_events: list = Field(default_factory=list)
    item_ownership: dict = Field(default_factory=dict)
    gimmick_rules: list = Field(default_factory=list)
    numeric_facts: dict = Field(default_factory=dict)


class RecallResponse(BaseModel):
    items: list[RecallItem]
    world_state_snapshot: WorldStateSnapshot


# ── Memory lifecycle / health ─────────────────────────────────────────────────

class MemorySettingsPatch(BaseModel):
    enable_l2: Optional[bool] = None
    enable_vector: Optional[bool] = None
    enable_l3: Optional[bool] = None
    enable_summaries: Optional[bool] = None
    l2_every_n_chapters: Optional[int] = Field(default=None, ge=1, le=50)
    l2_max_scenes_per_chapter: Optional[int] = Field(default=None, ge=1, le=20)
    vector_top_k: Optional[int] = Field(default=None, ge=1, le=50)
    summary_window_chapters: Optional[int] = Field(default=None, ge=1, le=50)


class MemorySettingsResponse(BaseModel):
    enable_l2: bool
    enable_vector: bool
    enable_l3: bool
    enable_summaries: bool
    l2_every_n_chapters: int
    l2_max_scenes_per_chapter: int
    vector_top_k: int
    summary_window_chapters: int
    embedding_provider: str = "hash"
    embedding_model: str = "text-embedding-3-small"
    env_overrides: list[str] = Field(default_factory=list)


class MemoryLayerHealth(BaseModel):
    rows: int = 0
    chapters: int = 0
    completed_chapters: int = 0
    coverage_pct: float = 0.0
    details: dict = Field(default_factory=dict)


class MemoryHealthResponse(BaseModel):
    latest_chapter: int = 0
    settings: dict = Field(default_factory=dict)
    layers: dict[str, MemoryLayerHealth] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class MemoryRebuildRequest(BaseModel):
    rebuild_l0: bool = True
    rebuild_l1: bool = True
    rebuild_l2: bool = True
    rebuild_vectors: bool = True
    force: bool = False


class MemoryRebuildResponse(BaseModel):
    result: dict = Field(default_factory=dict)


# ── State ─────────────────────────────────────────────────────────────────────

class StateQueryRequest(BaseModel):
    as_of_chapter: int
    entity_filter: Optional[list[str]] = None


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
    interaction_mode: Literal["guided", "copilot", "autonomous"] = "guided"
    budget_max_tokens: Optional[int] = None
    budget_max_usd: Optional[float] = None
    n_candidates: Optional[int] = Field(default=None, ge=1, le=3)  # M3-①: 多候选择优
    quality_check: Optional[bool] = None  # M5-⑦: 质量评分 + 低分润色


class StageResult(BaseModel):
    stage: str
    status: Literal["ok", "blocked", "skipped", "circuit_broken", "degraded"]
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
    quality_dimensions: Optional[dict] = None  # 维度分 {hook,pacing,character,prose}
    cache_read_tokens: int = 0             # M1-⑥: 前缀缓存命中量
    alignment_report: Optional[dict] = None
    recommended_plan_action: Optional[
        Literal["keep", "patch", "replan", "pause"]
    ] = None
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
    tokens_spent: Optional[int] = None     # 逐章成本（v11）
    usd_spent: Optional[float] = None


class PipelineRunDetail(PipelineRunRecord):
    """pipeline_run 详情（含完整正文）。"""
    draft_text: str = ""
    candidates: list["CandidateInfo"] = Field(default_factory=list)  # M6: 多候选时的全部候选
    winner_index: Optional[int] = None
    selected_by: Optional[str] = None    # "auto" | "human"
    patch_stats: Optional[dict] = None   # M7: 补丁式修订统计 {revise|polish: {rounds,patches,applied,failed}}
    quality_dimensions: Optional[dict] = None  # 维度分 {hook,pacing,character,prose}
    state_degraded: bool = False         # P1#11: 结算降级（正文已落袋，世界状态待修复）
    foreshadow_settle: Optional[dict] = None   # P1#6: 伏笔结算报告
    plan_reconcile: Optional[dict] = None      # v16: 未来计划完成/延期/偏航对账
    alignment_report: Optional[dict] = None
    recommended_plan_action: Optional[
        Literal["keep", "patch", "replan", "pause"]
    ] = None


class CandidateInfo(BaseModel):
    """M6: 单个候选稿（3 选 1 换稿用）。"""
    index: int
    draft_text: str
    length: int
    score: Optional[float] = None
    hard_blocks: int = 0
    continuity_blocks: int = 0
    plan_blocks: int = 0
    contract_score: Optional[float] = None
    arc_progress_score: Optional[float] = None
    quality_score: Optional[float] = None
    is_winner: bool = False
    proposal_count: int = 0


class SelectCandidateRequest(BaseModel):
    candidate_index: int = Field(ge=0, le=2)


# ── Pipeline stats（M6: 质量趋势看板）─────────────────────────────────────────

class ChapterStat(BaseModel):
    chapter: int
    word_count: Optional[int] = None
    quality_score: Optional[float] = None
    finished_at: Optional[str] = None
    tokens_spent: Optional[int] = None   # 逐章成本（v11）
    usd_spent: Optional[float] = None
    payoff_closed: bool = False          # P1#10: 本章有确定性爽点闭环证据
    # v22/F4：分维度质量门结论（None=未评分）+ 未过线项（如 "hook 4<5"）
    quality_low: Optional[bool] = None
    quality_gate_reasons: list[str] = Field(default_factory=list)
    judge_disagreement: Optional[bool] = None   # v22/F4：评委正反两序胜者不一致（None=未做两序校验）


class PipelineStats(BaseModel):
    """逐章质量/产量序列（每章取最新一次 completed run）+ 汇总。

    连写 50 章时「第几章开始崩」一眼可见——ConStory 实证错误高发于中段，
    分数趋势是最直接的监控信号。
    """
    series: list[ChapterStat] = Field(default_factory=list)
    chapters_completed: int = 0
    total_words: int = 0
    avg_quality_score: Optional[float] = None
    low_quality_count: int = 0      # 未过质量门的章数（v22/F4：分维度下限；无维度分时 < min_score）
    min_score_threshold: float = 6.0
    total_tokens_spent: int = 0     # 成本曲线汇总（v11；旧数据无成本列时为 0）
    total_usd_spent: float = 0.0
    payoff_loop_rate: Optional[float] = None   # P1#10: 闭环章/完成章（≥0.7 高 0.5-0.7 中 <0.5 低）
    # v22/F4：评委去偏看板（LongJudgeBench：换序改判率 10.6%–78.7%）
    judge_runs: int = 0                        # 做过正反两序校验的章数
    judge_disagreements: int = 0               # 两序胜者不一致 → 确定性回落的章数
    judge_disagreement_rate: Optional[float] = None


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
    to_chapter: Optional[int] = None     # 缺省 = min(卷末章, from+9)；单次 ≤10 章（长输出模型 ≤30 章）


class PlannedBeat(BaseModel):
    seq: int
    beat_type: str
    summary: str
    value_axis: Optional[str] = None
    arc_id: Optional[str] = None
    constraint_level: Literal["soft", "hard"] = "soft"
    witnesses: list[str] = Field(default_factory=list)   # v22/F5


class ChapterCardModel(BaseModel):
    chapter: int
    title: Optional[str] = None
    goal: Optional[str] = None
    hook_text: Optional[str] = None
    status: str = "planned"
    # P1#7 细纲契约
    target_emotion: Optional[str] = None
    opening_hook_type: Optional[str] = None
    hook_type: Optional[str] = None
    expectation_score: Optional[int] = None
    plan_version: int = 1
    locked: bool = False
    source_decision_id: Optional[str] = None
    current_plan_version_id: Optional[str] = None
    arc_targets: list[str] = Field(default_factory=list)
    volume_kr_targets: list[str] = Field(default_factory=list)
    milestone_targets: list[str] = Field(default_factory=list)
    must_advance: list[str] = Field(default_factory=list)
    must_resolve: list[str] = Field(default_factory=list)
    must_include_entities: list[str] = Field(default_factory=list)
    must_not_reveal: list[str] = Field(default_factory=list)
    must_not_resolve: list[str] = Field(default_factory=list)
    forbidden_state_changes: list[str] = Field(default_factory=list)
    target_state: dict = Field(default_factory=dict)
    success_criteria: list[str] = Field(default_factory=list)
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
    # P1#7 细纲契约（人审可改）
    target_emotion: Optional[str] = None
    opening_hook_type: Optional[str] = None
    hook_type: Optional[str] = None
    expectation_score: Optional[int] = Field(default=None, ge=1, le=5)
    arc_targets: Optional[list[str]] = None
    volume_kr_targets: Optional[list[str]] = None
    milestone_targets: Optional[list[str]] = None
    must_advance: Optional[list[str]] = None
    must_resolve: Optional[list[str]] = None
    must_include_entities: Optional[list[str]] = None
    must_not_reveal: Optional[list[str]] = None
    must_not_resolve: Optional[list[str]] = None
    forbidden_state_changes: Optional[list[str]] = None
    target_state: Optional[dict] = None
    success_criteria: Optional[list[str]] = None


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
    # P0#1: auto_promote 连续失败自动降级为 human_gate 时暂停挂机（status='paused'，
    # 可 resume）；False = 旧行为（转 human_gate 后继续连写）
    pause_on_degrade: bool = True
    quality_check: bool = False  # M5-⑦: 逐章质量评分；连续低分计入降级计数
    n_candidates: int = Field(default=1, ge=1, le=3)  # M3-①: 每章候选稿数（择优）


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
    # P2#13 卷级 Objective + KR（key_results 接受字符串列表或 {text,...} 列表）
    objective: Optional[str] = None
    key_results: Optional[list] = None


class VolumeUpdateRequest(BaseModel):
    title: Optional[str] = None
    synopsis: Optional[str] = None
    start_chapter: Optional[int] = Field(None, ge=1)
    end_chapter: Optional[int] = Field(None, ge=1)
    status: Optional[Literal["writing", "completed", "archived"]] = None
    objective: Optional[str] = None
    key_results: Optional[list] = None


class VolumeResponse(BaseModel):
    id: str
    volume_no: int
    title: str
    synopsis: Optional[str] = None
    start_chapter: Optional[int] = None
    end_chapter: Optional[int] = None
    status: str
    created_at: str
    # P2#13
    objective: Optional[str] = None
    key_results: list[dict] = Field(default_factory=list)


# ── Setting → Volume 向导（POST-v15 P2#6）────────────────────────────────────

class VolumeWizardRequest(BaseModel):
    """从项目锁定的情节设定一键生成首卷大纲 + 前几章细纲。

    读取建项目时落库的 meta_kv 设定（premise/main_conflict/power_system/...），
    生成卷 title/synopsis/objective/KRs 并建卷，随后复用卷规划管线预填
    chapter_cards + beats——「设定即细纲」。
    """
    volume_span: int = Field(default=20, ge=5, le=200)   # 本卷章数
    plan_chapters: int = Field(default=5, ge=1, le=10)   # 预填细纲的章数
    hints: Optional[str] = None                          # 额外方向提示


class VolumeWizardResponse(BaseModel):
    volume: VolumeResponse
    plan: Optional[VolumePlanResponse] = None
    tier_used: Optional[str] = None
    degraded: bool = False          # 无 key / 解析失败 → 确定性兜底大纲


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


class ColdStartImportRequest(BaseModel):
    """POST-v15 P1#4：整本存量正文批量导入（后台 job，并发抽取 + 去噪 + 分层晋升）。

    full_text 与 chapters 二选一：full_text 自动按「第N章」切章；
    dry_run=True 只返回切章预览，不启动抽取。
    """
    full_text: Optional[str] = None
    chapters: Optional[list[ColdStartChapter]] = None
    auto_promote_low_risk: bool = False        # 低风险条目由程序直接晋升 canon
    max_concurrency: int = Field(default=4, ge=1, le=8)
    dry_run: bool = False
    actor: str = "cold_start"


class ColdStartSplitPreview(BaseModel):
    chapters_total: int
    chapters: list[dict]                       # [{chapter_no, title, chars}]


class ColdStartJobInfo(BaseModel):
    job_id: str
    project_id: str
    status: str                                # running / completed / error
    chapters_total: int
    chapters_done: int
    proposals_extracted: int = 0
    dedup_dropped: int = 0
    candidates_created: int = 0
    auto_promoted: int = 0
    staged_low: int = 0
    queued_review: int = 0
    error: Optional[str] = None
    started_at: str
    finished_at: Optional[str] = None


# ── 三层摘要看板（POST-v15 P2#7）──────────────────────────────────────────────

class SummaryChapterItem(BaseModel):
    chapter: int
    summary: str
    volume_no: Optional[int] = None
    title: Optional[str] = None           # chapter_cards.title（有细纲时）
    word_count: Optional[int] = None      # draft_index 最新修订字数
    created_at: Optional[str] = None


class SummaryVolumeItem(BaseModel):
    volume_no: int
    title: str
    rolling_summary: Optional[str] = None
    start_chapter: Optional[int] = None
    end_chapter: Optional[int] = None
    status: str = "writing"


class SummaryBoardResponse(BaseModel):
    """全书至此的三层摘要：章摘要 → 卷滚动摘要 → 全局梗概（meta_kv）。"""
    global_synopsis: Optional[str] = None
    volumes: list[SummaryVolumeItem] = Field(default_factory=list)
    chapters: list[SummaryChapterItem] = Field(default_factory=list)


class ChapterSummaryUpdateRequest(BaseModel):
    summary: str = Field(..., min_length=1, max_length=2000)


class VolumeSummaryUpdateRequest(BaseModel):
    rolling_summary: str = Field(..., min_length=1, max_length=4000)


class GlobalSynopsisUpdateRequest(BaseModel):
    synopsis: str = Field(..., min_length=1, max_length=4000)


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
    # v12: 伏笔结算列（P1#6）
    last_mentioned_chapter: Optional[int] = None
    advance_count: int = 0
    last_advanced_chapter: Optional[int] = None
    origin: str = "manual"


# ── Style anchors（P1#9 文风锚点 few-shot）────────────────────────────────────

class StyleAnchorCreate(BaseModel):
    emotion: str = Field(min_length=1, max_length=50)
    title: Optional[str] = Field(default=None, max_length=200)
    content: str = Field(min_length=50, max_length=2000)   # 300-500 字最佳，硬限 50-2000
    language: Literal["zh-CN", "en-US", "en-GB"] = "zh-CN"
    source_type: Literal["user", "official", "licensed", "legacy"] = "user"
    source_note: Optional[str] = Field(default=None, max_length=500)
    rights_confirmed: bool = False
    profile_tags: list[str] = Field(default_factory=list, max_length=30)


class StyleAnchorUpdate(BaseModel):
    emotion: Optional[str] = Field(default=None, min_length=1, max_length=50)
    title: Optional[str] = Field(default=None, max_length=200)
    content: Optional[str] = Field(default=None, min_length=50, max_length=2000)
    enabled: Optional[bool] = None
    language: Optional[Literal["zh-CN", "en-US", "en-GB"]] = None
    source_type: Optional[Literal["user", "official", "licensed", "legacy"]] = None
    source_note: Optional[str] = Field(default=None, max_length=500)
    rights_confirmed: Optional[bool] = None
    profile_tags: Optional[list[str]] = Field(default=None, max_length=30)


class StyleAnchorResponse(BaseModel):
    id: str
    emotion: str
    title: Optional[str] = None
    content: str
    enabled: bool = True
    language: str = "zh-CN"
    source_type: str = "legacy"
    source_note: Optional[str] = None
    rights_confirmed: bool = False
    profile_tags: list[str] = Field(default_factory=list)
    created_at: Optional[str] = None


# ── Sessions / Turns / SSE ────────────────────────────────────────────────────

class SessionCreateRequest(BaseModel):
    client: Literal["cli", "web", "chat", "api"] = "api"
    actor: str = "user"
    mode: Optional[Literal["human_gate", "auto_promote", "hybrid"]] = None
    interaction_mode: Literal["guided", "copilot", "autonomous"] = "guided"


class SessionResponse(BaseModel):
    session_id: str
    client: str
    mode: Optional[str] = None
    actor: str
    interaction_mode: str = "guided"
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


# ── Choice-driven Author Agent (v16) ─────────────────────────────────────────

class AgentStartRequest(BaseModel):
    interaction_mode: Literal["guided", "copilot", "autonomous"] = "guided"
    actor: str = Field(default="agent:web", max_length=200)
    governance_mode: Literal["human_gate", "auto_promote", "hybrid"] = "human_gate"
    hints: Optional[str] = Field(default=None, max_length=2000)
    planning_horizon: int = Field(default=5, ge=1, le=10)
    auto_chapters: int = Field(default=1, ge=1, le=50)


class AgentChoiceRequest(BaseModel):
    action: Literal["select", "auto", "reroll"] = "select"
    option_id: Optional[str] = Field(default=None, max_length=200)
    hints: Optional[str] = Field(default=None, max_length=2000)


class AgentSessionResponse(BaseModel):
    session_id: str
    project_id: str
    interaction_mode: str
    stage: str
    status: str
    decision: Optional[dict] = None
    last_decision: Optional[dict] = None
    state: dict = Field(default_factory=dict)
    story_map: dict = Field(default_factory=dict)
    error: Optional[str] = None
