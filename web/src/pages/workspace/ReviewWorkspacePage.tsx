import { useCallback, useEffect, useState } from 'react';
import { ApiError, api } from '../../api/client';
import type { ReviewQueueItem } from '../../api/types';
import { useWorkspaceOutlet } from '../../components/workspace/WorkspaceShell';

function message(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return '审核队列加载失败';
}

export default function ReviewWorkspacePage(): JSX.Element {
  const { project, refetchProjects } = useWorkspaceOutlet();
  const [items, setItems] = useState<ReviewQueueItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [queue, staging] = await Promise.all([api.reviews(project.project_id), api.staging(project.project_id)]);
      const merged = new Map<string, ReviewQueueItem>();
      staging.forEach((item) => merged.set(item.candidate_id, item));
      queue.forEach((item) => merged.set(item.candidate_id, item));
      setItems(Array.from(merged.values()));
    } catch (loadError) {
      setError(message(loadError));
    } finally {
      setLoading(false);
    }
  }, [project.project_id]);

  useEffect(() => { void load(); }, [load]);

  const decide = async (item: ReviewQueueItem, approve: boolean): Promise<void> => {
    setBusy(item.candidate_id);
    setError(null);
    try {
      if (approve) {
        await api.approve(project.project_id, item.candidate_id, { actor: 'web-author' });
      } else {
        await api.reject(project.project_id, item.candidate_id, { actor: 'web-author', reason: '作者在审核台拒绝' });
      }
      await load();
      refetchProjects();
    } catch (decisionError) {
      setError(message(decisionError));
    } finally {
      setBusy(null);
    }
  };

  return (
    <main className="routed-workspace-page review-route-page">
      <header className="route-page-head">
        <div><span>HUMAN GATE · CANON PRESS</span><h1>审核印刷台</h1></div>
        <p>正文可以自由生长，只有经过这里盖章的事实才会进入 Canon。</p>
      </header>
      {error && <div className="workspace-banner" role="alert">{error}</div>}
      {loading ? <div className="workspace-loading"><span className="nf-spin" /> 正在搬运待审校样…</div> : items.length === 0 ? (
        <section className="review-route-empty"><span>ALL CLEAR</span><h2>没有待审提案</h2><p>下一章生成完成后，新事实会在这里等待作者裁决。</p></section>
      ) : (
        <div className="review-proof-list">
          {items.map((item, index) => (
            <article key={item.candidate_id}>
              <div className="review-proof-no">{String(index + 1).padStart(2, '0')}</div>
              <div className="review-proof-main">
                <div><span>{item.fact_type}</span><b>{item.risk_tier}</b><small>来自第 {item.source_chapter} 章</small></div>
                {item.reason && <p>{item.reason}</p>}
                <pre>{item.proposal_json}</pre>
              </div>
              <div className="review-proof-actions">
                <button type="button" onClick={() => void decide(item, false)} disabled={busy != null}>退稿</button>
                <button type="button" className="primary" onClick={() => void decide(item, true)} disabled={busy != null}>{busy === item.candidate_id ? '处理中…' : '盖章入 Canon'}</button>
              </div>
            </article>
          ))}
        </div>
      )}
    </main>
  );
}
