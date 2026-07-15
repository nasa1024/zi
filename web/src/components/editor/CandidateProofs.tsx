import type { PipelineRunDetail } from '../../api/types';

interface CandidateProofsProps {
  detail: PipelineRunDetail | null;
  selecting: number | null;
  onSelect: (index: number) => void;
}

function scoreLabel(value?: number | null): string {
  if (value == null) return '—';
  return value <= 1 ? `${Math.round(value * 100)}%` : value.toFixed(1);
}

export default function CandidateProofs({
  detail,
  selecting,
  onSelect,
}: CandidateProofsProps): JSX.Element | null {
  if (!detail || detail.candidates.length <= 1) return null;

  return (
    <section className="candidate-proofs" aria-labelledby="candidate-proof-title">
      <div className="candidate-proof-head">
        <div>
          <span>PROOFING TABLE</span>
          <h3 id="candidate-proof-title">候选校样</h3>
        </div>
        <p>先看硬约束与大纲契合度，再比较文气。</p>
      </div>

      <div className="candidate-proof-stack">
        {detail.candidates.map((candidate) => {
          const blocked = candidate.hard_blocks > 0 || (candidate.plan_blocks ?? 0) > 0;
          return (
            <article
              className={`candidate-proof${candidate.is_winner ? ' selected' : ''}${blocked ? ' blocked' : ''}`}
              key={candidate.index}
            >
              <div className="candidate-proof-index">
                <span>稿</span>
                <b>{String(candidate.index + 1).padStart(2, '0')}</b>
              </div>
              <div className="candidate-proof-copy">
                <div className="candidate-proof-badges">
                  {candidate.is_winner && <span className="proof-badge adopted">当前采用</span>}
                  {blocked && <span className="proof-badge danger">硬阻断 {candidate.hard_blocks + (candidate.plan_blocks ?? 0)}</span>}
                  <span>{candidate.length} 字</span>
                </div>
                <p>{candidate.draft_text.slice(0, 190)}{candidate.draft_text.length > 190 ? '…' : ''}</p>
              </div>
              <dl className="candidate-proof-scores">
                <div><dt>契约</dt><dd>{scoreLabel(candidate.contract_score)}</dd></div>
                <div><dt>情节弧</dt><dd>{scoreLabel(candidate.arc_progress_score)}</dd></div>
                <div><dt>文气</dt><dd>{scoreLabel(candidate.quality_score ?? candidate.score)}</dd></div>
              </dl>
              <button
                type="button"
                className="proof-adopt"
                disabled={candidate.is_winner || blocked || selecting != null}
                onClick={() => onSelect(candidate.index)}
              >
                {selecting === candidate.index ? '校验中…' : candidate.is_winner ? '已采用' : blocked ? '不可采用' : '采用此稿'}
              </button>
            </article>
          );
        })}
      </div>
    </section>
  );
}
