import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import type { ChapterReplanResponse } from '../../api/types';
import { userFacingError } from '../../utils/uiText';

interface ReplanDialogProps {
  projectId: string;
  chapter: number;
  open: boolean;
  onClose: () => void;
  onApplied: () => void;
}

function errorMessage(error: unknown): string {
  return userFacingError(error, '重规划失败，请稍后重试。');
}

export default function ReplanDialog({
  projectId,
  chapter,
  open,
  onClose,
  onApplied,
}: ReplanDialogProps): JSX.Element | null {
  const [mode, setMode] = useState<'patch' | 'replan'>('patch');
  const [horizon, setHorizon] = useState(5);
  const [reason, setReason] = useState('接受本章实际落点，但保持全书与本卷目标不变。');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ChapterReplanResponse | null>(null);

  useEffect(() => {
    if (open) {
      setError(null);
      setResult(null);
    }
  }, [open]);

  if (!open) return null;

  const generate = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const next = await api.replanChapter(projectId, chapter, {
        mode,
        horizon,
        preserve_locked: true,
        reason: reason.trim() || null,
        // Two-phase flow: preview a durable decision first, apply only after
        // the author explicitly approves the recommended option below.
        apply: false,
      });
      setResult(next);
      if (!next.window) onApplied();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const approve = async (): Promise<void> => {
    const decisionId = result?.window?.decision_id ?? result?.decision_id;
    if (!decisionId) return;
    setBusy(true);
    setError(null);
    try {
      await api.approvePlanningWindow(
        projectId,
        decisionId,
        result?.window?.recommended_option_id,
      );
      onApplied();
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="replan-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section className="replan-dialog" role="dialog" aria-modal="true" aria-labelledby="replan-title">
        <button type="button" className="replan-close" onClick={onClose} aria-label="关闭重规划对话框">×</button>
        <span className="replan-kicker">计划修订 · 只追加不覆盖</span>
        <h2 id="replan-title">接受第 {chapter} 章，重排未来</h2>
        <p>已写正文与已确认设定不会改变；旧计划会作为历史版本保留，新路线从下一章开始。</p>

        <div className="replan-mode-row">
          <button type="button" className={mode === 'patch' ? 'active' : ''} onClick={() => setMode('patch')}>
            <b>局部补丁</b><span>尽量保留原路线</span>
          </button>
          <button type="button" className={mode === 'replan' ? 'active' : ''} onClick={() => setMode('replan')}>
            <b>完整重排</b><span>重做未来窗口</span>
          </button>
        </div>

        <label className="replan-field">
          <span>重排范围</span>
          <select value={horizon} onChange={(event) => setHorizon(Number(event.target.value))}>
            {[3, 5, 8, 10].map((value) => <option key={value} value={value}>未来 {value} 章</option>)}
          </select>
        </label>
        <label className="replan-field">
          <span>作者理由</span>
          <textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} />
        </label>

        {result?.window && (
          <div className="replan-preview">
            <b>已生成 {result.window.candidates.length} 条未来路线</b>
            <p>推荐路线将覆盖第 {result.window.start_chapter} 章起的 {result.window.horizon} 章未执行计划。</p>
          </div>
        )}
        {error && <div className="desk-alert" role="alert">{error}</div>}

        <div className="replan-actions">
          <button type="button" onClick={onClose}>取消</button>
          {!result?.window ? (
            <button type="button" className="primary" onClick={() => void generate()} disabled={busy}>
              {busy ? '正在重排…' : '生成修订路线'}
            </button>
          ) : (
            <button type="button" className="primary" onClick={() => void approve()} disabled={busy}>
              {busy ? '批准中…' : '批准推荐路线'}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}
