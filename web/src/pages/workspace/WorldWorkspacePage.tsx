import { useCallback, useEffect, useState } from 'react';
import { ApiError, api } from '../../api/client';
import type { BibleRenderResponse, SearchFactsResponse, WorldStateSnapshot } from '../../api/types';
import { useWorkspaceOutlet } from '../../components/workspace/WorkspaceShell';

function message(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return '世界状态加载失败';
}

export default function WorldWorkspacePage(): JSX.Element {
  const { project } = useWorkspaceOutlet();
  const [asOf, setAsOf] = useState(Math.max(1, project.chapter_count || 1));
  const [bible, setBible] = useState<BibleRenderResponse | null>(null);
  const [state, setState] = useState<WorldStateSnapshot | null>(null);
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState<SearchFactsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [nextBible, nextState] = await Promise.all([
        api.bible(project.project_id, asOf),
        api.state(project.project_id, { as_of_chapter: asOf }),
      ]);
      setBible(nextBible);
      setState(nextState);
    } catch (loadError) {
      setError(message(loadError));
    } finally {
      setLoading(false);
    }
  }, [asOf, project.project_id]);

  useEffect(() => { void load(); }, [load]);

  const runSearch = async (): Promise<void> => {
    if (!query.trim()) return;
    setError(null);
    try {
      setSearch(await api.searchFacts(project.project_id, query.trim(), 12));
    } catch (searchError) {
      setError(message(searchError));
    }
  };

  return (
    <main className="routed-workspace-page world-route-page">
      <header className="route-page-head">
        <div><span>CANON · AS-OF PROJECTION</span><h1>世界档案室</h1></div>
        <p>计划放在规划室；这里只查看已经生效的 Canon 与指定章节时点的世界状态。</p>
      </header>

      <div className="world-toolbar">
        <label><span>截至章节</span><input type="number" min={1} value={asOf} onChange={(e) => setAsOf(Number(e.target.value))} /></label>
        <button type="button" onClick={() => void load()} disabled={loading}>重新投影</button>
        <label className="world-search"><span>检索 Canon</span><input value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') void runSearch(); }} placeholder="人物、地点、秘密或物品" /></label>
        <button type="button" onClick={() => void runSearch()}>检索</button>
      </div>

      {error && <div className="workspace-banner" role="alert">{error}</div>}
      {loading ? <div className="workspace-loading"><span className="nf-spin" /> 正在投影世界状态…</div> : (
        <div className="world-ledger-grid">
          <section className="world-bible-card">
            <div className="ledger-card-head"><span>01</span><h2>世界圣经</h2><small>READ ONLY</small></div>
            <pre>{bible?.content || '暂无 Canon 设定。'}</pre>
          </section>
          <section className="world-state-card">
            <div className="ledger-card-head"><span>02</span><h2>状态快照</h2><small>CH.{asOf}</small></div>
            <dl>
              <div><dt>境界</dt><dd>{Object.keys(state?.power_ranks ?? {}).length}</dd></div>
              <div><dt>知识边</dt><dd>{state?.knowledge_edges.length ?? 0}</dd></div>
              <div><dt>事件</dt><dd>{state?.timeline_events.length ?? 0}</dd></div>
              <div><dt>物品</dt><dd>{Object.keys(state?.item_ownership ?? {}).length}</dd></div>
            </dl>
            <pre>{JSON.stringify(state, null, 2)}</pre>
          </section>
        </div>
      )}

      {search && (
        <section className="world-search-results">
          <div className="ledger-card-head"><span>03</span><h2>检索校样</h2><small>{search.mode}</small></div>
          {search.hits.length === 0 ? <p>没有命中 Canon。</p> : search.hits.map((hit) => (
            <article key={hit.id}><span>CH.{hit.chapter}</span><p>{hit.snippet}</p></article>
          ))}
        </section>
      )}
    </main>
  );
}
