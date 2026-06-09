// Studio — 真实接入后端确定性核心的工作台 section（挂在 #studio 锚点）。
// 无需 LLM 即可跑通：seed → bible → state → search → reviews。
// pipeline tab 需后端配置 LLM provider key 才能真正生成。

import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../../api/client';
import { useHealth } from '../../api/hooks';
import type {
  BibleRenderResponse,
  NextChapterSuggestion,
  PipelineRunDetail,
  PipelineRunRecord,
  ProjectResponse,
  ReviewQueueItem,
  SSEDoneEvent,
  SSEStageEvent,
  SearchFactsResponse,
  SeedRequest,
  WorldStateSnapshot,
} from '../../api/types';
import '../../styles/studio.css';

export interface StudioProps {
  activeProjectId: string | null;
  onSelectProject: (id: string | null) => void;
  onRequestCreate: () => void;
  // 项目列表由 App 上提持有（支持新建后乐观注入 + 对账刷新）。
  projects: ProjectResponse[];
  projectsLoading: boolean;
  projectsError: string | null;
  onRefetchProjects: () => void;
}

type TabKey = 'seed' | 'bible' | 'state' | 'search' | 'reviews' | 'pipeline';

const TABS: { key: TabKey; label: string }[] = [
  { key: 'seed', label: '📝 录入设定' },
  { key: 'bible', label: '📖 世界圣经' },
  { key: 'state', label: '🌍 世界状态' },
  { key: 'search', label: '🔍 设定检索' },
  { key: 'reviews', label: '🧑‍⚖️ 审核队列' },
  { key: 'pipeline', label: '⚙️ 生成章节' },
];

function errMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return fallback;
}

// ============================================================
// Studio root
// ============================================================
export default function Studio({
  activeProjectId,
  onSelectProject,
  onRequestCreate,
  projects,
  projectsLoading: projLoading,
  projectsError: projError,
  onRefetchProjects: refetch,
}: StudioProps): JSX.Element {
  const { online, version, loading: healthLoading } = useHealth();
  const [tab, setTab] = useState<TabKey>('seed');

  const apiBase = import.meta.env.VITE_API_BASE || '(dev proxy → :8787)';
  const active = projects.find((p) => p.project_id === activeProjectId) ?? null;

  let statusClass = '';
  let statusText = '检测中…';
  if (online === true) {
    statusClass = 'online';
    statusText = '在线';
  } else if (online === false) {
    statusClass = 'offline';
    statusText = '离线';
  }

  return (
    <section className="studio" id="studio">
      <div className="wrap">
        {/* 标题区 */}
        <div className="studio-head">
          <div className="sh-left">
            <span className="eyebrow">REAL API · 工作台</span>
            <h2 className="cn">
              ⚙️ 工作台<span className="en">STUDIO</span>
            </h2>
            <p>真实调用后端确定性核心：录入设定、渲染世界圣经、投影世界状态、检索、审核与生成。</p>
          </div>
          <div className="studio-status">
            <span className={`status-pill ${statusClass}`}>
              <span className="dot" />
              {healthLoading ? '连接中…' : `引擎${statusText}`}
              {version && <span className="ver">v{version}</span>}
            </span>
            <span className="api-base">
              API_BASE：<b>{apiBase}</b>
            </span>
          </div>
        </div>

        {/* 项目栏 */}
        <div className="proj-bar">
          <span className="pb-label">项目</span>
          <div className="pb-chips">
            {projLoading && <span className="pb-loading">加载项目中…</span>}
            {projError && <span className="pb-error">⚠ {projError}</span>}
            {!projLoading &&
              !projError &&
              projects.map((p) => (
                <button
                  key={p.project_id}
                  type="button"
                  className={`proj-chip${p.project_id === activeProjectId ? ' active' : ''}`}
                  onClick={() => onSelectProject(p.project_id)}
                >
                  {p.name}
                  <span className="pc-meta">{p.genre}</span>
                </button>
              ))}
            {!projLoading && !projError && projects.length === 0 && (
              <span className="pb-loading">暂无项目，先新建一个 →</span>
            )}
          </div>
          <button type="button" className="proj-chip new" onClick={onRequestCreate}>
            ＋ 新建项目
          </button>
        </div>

        {/* active 项目摘要 */}
        {active && (
          <div className="proj-active-card">
            <span className="pac-name">{active.name}</span>
            <span className="pac-genre">{active.genre}</span>
            <span className="pac-spacer" />
            <span className="pac-stat">
              <b>{active.chapter_count}</b> 章
            </span>
            <span className="pac-stat">
              <b>{active.canon_fact_count}</b> canon 设定
            </span>
          </div>
        )}

        {/* 主体 */}
        {!active ? (
          <div className="studio-empty">
            <span className="se-emoji">📚</span>
            <h3 className="cn">还没有选中项目</h3>
            <p>
              选择上方的项目，或新建一本书。建库零外网依赖，下面的录入 / 圣经 / 状态 /
              检索 / 审核全部无需 LLM 即可跑通。
            </p>
            <button type="button" className="nf-btn pink" onClick={onRequestCreate}>
              ＋ 新建项目
            </button>
          </div>
        ) : (
          <>
            <div className="studio-tabs">
              {TABS.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  className={`tab${tab === t.key ? ' active' : ''}`}
                  onClick={() => setTab(t.key)}
                >
                  {t.label}
                </button>
              ))}
            </div>

            {tab === 'seed' && (
              <SeedPanel projectId={active.project_id} onChanged={refetch} />
            )}
            {tab === 'bible' && <BiblePanel projectId={active.project_id} />}
            {tab === 'state' && <StatePanel projectId={active.project_id} />}
            {tab === 'search' && <SearchPanel projectId={active.project_id} />}
            {tab === 'reviews' && (
              <ReviewsPanel projectId={active.project_id} onChanged={refetch} />
            )}
            {tab === 'pipeline' && (
              <PipelinePanel projectId={active.project_id} onChanged={refetch} />
            )}
          </>
        )}
      </div>
    </section>
  );
}

// ============================================================
// (a) Seed 录入设定
// ============================================================
const SEED_FACT_TYPES = [
  'character_trait',
  'power_system',
  'world_rule',
  'relationship',
  'item',
  'location',
  'event',
];

function SeedPanel({
  projectId,
  onChanged,
}: {
  projectId: string;
  onChanged: () => void;
}): JSX.Element {
  const [subject, setSubject] = useState<string>('');
  const [predicate, setPredicate] = useState<string>('');
  const [object, setObject] = useState<string>('');
  const [factType, setFactType] = useState<string>('character_trait');
  const [riskTier, setRiskTier] = useState<string>('low');
  const [autoApprove, setAutoApprove] = useState<boolean>(true);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ approved: number; queued: number } | null>(null);

  const canSubmit =
    subject.trim().length > 0 &&
    predicate.trim().length > 0 &&
    object.trim().length > 0 &&
    !busy;

  const submitSeed = async (req: SeedRequest) => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.seed(projectId, req);
      setResult({ approved: res.auto_approved.length, queued: res.queued.length });
      onChanged();
    } catch (err) {
      setError(errMessage(err, '录入设定失败'));
    } finally {
      setBusy(false);
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    const s = subject.trim();
    void submitSeed({
      proposals: [
        {
          op: 'add',
          fact_type: factType,
          entity: s,
          new: { subject: s, predicate: predicate.trim(), object: object.trim() },
          valid_from_chapter: 1,
          risk_tier: riskTier,
        },
      ],
      auto_approve_low_risk: autoApprove,
      actor: 'web',
    });
  };

  const fillDemo = () => {
    if (busy) return;
    void submitSeed({
      proposals: [
        {
          op: 'add',
          fact_type: 'character_trait',
          entity: '陆天',
          new: { subject: '陆天', predicate: '境界', object: '炼气期' },
          valid_from_chapter: 1,
          risk_tier: 'low',
        },
        {
          op: 'add',
          fact_type: 'character_trait',
          entity: '陆天',
          new: { subject: '陆天', predicate: '性格', object: '坚韧不拔' },
          valid_from_chapter: 1,
          risk_tier: 'low',
        },
        {
          op: 'add',
          fact_type: 'character_trait',
          entity: '陆天',
          new: { subject: '陆天', predicate: '天赋', object: '剑道奇才' },
          valid_from_chapter: 1,
          risk_tier: 'low',
        },
        {
          // medium 风险 → 不自动批准，留待人工审批（去「审核队列」tab 处理）
          op: 'add',
          fact_type: 'character_trait',
          entity: '苏雪',
          new: { subject: '苏雪', predicate: '身份', object: '神秘的青衣女子' },
          valid_from_chapter: 1,
          risk_tier: 'medium',
        },
      ],
      auto_approve_low_risk: true,
      actor: 'web',
    });
  };

  return (
    <div className="studio-panel">
      <div className="panel-head">
        <h3 className="cn">录入设定</h3>
        <span className="ph-hint">三元组 subject · predicate · object → canon 账本</span>
      </div>

      <form className="nf-form" onSubmit={handleSubmit}>
        <div className="nf-field">
          <label htmlFor="sd-subj">
            主体 SUBJECT<span className="req">*</span>
          </label>
          <input
            id="sd-subj"
            className="nf-input"
            type="text"
            placeholder="陆天"
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            disabled={busy}
          />
        </div>
        <div className="nf-field">
          <label htmlFor="sd-pred">
            谓词 PREDICATE<span className="req">*</span>
          </label>
          <input
            id="sd-pred"
            className="nf-input"
            type="text"
            placeholder="境界"
            value={predicate}
            onChange={(e) => setPredicate(e.target.value)}
            disabled={busy}
          />
        </div>
        <div className="nf-field">
          <label htmlFor="sd-obj">
            客体 OBJECT<span className="req">*</span>
          </label>
          <input
            id="sd-obj"
            className="nf-input"
            type="text"
            placeholder="炼气期"
            value={object}
            onChange={(e) => setObject(e.target.value)}
            disabled={busy}
          />
        </div>
        <div className="nf-field">
          <label htmlFor="sd-ft">事实类型 FACT TYPE</label>
          <select
            id="sd-ft"
            className="nf-select"
            value={factType}
            onChange={(e) => setFactType(e.target.value)}
            disabled={busy}
          >
            {SEED_FACT_TYPES.map((ft) => (
              <option key={ft} value={ft}>
                {ft}
              </option>
            ))}
          </select>
        </div>
        <div className="nf-field">
          <label htmlFor="sd-risk">风险等级 RISK TIER</label>
          <select
            id="sd-risk"
            className="nf-select"
            value={riskTier}
            onChange={(e) => setRiskTier(e.target.value)}
            disabled={busy}
          >
            <option value="low">low</option>
            <option value="medium">medium</option>
            <option value="high">high</option>
          </select>
        </div>
        <div className="nf-field" style={{ justifyContent: 'flex-end' }}>
          <label className="nf-check">
            <input
              type="checkbox"
              checked={autoApprove}
              onChange={(e) => setAutoApprove(e.target.checked)}
              disabled={busy}
            />
            自动批准低风险
          </label>
        </div>

        <div className="nf-actions" style={{ gridColumn: '1 / -1' }}>
          <button type="submit" className="nf-btn" disabled={!canSubmit}>
            {busy ? (
              <>
                <span className="nf-spin" /> 提交中…
              </>
            ) : (
              <>⚡ 录入设定</>
            )}
          </button>
          <button type="button" className="nf-btn blue" onClick={fillDemo} disabled={busy}>
            🎲 填充示例
          </button>
        </div>
      </form>

      {result && (
        <div className="nf-msg ok">
          <span>✓</span>
          <span>
            录入成功 —— 自动晋升 canon <b>{result.approved}</b> 条
            {result.queued > 0 ? (
              <>
                ，另有 <b>{result.queued}</b> 条留待人工审批（见「🧑‍⚖️ 审核队列」tab）
              </>
            ) : null}
            。项目统计已刷新。
          </span>
        </div>
      )}
      {error && (
        <div className="nf-msg err">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}
    </div>
  );
}

// ============================================================
// (b) Bible 世界圣经
// ============================================================
function BiblePanel({ projectId }: { projectId: string }): JSX.Element {
  const [data, setData] = useState<BibleRenderResponse | null>(null);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const render = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.bible(projectId);
      setData(res);
    } catch (err) {
      setError(errMessage(err, '渲染世界圣经失败'));
      setData(null);
    } finally {
      setBusy(false);
    }
  }, [projectId]);

  return (
    <div className="studio-panel">
      <div className="panel-head">
        <h3 className="cn">世界圣经</h3>
        <span className="ph-hint">只读 · 由当前 canon 设定确定性渲染</span>
      </div>

      <button type="button" className="nf-btn" onClick={() => void render()} disabled={busy}>
        {busy ? (
          <>
            <span className="nf-spin" /> 渲染中…
          </>
        ) : (
          <>📖 渲染世界圣经</>
        )}
      </button>

      {error && (
        <div className="nf-msg err">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}

      {data && (
        <>
          <div className="bible-box">
            <pre>{data.content || '（当前没有可渲染的 canon 设定，先去「录入设定」补充。）'}</pre>
          </div>
          <div className="bible-meta">
            {Object.entries(data.rendered_from).map(([k, v]) => (
              <span key={k} className="bm-chip">
                {k}：<b>{String(v)}</b>
              </span>
            ))}
            <span className="bm-chip">只读：<b>{String(data.is_readonly)}</b></span>
          </div>
        </>
      )}
    </div>
  );
}

// ============================================================
// (c) State 世界状态
// ============================================================
function StatePanel({ projectId }: { projectId: string }): JSX.Element {
  const [asOf, setAsOf] = useState<string>('99999');
  const [data, setData] = useState<WorldStateSnapshot | null>(null);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const query = async () => {
    setBusy(true);
    setError(null);
    const n = Number.parseInt(asOf, 10);
    try {
      const res = await api.state(projectId, {
        as_of_chapter: Number.isFinite(n) ? n : 99999,
      });
      setData(res);
    } catch (err) {
      setError(errMessage(err, '查询世界状态失败'));
      setData(null);
    } finally {
      setBusy(false);
    }
  };

  const powerRanks = data ? Object.entries(data.power_ranks) : [];

  return (
    <div className="studio-panel">
      <div className="panel-head">
        <h3 className="cn">世界状态</h3>
        <span className="ph-hint">as-of 时点投影 · 角色境界排名</span>
      </div>

      <div className="nf-form">
        <div className="nf-field">
          <label htmlFor="st-asof">截至章节 AS-OF CHAPTER</label>
          <input
            id="st-asof"
            className="nf-input"
            type="number"
            min={1}
            value={asOf}
            onChange={(e) => setAsOf(e.target.value)}
            disabled={busy}
          />
        </div>
        <div className="nf-field" style={{ justifyContent: 'flex-end' }}>
          <button
            type="button"
            className="nf-btn"
            onClick={() => void query()}
            disabled={busy}
          >
            {busy ? (
              <>
                <span className="nf-spin" /> 投影中…
              </>
            ) : (
              <>🌍 投影世界状态</>
            )}
          </button>
        </div>
      </div>

      {error && (
        <div className="nf-msg err">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}

      {data && (
        <>
          {powerRanks.length > 0 ? (
            <div className="nf-table-wrap">
              <table className="nf-table">
                <thead>
                  <tr>
                    <th>角色 / ENTITY</th>
                    <th>境界 / POWER RANK</th>
                  </tr>
                </thead>
                <tbody>
                  {powerRanks.map(([entity, rank]) => (
                    <tr key={entity}>
                      <td className="t-key">{entity}</td>
                      <td className="t-val">{rank}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="nf-hollow">
              截至第 {data.as_of_chapter} 章，暂无境界数据。先在「录入设定」加入
              character_trait（境界）类设定。
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ============================================================
// (d) Search 设定检索
// ============================================================
function SearchPanel({ projectId }: { projectId: string }): JSX.Element {
  const [q, setQ] = useState<string>('');
  const [data, setData] = useState<SearchFactsResponse | null>(null);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const canSearch = q.trim().length > 0 && !busy;

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSearch) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.searchFacts(projectId, q.trim());
      setData(res);
    } catch (err) {
      setError(errMessage(err, '检索失败'));
      setData(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="studio-panel">
      <div className="panel-head">
        <h3 className="cn">设定检索</h3>
        <span className="ph-hint">FTS5 + 实体优先召回</span>
      </div>

      <form className="nf-form" onSubmit={handleSearch}>
        <div className="nf-field full">
          <label htmlFor="se-q">关键词 QUERY</label>
          <input
            id="se-q"
            className="nf-input"
            type="text"
            placeholder="例如：陆天 / 玄铁剑 / 境界"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            disabled={busy}
          />
        </div>
        <div className="nf-actions" style={{ gridColumn: '1 / -1' }}>
          <button type="submit" className="nf-btn" disabled={!canSearch}>
            {busy ? (
              <>
                <span className="nf-spin" /> 检索中…
              </>
            ) : (
              <>🔍 检索设定</>
            )}
          </button>
        </div>
      </form>

      {error && (
        <div className="nf-msg err">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}

      {data && (
        <>
          {data.hits.length > 0 ? (
            <div className="hit-list">
              {data.hits.map((h) => (
                <div key={h.id} className="hit">
                  <div className="hit-top">
                    <span className="hit-id">{h.id}</span>
                    <span className="hit-ch">ch.{h.chapter}</span>
                  </div>
                  <div className="hit-snippet">{h.snippet}</div>
                </div>
              ))}
            </div>
          ) : (
            <div className="nf-hollow">没有命中（mode：{data.mode}）。换个关键词再试。</div>
          )}
        </>
      )}
    </div>
  );
}

// ============================================================
// (e) Reviews 审核队列
// ============================================================
function ReviewsPanel({
  projectId,
  onChanged,
}: {
  projectId: string;
  onChanged: () => void;
}): JSX.Element {
  const [items, setItems] = useState<ReviewQueueItem[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [actingId, setActingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // 审核队列（review_queue pending，来自 pipeline gate）
      // + 暂存待审（fact_candidates proposed，来自 seed 未自动晋升）。
      const [queue, staging] = await Promise.all([
        api.reviews(projectId),
        api.staging(projectId),
      ]);
      // 按 candidate_id 去重，review_queue 条目优先。
      const seen = new Set(queue.map((q) => q.candidate_id));
      const merged = [...queue, ...staging.filter((s) => !seen.has(s.candidate_id))];
      setItems(merged);
    } catch (err) {
      setError(errMessage(err, '加载审核队列失败'));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const approve = async (candidateId: string) => {
    setActingId(candidateId);
    setError(null);
    try {
      await api.approve(projectId, candidateId, { actor: 'web' });
      await load();
      onChanged(); // 晋升 canon → 刷新项目统计
    } catch (err) {
      setError(errMessage(err, '通过失败'));
    } finally {
      setActingId(null);
    }
  };

  const reject = async (candidateId: string) => {
    setActingId(candidateId);
    setError(null);
    try {
      await api.reject(projectId, candidateId, { actor: 'web', reason: '人工拒绝' });
      await load();
      onChanged();
    } catch (err) {
      setError(errMessage(err, '拒绝失败'));
    } finally {
      setActingId(null);
    }
  };

  const riskClass = (tier: string): string => {
    const t = tier.toLowerCase();
    if (t === 'low' || t === 'medium' || t === 'high') return t;
    return 'low';
  };

  return (
    <div className="studio-panel">
      <div className="panel-head">
        <h3 className="cn">审核队列</h3>
        <span className="ph-hint">review_queue（pipeline gate）+ 暂存待审（seed）</span>
        <button
          type="button"
          className="nf-btn ghost sm"
          onClick={() => void load()}
          disabled={loading}
        >
          ↻ 刷新
        </button>
      </div>

      {loading && (
        <span className="nf-loading">
          <span className="nf-spin" /> 加载中…
        </span>
      )}
      {error && (
        <div className="nf-msg err">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}

      {!loading && !error && items.length === 0 && (
        <div className="nf-hollow">审核队列为空 —— 所有设定都已处理。</div>
      )}

      {!loading && items.length > 0 && (
        <div className="review-list">
          {items.map((it) => (
            <div key={it.candidate_id} className="review-card">
              <div className="rc-top">
                <span className="rc-type">{it.fact_type}</span>
                <span className={`risk-tag ${riskClass(it.risk_tier)}`}>{it.risk_tier}</span>
                <span className="rc-spacer" />
                <span className="rc-status">
                  ch.{it.source_chapter} · {it.status}
                </span>
              </div>
              {it.reason && <div className="rc-reason">原因：{it.reason}</div>}
              <pre className="rc-json">{it.proposal_json}</pre>
              <div className="rc-actions">
                <button
                  type="button"
                  className="nf-btn sm"
                  onClick={() => void approve(it.candidate_id)}
                  disabled={actingId === it.candidate_id}
                >
                  {actingId === it.candidate_id ? '处理中…' : '✓ 通过'}
                </button>
                <button
                  type="button"
                  className="nf-btn pink sm"
                  onClick={() => void reject(it.candidate_id)}
                  disabled={actingId === it.candidate_id}
                >
                  ✕ 拒绝
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ============================================================
// (f) Pipeline 生成章节（SSE 流式 + 历史记录）
// ============================================================
function PipelinePanel({
  projectId,
  onChanged,
}: {
  projectId: string;
  onChanged: () => void;
}): JSX.Element {
  const [chapterNo, setChapterNo] = useState<string>('1');
  const [chapterGoal, setChapterGoal] = useState<string>('');
  const [mode, setMode] = useState<'human_gate' | 'auto_promote' | 'hybrid'>('human_gate');
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // 「下一章」自动建议 + 连续生成
  const [suggestion, setSuggestion] = useState<NextChapterSuggestion | null>(null);
  const [chainCount, setChainCount] = useState<string>('1');
  const [chainProgress, setChainProgress] = useState<{ done: number; total: number } | null>(null);
  const stopChainRef = useRef<boolean>(false);
  const chapterNoTouched = useRef<boolean>(false);

  // SSE 流式状态
  const [liveStages, setLiveStages] = useState<SSEStageEvent[]>([]);
  const [liveDraft, setLiveDraft] = useState<string>('');
  const [doneData, setDoneData] = useState<SSEDoneEvent | null>(null);

  // 历史记录
  const [history, setHistory] = useState<PipelineRunRecord[]>([]);
  const [histLoading, setHistLoading] = useState<boolean>(false);
  const [expandedRun, setExpandedRun] = useState<string | null>(null);
  const [runDetails, setRunDetails] = useState<Record<string, PipelineRunDetail>>({});
  const [detailLoading, setDetailLoading] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  const loadHistory = useCallback(async () => {
    setHistLoading(true);
    try {
      const list = await api.listPipelineRuns(projectId);
      setHistory(list);
    } catch { /* ignore */ } finally {
      setHistLoading(false);
    }
  }, [projectId]);

  // 「下一章」建议：取已完成最大章 +1，并自动拼装最优 chapter_goal
  const loadSuggestion = useCallback(async (): Promise<NextChapterSuggestion | null> => {
    try {
      const sug = await api.pipelineNext(projectId);
      setSuggestion(sug);
      if (!chapterNoTouched.current) {
        setChapterNo(String(sug.next_chapter));
      }
      return sug;
    } catch {
      return null;
    }
  }, [projectId]);

  // 首次挂载和 projectId 变化时拉取历史 + 下一章建议
  useEffect(() => { void loadHistory(); }, [loadHistory]);
  useEffect(() => {
    chapterNoTouched.current = false;
    void loadSuggestion();
  }, [loadSuggestion]);

  // 跑单章；返回是否成功（done 事件且无 error），供连续生成判断是否继续
  const runOne = async (no: number, goal: string): Promise<boolean> => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    setBusy(true);
    setError(null);
    setLiveStages([]);
    setLiveDraft('');
    setDoneData(null);

    let ok = false;
    try {
      await api.runPipelineStream(
        projectId,
        {
          chapter_no: no,
          chapter_goal: goal.trim() || undefined,
          mode,
        },
        {
          onStage: (e) => setLiveStages((prev) => [...prev, e]),
          onDone: (e) => {
            ok = !e.error;
            setDoneData(e);
            setLiveDraft(e.draft_text ?? '');
            onChanged();
            void loadHistory();
          },
          onError: (e) => setError(e.message || '生成失败'),
        },
        ctrl.signal,
      );
    } catch (err) {
      if ((err as { name?: string }).name !== 'AbortError') {
        setError(errMessage(err, '生成失败'));
      }
    } finally {
      setBusy(false);
    }
    return ok;
  };

  // 手动模式：按表单里的章节号/目标跑一章
  const run = async () => {
    const n = Number.parseInt(chapterNo, 10);
    await runOne(Number.isFinite(n) ? n : 1, chapterGoal);
    void loadSuggestion();
  };

  // 自动模式：每章先取「下一章」最优建议（章号 + 目标），再连写 N 章
  const runAuto = async () => {
    const total = Math.max(1, Math.min(50, Number.parseInt(chainCount, 10) || 1));
    stopChainRef.current = false;
    chapterNoTouched.current = false;
    setChainProgress({ done: 0, total });
    try {
      for (let i = 0; i < total; i += 1) {
        if (stopChainRef.current) break;
        const sug = await loadSuggestion();
        if (!sug) {
          setError('获取下一章建议失败');
          break;
        }
        setChapterNo(String(sug.next_chapter));
        setChapterGoal(sug.suggested_goal);
        const ok = await runOne(sug.next_chapter, sug.suggested_goal);
        setChainProgress({ done: i + 1, total });
        if (!ok || stopChainRef.current) break;
      }
    } finally {
      setChainProgress(null);
      void loadSuggestion();
    }
  };

  const stopChain = () => {
    stopChainRef.current = true;
    abortRef.current?.abort();
  };

  const toggleExpand = async (runId: string) => {
    if (expandedRun === runId) {
      setExpandedRun(null);
      return;
    }
    setExpandedRun(runId);
    if (!runDetails[runId]) {
      setDetailLoading(runId);
      try {
        const detail = await api.getPipelineRun(projectId, runId);
        setRunDetails((prev) => ({ ...prev, [runId]: detail }));
      } catch { /* ignore */ } finally {
        setDetailLoading(null);
      }
    }
  };

  const stageOrder = ['recall', 'plan', 'draft', 'check', 'gate'];
  const allStages = stageOrder.map((s) => liveStages.find((e) => e.stage === s));
  const hasLiveResult = liveStages.length > 0 || doneData;

  return (
    <div className="studio-panel">
      <div className="panel-head">
        <h3 className="cn">生成章节</h3>
        <span className="ph-hint">SSE 流式 · plan → recall → draft → check → gate</span>
      </div>

      <div className="nf-notice">
        ⚠ <b>需要 LLM provider key</b>：后端配置{' '}
        <code>DEEPSEEK_API_KEY</code>{' '}
        后方能真正生成正文；确定性核心（seed / bible / state / search / reviews）不受影响。
      </div>

      <div className="nf-form">
        <div className="nf-field">
          <label htmlFor="pl-no">章节号 CHAPTER NO</label>
          <input
            id="pl-no"
            className="nf-input"
            type="number"
            min={1}
            value={chapterNo}
            onChange={(e) => {
              chapterNoTouched.current = true;
              setChapterNo(e.target.value);
            }}
            disabled={busy}
          />
        </div>
        <div className="nf-field">
          <label htmlFor="pl-mode">模式 MODE</label>
          <select
            id="pl-mode"
            className="nf-select"
            value={mode}
            onChange={(e) =>
              setMode(e.target.value as 'human_gate' | 'auto_promote' | 'hybrid')
            }
            disabled={busy}
          >
            <option value="human_gate">human_gate · 人审</option>
            <option value="auto_promote">auto_promote · 全自动</option>
            <option value="hybrid">hybrid · 混合</option>
          </select>
        </div>
        <div className="nf-field full">
          <label htmlFor="pl-goal">本章目标 CHAPTER GOAL（可选）</label>
          <textarea
            id="pl-goal"
            className="nf-textarea"
            placeholder="例如：陆天初入宗门，与同门起冲突，露出锋芒。"
            value={chapterGoal}
            onChange={(e) => setChapterGoal(e.target.value)}
            disabled={busy}
          />
        </div>
        <div className="nf-actions" style={{ gridColumn: '1 / -1', display: 'flex', alignItems: 'center', gap: '0.6rem', flexWrap: 'wrap' }}>
          <button type="button" className="nf-btn" onClick={() => void run()} disabled={busy}>
            {busy && !chainProgress ? (
              <>
                <span className="nf-spin" /> 生成中…
              </>
            ) : (
              <>⚙️ 运行流水线</>
            )}
          </button>

          <button
            type="button"
            className="nf-btn"
            onClick={() => void runAuto()}
            disabled={busy}
            title="自动取「已完成最大章 +1」为章号，并按卷大纲 / 章节卡 / 待回收伏笔自动拼装本章目标"
          >
            {chainProgress ? (
              <>
                <span className="nf-spin" /> 连写中 {chainProgress.done}/{chainProgress.total}…
              </>
            ) : (
              <>▶ 下一章 · 自动连写</>
            )}
          </button>

          <label htmlFor="pl-chain" style={{ display: 'flex', alignItems: 'center', gap: '0.35rem', fontSize: '0.8rem', opacity: 0.85 }}>
            连写章数
            <input
              id="pl-chain"
              className="nf-input"
              type="number"
              min={1}
              max={50}
              value={chainCount}
              onChange={(e) => setChainCount(e.target.value)}
              disabled={busy}
              style={{ width: '4.5rem' }}
            />
          </label>

          {busy && (
            <button type="button" className="nf-btn-sm" onClick={stopChain}>
              ⏹ 停止
            </button>
          )}
        </div>

        {suggestion && !busy && (
          <div className="ph-hint" style={{ gridColumn: '1 / -1' }}>
            💡 建议下一章：<b>第 {suggestion.next_chapter} 章</b>
            {suggestion.last_completed_chapter > 0 && (
              <>（已完成至第 {suggestion.last_completed_chapter} 章）</>
            )}
            {suggestion.sources.length > 0 && (
              <>　·　目标依据：{suggestion.sources.join(' / ')}</>
            )}
          </div>
        )}
      </div>

      {/* ── 实时进度条 ─────────────────────────────────── */}
      {hasLiveResult && (
        <>
          <div className="stage-row">
            {stageOrder.map((s) => {
              const ev = allStages[stageOrder.indexOf(s)];
              const cls = ev ? ev.status : busy ? 'pending' : '';
              return (
                <span key={s} className={`stage-pill ${cls}`}>
                  {busy && !ev && <span className="nf-spin" style={{ marginRight: 4 }} />}
                  {ev && <span className="sp-dot" />}
                  {s}
                  {ev && <span className="sp-status">{ev.status}</span>}
                </span>
              );
            })}
          </div>

          {doneData && (
            <div className="run-summary">
              <span className="rs-chip gate">
                final_gate：<b>{doneData.final_gate}</b>
              </span>
              <span className="rs-chip">tokens：<b>{doneData.tokens}</b></span>
              <span className="rs-chip">usd：<b>${doneData.usd.toFixed(4)}</b></span>
            </div>
          )}

          {error && (
            <div className="nf-msg err">
              <span>⚠</span>
              <span>{error}</span>
            </div>
          )}

          {(doneData?.error) && (
            <div className="nf-msg err">
              <span>⚠</span>
              <span>{doneData.error}</span>
            </div>
          )}

          {liveDraft && (
            <div className="bible-box">
              <pre>{liveDraft}</pre>
            </div>
          )}
        </>
      )}

      {/* ── 历史记录 ─────────────────────────────────── */}
      <div className="panel-section-head" style={{ marginTop: '1.5rem' }}>
        <span className="cn" style={{ fontSize: '0.9rem', opacity: 0.8 }}>生成历史</span>
        <button
          type="button"
          className="nf-btn-sm"
          onClick={() => void loadHistory()}
          disabled={histLoading}
          style={{ marginLeft: 'auto' }}
        >
          {histLoading ? '…' : '↻ 刷新'}
        </button>
      </div>

      {history.length === 0 && !histLoading && (
        <div className="nf-empty">尚无生成记录</div>
      )}

      <div className="pipeline-history">
        {history.map((rec) => (
          <div key={rec.run_id} className={`ph-row ${expandedRun === rec.run_id ? 'expanded' : ''}`}>
            <button
              type="button"
              className="ph-header"
              onClick={() => void toggleExpand(rec.run_id)}
            >
              <span className={`ph-status ${rec.status}`}>{rec.status}</span>
              <span className="ph-ch">第 {rec.chapter} 章</span>
              <span className="ph-wc">{rec.word_count != null ? `${rec.word_count} 字` : '—'}</span>
              <span className="ph-time">{rec.started_at.replace('T', ' ').slice(0, 16)}</span>
              <span className="ph-arrow">{expandedRun === rec.run_id ? '▲' : '▼'}</span>
            </button>

            {expandedRun === rec.run_id && (
              <div className="ph-body">
                {detailLoading === rec.run_id ? (
                  <div className="nf-loading">加载中…</div>
                ) : runDetails[rec.run_id]?.draft_text ? (
                  <div className="bible-box">
                    <pre>{runDetails[rec.run_id].draft_text}</pre>
                  </div>
                ) : (
                  <div className="nf-empty">草稿文件不存在或已删除</div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
