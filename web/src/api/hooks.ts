// React hooks over the NovelForge API client. See SHARED CONTRACT.

import { useCallback, useEffect, useState } from 'react';
import { ApiError, api } from './client';
import type { ProjectResponse } from './types';

export interface UseHealthResult {
  online: boolean | null;
  version: string | null;
  loading: boolean;
  refetch: () => void;
}

// Fetches /health once on mount. online=null while loading; catch → false.
export function useHealth(): UseHealthResult {
  const [online, setOnline] = useState<boolean | null>(null);
  const [version, setVersion] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  const refetch = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setOnline(null);
    api
      .health()
      .then((res) => {
        if (cancelled) return;
        setOnline(true);
        setVersion(res.version);
      })
      .catch(() => {
        if (cancelled) return;
        setOnline(false);
        setVersion(null);
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const cancel = refetch();
    return cancel;
  }, [refetch]);

  return { online, version, loading, refetch };
}

export interface UseProjectsResult {
  projects: ProjectResponse[];
  loading: boolean;
  error: string | null;
  refetch: () => void;
  // 乐观注入一个刚创建的项目，避免“创建成功→列表未刷新→空态闪烁”。
  // 已存在同 id 则替换，否则追加。后续 refetch 会与后端对账。
  addProject: (p: ProjectResponse) => void;
}

export function useProjects(): UseProjectsResult {
  const [projects, setProjects] = useState<ProjectResponse[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const addProject = useCallback((p: ProjectResponse) => {
    setProjects((prev) => {
      const idx = prev.findIndex((x) => x.project_id === p.project_id);
      if (idx === -1) return [...prev, p];
      const next = prev.slice();
      next[idx] = p;
      return next;
    });
  }, []);

  const refetch = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .listProjects()
      .then((res) => {
        if (cancelled) return;
        setProjects(res);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError) {
          setError(err.message);
        } else if (err instanceof Error) {
          setError(err.message);
        } else {
          setError('加载项目失败');
        }
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const cancel = refetch();
    return cancel;
  }, [refetch]);

  return { projects, loading, error, refetch, addProject };
}
