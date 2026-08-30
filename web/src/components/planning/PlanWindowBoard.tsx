import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import type { GeneratedPlanWindow } from '../../api/types';
import { userFacingError } from '../../utils/uiText';

interface PlanWindowBoardProps {
  projectId: string;
  defaultStartChapter: number;
  onChanged: () => void;
}

function message(error: unknown): string {
  return userFacingError(error, '未来章节路线生成失败，请稍后重试。');
}

function normalizeStartChapter(chapter: number): number {
  return Number.isFinite(chapter) ? Math.max(1, Math.floor(chapter)) : 1;
}

export default function PlanWindowBoard({ projectId, defaultStartChapter, onChanged }: PlanWindowBoardProps): JSX.Element {
  const [startChapter, setStartChapter] = useState(() => normalizeStartChapter(defaultStartChapter));
  const [horizon, setHorizon] = useState(5);
  const [hints, setHints] = useState('');
  const [window, setWindow] = useState<GeneratedPlanWindow | null>(null);
  const [busy, setBusy] = useState(false);
  const [approving, setApproving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setStartChapter(normalizeStartChapter(defaultStartChapter));
    setWindow(null);
    setError(null);
  }, [defaultStartChapter, projectId]);

  const generate = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      setWindow(await api.generatePlanningWindow(projectId, {
        start_chapter: startChapter,
        horizon,
        hints: hints.trim() || null,
        candidate_count: 3,
      }));
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  };

  const approve = async (optionId?: string): Promise<void> => {
    if (!window) return;
    setApproving(optionId ?? 'recommended');
    setError(null);
    try {
      await api.approvePlanningWindow(projectId, window.decision_id, optionId);
      setWindow(null);
      onChanged();
    } catch (err) {
      setError(message(err));
    } finally {
      setApproving(null);
    }
  };

  return (
    <section className="plan-window-board">
      <header>
        <div>
          <span>滚动章节路线</span>
          <h2>未来章节窗口</h2>
          <p>每次只锻造一小段可执行契约，已写章节和锁定路线不会被覆盖。</p>
        </div>
        <div className="plan-window-controls">
          <label><span>起始章</span><input type="number" min={1} value={startChapter} onChange={(e) => setStartChapter(Number(e.target.value))} /></label>
          <label><span>窗口</span><select value={horizon} onChange={(e) => setHorizon(Number(e.target.value))}>{[3, 5, 8, 10].map((v) => <option key={v} value={v}>{v} 章</option>)}</select></label>
          <button type="button" onClick={() => void generate()} disabled={busy}>{busy ? '锻造中…' : '生成三条路线'}</button>
        </div>
      </header>
      <label className="plan-window-hints">
        <span>方向提示</span>
        <input value={hints} onChange={(e) => setHints(e.target.value)} placeholder="例如：增加悬疑，不进入感情线。" />
      </label>

      {error && <div className="desk-alert" role="alert">{error}</div>}

      {window && (
        <div className="plan-window-candidates">
          {window.candidates.map((candidate, index) => {
            const optionId = candidate.id;
            const recommended = optionId != null && optionId === window.recommended_option_id;
            return (
              <article className={recommended ? 'recommended' : ''} key={optionId ?? index}>
                <div className="plan-window-index">路线 {index + 1}{recommended && <span>智能推荐</span>}</div>
                <h3>{candidate.title || `未来 ${candidate.chapters.length} 章`}</h3>
                {candidate.summary && <p>{candidate.summary}</p>}
                <ol>
                  {candidate.chapters.map((chapter) => (
                    <li key={chapter.chapter}><b>{chapter.chapter}</b><span>{chapter.title || chapter.goal || '待定'}</span></li>
                  ))}
                </ol>
                <button type="button" onClick={() => void approve(optionId)} disabled={approving != null}>
                  {approving === (optionId ?? 'recommended') ? '采用中…' : '采用这条路线'}
                </button>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
