// NovelForge API types — mirror of backend Pydantic models.
// Keep field names/shapes in sync with the backend; see SHARED CONTRACT.

export interface HealthResponse {
  status: string;
  version: string;
}

export interface ProjectCreateRequest {
  name: string;
  genre?: string;
  power_system?: string | null;
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
}

export interface StageResult {
  stage: string;
  status: 'ok' | 'blocked' | 'skipped' | 'circuit_broken';
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
}

export interface PipelineRunDetail extends PipelineRunRecord {
  draft_text: string;
}

export interface NextChapterSuggestion {
  next_chapter: number;
  last_completed_chapter: number;
  suggested_goal: string;
  sources: string[];
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
}

export interface AutopilotSessionInfo {
  session_id: string;
  project_id: string;
  from_chapter: number;
  to_chapter: number;
  current_chapter: number;
  status: 'running' | 'degraded' | 'circuit_broken' | 'completed' | 'error' | 'canceled' | 'interrupted' | string;
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
