// NovelForge API client.
// Single source of truth for HTTP access; all methods are async and throw
// ApiError on non-2xx responses. See SHARED CONTRACT.

import type {
  ApproveRequest,
  ApproveResponse,
  BibleRenderResponse,
  HealthResponse,
  PipelineRunRequest,
  PipelineRunResponse,
  ProjectCreateRequest,
  ProjectResponse,
  RejectRequest,
  ReviewQueueItem,
  SearchFactsResponse,
  SeedRequest,
  SeedResponse,
  StateQueryRequest,
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

  runPipeline(id: string, req: PipelineRunRequest): Promise<PipelineRunResponse> {
    return request<PipelineRunResponse>('POST', `/v1/${encodeURIComponent(id)}/pipeline/run`, req);
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
