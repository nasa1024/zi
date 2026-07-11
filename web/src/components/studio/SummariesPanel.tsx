// SummariesPanel（POST-v15 P2#7）：三层摘要可视化看板。
// 全局梗概 → 卷滚动摘要 → 逐章大事记时间轴；三层均可就地编辑（改真相源：
// 改过的章摘要被后续 recall 前情窗口与卷 rollup 消费）。

import { useCallback, useEffect, useState } from 'react';
import { ApiError, api } from '../../api/client';
import type { SummaryBoardResponse, SummaryChapterItem, SummaryVolumeItem } from '../../api/types';

function errMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return fallback;
}

// 可就地编辑的摘要块：点「编辑」展开 textarea，保存后回调
function EditableSummary({
  text,
  placeholder,
  rows = 3,
  onSave,
}: {
  text: string;
  placeholder: string;
  rows?: number;
  onSave: (value: string) => Promise<void>;
}): JSX.Element {
  const [editing, setEditing] = useState<boolean>(false);
  const [value, setValue] = useState<string>(text);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { setValue(text); }, [text]);

  const save = async () => {
    if (!value.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await onSave(value.trim());
      setEditing(false);
    } catch (err) {
      setError(errMessage(err, '保存失败'));
    } finally {
      setBusy(false);
    }
  };

  if (!editing) {
    return (
      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'flex-start' }}>
        <div style={{ flex: 1, whiteSpace: 'pre-wrap' }}>
          {text || <span className="ph-hint">{placeholder}</span>}
        </div>
        <button type="button" className="nf-btn-sm" onClick={() => setEditing(true)} title="编辑（改真相源）">
          ✏️
        </button>
      </div>
    );
  }
  return (
    <div>
      <textarea
        className="nf-textarea"
        rows={rows}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        disabled={busy}
        style={{ width: '100%' }}
      />
      <div style={{ display: 'flex', gap: '0.4rem', marginTop: '0.3rem' }}>
        <button type="button" className="nf-btn sm" onClick={() => void save()} disabled={busy || !value.trim()}>
          {busy ? '保存中…' : '💾 保存'}
        </button>
        <button type="button" className="nf-btn-sm" onClick={() => { setEditing(false); setValue(text); }} disabled={busy}>
          取消
        </button>
      </div>
      {error && <div className="nf-msg err"><span>⚠</span><span>{error}</span></div>}
    </div>
  );
}

export default function SummariesPanel({ projectId }: { projectId: string }): JSX.Element {
  const [board, setBoard] = useState<SummaryBoardResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [newChapter, setNewChapter] = useState<string>('');

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setBoard(await api.summaryBoard(projectId));
    } catch (err) {
      setError(errMessage(err, '加载摘要看板失败'));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  // 按卷分组章摘要；无卷归属的收进「未分卷」
  const grouped = new Map<number | null, SummaryChapterItem[]>();
  for (const c of board?.chapters ?? []) {
    const key = c.volume_no ?? null;
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key)!.push(c);
  }
  const volumes: SummaryVolumeItem[] = board?.volumes ?? [];
  const orphanChapters = grouped.get(null) ?? [];

  const addChapterSummary = async () => {
    const n = Number.parseInt(newChapter, 10);
    if (!Number.isFinite(n) || n < 1) return;
    try {
      await api.updateChapterSummary(projectId, n, '（待补充：本章大事记）');
      setNewChapter('');
      await load();
    } catch (err) {
      setError(errMessage(err, '新建章摘要失败'));
    }
  };

  const renderChapterRow = (c: SummaryChapterItem) => (
    <div key={c.chapter} className="ph-row" style={{ padding: '0.5rem 0.75rem' }}>
      <div style={{ display: 'grid', gridTemplateColumns: '6.5rem 1fr', gap: '0.6rem', alignItems: 'start' }}>
        <div>
          <div className="ph-ch">第 {c.chapter} 章</div>
          {c.title && <div className="ph-hint">{c.title}</div>}
          {c.word_count != null && <div className="ph-hint">{c.word_count} 字</div>}
        </div>
        <EditableSummary
          text={c.summary}
          placeholder="（无摘要）"
          onSave={async (v) => {
            await api.updateChapterSummary(projectId, c.chapter, v);
            await load();
          }}
        />
      </div>
    </div>
  );

  return (
    <div className="studio-panel">
      <div className="panel-head">
        <h3 className="cn">故事脉络</h3>
        <span className="ph-hint">
          叙事摘要：章大事记 → 卷概要 → 全书梗概 · 与 L1/L2/L3 记忆管线分开
        </span>
        <button type="button" className="nf-btn ghost sm" onClick={() => void load()} disabled={loading}>
          ↻ 刷新
        </button>
      </div>

      {loading && <span className="nf-loading"><span className="nf-spin" /> 加载中…</span>}
      {error && <div className="nf-msg err"><span>⚠</span><span>{error}</span></div>}

      {board && (
        <>
          {/* ── 全书级摘要（不是 L3 Canon 事件提案） ───── */}
          <div className="panel-section-head">
            <span className="cn" style={{ fontSize: '0.9rem', opacity: 0.8 }}>🌐 全书至此</span>
            <span className="ph-hint" style={{ marginLeft: 'auto' }}>
              每 10 章 / 卷末自动刷新（meta_kv global_synopsis）
            </span>
          </div>
          <div className="review-card" style={{ marginTop: '0.4rem' }}>
            <EditableSummary
              text={board.global_synopsis ?? ''}
              placeholder="尚无全书梗概 —— 连写满 10 章后自动生成，或点右侧 ✏️ 手写"
              rows={4}
              onSave={async (v) => {
                await api.updateGlobalSynopsis(projectId, v);
                await load();
              }}
            />
          </div>

          {/* ── 卷级摘要 + 章节级摘要（不是 L2 场景 / L1 原子） ── */}
          {volumes.length === 0 && grouped.size === 0 && (
            <div className="nf-hollow" style={{ marginTop: '0.8rem' }}>
              暂无摘要数据 —— 生成章节后自动产生章摘要；也可在下方手动补录。
            </div>
          )}

          {volumes.map((v) => {
            const chs = grouped.get(v.volume_no) ?? [];
            return (
              <div key={v.volume_no} style={{ marginTop: '1rem' }}>
                <div className="panel-section-head">
                  <span className="cn" style={{ fontSize: '0.9rem', opacity: 0.8 }}>
                    📕 第 {v.volume_no} 卷 · {v.title}
                  </span>
                  <span className="ph-hint" style={{ marginLeft: 'auto' }}>
                    {v.start_chapter != null && `第 ${v.start_chapter}–${v.end_chapter ?? '…'} 章 · `}
                    {v.status} · {chs.length} 章有摘要
                  </span>
                </div>
                <div className="review-card" style={{ margin: '0.4rem 0' }}>
                  <EditableSummary
                    text={v.rolling_summary ?? ''}
                    placeholder="尚无卷概要 —— 每 5 章 / 卷末自动 rollup"
                    onSave={async (val) => {
                      await api.updateVolumeSummary(projectId, v.volume_no, val);
                      await load();
                    }}
                  />
                </div>
                {chs.length > 0 && (
                  <div className="pipeline-history">{chs.map(renderChapterRow)}</div>
                )}
              </div>
            );
          })}

          {orphanChapters.length > 0 && (
            <div style={{ marginTop: '1rem' }}>
              <div className="panel-section-head">
                <span className="cn" style={{ fontSize: '0.9rem', opacity: 0.8 }}>📄 未分卷章节</span>
              </div>
              <div className="pipeline-history">{orphanChapters.map(renderChapterRow)}</div>
            </div>
          )}

          {/* ── 手动补录章摘要 ─────────────────────────── */}
          <div className="nf-actions" style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', marginTop: '1rem' }}>
            <label className="ph-hint" htmlFor="sm-new-ch">补录章摘要：章号</label>
            <input
              id="sm-new-ch"
              className="nf-input"
              type="number"
              min={1}
              style={{ width: '5.5rem' }}
              value={newChapter}
              onChange={(e) => setNewChapter(e.target.value)}
            />
            <button
              type="button"
              className="nf-btn-sm"
              onClick={() => void addChapterSummary()}
              disabled={!newChapter.trim()}
            >
              ＋ 新建
            </button>
          </div>
        </>
      )}
    </div>
  );
}
