import { useCallback, useEffect, useState } from 'react';
import { api } from '../../api/client';
import type { ReviewQueueItem } from '../../api/types';
import { useWorkspaceOutlet } from '../../components/workspace/WorkspaceShell';
import {
  factTypeLabel,
  riskTierLabel,
  targetStateKeyLabel,
  userFacingError,
} from '../../utils/uiText';

const PROPOSAL_FIELD_LABELS: Readonly<Record<string, string>> = {
  subject: '对象',
  predicate: '变化',
  object: '内容',
  statement: '事实',
  canon_kind: '设定类别',
  fact_type: '设定类别',
  facet: '所属状态',
  from: '原状态',
  to: '新状态',
  valid_from_chapter: '生效章节',
  name: '名称',
  description: '说明',
  op: '变更方式',
  entity: '设定对象',
  entity_id: '设定对象',
  target_fact_id: '关联的已有设定',
  old: '变更前',
  new: '变更后',
  reason: '变更原因',
  note: '补充说明',
  status: '处理状态',
};

const PROPOSAL_ACTION_LABELS: Readonly<Record<string, string>> = {
  add: '新增设定',
  update: '更新设定',
  retcon: '改写已有设定',
  deprecate: '停用设定',
};

function message(error: unknown, fallback: string): string {
  return userFacingError(error, fallback);
}

function proposalFieldLabel(key: string): string {
  if (/[\u3400-\u9fff]/u.test(key)) return key;
  return PROPOSAL_FIELD_LABELS[key] ?? '补充信息';
}

function proposalValue(value: unknown, key?: string): string {
  if (value == null || value === '') return '未填写';
  if (key === 'op') return PROPOSAL_ACTION_LABELS[String(value)] ?? '设定变更';
  if (key === 'status') {
    const statuses: Record<string, string> = {
      proposed: '等待处理',
      pending_review: '等待审核',
      approved: '已确认',
      rejected: '已退回',
    };
    return statuses[String(value)] ?? '状态待确认';
  }
  if (key === 'canon_kind' || key === 'fact_type') return factTypeLabel(value);
  if (key === 'facet') return targetStateKeyLabel(value);
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) return value.map((item) => proposalValue(item, key)).join('、');
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([nestedKey, nested]) => `${proposalFieldLabel(nestedKey)}：${proposalValue(nested, nestedKey)}`)
      .join('；');
  }
  return '已记录';
}

function proposalRecordFields(value: string): Array<[string, string]> {
  try {
    const payload = JSON.parse(value) as Record<string, unknown>;
    return Object.entries(payload).map(([key, entry], index) => [
      PROPOSAL_FIELD_LABELS[key] ?? (/[\u3400-\u9fff]/u.test(key) ? key : `补充信息 ${index + 1}`),
      proposalValue(entry, key),
    ]);
  } catch {
    return [];
  }
}

function proposalReason(reason: string | null | undefined, fallback: string): string {
  const text = reason?.trim();
  if (!text) return fallback;
  if (/\b[a-z]+(?:_[a-z0-9]+)+\b/i.test(text)) return fallback;
  return text;
}

function proposalSummary(value: string): { action: string; subject: string; detail: string; fields: Array<[string, string]> } {
  try {
    const payload = JSON.parse(value) as Record<string, unknown>;
    const next = payload.new && typeof payload.new === 'object'
      ? payload.new as Record<string, unknown>
      : {};
    const subject = String(payload.entity ?? payload.entity_id ?? next.name ?? next.entity ?? '未命名对象');
    const entries = Object.entries(next).slice(0, 6).map(([key, entry]) => [
      proposalFieldLabel(key),
      proposalValue(entry, key),
    ] as [string, string]);
    return {
      action: PROPOSAL_ACTION_LABELS[String(payload.op ?? 'add')] ?? '设定变更',
      subject,
      detail: proposalReason(
        String(payload.reason ?? payload.note ?? payload.description ?? ''),
        '这条信息确认后可供后续章节引用。',
      ),
      fields: entries,
    };
  } catch {
    return {
      action: '设定变更',
      subject: '待确认内容',
      detail: '这条记录暂时无法整理为摘要，请刷新后重试。',
      fields: [],
    };
  }
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
      setError(message(loadError, '审核队列加载失败，请稍后重试。'));
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
      setError(message(decisionError, approve ? '确认设定失败，请稍后重试。' : '退回提案失败，请稍后重试。'));
    } finally {
      setBusy(null);
    }
  };

  return (
    <main className="routed-workspace-page review-route-page">
      <header className="route-page-head">
        <div><span>人工审核 · 设定确认</span><h1>审核印刷台</h1></div>
        <p>正文可以自由生长，只有经过这里盖章的事实才会成为已确认设定。</p>
      </header>
      {error && <div className="workspace-banner" role="alert">{error}</div>}
      {loading ? <div className="workspace-loading"><span className="nf-spin" /> 正在搬运待审校样…</div> : items.length === 0 ? (
        <section className="review-route-empty"><span>全部处理完成</span><h2>没有待确认设定</h2><p>下一章生成完成后，新设定会在这里等待你确认。</p></section>
      ) : (
        <div className="review-proof-list">
          {items.map((item, index) => (
            <article key={item.candidate_id}>
              <div className="review-proof-no">{String(index + 1).padStart(2, '0')}</div>
              <div className="review-proof-main">
                <div><span>{factTypeLabel(item.fact_type)}</span><b>{riskTierLabel(item.risk_tier)}</b><small>来自第 {item.source_chapter} 章</small></div>
                {(() => {
                  const summary = proposalSummary(item.proposal_json);
                  const recordFields = proposalRecordFields(item.proposal_json);
                  return (
                    <div className="review-readable-change">
                      <span>{summary.action}</span>
                      <h3>{summary.subject}</h3>
                      <p>{proposalReason(item.reason, summary.detail)}</p>
                      {summary.fields.length > 0 && (
                        <dl>{summary.fields.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
                      )}
                      {recordFields.length > 0 && (
                        <details>
                          <summary>查看全部变更信息</summary>
                          <dl>{recordFields.map(([key, value], fieldIndex) => <div key={`${key}-${fieldIndex}`}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
                        </details>
                      )}
                    </div>
                  );
                })()}
              </div>
              <div className="review-proof-actions">
                <button type="button" onClick={() => void decide(item, false)} disabled={busy != null}>退稿</button>
                <button type="button" className="primary" onClick={() => void decide(item, true)} disabled={busy != null}>{busy === item.candidate_id ? '处理中…' : '确认这条设定'}</button>
              </div>
            </article>
          ))}
        </div>
      )}
    </main>
  );
}
