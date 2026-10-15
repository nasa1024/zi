// v22/F6 多章连写：一次长输出调用起草 N 章，再逐章走完整校验/修订/提交流水线。
// 非长输出模型由后端自动退化为逐章顺序写。进度按章分组展示（SSE stage 带 chapter）。

import { useRef, useState } from 'react';
import { ApiError, api } from '../../api/client';
import type {
  SSEBatchDoneEvent,
  SSEBatchStageEvent,
  SSEBatchStartEvent,
  SSEChapterDoneEvent,
} from '../../api/types';

const STAGE_ORDER = ['recall', 'plan', 'draft', 'check', 'gate'];

const REASON_LABELS: Record<string, string> = {
  missing: '未产出',
  empty: '空章',
  truncated: '输出被截断',
  too_short: '字数不足',
  language_mismatch: '语言不符',
  canceled: '已停止',
  chapter_failed: '本章校验失败',
  unresolved_blocks: '残留硬问题',
};

function reasonLabel(reason?: string | null): string {
  if (!reason) return '—';
  const [head, chapter] = reason.split(':');
  if (chapter && head.startsWith('after_')) return `第 ${chapter} 章之后的依赖章`;
  if (head.startsWith('draft_')) return REASON_LABELS[head.slice(6)] ?? reason;
  return REASON_LABELS[head] ?? reason;
}

function errMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return fallback;
}

export interface BatchDraftPanelProps {
  projectId: string;
  mode: 'human_gate' | 'auto_promote' | 'hybrid';
  nCandidates: number;
  qualityCheck: boolean;
  disabled: boolean;
  onBusyChange: (busy: boolean) => void;
  onChanged: () => void;
}

export default function BatchDraftPanel({
  projectId,
  mode,
  nCandidates,
  qualityCheck,
  disabled,
  onBusyChange,
  onChanged,
}: BatchDraftPanelProps): JSX.Element {
  const [count, setCount] = useState<string>('5');
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [start, setStart] = useState<SSEBatchStartEvent | null>(null);
  const [stages, setStages] = useState<Record<number, SSEBatchStageEvent[]>>({});
  const [done, setDone] = useState<SSEChapterDoneEvent[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const [summary, setSummary] = useState<SSEBatchDoneEvent | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const run = async () => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const total = Math.max(1, Math.min(30, Number.parseInt(count, 10) || 5));
    setBusy(true);
    onBusyChange(true);
    setError(null);
    setStart(null);
    setStages({});
    setDone([]);
    setNotes([]);
    setSummary(null);
    const note = (line: string) => setNotes((prev) => [...prev, line]);
    try {
      await api.runBatchStream(
        projectId,
        {
          chapters: total,
          mode,
          n_candidates: nCandidates > 1 ? nCandidates : undefined,
          quality_check: qualityCheck || undefined,
        },
        {
          onStart: (e) => setStart(e),
          onDraft: (e) => {
            if (e.error) {
              note(`✍ 连写调用失败（第 ${e.from_chapter}–${e.to_chapter} 章）：${e.error}`);
              return;
            }
            const rejected = Object.entries(e.rejected)
              .map(([ch, reason]) => `第 ${ch} 章${reasonLabel(reason)}`)
              .join('、');
            note(`✍ 连写调用：第 ${e.from_chapter}–${e.to_chapter} 章，切分成功 ${e.parsed.length} 章`
              + (rejected ? `（未通过：${rejected}）` : ''));
          },
          onStage: (e) => setStages((prev) => ({
            ...prev,
            [e.chapter]: [...(prev[e.chapter] ?? []), e],
          })),
          onChapterDone: (e) => {
            setDone((prev) => [...prev, e]);
            onChanged();
          },
          onDiscarded: (e) => note(
            `🗑 丢弃第 ${e.chapters.join('、')} 章（${reasonLabel(e.reason)}）`,
          ),
          onRegenerate: (e) => note(`🔁 从第 ${e.from_chapter} 章重写（第 ${e.attempt} 次）`),
          onDone: (e) => {
            setSummary(e);
            if (e.error) setError(e.error);
          },
          onError: (e) => setError(e.message || '连写失败'),
        },
        ctrl.signal,
      );
    } catch (err) {
      if ((err as { name?: string }).name !== 'AbortError') {
        setError(errMessage(err, '连写失败'));
      }
    } finally {
      setBusy(false);
      onBusyChange(false);
      onChanged();
    }
  };

  // 断开流 = 后端在当前章结束后停止（剩余章作废，进度不受影响）
  const stop = () => abortRef.current?.abort();

  const chapters = start?.chapters ?? [];
  const lastDone = (chapter: number): SSEChapterDoneEvent | undefined =>
    [...done].reverse().find((e) => e.chapter === chapter);

  return (
    <>
      <div className="panel-section-head" style={{ marginTop: '1.5rem' }}>
        <span className="cn" style={{ fontSize: '0.9rem', opacity: 0.8 }}>📚 多章连写 BATCH</span>
      </div>
      <div className="ph-hint">
        长输出模型（如 Gemini 4）一次调用连续起草多章，每章带各自的契约与节拍；
        随后逐章执行一致性校验、修订、质量门控与结算。某章未通过时丢弃其后章节并从该章重写。
        非长输出模型自动退化为逐章顺序写。上方「模式 / 候选稿数 / 质量评分」同样生效；
        停止或关闭页面会在当前章完成后停下（已提交章节保留）。需要后台长跑请用下方挂机连写。
      </div>

      <div className="nf-actions" style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', flexWrap: 'wrap', marginTop: '0.6rem' }}>
        <label htmlFor="bt-count" style={{ display: 'flex', alignItems: 'center', gap: '0.35rem', fontSize: '0.8rem', opacity: 0.85 }}>
          章数
          <input
            id="bt-count"
            className="nf-input"
            type="number"
            min={1}
            max={30}
            value={count}
            onChange={(e) => setCount(e.target.value)}
            disabled={busy}
            style={{ width: '4.5rem' }}
          />
        </label>
        <button
          type="button"
          className="nf-btn"
          onClick={() => void run()}
          disabled={busy || disabled}
          title="从「下一章」起连续写 N 章"
        >
          {busy ? (
            <>
              <span className="nf-spin" /> 连写中 {done.filter((e) => e.ok).length}/{chapters.length || '…'}
            </>
          ) : (
            <>📚 连写 {Math.max(1, Math.min(30, Number.parseInt(count, 10) || 5))} 章</>
          )}
        </button>
        {busy && (
          <button type="button" className="nf-btn-sm" onClick={stop} title="当前章完成后停止，剩余章作废">
            ⏹ 停止连写
          </button>
        )}
      </div>

      {start && (
        <div className="run-summary" style={{ marginTop: '0.6rem' }}>
          <span className="rs-chip gate">
            模式：<b>{start.mode === 'batch' ? '长输出连写' : '逐章顺序写'}</b>
          </span>
          {chapters.length > 0 && (
            <span className="rs-chip">
              第 {chapters[0]}–{chapters[chapters.length - 1]} 章
            </span>
          )}
          {start.mode === 'batch' && (
            <span className="rs-chip" title="单次连写调用最多起草的章数（模型输出预算）">
              单次容量：<b>{start.capacity}</b> 章
            </span>
          )}
        </div>
      )}

      {chapters.length > 0 && (
        <div className="pipeline-history" style={{ marginTop: '0.6rem' }}>
          {chapters.map((chapter) => {
            const events = stages[chapter] ?? [];
            const latest = (stage: string) =>
              [...events].reverse().find((e) => e.stage === stage);
            const result = lastDone(chapter);
            return (
              <div key={chapter} className="ph-row">
                <div className="ph-body" style={{ display: 'grid', gridTemplateColumns: '5rem 1fr', gap: '0.5rem', alignItems: 'center', padding: '0.4rem 0.75rem' }}>
                  <div className="ph-ch">第 {chapter} 章</div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.35rem', alignItems: 'center' }}>
                    {STAGE_ORDER.map((stage) => {
                      const ev = latest(stage);
                      const pending = busy && !ev && !result;
                      return (
                        <span key={stage} className={`stage-pill ${ev ? ev.status : ''}`}>
                          {ev && <span className="sp-dot" />}
                          {stage}
                          {ev && <span className="sp-status">{ev.status}</span>}
                          {pending && events.length > 0 && <span className="nf-spin" style={{ marginLeft: 4 }} />}
                        </span>
                      );
                    })}
                    {result && (
                      <span className={`rs-chip ${result.ok ? 'gate' : ''}`} title={result.error ?? undefined}>
                        {result.ok ? '✅ 已提交' : '❌ 未通过'}
                        {result.blocks > 0 && <>　残留硬问题 {result.blocks}</>}
                        {result.quality_score != null && <>　★{result.quality_score.toFixed(1)}</>}
                        　tokens {result.tokens}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {notes.length > 0 && (
        <div className="ph-hint" style={{ marginTop: '0.4rem', display: 'grid', gap: '0.15rem' }}>
          {notes.map((line, i) => <span key={i}>{line}</span>)}
        </div>
      )}

      {summary && (
        <div className="run-summary" style={{ marginTop: '0.4rem' }}>
          <span className="rs-chip gate">结果：<b>{summary.status}</b></span>
          <span className="rs-chip">
            提交 <b>{summary.chapters.filter((c) => c.ok).length}</b> 章
          </span>
          {summary.regenerations > 0 && (
            <span className="rs-chip">重写 <b>{summary.regenerations}</b> 次</span>
          )}
          <span className="rs-chip">下一章：<b>第 {summary.next_chapter} 章</b></span>
          <span className="rs-chip">tokens：<b>{summary.tokens}</b></span>
          <span className="rs-chip">usd：<b>${summary.usd.toFixed(4)}</b></span>
        </div>
      )}

      {error && (
        <div className="nf-msg err">
          <span>⚠</span>
          <span>{error}</span>
        </div>
      )}
    </>
  );
}
