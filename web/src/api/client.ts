// zi API client.
// Single source of truth for HTTP access; all methods are async and throw
// ApiError on non-2xx responses. See SHARED CONTRACT.

import type {
  AlignmentReport,
  AppliedPlanWindow,
  AgentChoiceRequest,
  AgentSessionResponse,
  AgentStartRequest,
  ApproveRequest,
  ApproveResponse,
  AutopilotSSEEvent,
  AutopilotSessionInfo,
  AutopilotStartRequest,
  AutopilotStreamHandlers,
  BatchRunRequest,
  BatchRunResponse,
  BatchStreamHandlers,
  BibleRenderResponse,
  ChapterCard,
  ChapterCardUpdateRequest,
  ChapterContract,
  ChapterDraft,
  ChapterDraftUpdateRequest,
  ChapterPlanVersion,
  ChapterReplanRequest,
  ChapterReplanResponse,
  DraftBatchDetail,
  DraftBatchInfo,
  ForeshadowHealth,
  GeneratedPlanWindow,
  GenreCatalogItem,
  HealthResponse,
  MemoryHealth,
  MemoryRebuildRequest,
  MemoryRebuildResponse,
  MemorySettings,
  MemorySettingsPatch,
  NextChapterSuggestion,
  PipelineRunDetail,
  PipelineRunRecord,
  PipelineRunRequest,
  PipelineRunResponse,
  PipelineStats,
  PipelineStreamHandlers,
  PlanningWindowGenerateRequest,
  ProjectCreateRequest,
  ProjectResponse,
  ProjectWritingProfilePatch,
  ProjectWritingProfileResponse,
  WritingProfileVersion,
  RejectRequest,
  ReviewQueueItem,
  SearchFactsResponse,
  SeedRequest,
  SeedResponse,
  SettingSuggestRequest,
  SettingSuggestResponse,
  SettleKrResponse,
  SSEBatchEvent,
  SSEPipelineEvent,
  StateQueryRequest,
  StoryMap,
  SummaryBoardResponse,
  SummaryChapterItem,
  VolumeCreateRequest,
  VolumeInfo,
  VolumePlanRequest,
  VolumePlanResponse,
  VolumeUpdateRequest,
  VolumeWizardRequest,
  VolumeWizardResponse,
  WorldStateSnapshot,
  WritingProfileCatalogItem,
} from './types';

// Empty string = relative path; in dev Vite proxy forwards to the backend.
const API_BASE = import.meta.env.VITE_API_BASE ?? '';

const API_KEY_STORAGE = 'nf_api_key';

export class ApiError extends Error {
  status: number;
  code?: string;
  details?: Record<string, unknown>;

  constructor(message: string, status: number, code?: string, details?: Record<string, unknown>) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function setApiKey(k: string | null): void {
  try {
    if (k) {
      localStorage.setItem(API_KEY_STORAGE, k);
    } else {
      localStorage.removeItem(API_KEY_STORAGE);
    }
  } catch {
    // localStorage unavailable (e.g. SSR / private mode) — ignore.
  }
}

export function getApiKey(): string | null {
  try {
    return localStorage.getItem(API_KEY_STORAGE);
  } catch {
    return null;
  }
}

type HttpMethod = 'GET' | 'POST' | 'DELETE' | 'PUT' | 'PATCH';

/**
 * Split an SSE text buffer into complete `data:` payloads.
 * Returns the parsed JSON payloads and the trailing partial line (feed it back
 * with the next chunk). Comment lines (`: ping`) and malformed JSON are skipped.
 */
export function parseSSEBuffer(buffer: string): { events: unknown[]; rest: string } {
  const lines = buffer.split('\n');
  const rest = lines.pop() ?? '';
  const events: unknown[] = [];
  for (const raw of lines) {
    const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw;
    if (!line.startsWith('data: ')) continue;
    try {
      events.push(JSON.parse(line.slice(6)));
    } catch { /* malformed line */ }
  }
  return { events, rest };
}

/** Route one multi-chapter batch SSE payload (v22/F6) to its handler. */
export function dispatchBatchEvent(raw: unknown, handlers: BatchStreamHandlers): void {
  if (!raw || typeof raw !== 'object') return;
  const ev = raw as SSEBatchEvent;
  switch (ev.event) {
    case 'batch_start': handlers.onStart?.(ev); break;
    case 'batch_draft': handlers.onDraft?.(ev); break;
    case 'stage': handlers.onStage?.(ev); break;
    case 'chapter_done': handlers.onChapterDone?.(ev); break;
    case 'batch_discarded': handlers.onDiscarded?.(ev); break;
    case 'batch_regenerate': handlers.onRegenerate?.(ev); break;
    case 'batch_done': handlers.onDone?.(ev); break;
    case 'error': handlers.onError?.(ev); break;
    default: break;
  }
}

function fastApiDetailMessage(detail: unknown): string | undefined {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const messages = detail.flatMap((item) => {
      if (!item || typeof item !== 'object') return [];
      const entry = item as Record<string, unknown>;
      if (typeof entry.msg !== 'string') return [];
      const location = Array.isArray(entry.loc)
        ? entry.loc
            .filter((part) => part !== 'body')
            .map((part) => String(part))
            .join('.')
        : '';
      return [location ? `${location}: ${entry.msg}` : entry.msg];
    });
    return messages.length > 0 ? messages.join('; ') : undefined;
  }
  if (detail && typeof detail === 'object') {
    const entry = detail as Record<string, unknown>;
    if (typeof entry.message === 'string') return entry.message;
    if (typeof entry.msg === 'string') return entry.msg;
  }
  return undefined;
}

async function request<T>(method: HttpMethod, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  const key = getApiKey();
  if (key) {
    headers['Authorization'] = 'Bearer ' + key;
  }

  const res = await fetch(API_BASE + path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    let message = res.statusText || 'Request failed';
    let code: string | undefined;
    let details: Record<string, unknown> | undefined;
    try {
      const text = await res.text();
      if (text) {
        try {
          const data = JSON.parse(text);
          if (data && typeof data === 'object' && data.error && typeof data.error === 'object') {
            message = data.error.message ?? message;
            code = data.error.code;
            if (data.error.details && typeof data.error.details === 'object') {
              details = data.error.details as Record<string, unknown>;
            } else if (data.error.params && typeof data.error.params === 'object') {
              details = data.error.params as Record<string, unknown>;
            }
          } else if (data && typeof data === 'object' && typeof data.message === 'string') {
            message = data.message;
            code = typeof data.code === 'string' ? data.code : undefined;
          } else if (data && typeof data === 'object' && 'detail' in data) {
            const detail = record(data.detail);
            message = fastApiDetailMessage(data.detail) ?? text;
            code = stringOrNull(detail.code) ?? undefined;
            if (detail.params && typeof detail.params === 'object') {
              details = detail.params as Record<string, unknown>;
            }
          } else {
            message = text;
          }
        } catch {
          message = text;
        }
      }
    } catch {
      // body unreadable — keep statusText.
    }
    throw new ApiError(message, res.status, code, details);
  }

  // 204 No Content or empty body → no JSON to parse.
  if (res.status === 204) {
    return undefined as T;
  }
  const text = await res.text();
  if (!text) {
    return undefined as T;
  }
  return JSON.parse(text) as T;
}

function record(value: unknown): Record<string, unknown> {
  return value != null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

function catalogItems(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  const envelope = record(value);
  return Array.isArray(envelope.items) ? envelope.items : [];
}

export function normalizeWritingProfileCatalog(value: unknown): WritingProfileCatalogItem[] {
  return catalogItems(value).map((entry) => {
    const item = record(entry);
    const defaults = record(item.defaults);
    const promptRules = { ...defaults, ...record(item.prompt_rules) };
    const qualityRules = { ...record(item.quality_rules) };
    const chapterRange = Array.isArray(defaults.chapter_word_range)
      ? defaults.chapter_word_range.map(Number).filter(Number.isFinite)
      : Array.isArray(qualityRules.chapter_word_range)
        ? (qualityRules.chapter_word_range as unknown[]).map(Number).filter(Number.isFinite)
        : [];
    if (chapterRange.length >= 2) qualityRules.chapter_word_range = chapterRange.slice(0, 2);
    return {
      id: stringOrNull(item.id) ?? '',
      version: stringOrNull(item.version) ?? '1.0.0',
      name:
        stringOrNull(item.display_name)
        ?? stringOrNull(item.name)
        ?? stringOrNull(item.label)
        ?? stringOrNull(item.id)
        ?? '',
      description: stringOrNull(item.description),
      preview: stringOrNull(item.preview) ?? stringOrNull(item.example),
      language: stringOrNull(item.language) ?? 'zh-CN',
      market: stringOrNull(item.market) ?? 'cn_web',
      format:
        stringOrNull(item.format) ?? stringOrNull(item.publication_format) ?? 'web_serial',
      primary_genre: stringOrNull(item.primary_genre),
      compatible_genres: stringList(item.compatible_genres),
      story_tags: stringList(item.story_tags),
      default_story_tags: stringList(item.default_story_tags),
      audience_rating: stringOrNull(item.audience_rating),
      prompt_rules: promptRules,
      quality_rules: qualityRules,
      prompt_version: stringOrNull(item.prompt_version),
    };
  }).filter((item) => item.id.length > 0);
}

export function normalizeGenreCatalog(value: unknown): GenreCatalogItem[] {
  return catalogItems(value).map((entry) => {
    const item = record(entry);
    const id = stringOrNull(item.id) ?? stringOrNull(item.value) ?? '';
    return {
      id,
      label: stringOrNull(item.label) ?? stringOrNull(item.name) ?? id,
      description: stringOrNull(item.description),
      language: stringOrNull(item.language),
      market: stringOrNull(item.market),
      story_tags: stringList(item.story_tags),
    };
  }).filter((item) => item.id.length > 0);
}

function normalizeWritingProfileVersion(value: unknown): WritingProfileVersion {
  const item = record(value);
  const resolved = record(item.resolved_profile);
  return {
    content_language: stringOrNull(item.content_language) ?? 'zh-CN',
    target_market: stringOrNull(item.target_market) ?? 'cn_web',
    publication_format: stringOrNull(item.publication_format) ?? 'web_serial',
    primary_genre: stringOrNull(item.primary_genre),
    story_tags: stringList(item.story_tags),
    audience_rating: stringOrNull(item.audience_rating),
    content_warnings: stringList(item.content_warnings),
    writing_profile_id:
      stringOrNull(item.writing_profile_id)
      ?? stringOrNull(item.profile_id)
      ?? stringOrNull(resolved.id)
      ?? 'cn_web_novel_v1',
    writing_profile_version:
      Number(item.writing_profile_version ?? item.revision ?? item.profile_version) || 0,
    writing_profile_overrides:
      Object.keys(record(item.writing_profile_overrides)).length > 0
        ? record(item.writing_profile_overrides)
        : record(item.overrides),
    writing_profile_effective_from_chapter:
      Number(item.writing_profile_effective_from_chapter ?? item.effective_from_chapter) || 1,
    created_at: stringOrNull(item.created_at),
    changed_by: stringOrNull(item.changed_by),
    change_reason: stringOrNull(item.change_reason),
    prompt_version: stringOrNull(item.prompt_version) ?? stringOrNull(resolved.prompt_version),
  };
}

export function normalizeProjectWritingProfile(value: unknown): ProjectWritingProfileResponse {
  const envelope = record(value);
  const current = Object.keys(record(envelope.current)).length > 0
    ? record(envelope.current)
    : envelope;
  const selection = normalizeWritingProfileVersion(current);
  let resolved: unknown = Object.keys(record(current.resolved_profile)).length > 0
    ? current.resolved_profile
    : current.profile;
  if (!resolved && stringOrNull(current.writing_profile_id)) {
    resolved = {
      ...current,
      id: current.writing_profile_id,
      version: current.writing_profile_catalog_version ?? current.catalog_version ?? '1.0.0',
      language: current.content_language,
      market: current.target_market,
      format: current.publication_format,
    };
  }
  const normalizedProfiles = resolved ? normalizeWritingProfileCatalog([resolved]) : [];
  return {
    ...selection,
    project_id:
      stringOrNull(current.project_id) ?? stringOrNull(envelope.project_id) ?? '',
    profile: normalizedProfiles[0] ?? null,
    history: Array.isArray(envelope.history)
      ? envelope.history.map(normalizeWritingProfileVersion)
      : Array.isArray(current.history)
        ? current.history.map(normalizeWritingProfileVersion)
        : [],
  };
}

function normalizeChapterCard(value: unknown): ChapterCard {
  const item = record(value);
  const chapter = Number(item.chapter);
  return {
    ...(item as unknown as ChapterCard),
    chapter: Number.isFinite(chapter) ? chapter : 0,
    status: stringOrNull(item.status) ?? 'candidate',
    beats: Array.isArray(item.beats) ? item.beats as ChapterCard['beats'] : [],
  };
}

export function normalizePlanningWindow(value: unknown): GeneratedPlanWindow {
  const raw = record(value);
  const decision = record(raw.decision);
  const decisionOptions = Array.isArray(decision.options) ? decision.options : [];
  const generatedCandidates = Array.isArray(raw.candidates) ? raw.candidates : [];
  // Durable decision options carry the option id required by the approve API.
  // Prefer them over the generator's id-less candidate drafts.
  const options = decisionOptions.length > 0 ? decisionOptions : generatedCandidates;
  const decisionId = stringOrNull(decision.decision_id) ?? stringOrNull(raw.decision_id);
  if (!decisionId) {
    throw new ApiError('规划响应缺少 decision_id，无法批准候选路线', 502);
  }

  return {
    decision_id: decisionId,
    start_chapter: Number(raw.start_chapter) || 1,
    horizon: Number(raw.horizon) || 1,
    recommended_option_id:
      stringOrNull(decision.recommended_option_id) ?? stringOrNull(raw.recommended_option_id),
    candidates: options.map((value) => {
      const option = record(value);
      const payload = record(option.payload);
      const chapters = Array.isArray(payload.chapters)
        ? payload.chapters
        : Array.isArray(option.chapters) ? option.chapters : [];
      return {
        id: stringOrNull(option.id) ?? undefined,
        title: stringOrNull(option.title) ?? stringOrNull(payload.route_title),
        summary: stringOrNull(option.summary) ?? stringOrNull(payload.route_summary),
        chapters: chapters.map(normalizeChapterCard),
        score: typeof option.score === 'number' ? option.score : null,
      };
    }),
  };
}

export function normalizeChapterContract(value: unknown): ChapterContract {
  const envelope = record(value);
  const source = Object.keys(record(envelope.contract)).length > 0
    ? record(envelope.contract)
    : envelope;
  const storyPlan = record(envelope.story_plan);
  const volume = record(envelope.current_volume);
  const version = record(envelope.plan_version);
  const rawKeyResults = Array.isArray(volume.key_results) ? volume.key_results : [];
  const normalizedVolume = Object.keys(volume).length === 0 ? null : {
    ...volume,
    volume_no: Number(volume.volume_no) || undefined,
    title: stringOrNull(volume.title),
    objective: stringOrNull(volume.objective),
    key_results: rawKeyResults.map((value, index) => {
      if (typeof value === 'string') {
        return { id: `kr-${index}`, text: value, status: 'pending', evidence: '' };
      }
      const item = record(value);
      return {
        id: stringOrNull(item.id) ?? `kr-${index}`,
        text: stringOrNull(item.text) ?? stringOrNull(item.title) ?? '未命名 KR',
        status: stringOrNull(item.status) ?? 'pending',
        evidence: stringOrNull(item.evidence) ?? '',
      };
    }),
  } as ChapterContract['volume'];

  const list = (key: string): ChapterContract['arc_targets'] => (
    Array.isArray(source[key]) ? source[key] as ChapterContract['arc_targets'] : []
  );

  return {
    ...(source as unknown as ChapterContract),
    chapter: Number(envelope.chapter ?? source.chapter) || 1,
    premise: stringOrNull(source.premise) ?? stringOrNull(storyPlan.premise),
    ending_direction:
      stringOrNull(source.ending_direction) ?? stringOrNull(storyPlan.ending_direction),
    volume: Object.keys(record(source.volume)).length > 0
      ? source.volume as ChapterContract['volume']
      : normalizedVolume,
    plan_version: Number(source.plan_version ?? version.version) || 1,
    arc_targets: list('arc_targets'),
    volume_kr_targets: list('volume_kr_targets'),
    milestone_targets: list('milestone_targets'),
    must_advance: list('must_advance'),
    must_resolve: list('must_resolve'),
    must_include_entities: list('must_include_entities'),
    must_not_reveal: list('must_not_reveal'),
    must_not_resolve: list('must_not_resolve'),
    forbidden_state_changes: list('forbidden_state_changes'),
    success_criteria: list('success_criteria'),
    target_state: record(source.target_state),
    beats: Array.isArray(source.beats) ? source.beats as ChapterContract['beats'] : [],
  };
}

export function normalizeAlignmentReport(value: unknown): AlignmentReport {
  const raw = record(value);
  const detail = record(raw.detail);
  const actualsEnvelope = record(raw.actuals);
  const actualsPayload = record(actualsEnvelope.payload);
  const actualsSource = Object.keys(actualsPayload).length > 0 ? actualsPayload : actualsEnvelope;
  const findings = Array.isArray(raw.findings) ? raw.findings : [];
  const findingEvidence = findings.flatMap((finding) => {
    const spans = record(finding).evidence_spans;
    return Array.isArray(spans) ? spans : [];
  });
  const evidenceSource = Array.isArray(raw.evidence_spans)
    ? raw.evidence_spans
    : Array.isArray(actualsSource.evidence_spans)
      ? actualsSource.evidence_spans
      : findingEvidence;
  const evidenceSpans = evidenceSource.map((value) => {
    const span = record(value);
    return {
      label: stringOrNull(span.label),
      quote: stringOrNull(span.quote) ?? stringOrNull(span.text),
      start: typeof span.start === 'number'
        ? span.start
        : typeof span.start_char === 'number' ? span.start_char : null,
      end: typeof span.end === 'number'
        ? span.end
        : typeof span.end_char === 'number' ? span.end_char : null,
      confidence: typeof span.confidence === 'number' ? span.confidence : null,
    };
  });
  const actuals = Object.keys(actualsSource).length === 0 ? null : {
    ...(actualsSource as unknown as NonNullable<AlignmentReport['actuals']>),
    id: stringOrNull(actualsEnvelope.id) ?? stringOrNull(actualsSource.id) ?? undefined,
    chapter: Number(actualsSource.chapter ?? raw.chapter) || 1,
    events: Array.isArray(actualsSource.events) ? actualsSource.events : [],
    state_changes: Array.isArray(actualsSource.state_changes) ? actualsSource.state_changes : [],
    completed_beats: Array.isArray(actualsSource.completed_beats) ? actualsSource.completed_beats : [],
    arc_progress: Array.isArray(actualsSource.arc_progress) ? actualsSource.arc_progress : [],
    revealed_information: Array.isArray(actualsSource.revealed_information)
      ? actualsSource.revealed_information
      : [],
    unresolved_promises: Array.isArray(actualsSource.unresolved_promises)
      ? actualsSource.unresolved_promises.map(String)
      : [],
    involved_entities: Array.isArray(actualsSource.involved_entities)
      ? actualsSource.involved_entities.map(String)
      : [],
    evidence_spans: evidenceSpans,
  };
  const action = stringOrNull(raw.recommended_plan_action) ?? stringOrNull(raw.decision);

  return {
    ...(raw as unknown as AlignmentReport),
    chapter: Number(raw.chapter) || 1,
    goal_score: Number(raw.goal_score) || 0,
    beat_score: Number(raw.beat_score) || 0,
    arc_score: Number(raw.arc_score) || 0,
    volume_score: Number(raw.volume_score) || 0,
    reveal_score: Number(raw.reveal_score) || 0,
    divergence_score: Number(raw.divergence_score) || 0,
    level: stringOrNull(raw.level) ?? stringOrNull(raw.divergence_level) ?? undefined,
    recommended_plan_action: action as AlignmentReport['recommended_plan_action'],
    hard_reveal: Boolean(raw.hard_reveal ?? detail.hard_reveal),
    detail,
    evidence_spans: evidenceSpans,
    actuals,
  };
}

function normalizePipelineRunDetail(value: unknown): PipelineRunDetail {
  const raw = record(value);
  const alignment = record(raw.alignment_report);
  return {
    ...(raw as unknown as PipelineRunDetail),
    alignment_report: Object.keys(alignment).length > 0
      ? normalizeAlignmentReport(alignment)
      : undefined,
  };
}

function normalizeAppliedWindow(value: unknown, decisionId: string): AppliedPlanWindow {
  const raw = record(value);
  const versionIds = record(raw.version_ids);
  return {
    decision_id: stringOrNull(raw.decision_id) ?? decisionId,
    selected_option_id: stringOrNull(raw.selected_option_id),
    planned: Array.isArray(raw.planned)
      ? raw.planned.map(Number).filter(Number.isFinite)
      : [],
    skipped: Array.isArray(raw.skipped)
      ? raw.skipped.map(Number).filter(Number.isFinite)
      : [],
    version_ids: Object.fromEntries(
      Object.entries(versionIds).filter((entry): entry is [string, string] => typeof entry[1] === 'string'),
    ),
  };
}

export const api = {
  health(): Promise<HealthResponse> {
    return request<HealthResponse>('GET', '/health');
  },

  listProjects(): Promise<ProjectResponse[]> {
    return request<ProjectResponse[]>('GET', '/v1/projects');
  },

  createProject(req: ProjectCreateRequest): Promise<ProjectResponse> {
    return request<ProjectResponse>('POST', '/v1/projects', req);
  },

  async listWritingProfiles(
    language: string,
    format: string,
    market?: string,
  ): Promise<WritingProfileCatalogItem[]> {
    const params = new URLSearchParams({ language, format });
    if (market) params.set('market', market);
    const response = await request<unknown>(
      'GET',
      `/v1/catalogs/writing-profiles?${params.toString()}`,
    );
    return normalizeWritingProfileCatalog(response);
  },

  async listGenres(language: string, market: string): Promise<GenreCatalogItem[]> {
    const params = new URLSearchParams({ language, market });
    const response = await request<unknown>('GET', `/v1/catalogs/genres?${params.toString()}`);
    return normalizeGenreCatalog(response);
  },

  startAgent(id: string, req: AgentStartRequest): Promise<AgentSessionResponse> {
    return request<AgentSessionResponse>(
      'POST',
      `/v1/${encodeURIComponent(id)}/agent/start`,
      req,
    );
  },

  listAgentSessions(id: string): Promise<AgentSessionResponse[]> {
    return request<AgentSessionResponse[]>(
      'GET',
      `/v1/${encodeURIComponent(id)}/agent/sessions`,
    );
  },

  getAgentSession(id: string, sessionId: string): Promise<AgentSessionResponse> {
    return request<AgentSessionResponse>(
      'GET',
      `/v1/${encodeURIComponent(id)}/agent/${encodeURIComponent(sessionId)}`,
    );
  },

  chooseAgent(
    id: string,
    sessionId: string,
    req: AgentChoiceRequest,
  ): Promise<AgentSessionResponse> {
    return request<AgentSessionResponse>(
      'POST',
      `/v1/${encodeURIComponent(id)}/agent/${encodeURIComponent(sessionId)}/choose`,
      req,
    );
  },

  storyMap(id: string): Promise<StoryMap> {
    return request<StoryMap>('GET', `/v1/${encodeURIComponent(id)}/story-map`);
  },

  async generatePlanningWindow(
    id: string,
    req: PlanningWindowGenerateRequest,
  ): Promise<GeneratedPlanWindow> {
    const response = await request<unknown>(
      'POST',
      `/v1/${encodeURIComponent(id)}/planning/windows/generate`,
      req,
    );
    return normalizePlanningWindow(response);
  },

  async approvePlanningWindow(
    id: string,
    decisionId: string,
    optionId?: string | null,
  ): Promise<AppliedPlanWindow> {
    const response = await request<unknown>(
      'POST',
      `/v1/${encodeURIComponent(id)}/planning/windows/${encodeURIComponent(decisionId)}/approve`,
      { option_id: optionId ?? null },
    );
    return normalizeAppliedWindow(response, decisionId);
  },

  async getChapterContract(id: string, chapter: number): Promise<ChapterContract> {
    const response = await request<unknown>(
      'GET',
      `/v1/${encodeURIComponent(id)}/chapters/${chapter}/contract`,
    );
    return normalizeChapterContract(response);
  },

  async getChapterAlignment(id: string, chapter: number): Promise<AlignmentReport> {
    const response = await request<unknown>(
      'GET',
      `/v1/${encodeURIComponent(id)}/chapters/${chapter}/alignment`,
    );
    return normalizeAlignmentReport(response);
  },

  async replanChapter(
    id: string,
    chapter: number,
    req: ChapterReplanRequest,
  ): Promise<ChapterReplanResponse> {
    const response = await request<unknown>(
      'POST',
      `/v1/${encodeURIComponent(id)}/chapters/${chapter}/replan`,
      req,
    );
    const raw = record(response);
    const decision = record(raw.decision);
    const decisionId = stringOrNull(decision.decision_id) ?? stringOrNull(raw.decision_id);
    const applied = Object.keys(record(raw.applied)).length > 0 && decisionId
      ? normalizeAppliedWindow({ ...record(raw.applied), decision_id: decisionId }, decisionId)
      : null;
    const window = applied ? null : normalizePlanningWindow(response);
    return {
      action: req.mode,
      decision_id: decisionId,
      window,
      applied,
    };
  },

  getChapterDraft(id: string, chapter: number): Promise<ChapterDraft> {
    return request<ChapterDraft>(
      'GET',
      `/v1/${encodeURIComponent(id)}/chapters/${chapter}/draft`,
    );
  },

  updateChapterDraft(
    id: string,
    chapter: number,
    req: ChapterDraftUpdateRequest,
  ): Promise<ChapterDraft> {
    return request<ChapterDraft>(
      'PATCH',
      `/v1/${encodeURIComponent(id)}/chapters/${chapter}/draft`,
      req,
    );
  },

  getChapterPlanVersions(id: string, chapter: number): Promise<ChapterPlanVersion[]> {
    return request<ChapterPlanVersion[]>(
      'GET',
      `/v1/${encodeURIComponent(id)}/chapters/${chapter}/plan-versions`,
    );
  },

  // 生成情节设定候选（建项目前；无 key 或解析失败时返回示例数据 degraded=true）。
  suggestSetting(req: SettingSuggestRequest): Promise<SettingSuggestResponse> {
    return request<SettingSuggestResponse>('POST', '/v1/projects/setup/suggest', req);
  },

  getProject(id: string): Promise<ProjectResponse> {
    return request<ProjectResponse>('GET', `/v1/projects/${encodeURIComponent(id)}`);
  },

  async getWritingProfile(id: string): Promise<ProjectWritingProfileResponse> {
    const response = await request<unknown>(
      'GET',
      `/v1/${encodeURIComponent(id)}/writing-profile`,
    );
    return normalizeProjectWritingProfile(response);
  },

  async updateWritingProfile(
    id: string,
    patch: ProjectWritingProfilePatch,
  ): Promise<ProjectWritingProfileResponse> {
    const response = await request<unknown>(
      'PATCH',
      `/v1/${encodeURIComponent(id)}/writing-profile`,
      patch,
    );
    return normalizeProjectWritingProfile(response);
  },

  archiveProject(id: string): Promise<void> {
    return request<void>('DELETE', `/v1/projects/${encodeURIComponent(id)}`);
  },

  seed(id: string, req: SeedRequest): Promise<SeedResponse> {
    return request<SeedResponse>('POST', `/v1/${encodeURIComponent(id)}/seed`, req);
  },

  bible(id: string, asOf?: number): Promise<BibleRenderResponse> {
    let path = `/v1/${encodeURIComponent(id)}/bible`;
    if (asOf !== undefined && asOf !== null) {
      const params = new URLSearchParams({ as_of_chapter: String(asOf) });
      path += `?${params.toString()}`;
    }
    return request<BibleRenderResponse>('GET', path);
  },

  searchFacts(id: string, q: string, topK?: number): Promise<SearchFactsResponse> {
    const params = new URLSearchParams({ q });
    if (topK !== undefined && topK !== null) {
      params.set('top_k', String(topK));
    }
    return request<SearchFactsResponse>(
      'GET',
      `/v1/${encodeURIComponent(id)}/search/facts?${params.toString()}`,
    );
  },

  state(id: string, req: StateQueryRequest): Promise<WorldStateSnapshot> {
    return request<WorldStateSnapshot>('POST', `/v1/${encodeURIComponent(id)}/state`, req);
  },

  getMemorySettings(id: string): Promise<MemorySettings> {
    return request<MemorySettings>(
      'GET',
      `/v1/${encodeURIComponent(id)}/memory/settings`,
    );
  },

  updateMemorySettings(id: string, req: MemorySettingsPatch): Promise<MemorySettings> {
    return request<MemorySettings>(
      'PATCH',
      `/v1/${encodeURIComponent(id)}/memory/settings`,
      req,
    );
  },

  getMemoryHealth(id: string): Promise<MemoryHealth> {
    return request<MemoryHealth>(
      'GET',
      `/v1/${encodeURIComponent(id)}/memory/health`,
    );
  },

  rebuildMemory(id: string, req: MemoryRebuildRequest): Promise<MemoryRebuildResponse> {
    return request<MemoryRebuildResponse>(
      'POST',
      `/v1/${encodeURIComponent(id)}/memory/rebuild`,
      req,
    );
  },

  runPipeline(id: string, req: PipelineRunRequest): Promise<PipelineRunResponse> {
    return request<PipelineRunResponse>('POST', `/v1/${encodeURIComponent(id)}/pipeline/run`, req);
  },

  async runPipelineStream(
    id: string,
    req: PipelineRunRequest,
    handlers: PipelineStreamHandlers,
    signal?: AbortSignal,
  ): Promise<void> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    };
    const key = getApiKey();
    if (key) headers['Authorization'] = 'Bearer ' + key;

    const res = await fetch(API_BASE + `/v1/${encodeURIComponent(id)}/pipeline/run/stream`, {
      method: 'POST',
      headers,
      body: JSON.stringify(req),
      signal,
    });

    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      throw new ApiError(text || res.statusText, res.status);
    }

    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop()!;
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const ev = JSON.parse(line.slice(6)) as SSEPipelineEvent;
          if (ev.event === 'stage') handlers.onStage?.(ev);
          else if (ev.event === 'done') {
            handlers.onDone?.({
              ...ev,
              alignment_report: ev.alignment_report
                ? normalizeAlignmentReport(ev.alignment_report)
                : ev.alignment_report,
            });
          }
          else if (ev.event === 'error') handlers.onError?.(ev);
        } catch { /* malformed line */ }
      }
    }
  },

  // v22/F6 多章连写：长输出模型一次起草 N 章再逐章校验提交；否则逐章顺序写。
  runBatch(id: string, req: BatchRunRequest): Promise<BatchRunResponse> {
    return request<BatchRunResponse>('POST', `/v1/${encodeURIComponent(id)}/pipeline/batch`, req);
  },

  // 事件：batch_start → [batch_draft] → stage(带 chapter)… → chapter_done …
  // → [batch_discarded → batch_regenerate …] → batch_done；异常推 error。
  async runBatchStream(
    id: string,
    req: BatchRunRequest,
    handlers: BatchStreamHandlers,
    signal?: AbortSignal,
  ): Promise<void> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    };
    const key = getApiKey();
    if (key) headers['Authorization'] = 'Bearer ' + key;

    const res = await fetch(API_BASE + `/v1/${encodeURIComponent(id)}/pipeline/batch/stream`, {
      method: 'POST',
      headers,
      body: JSON.stringify(req),
      signal,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      throw new ApiError(text || res.statusText, res.status);
    }

    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseSSEBuffer(buffer);
      buffer = parsed.rest;
      for (const ev of parsed.events) dispatchBatchEvent(ev, handlers);
    }
  },

  listDraftBatches(id: string, limit = 20): Promise<DraftBatchInfo[]> {
    return request<DraftBatchInfo[]>(
      'GET',
      `/v1/${encodeURIComponent(id)}/pipeline/batches?limit=${limit}`,
    );
  },

  getDraftBatch(id: string, batchId: string, includeText = false): Promise<DraftBatchDetail> {
    return request<DraftBatchDetail>(
      'GET',
      `/v1/${encodeURIComponent(id)}/pipeline/batches/${encodeURIComponent(batchId)}`
        + (includeText ? '?include_text=true' : ''),
    );
  },

  foreshadowHealth(id: string): Promise<ForeshadowHealth> {
    return request<ForeshadowHealth>(
      'GET',
      `/v1/${encodeURIComponent(id)}/foreshadow/health`,
    );
  },

  listVolumes(id: string): Promise<VolumeInfo[]> {
    return request<VolumeInfo[]>('GET', `/v1/${encodeURIComponent(id)}/volumes`);
  },

  createVolume(id: string, req: VolumeCreateRequest): Promise<VolumeInfo> {
    return request<VolumeInfo>('POST', `/v1/${encodeURIComponent(id)}/volumes`, req);
  },

  updateVolume(id: string, volumeNo: number, req: VolumeUpdateRequest): Promise<VolumeInfo> {
    return request<VolumeInfo>(
      'PATCH',
      `/v1/${encodeURIComponent(id)}/volumes/${volumeNo}`,
      req,
    );
  },

  // P2#13: 手动触发本卷 KR 结算（对照卷情节判每条 KR 兑现度并写回）
  settleVolumeKr(id: string, volumeNo: number): Promise<SettleKrResponse> {
    return request<SettleKrResponse>(
      'POST',
      `/v1/${encodeURIComponent(id)}/volumes/${volumeNo}/settle-kr`,
    );
  },

  // POST-v15 P2#6: 设定→卷大纲端到端向导
  volumeWizard(id: string, req: VolumeWizardRequest): Promise<VolumeWizardResponse> {
    return request<VolumeWizardResponse>(
      'POST',
      `/v1/${encodeURIComponent(id)}/volumes/wizard`,
      req,
    );
  },

  // POST-v15 P2#7: 三层摘要看板
  summaryBoard(id: string): Promise<SummaryBoardResponse> {
    return request<SummaryBoardResponse>('GET', `/v1/${encodeURIComponent(id)}/summaries`);
  },

  updateChapterSummary(id: string, chapter: number, summary: string): Promise<SummaryChapterItem> {
    return request<SummaryChapterItem>(
      'PATCH',
      `/v1/${encodeURIComponent(id)}/summaries/chapters/${chapter}`,
      { summary },
    );
  },

  updateVolumeSummary(id: string, volumeNo: number, rollingSummary: string): Promise<void> {
    return request<void>(
      'PATCH',
      `/v1/${encodeURIComponent(id)}/summaries/volumes/${volumeNo}`,
      { rolling_summary: rollingSummary },
    );
  },

  updateGlobalSynopsis(id: string, synopsis: string): Promise<void> {
    return request<void>(
      'PATCH',
      `/v1/${encodeURIComponent(id)}/summaries/global`,
      { synopsis },
    );
  },

  planVolume(id: string, volumeNo: number, req: VolumePlanRequest): Promise<VolumePlanResponse> {
    return request<VolumePlanResponse>(
      'POST',
      `/v1/${encodeURIComponent(id)}/volumes/${volumeNo}/plan`,
      req,
    );
  },

  listChapterCards(id: string, from?: number, to?: number): Promise<ChapterCard[]> {
    const params = new URLSearchParams();
    if (from !== undefined) params.set('from_chapter', String(from));
    if (to !== undefined) params.set('to_chapter', String(to));
    const qs = params.toString();
    return request<ChapterCard[]>(
      'GET',
      `/v1/${encodeURIComponent(id)}/chapter-cards${qs ? `?${qs}` : ''}`,
    );
  },

  updateChapterCard(id: string, chapter: number, req: ChapterCardUpdateRequest): Promise<ChapterCard> {
    return request<ChapterCard>(
      'PATCH',
      `/v1/${encodeURIComponent(id)}/chapter-cards/${chapter}`,
      req,
    );
  },

  autopilotStart(id: string, req: AutopilotStartRequest): Promise<AutopilotSessionInfo> {
    return request<AutopilotSessionInfo>(
      'POST',
      `/v1/${encodeURIComponent(id)}/autopilot/start`,
      req,
    );
  },

  autopilotStatus(id: string): Promise<AutopilotSessionInfo[]> {
    return request<AutopilotSessionInfo[]>(
      'GET',
      `/v1/${encodeURIComponent(id)}/autopilot/status`,
    );
  },

  autopilotCancel(id: string, sessionId: string): Promise<AutopilotSessionInfo> {
    return request<AutopilotSessionInfo>(
      'POST',
      `/v1/${encodeURIComponent(id)}/autopilot/${encodeURIComponent(sessionId)}/cancel`,
    );
  },

  autopilotResume(id: string, sessionId: string): Promise<AutopilotSessionInfo> {
    return request<AutopilotSessionInfo>(
      'POST',
      `/v1/${encodeURIComponent(id)}/autopilot/${encodeURIComponent(sessionId)}/resume`,
    );
  },

  // M8: 挂机进度 SSE（与 runPipelineStream 同款 fetch 流读取）。
  // 流正常结束（终态）后 resolve；网络/服务异常时 reject，调用方回退轮询。
  async autopilotEvents(
    id: string,
    sessionId: string,
    handlers: AutopilotStreamHandlers,
    signal?: AbortSignal,
  ): Promise<void> {
    const headers: Record<string, string> = { Accept: 'text/event-stream' };
    const key = getApiKey();
    if (key) headers['Authorization'] = 'Bearer ' + key;

    const res = await fetch(
      API_BASE + `/v1/${encodeURIComponent(id)}/autopilot/${encodeURIComponent(sessionId)}/events`,
      { headers, signal },
    );
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      throw new ApiError(text || res.statusText, res.status);
    }

    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop()!;
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;   // ": ping" 保活行直接忽略
        try {
          const ev = JSON.parse(line.slice(6)) as AutopilotSSEEvent;
          if (ev.event === 'session') handlers.onSession?.(ev);
          else if (ev.event === 'stage') handlers.onStage?.(ev);
        } catch { /* malformed line */ }
      }
    }
  },

  pipelineNext(id: string): Promise<NextChapterSuggestion> {
    return request<NextChapterSuggestion>(
      'GET',
      `/v1/${encodeURIComponent(id)}/pipeline/next`,
    );
  },

  pipelineStats(id: string): Promise<PipelineStats> {
    return request<PipelineStats>(
      'GET',
      `/v1/${encodeURIComponent(id)}/pipeline/stats`,
    );
  },

  listPipelineRuns(id: string, limit = 30): Promise<PipelineRunRecord[]> {
    return request<PipelineRunRecord[]>(
      'GET',
      `/v1/${encodeURIComponent(id)}/pipeline/runs?limit=${limit}`,
    );
  },

  async getPipelineRun(id: string, runId: string): Promise<PipelineRunDetail> {
    const response = await request<unknown>(
      'GET',
      `/v1/${encodeURIComponent(id)}/pipeline/runs/${encodeURIComponent(runId)}`,
    );
    return normalizePipelineRunDetail(response);
  },

  async selectCandidate(id: string, runId: string, candidateIndex: number): Promise<PipelineRunDetail> {
    const response = await request<unknown>(
      'POST',
      `/v1/${encodeURIComponent(id)}/pipeline/runs/${encodeURIComponent(runId)}/select-candidate`,
      { candidate_index: candidateIndex },
    );
    return normalizePipelineRunDetail(response);
  },

  reviews(id: string): Promise<ReviewQueueItem[]> {
    return request<ReviewQueueItem[]>('GET', `/v1/${encodeURIComponent(id)}/reviews`);
  },

  // 暂存待审：fact_candidates 中 proposed/pending_review（seed 未自动晋升的条目）。
  staging(id: string): Promise<ReviewQueueItem[]> {
    return request<ReviewQueueItem[]>('GET', `/v1/${encodeURIComponent(id)}/staging`);
  },

  approve(id: string, candidateId: string, req: ApproveRequest): Promise<ApproveResponse> {
    return request<ApproveResponse>(
      'POST',
      `/v1/${encodeURIComponent(id)}/reviews/${encodeURIComponent(candidateId)}/approve`,
      req,
    );
  },

  reject(id: string, candidateId: string, req: RejectRequest): Promise<void> {
    return request<void>(
      'POST',
      `/v1/${encodeURIComponent(id)}/reviews/${encodeURIComponent(candidateId)}/reject`,
      req,
    );
  },
};
