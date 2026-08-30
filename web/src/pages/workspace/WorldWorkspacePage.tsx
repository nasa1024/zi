import { useCallback, useEffect, useState } from 'react';
import { api } from '../../api/client';
import type { BibleRenderResponse, SearchFactsResponse, WorldStateSnapshot } from '../../api/types';
import { useWorkspaceOutlet } from '../../components/workspace/WorkspaceShell';
import {
  factTypeLabel,
  searchModeLabel,
  targetStateKeyLabel,
  targetStateValueLabel,
  userFacingError,
} from '../../utils/uiText';

function message(error: unknown, fallback: string): string {
  return userFacingError(error, fallback);
}

function stateFieldLabel(key: string): string {
  if (/[\u3400-\u9fff]/u.test(key)) return key;
  const labels: Record<string, string> = {
    subject: '人物',
    predicate: '关系',
    object: '内容',
    knower_entity_id: '知情者',
    secret_key: '信息',
    knowledge_state: '知情状态',
    secrecy_level: '保密程度',
    value: '数值',
    unit: '单位',
    cooldown: '间隔章数',
    last_use: '最近使用',
  };
  return labels[key] ?? targetStateKeyLabel(key);
}

function displayValue(value: unknown, field?: string): string {
  if (value == null || value === '') return '—';
  if (typeof value === 'string') {
    return field === 'knowledge_state' || field === 'secrecy_level'
      ? targetStateValueLabel(value)
      : value;
  }
  if (typeof value === 'number') return String(value);
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) return value.map((item) => displayValue(item, field)).join('、');
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, nested]) => [stateFieldLabel(key), displayValue(nested, key)].join('：'))
      .join('；');
  }
  return '已记录';
}

export function localizedBibleContent(content: string | null | undefined, fallbackChapter: number): string {
  if (!content?.trim()) return '暂无已确认设定。';
  return content
    .replace(/^#\s*Story Bible（as of ch\.(\d+)）\s*$/gim, '# 已确认设定总览（截至第 $1 章）')
    .replace(
      /^>\s*本文档由\s*`facts`\s*表确定性渲染，只读。共\s*(\d+)\s*条\s*canon facts。?\s*$/gim,
      '> 本档案根据已确认设定自动生成，只读。共 $1 条设定。',
    )
    .replace(/^###\s+([a-z][a-z0-9_]*)\s*$/gim, (_match, type: string) => `### ${factTypeLabel(type)}`)
    .replace(/\*\(ch\.(\d+)\)\*/gi, '*(第 $1 章)*')
    .replace(/\bas of ch\.(\d+)\b/gi, '截至第 $1 章')
    .replace(/\bcanon facts?\b/gi, '已确认设定')
    .replace(/`facts`\s*表/gi, '已确认设定')
    .replace(/^#\s*Story Bible\s*$/gim, `# 已确认设定总览（截至第 ${fallbackChapter} 章）`);
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
      setError(message(loadError, '世界状态加载失败，请稍后重试。'));
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
      setError(message(searchError, '检索失败，请稍后重试。'));
    }
  };

  return (
    <main className="routed-workspace-page world-route-page">
      <header className="route-page-head">
        <div><span>已确认设定 · 查看某章状态</span><h1>世界档案室</h1></div>
        <p>计划放在规划室；这里只查看已经生效的设定与指定章节时点的世界状态。</p>
      </header>

      <div className="world-toolbar">
        <label><span>截至章节</span><input type="number" min={1} value={asOf} onChange={(e) => setAsOf(Number(e.target.value))} /></label>
        <button type="button" onClick={() => void load()} disabled={loading}>查看该章状态</button>
        <label className="world-search"><span>检索已确认设定</span><input value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') void runSearch(); }} placeholder="人物、地点、秘密或物品" /></label>
        <button type="button" onClick={() => void runSearch()}>检索</button>
      </div>

      {error && <div className="workspace-banner" role="alert">{error}</div>}
      {loading ? <div className="workspace-loading"><span className="nf-spin" /> 正在读取该章状态…</div> : (
        <div className="world-ledger-grid">
          <section className="world-bible-card">
            <div className="ledger-card-head"><span>01</span><h2>设定总览</h2><small>只读</small></div>
            <pre>{localizedBibleContent(bible?.content, asOf)}</pre>
          </section>
          <section className="world-state-card">
            <div className="ledger-card-head"><span>02</span><h2>状态快照</h2><small>第 {asOf} 章</small></div>
            <dl>
              <div><dt>力量等级</dt><dd>{Object.keys(state?.power_ranks ?? {}).length}</dd></div>
              <div><dt>人物认知</dt><dd>{state?.knowledge_edges.length ?? 0}</dd></div>
              <div><dt>事件</dt><dd>{state?.timeline_events.length ?? 0}</dd></div>
              <div><dt>物品</dt><dd>{Object.keys(state?.item_ownership ?? {}).length}</dd></div>
            </dl>
            <div className="world-readable-state">
              <section><h3>人物力量等级</h3>{Object.entries(state?.power_ranks ?? {}).length === 0 ? <p>还没有记录人物力量等级。</p> : Object.entries(state?.power_ranks ?? {}).map(([key, value]) => <div key={key}><b>{key}</b><span>{displayValue(value)}</span></div>)}</section>
              <section><h3>物品归属</h3>{Object.entries(state?.item_ownership ?? {}).length === 0 ? <p>还没有记录关键物品。</p> : Object.entries(state?.item_ownership ?? {}).map(([key, value]) => <div key={key}><b>{key}</b><span>{displayValue(value)}</span></div>)}</section>
              <section className="world-recent-events"><h3>最近发生</h3>{(state?.timeline_events ?? []).length === 0 ? <p>这个章节时点还没有时间线事件。</p> : (state?.timeline_events ?? []).slice(-8).map((event, index) => <div key={index}><span>{displayValue(event)}</span></div>)}</section>
              <details>
                <summary>查看人物认知、世界规则与数值设定</summary>
                <div className="world-extra-state">
                  <section>
                    <h3>人物认知</h3>
                    {(state?.knowledge_edges ?? []).length === 0
                      ? <p>还没有记录人物已知道的信息。</p>
                      : (state?.knowledge_edges ?? []).map((item, index) => <div key={index}><span>{displayValue(item)}</span></div>)}
                  </section>
                  <section>
                    <h3>世界规则</h3>
                    {(state?.gimmick_rules ?? []).length === 0
                      ? <p>还没有记录特殊世界规则。</p>
                      : (state?.gimmick_rules ?? []).map((item, index) => <div key={index}><span>{displayValue(item)}</span></div>)}
                  </section>
                  <section>
                    <h3>数值设定</h3>
                    {Object.entries(state?.numeric_facts ?? {}).length === 0
                      ? <p>还没有记录数值设定。</p>
                      : Object.entries(state?.numeric_facts ?? {}).map(([key, value]) => <div key={key}><b>{key}</b><span>{displayValue(value)}</span></div>)}
                  </section>
                </div>
              </details>
            </div>
          </section>
        </div>
      )}

      {search && (
        <section className="world-search-results">
          <div className="ledger-card-head"><span>03</span><h2>检索校样</h2><small>检索方式：{searchModeLabel(search.mode)}</small></div>
          {search.hits.length === 0 ? <p>没有命中已确认设定。</p> : search.hits.map((hit) => (
            <article key={hit.id}><span>第 {hit.chapter} 章</span><p>{hit.snippet}</p></article>
          ))}
        </section>
      )}
    </main>
  );
}
