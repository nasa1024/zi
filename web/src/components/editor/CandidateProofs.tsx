import { useEffect, useMemo, useState } from 'react';
import type { PipelineRunDetail } from '../../api/types';
import '../../styles/writing-redesign.css';

interface CandidateProofsProps {
  detail: PipelineRunDetail | null;
  contentLanguage?: string | null;
  selecting: number | null;
  onSelect: (index: number) => void;
  readOnlyReason?: string | null;
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, value));
}

function ratioScorePercent(value?: number | null): number {
  if (value == null || !Number.isFinite(value)) return 0;
  return clampPercent(value * 100);
}

function ratioScoreLabel(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return '—';
  return `${Math.round(ratioScorePercent(value))}%`;
}

function qualityScorePercent(value?: number | null): number {
  if (value == null || !Number.isFinite(value)) return 0;
  return clampPercent(value * 10);
}

function qualityScoreLabel(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return '—';
  return value.toFixed(1);
}

function candidateLabel(index: number): string {
  return `版本 ${String.fromCharCode(65 + Math.max(0, index))}`;
}

function excerpt(text: string, expanded: boolean): string {
  if (expanded || text.length <= 220) return text;
  return `${text.slice(0, 220).trimEnd()}…`;
}

export default function CandidateProofs({
  detail,
  contentLanguage,
  selecting,
  onSelect,
  readOnlyReason,
}: CandidateProofsProps): JSX.Element | null {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const readOnlyMessage = readOnlyReason?.trim() || null;

  useEffect(() => {
    setExpanded(new Set());
  }, [detail?.run_id]);

  const allExpanded = useMemo(() => (
    detail != null
      && detail.candidates.length > 0
      && detail.candidates.every((candidate) => expanded.has(candidate.index))
  ), [detail, expanded]);

  if (!detail || detail.candidates.length <= 1) return null;

  const toggleExpanded = (index: number): void => {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  const toggleAll = (): void => {
    setExpanded(allExpanded ? new Set() : new Set(detail.candidates.map((candidate) => candidate.index)));
  };

  return (
    <section
      className={`candidate-proofs writing-candidate-proofs${readOnlyMessage ? ' is-read-only' : ''}`}
      id="candidates"
      aria-labelledby="candidate-proof-title"
      aria-describedby={readOnlyMessage ? 'candidate-readonly-note' : undefined}
    >
      <div className="candidate-proof-head">
        <div>
          <span>候选稿对照</span>
          <h3 id="candidate-proof-title">候选校样</h3>
        </div>
        <div className="writing-candidate-head-actions">
          <p>先看硬约束与大纲契合度，再比较文气。</p>
          <button
            type="button"
            className="writing-candidate-expand-all"
            aria-pressed={allExpanded}
            onClick={toggleAll}
          >
            {allExpanded ? '收起全文' : '展开全文'}
          </button>
        </div>
      </div>

      {readOnlyMessage && (
        <div className="writing-candidate-readonly" id="candidate-readonly-note" role="note">
          <strong>历史候选 / 只读</strong>
          <span>{readOnlyMessage}</span>
        </div>
      )}

      <div className="candidate-proof-stack writing-candidate-grid">
        {detail.candidates.map((candidate) => {
          const blocked = candidate.hard_blocks > 0 || (candidate.plan_blocks ?? 0) > 0;
          const isExpanded = expanded.has(candidate.index);
          const qualityScore = candidate.quality_score ?? candidate.score;
          return (
            <article
              className={`candidate-proof writing-candidate-card${candidate.is_winner ? ' selected' : ''}${blocked ? ' blocked' : ''}`}
              key={candidate.index}
            >
              <header className="writing-candidate-card-head">
                <div className="candidate-proof-index">
                  <span>稿</span>
                  <b>{String(candidate.index + 1).padStart(2, '0')}</b>
                </div>
                <div className="writing-candidate-name">
                  <strong>{candidateLabel(candidate.index)}</strong>
                  <span>{readOnlyMessage ? (candidate.is_winner ? '当时采用' : '历史校样') : candidate.is_winner ? '当前采用' : blocked ? '需要修订' : '可供选择'}</span>
                </div>
                <span className={`writing-candidate-radio${candidate.is_winner ? ' is-selected' : ''}`} aria-hidden="true" />
              </header>

              <div className="candidate-proof-copy writing-candidate-copy">
                <div className="candidate-proof-badges">
                  {candidate.is_winner && <span className="proof-badge adopted">{readOnlyMessage ? '当时采用' : '当前采用'}</span>}
                  {blocked && <span className="proof-badge danger">硬阻断 {candidate.hard_blocks + (candidate.plan_blocks ?? 0)}</span>}
                  <span>{candidate.length} {contentLanguage?.startsWith('en') ? '词' : '字'}</span>
                </div>
                <p className={isExpanded ? 'is-expanded' : ''}>{excerpt(candidate.draft_text, isExpanded)}</p>
                {candidate.draft_text.length > 220 && (
                  <button
                    type="button"
                    className="writing-candidate-expand"
                    aria-expanded={isExpanded}
                    onClick={() => toggleExpanded(candidate.index)}
                  >
                    {isExpanded ? '收起全文' : '查看全文'}
                  </button>
                )}
              </div>
              <dl className="candidate-proof-scores writing-candidate-scores">
                <div>
                  <dt>契约</dt>
                  <dd>{ratioScoreLabel(candidate.contract_score)}</dd>
                  <span className="writing-score-meter"><i style={{ width: `${ratioScorePercent(candidate.contract_score)}%` }} /></span>
                </div>
                <div>
                  <dt>情节弧</dt>
                  <dd>{ratioScoreLabel(candidate.arc_progress_score)}</dd>
                  <span className="writing-score-meter"><i style={{ width: `${ratioScorePercent(candidate.arc_progress_score)}%` }} /></span>
                </div>
                <div>
                  <dt>文气</dt>
                  <dd>{qualityScoreLabel(qualityScore)}</dd>
                  <span className="writing-score-meter"><i style={{ width: `${qualityScorePercent(qualityScore)}%` }} /></span>
                </div>
              </dl>
              <div className="writing-candidate-actions">
                <button
                  type="button"
                  className="proof-adopt"
                  disabled={readOnlyMessage != null || candidate.is_winner || blocked || selecting != null}
                  onClick={() => onSelect(candidate.index)}
                >
                  {readOnlyMessage ? '只读，不可采用' : selecting === candidate.index ? '校验中…' : candidate.is_winner ? '已采用' : blocked ? '不可采用' : '采用此稿'}
                </button>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
