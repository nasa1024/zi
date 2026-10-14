// zi API types — mirror of backend Pydantic models.
// Keep field names/shapes in sync with the backend; see SHARED CONTRACT.

export interface HealthResponse {
  status: string;
  version: string;
}

export type ContentLanguage = 'zh-CN' | 'en-US' | 'en-GB';
export type TargetMarket = 'cn_web' | 'global_english' | 'us' | 'uk';
export type PublicationFormat = 'web_serial' | 'novel';

export interface GenreCatalogItem {
  id: string;
  label: string;
  description?: string | null;
  language?: string | null;
  market?: string | null;
  story_tags?: string[];
}

export interface WritingProfilePromptRules {
  pov?: string;
  tense?: string;
  pace?: string;
  prose_density?: string;
  dialogue_density?: string;
  [key: string]: unknown;
}

export interface WritingProfileQualityRules {
  chapter_word_range?: [number, number] | number[];
  [key: string]: unknown;
}

export interface WritingProfileCatalogItem {
  id: string;
  version: string;
  name: string;
  description?: string | null;
  preview?: string | null;
  language: string;
  market: string;
  format: string;
  primary_genre?: string | null;
  compatible_genres?: string[];
  story_tags?: string[];
  default_story_tags?: string[];
  audience_rating?: string | null;
  prompt_rules: WritingProfilePromptRules;
  quality_rules: WritingProfileQualityRules;
  prompt_version?: string | null;
}

export interface WritingProfileSummary {
  id?: string;
  display_name?: string;
  version?: number;
  content_language?: string;
}

export interface WritingProfileSelection {
  content_language: ContentLanguage | string;
  target_market: TargetMarket | string;
  publication_format: PublicationFormat | string;
  primary_genre?: string | null;
  story_tags: string[];
  audience_rating?: string | null;
  content_warnings: string[];
  writing_profile_id: string;
  writing_profile_version: number;
  writing_profile_overrides: Record<string, unknown>;
  writing_profile_effective_from_chapter?: number | null;
}

export interface WritingProfileVersion extends WritingProfileSelection {
  created_at?: string | null;
  changed_by?: string | null;
  change_reason?: string | null;
  prompt_version?: string | null;
}

export interface ProjectWritingProfileResponse extends WritingProfileSelection {
  project_id?: string;
  profile?: WritingProfileCatalogItem | null;
  history: WritingProfileVersion[];
}

export interface ProjectWritingProfilePatch {
  content_language?: ContentLanguage | string;
  target_market?: TargetMarket | string;
  publication_format?: PublicationFormat | string;
  primary_genre?: string | null;
  story_tags?: string[];
  audience_rating?: string | null;
  content_warnings?: string[];
  writing_profile_id?: string;
  writing_profile_overrides?: Record<string, unknown>;
  effective_from_chapter?: number | null;
  confirm_high_risk?: boolean;
  change_reason?: string | null;
  changed_by?: string;
}

// 情节设定（手填或生成的候选）；与后端 PlotSetting 逐字段镜像。
export interface PlotSetting {
  genre: string;
  premise?: string | null;
  world_setting?: string | null;
  power_system?: string | null;
  power_ranks: string[];
  protagonist?: string | null;
  main_conflict?: string | null;
  opening_hook?: string | null;
  target_audience?: string | null;
  tone?: string | null;
}

export interface SettingSuggestRequest {
  name?: string | null;
  genre?: string | null;
  hints?: string | null;
  n?: number;
  locked?: PlotSetting | null;
  locked_fields?: string[];
  field?: string | null;
  avoid?: string[];
  content_language?: ContentLanguage | string;
  target_market?: TargetMarket | string;
  publication_format?: PublicationFormat | string;
  primary_genre?: string | null;
  story_tags?: string[];
  audience_rating?: string | null;
  content_warnings?: string[];
  writing_profile_id?: string | null;
  writing_profile_overrides?: Record<string, unknown> | null;
}

export interface SettingSuggestResponse {
  candidates: PlotSetting[];
  degraded: boolean;
  tier_used?: string | null;
}

export interface ProjectCreateRequest {
  name?: string;
  genre?: string;
  power_system?: string | null;
  config_overrides?: Record<string, unknown> | null;
  setting?: PlotSetting | null;
  content_language?: ContentLanguage | string;
  target_market?: TargetMarket | string;
  publication_format?: PublicationFormat | string;
  primary_genre?: string | null;
  story_tags?: string[];
  audience_rating?: string | null;
  content_warnings?: string[];
  writing_profile_id?: string | null;
  writing_profile_overrides?: Record<string, unknown> | null;
}

export interface ProjectResponse {
  project_id: string;
  name: string;
  genre: string;
  db_path: string;
  created_at: string;
  chapter_count: number;
  canon_fact_count: number;
  archived: boolean;
  power_system?: string | null;
  premise?: string | null;
  content_language?: ContentLanguage | string;
  target_market?: TargetMarket | string;
  publication_format?: PublicationFormat | string;
  primary_genre?: string | null;
  story_tags?: string[];
  audience_rating?: string | null;
  content_warnings?: string[];
  writing_profile_id?: string | null;
  writing_profile_version?: number | null;
  writing_profile_effective_from_chapter?: number | null;
  writing_profile_name?: string | null;
  writing_profile_summary?: WritingProfileSummary | string | null;
}

export interface SeedProposal {
  op?: 'add' | 'update' | 'deprecate' | 'retcon';
  fact_type: string;
  entity?: string | null;
  new?: Record<string, unknown> | null;
  valid_from_chapter?: number;
  risk_tier?: string;
}

export interface SeedRequest {
  proposals: SeedProposal[];
  auto_approve_low_risk?: boolean;
  actor?: string;
}

export interface SeedResponse {
  candidate_ids: string[];
  auto_approved: string[];
  queued: string[];
}

export interface BibleRenderResponse {
  content: string;
  rendered_from: Record<string, unknown>;
  is_readonly: true;
}

export interface FactHit {
  id: string;
  snippet: string;
  chapter: number;
}

export interface SearchFactsResponse {
  hits: FactHit[];
  mode: string;
}

export interface StateQueryRequest {
  as_of_chapter: number;
  entity_filter?: string[] | null;
}

export interface WorldStateSnapshot {
  as_of_chapter: number;
  power_ranks: Record<string, string>;
  knowledge_edges: unknown[];
  timeline_events: unknown[];
  item_ownership: Record<string, unknown>;
  gimmick_rules: unknown[];
  numeric_facts: Record<string, unknown>;
}

export interface PipelineRunRequest {
  chapter_no: number;
  chapter_goal?: string;
  entity_ids?: string[] | null;
  keyword_query?: string | null;
  mode?: 'human_gate' | 'auto_promote' | 'hybrid' | null;
  budget_max_tokens?: number | null;
  budget_max_usd?: number | null;
  n_candidates?: number | null;
  quality_check?: boolean | null;
}

export interface StageResult {
  stage: string;
  status: 'ok' | 'blocked' | 'skipped' | 'circuit_broken' | 'degraded';
  detail: Record<string, unknown>;
}

export interface BudgetSpent {
  tokens: number;
  usd: number;
  revise_rounds?: number;
}

export interface PipelineRunResponse {
  run_id: string;
  chapter_no: number;
  stages: StageResult[];
  final_gate: string;
  draft_text: string;
  budget_spent: BudgetSpent;
  circuit_breaker_tripped: boolean;
  error?: string | null;
}

export interface PipelineRunRecord {
  run_id: string;
  chapter: number;
  status: 'running' | 'completed' | 'crashed' | string;
  started_at: string;
  finished_at?: string | null;
  word_count?: number | null;
  quality_score?: number | null;
  tokens_spent?: number | null;   // 逐章成本（v11）
  usd_spent?: number | null;
}

// M7: 补丁式修订统计（kind: revise | polish）
export interface PatchStatsEntry {
  rounds: number;
  patches: number;
  applied: number;
  failed: number;
}
export type PatchStats = Record<string, PatchStatsEntry>;

export interface PipelineRunDetail extends PipelineRunRecord {
  draft_text: string;
  candidates: CandidateInfo[];
  winner_index?: number | null;
  selected_by?: 'auto' | 'human' | string | null;
  patch_stats?: PatchStats | null;
  quality_dimensions?: Record<string, number> | null;  // {hook,pacing,character,prose}
  state_degraded?: boolean;            // P1#11: 结算降级（正文已落袋，世界状态待修复）
  foreshadow_settle?: {                // P1#6: 伏笔结算报告
    mentions?: number; advances?: number; payoffs?: number;
    new_created?: string[]; rejected?: string[];
  } | null;
  plan_reconcile?: {                   // v16: 未来计划对账
    completed: string[];
    deferred: string[];
    divergence_score: number;
    needs_replan: boolean;
  } | null;
  alignment_report?: AlignmentReport | null;
  recommended_plan_action?: PlanAction | null;
}

// v16: Choice-driven Author Agent
export type AgentInteractionMode = 'guided' | 'copilot' | 'autonomous';
export type AgentStage =
  | 'bootstrap'
  | 'story_plan'
  | 'volume_plan'
  | 'chapter_route'
  | 'chapter_action'
  | 'draft_choice'
  | 'post_chapter'
  | 'finished'
  | string;

export interface AgentStartRequest {
  interaction_mode?: AgentInteractionMode;
  actor?: string;
  governance_mode?: 'human_gate' | 'auto_promote' | 'hybrid';
  hints?: string | null;
  planning_horizon?: number;
  auto_chapters?: number;
}

export interface AgentChoiceRequest {
  action: 'select' | 'auto' | 'reroll';
  option_id?: string | null;
  hints?: string | null;
}

export interface AgentDecisionOption {
  id: string;
  decision_id: string;
  option_index: number;
  title: string;
  summary: string;
  payload: Record<string, unknown>;
  rationale?: string | null;
  impact: Record<string, unknown>;
  score?: number | null;
  selected: boolean;
  created_at?: string;
}

export interface AgentDecision {
  decision_id: string;
  stage: AgentStage;
  scope_type: string;
  scope_id?: string | null;
  question: string;
  status: string;
  recommended_option_id?: string | null;
  selected_option_id?: string | null;
  context: Record<string, unknown>;
  options: AgentDecisionOption[];
}

export interface StoryMilestone {
  id: string;
  arc_id: string;
  title: string;
  summary: string;
  due_start_chapter?: number | null;
  due_end_chapter?: number | null;
  not_before_chapter?: number | null;
  status: string;
  completed_chapter?: number | null;
  target_state?: Record<string, unknown>;
}

export interface StoryArc {
  id: string;
  arc_type: string;
  title: string;
  summary: string;
  objective?: string | null;
  start_chapter: number;
  end_chapter?: number | null;
  priority: number;
  status: string;
  milestones: StoryMilestone[];
}

export interface StoryMapChapter {
  chapter: number;
  title?: string | null;
  goal?: string | null;
  hook_text?: string | null;
  status: string;
  plan_version: number;
  locked: number | boolean;
  source_decision_id?: string | null;
}

export interface StoryMap {
  plan?: {
    id: string;
    version: number;
    title: string;
    premise?: string | null;
    ending_direction?: string | null;
    phase_summary?: string | null;
    status: string;
  } | null;
  arcs: StoryArc[];
  volumes: VolumeInfo[];
  chapters: StoryMapChapter[];
}

export interface AgentSessionResponse {
  session_id: string;
  project_id: string;
  interaction_mode: AgentInteractionMode;
  stage: AgentStage;
  status: 'running' | 'awaiting_choice' | 'finished' | string;
  decision?: AgentDecision | null;
  last_decision?: AgentDecision | null;
  state: Record<string, unknown> & {
    current_chapter?: number | null;
    chapters_generated?: number;
    last_run_id?: string | null;
  };
  story_map: StoryMap;
  error?: string | null;
}

export interface CandidateInfo {
  index: number;
  draft_text: string;
  length: number;
  score?: number | null;
  hard_blocks: number;
  is_winner: boolean;
  proposal_count: number;
  continuity_blocks?: number;
  plan_blocks?: number;
  contract_score?: number | null;
  arc_progress_score?: number | null;
  quality_score?: number | null;
}

export interface NextChapterSuggestion {
  next_chapter: number;
  last_completed_chapter: number;
  suggested_goal: string;
  sources: string[];
}

// P2#13 卷级 Objective/KR
export interface KeyResult {
  id: string;
  text: string;
  status: 'pending' | 'met' | 'partial' | 'missed' | string;
  evidence: string;
}

export interface VolumeInfo {
  id: string;
  volume_no: number;
  title: string;
  synopsis?: string | null;
  start_chapter?: number | null;
  end_chapter?: number | null;
  status: string;
  created_at: string;
  objective?: string | null;
  key_results: KeyResult[];
}

export interface VolumeCreateRequest {
  volume_no: number;
  title: string;
  synopsis?: string | null;
  start_chapter?: number | null;
  end_chapter?: number | null;
  objective?: string | null;
  key_results?: (string | Partial<KeyResult>)[] | null;
}

export interface VolumeUpdateRequest {
  title?: string | null;
  synopsis?: string | null;
  start_chapter?: number | null;
  end_chapter?: number | null;
  status?: 'writing' | 'completed' | 'archived' | null;
  objective?: string | null;
  key_results?: (string | Partial<KeyResult>)[] | null;
}

export interface SettleKrResponse {
  settled: boolean;
  reason?: string;
  objective?: string;
  met?: number;
  partial?: number;
  missed?: number;
  results?: KeyResult[];
}

// POST-v15 P2#6 设定→卷向导
export interface VolumeWizardRequest {
  volume_span?: number;
  plan_chapters?: number;
  hints?: string | null;
}

export interface VolumeWizardResponse {
  volume: VolumeInfo;
  plan?: VolumePlanResponse | null;
  tier_used?: string | null;
  degraded: boolean;
}

// POST-v15 P2#7 三层摘要看板
export interface SummaryChapterItem {
  chapter: number;
  summary: string;
  volume_no?: number | null;
  title?: string | null;
  word_count?: number | null;
  created_at?: string | null;
}

export interface SummaryVolumeItem {
  volume_no: number;
  title: string;
  rolling_summary?: string | null;
  start_chapter?: number | null;
  end_chapter?: number | null;
  status: string;
}

export interface SummaryBoardResponse {
  global_synopsis?: string | null;
  volumes: SummaryVolumeItem[];
  chapters: SummaryChapterItem[];
}

// 项目级记忆管线设置。env_overrides 中的字段由服务端环境变量接管，
// Studio 只展示其最终生效值，不允许误以为项目设置可以覆盖运维配置。
export interface MemorySettings {
  enable_l2: boolean;
  enable_vector: boolean;
  enable_l3: boolean;
  enable_summaries: boolean;
  l2_every_n_chapters: number;
  l2_max_scenes_per_chapter: number;
  vector_top_k: number;
  summary_window_chapters: number;
  embedding_provider: string;
  embedding_model: string;
  env_overrides: string[];
}

export interface MemorySettingsPatch {
  enable_l2?: boolean;
  enable_vector?: boolean;
  enable_l3?: boolean;
  enable_summaries?: boolean;
  l2_every_n_chapters?: number;
  l2_max_scenes_per_chapter?: number;
  vector_top_k?: number;
  summary_window_chapters?: number;
}

export interface MemoryLayerHealth {
  rows: number;
  chapters: number;
  completed_chapters: number;
  coverage_pct: number;
  details: Record<string, number | string | boolean | null>;
}

export interface MemoryHealth {
  latest_chapter: number;
  settings: Record<string, unknown>;
  layers: {
    l0: MemoryLayerHealth;
    l1: MemoryLayerHealth;
    l2: MemoryLayerHealth;
    summaries: MemoryLayerHealth;
    [key: string]: MemoryLayerHealth;
  };
  warnings: string[];
}

export interface MemoryRebuildRequest {
  rebuild_l0?: boolean;
  rebuild_l1?: boolean;
  rebuild_l2?: boolean;
  rebuild_vectors?: boolean;
  force?: boolean;
}

export interface MemoryRebuildResult {
  l0?: Record<string, unknown>;
  l1?: Record<string, unknown>;
  l2?: Record<string, unknown>;
  vectors?: Record<string, unknown>;
  health?: MemoryHealth;
  [key: string]: unknown;
}

export interface MemoryRebuildResponse {
  result: MemoryRebuildResult;
}

export interface PlannedBeat {
  seq: number;
  beat_type: string;
  summary: string;
  value_axis?: string | null;
  arc_id?: string | null;
  constraint_level?: 'soft' | 'hard' | string | null;
  source_decision_id?: string | null;
}

export interface ChapterCard {
  chapter: number;
  title?: string | null;
  goal?: string | null;
  hook_text?: string | null;
  status: string;
  // P1#7 细纲契约
  target_emotion?: string | null;
  opening_hook_type?: string | null;
  hook_type?: string | null;
  expectation_score?: number | null;
  plan_version?: number;
  locked?: number | boolean;
  source_decision_id?: string | null;
  beats: PlannedBeat[];
}

// 大纲闭环：章节契约、实际结果、偏航报告和 append-only 计划版本。
export type ContractItem = string | {
  id?: string;
  text?: string;
  label?: string;
  status?: 'pending' | 'met' | 'missed' | string;
  evidence?: string | null;
};

export interface ChapterContract {
  chapter: number;
  title?: string | null;
  goal?: string | null;
  plan_version_id?: string | null;
  current_plan_version_id?: string | null;
  plan_version?: number;
  locked?: boolean | number;
  premise?: string | null;
  ending_direction?: string | null;
  volume?: {
    volume_no?: number;
    title?: string | null;
    objective?: string | null;
    key_results?: KeyResult[];
  } | null;
  arc_targets: ContractItem[];
  volume_kr_targets: ContractItem[];
  milestone_targets: ContractItem[];
  must_advance: ContractItem[];
  must_resolve: ContractItem[];
  must_include_entities: ContractItem[];
  must_not_reveal: ContractItem[];
  must_not_resolve: ContractItem[];
  forbidden_state_changes: ContractItem[];
  target_state: Record<string, unknown>;
  success_criteria: ContractItem[];
  target_emotion?: string | null;
  opening_hook_type?: string | null;
  hook_type?: string | null;
  expectation_score?: number | null;
  beats: PlannedBeat[];
}

export interface EvidenceSpan {
  label?: string | null;
  quote?: string | null;
  start?: number | null;
  end?: number | null;
  confidence?: number | null;
}

export interface ChapterActuals {
  id?: string;
  chapter: number;
  events: unknown[];
  state_changes: unknown[];
  completed_beats: unknown[];
  arc_progress: unknown[];
  revealed_information: unknown[];
  unresolved_promises: string[];
  involved_entities: string[];
  evidence_spans: EvidenceSpan[];
}

export type PlanAction = 'keep' | 'patch' | 'replan' | 'pause';
export type DivergenceLevel = 'normal' | 'minor' | 'major' | 'severe';

export interface AlignmentReport {
  id?: string;
  chapter: number;
  plan_version_id?: string | null;
  actuals_id?: string | null;
  goal_score: number;
  beat_score: number;
  arc_score: number;
  volume_score: number;
  reveal_score: number;
  divergence_score: number;
  level?: DivergenceLevel | string;
  decision?: string | null;
  recommended_plan_action?: PlanAction | null;
  hard_reveal?: boolean;
  detail?: Record<string, unknown>;
  evidence_spans?: EvidenceSpan[];
  actuals?: ChapterActuals | null;
  created_at?: string | null;
}

export interface ChapterPlanVersion {
  id: string;
  chapter: number;
  version: number;
  payload: Record<string, unknown>;
  status: 'candidate' | 'active' | 'superseded' | 'executed' | 'abandoned' | string;
  source_decision_id?: string | null;
  based_on_chapter?: number | null;
  reason?: string | null;
  divergence_score?: number | null;
  created_at: string;
}

export interface ChapterDraft {
  chapter: number;
  draft_id?: string | null;
  revision_id?: string | null;
  revision_round?: number | null;
  draft_text: string;
  word_count?: number | null;
  updated_at?: string | null;
}

export interface ChapterDraftUpdateRequest {
  draft_text: string;
  base_revision_id: string;
}

export interface PlanningWindowGenerateRequest {
  start_chapter: number;
  horizon: number;
  hints?: string | null;
  candidate_count?: number;
}

export interface PlanningWindowCandidate {
  id?: string;
  title?: string | null;
  summary?: string | null;
  chapters: ChapterCard[];
  score?: number | null;
}

export interface GeneratedPlanWindow {
  decision_id: string;
  start_chapter: number;
  horizon: number;
  candidates: PlanningWindowCandidate[];
  recommended_option_id?: string | null;
}

export interface AppliedPlanWindow {
  decision_id: string;
  selected_option_id?: string | null;
  planned: number[];
  skipped?: number[];
  version_ids?: Record<string, string>;
}

export interface ChapterReplanRequest {
  mode: 'patch' | 'replan';
  horizon: number;
  preserve_locked: boolean;
  reason?: string | null;
  apply?: boolean;
}

export interface ChapterReplanResponse {
  action?: PlanAction;
  decision_id?: string | null;
  window?: GeneratedPlanWindow | null;
  alignment_report?: AlignmentReport | null;
  applied?: AppliedPlanWindow | null;
}

export interface VolumePlanRequest {
  from_chapter?: number | null;
  to_chapter?: number | null;
}

export interface VolumePlanResponse {
  volume_no: number;
  from_chapter: number;
  to_chapter: number;
  planned: ChapterCard[];
  skipped: number[];
  error?: string | null;
}

export interface ChapterCardUpdateRequest {
  title?: string | null;
  goal?: string | null;
  hook_text?: string | null;
  target_emotion?: string | null;
  opening_hook_type?: string | null;
  hook_type?: string | null;
  expectation_score?: number | null;
}

export interface AutopilotStartRequest {
  from_chapter: number;
  to_chapter: number;
  chapter_goals?: Record<string, string>;
  mode?: 'auto_promote' | 'hybrid';
  budget_max_tokens_per_chapter?: number | null;
  budget_max_usd_per_chapter?: number | null;
  budget_session_max_tokens?: number | null;
  budget_session_max_usd?: number | null;
  auto_degrade_after_consecutive_issues?: number;
  quality_check?: boolean;
  n_candidates?: number;
  // v22/F6：每次长输出调用起草的章数；缺省 = 自动（模型容量），1 = 逐章
  batch_size?: number | null;
}

export interface ChapterStat {
  chapter: number;
  word_count?: number | null;
  quality_score?: number | null;
  finished_at?: string | null;
  tokens_spent?: number | null;   // 逐章成本（v11）
  usd_spent?: number | null;
  payoff_closed?: boolean;        // P1#10: 本章有确定性爽点闭环证据
  quality_low?: boolean | null;   // v22/F4: 未过分维度质量门（null=未评分）
  quality_gate_reasons?: string[]; // v22/F4: 未过线项，如 "hook 4<5"
  judge_disagreement?: boolean | null; // v22/F4: 评委正反两序胜者不一致（null=未做两序校验）
}

export interface PipelineStats {
  series: ChapterStat[];
  chapters_completed: number;
  total_words: number;
  avg_quality_score?: number | null;
  low_quality_count: number;
  min_score_threshold: number;
  total_tokens_spent: number;     // 成本曲线汇总（v11）
  total_usd_spent: number;
  payoff_loop_rate?: number | null;  // P1#10: 闭环章/完成章
  judge_runs?: number;               // v22/F4: 做过正反两序校验的章数
  judge_disagreements?: number;      // v22/F4: 两序不一致 → 确定性回落的章数
  judge_disagreement_rate?: number | null;
}

export interface ForeshadowHealth {
  open_count: number;
  overdue_count: number;
  oldest_overdue_chapter?: number | null;
  due_soon: { label: string; due_chapter: number }[];
  status: 'green' | 'yellow' | 'red' | string;
  dormant_count?: number;  // v22/F1: 已休眠的读者可见长/短线承诺数
}

// M8: Autopilot 进度 SSE 事件
// POST-v15 P0#1：reason='degrade_alert' 为降级预警（连续失败自动转人审并暂停）
export interface AutopilotSessionEvent {
  event: 'session';
  reason: 'snapshot' | 'chapter_done' | 'finished' | 'canceled' | 'circuit_broken'
    | 'degrade_alert' | string;
  session: AutopilotSessionInfo;
  chapter?: number;
  consecutive_hard_issues?: number;
}

export interface AutopilotStageEvent {
  event: 'stage';
  chapter: number;
  stage: string;
  status: string;
  detail: Record<string, unknown>;
}

export type AutopilotSSEEvent = AutopilotSessionEvent | AutopilotStageEvent;

export interface AutopilotStreamHandlers {
  onSession?: (e: AutopilotSessionEvent) => void;
  onStage?: (e: AutopilotStageEvent) => void;
}

export interface AutopilotSessionInfo {
  session_id: string;
  project_id: string;
  from_chapter: number;
  to_chapter: number;
  current_chapter: number;
  status: 'running' | 'degraded' | 'paused' | 'circuit_broken' | 'completed' | 'error' | 'canceled' | 'interrupted' | string;
  policy_mode: string;
  chapters_done: number;
  chapters_total: number;
  budget_tokens_total: number;
  budget_usd_total: number;
  pending_reviews: number;
  consecutive_hard_issues: number;
  last_error?: string | null;
  started_at: string;
  finished_at?: string | null;
}

// SSE 事件类型
export interface SSEStageEvent {
  event: 'stage';
  stage: string;
  status: string;
  detail: Record<string, unknown>;
}

export interface SSEDoneEvent {
  event: 'done';
  run_id: string;
  chapter_no: number;
  draft_text: string;
  final_gate: string;
  tokens: number;
  usd: number;
  cache_read_tokens?: number;
  quality_score?: number | null;
  quality_dimensions?: Record<string, number> | null;
  alignment_report?: AlignmentReport | null;
  recommended_plan_action?: PlanAction | null;
  error?: string | null;
}

export interface SSEErrorEvent {
  event: 'error';
  message: string;
  type?: string;
}

export type SSEPipelineEvent = SSEStageEvent | SSEDoneEvent | SSEErrorEvent;

export interface PipelineStreamHandlers {
  onStage?: (e: SSEStageEvent) => void;
  onDone?: (e: SSEDoneEvent) => void;
  onError?: (e: SSEErrorEvent) => void;
}

// ── v22/F6 多章连写（/pipeline/batch, /pipeline/batch/stream）────────────────

export interface BatchRunRequest {
  start_chapter?: number | null;          // 缺省 = 下一章
  chapters?: number | null;               // 1..30；缺省 = 后端 cfg.batch.max_chapters
  chapter_goals?: Record<string, string>; // {章号: 目标}；缺省逐章自动拼装
  interaction_mode?: 'guided' | 'copilot' | 'autonomous' | null;
  mode?: 'human_gate' | 'auto_promote' | 'hybrid' | null;
  budget_max_tokens?: number | null;
  budget_max_usd?: number | null;
  n_candidates?: number | null;           // 仅顺序模式（非长输出模型）生效
  quality_check?: boolean | null;
}

export interface BatchChapterResult {
  chapter: number;
  ok: boolean;
  run_id?: string | null;
  batch_id?: string | null;
  issues: number;
  blocks: number;
  quality_score?: number | null;
  tokens: number;
  usd: number;
  state_degraded: boolean;
  error?: string | null;
}

export interface BatchDiscardedChapter {
  chapter: number;
  batch_id?: string | null;
  reason?: string | null;
}

export type BatchMode = 'batch' | 'sequential';
export type BatchStatus = 'completed' | 'partial' | 'failed' | 'canceled';

export interface BatchRunResponse {
  batch_id?: string | null;
  batch_ids: string[];
  mode: BatchMode;
  status: BatchStatus;
  requested: number[];
  chapters: BatchChapterResult[];
  discarded: BatchDiscardedChapter[];
  regenerations: number;
  next_chapter: number;
  tokens: number;
  usd: number;
  batch_tokens: number;
  batch_usd: number;
  capacity: number;
  error?: string | null;
}

export interface DraftBatchChapterInfo {
  chapter: number;
  seq: number;
  status: 'pending' | 'accepted' | 'rejected' | 'discarded' | string;
  run_id?: string | null;
  reason?: string | null;
  length: number;
  draft_text?: string | null;
  updated_at?: string | null;
}

export interface DraftBatchInfo {
  batch_id: string;
  start_chapter: number;
  end_chapter: number;
  status: 'running' | 'completed' | 'partial' | 'failed' | 'canceled' | string;
  origin: string;
  model_id?: string | null;
  tokens_spent: number;
  usd_spent: number;
  created_at: string;
  updated_at?: string | null;
  detail: Record<string, unknown>;
  counts: Record<string, number>;
}

export interface DraftBatchDetail extends DraftBatchInfo {
  chapters: DraftBatchChapterInfo[];
}

export interface SSEBatchStartEvent {
  event: 'batch_start';
  batch_id?: string | null;
  mode: BatchMode;
  chapters: number[];
  capacity: number;
}

export interface SSEBatchDraftEvent {
  event: 'batch_draft';
  batch_id: string;
  from_chapter: number;
  to_chapter: number;
  parsed: number[];
  rejected: Record<string, string>;
  tokens: number;
  usd: number;
  error?: string | null;      // 连写调用本身失败（成本照常通报）
}

export interface SSEBatchStageEvent {
  event: 'stage';
  chapter: number;
  stage: string;
  status: string;
  detail: Record<string, unknown>;
}

export interface SSEChapterDoneEvent extends BatchChapterResult {
  event: 'chapter_done';
}

export interface SSEBatchDiscardedEvent {
  event: 'batch_discarded';
  batch_id: string;
  from_chapter: number;
  chapters: number[];
  reason?: string | null;
}

export interface SSEBatchRegenerateEvent {
  event: 'batch_regenerate';
  batch_id: string;
  from_chapter: number;
  attempt: number;
}

export interface SSEBatchDoneEvent extends BatchRunResponse {
  event: 'batch_done';
}

export type SSEBatchEvent =
  | SSEBatchStartEvent
  | SSEBatchDraftEvent
  | SSEBatchStageEvent
  | SSEChapterDoneEvent
  | SSEBatchDiscardedEvent
  | SSEBatchRegenerateEvent
  | SSEBatchDoneEvent
  | SSEErrorEvent;

export interface BatchStreamHandlers {
  onStart?: (e: SSEBatchStartEvent) => void;
  onDraft?: (e: SSEBatchDraftEvent) => void;
  onStage?: (e: SSEBatchStageEvent) => void;
  onChapterDone?: (e: SSEChapterDoneEvent) => void;
  onDiscarded?: (e: SSEBatchDiscardedEvent) => void;
  onRegenerate?: (e: SSEBatchRegenerateEvent) => void;
  onDone?: (e: SSEBatchDoneEvent) => void;
  onError?: (e: SSEErrorEvent) => void;
}

export interface ReviewQueueItem {
  candidate_id: string;
  fact_type: string;
  risk_tier: string;
  status: string;
  reason?: string | null;
  proposal_json: string;
  source_chapter: number;
  created_at?: string | null;
}

export interface ApproveRequest {
  actor: string;
  note?: string | null;
  valid_from_chapter_override?: number | null;
}

export interface ApproveResponse {
  candidate_id: string;
  fact_id: string;
  new_status: 'canon';
}

export interface RejectRequest {
  actor: string;
  reason?: string;
}
