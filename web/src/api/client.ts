// NovelForge API client.
// Single source of truth for HTTP access; all methods are async and throw
// ApiError on non-2xx responses. See SHARED CONTRACT.

import type {
  AgentChoiceRequest,
  AgentSessionResponse,
  AgentStartRequest,
  ApproveRequest,
  ApproveResponse,
  AutopilotSSEEvent,
  AutopilotSessionInfo,
  AutopilotStartRequest,
  AutopilotStreamHandlers,
  BibleRenderResponse,
  ChapterCard,
  ChapterCardUpdateRequest,
  ForeshadowHealth,
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
  ProjectCreateRequest,
  ProjectResponse,
  RejectRequest,
  ReviewQueueItem,
  SearchFactsResponse,
  SeedRequest,
  SeedResponse,
  SettingSuggestRequest,
  SettingSuggestResponse,
  SettleKrResponse,
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
} from './types';

// Empty string = relative path; in dev Vite proxy forwards to the backend.
const API_BASE = import.meta.env.VITE_API_BASE ?? '';

const API_KEY_STORAGE = 'nf_api_key';

export class ApiError extends Error {
  status: number;
  code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
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
    try {
      const text = await res.text();
      if (text) {
        try {
          const data = JSON.parse(text);
          if (data && typeof data === 'object' && data.error && typeof data.error === 'object') {
            message = data.error.message ?? message;
            code = data.error.code;
          } else if (data && typeof data === 'object' && typeof data.message === 'string') {
            message = data.message;
            code = typeof data.code === 'string' ? data.code : undefined;
          } else if (data && typeof data === 'object' && 'detail' in data) {
            message = fastApiDetailMessage(data.detail) ?? text;
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
    throw new ApiError(message, res.status, code);
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

  // 生成情节设定候选（建项目前；无 key 或解析失败时返回示例数据 degraded=true）。
  suggestSetting(req: SettingSuggestRequest): Promise<SettingSuggestResponse> {
    return request<SettingSuggestResponse>('POST', '/v1/projects/setup/suggest', req);
  },

  getProject(id: string): Promise<ProjectResponse> {
    return request<ProjectResponse>('GET', `/v1/projects/${encodeURIComponent(id)}`);
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
          else if (ev.event === 'done') handlers.onDone?.(ev);
          else if (ev.event === 'error') handlers.onError?.(ev);
        } catch { /* malformed line */ }
      }
    }
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

  getPipelineRun(id: string, runId: string): Promise<PipelineRunDetail> {
    return request<PipelineRunDetail>(
      'GET',
      `/v1/${encodeURIComponent(id)}/pipeline/runs/${encodeURIComponent(runId)}`,
    );
  },

  selectCandidate(id: string, runId: string, candidateIndex: number): Promise<PipelineRunDetail> {
    return request<PipelineRunDetail>(
      'POST',
      `/v1/${encodeURIComponent(id)}/pipeline/runs/${encodeURIComponent(runId)}/select-candidate`,
      { candidate_index: candidateIndex },
    );
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
