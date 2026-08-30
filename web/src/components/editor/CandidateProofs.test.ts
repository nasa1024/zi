import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { PipelineRunDetail } from '../../api/types';
import CandidateProofs from './CandidateProofs';

const detail: PipelineRunDetail = {
  run_id: 'run-history',
  chapter: 4,
  status: 'completed',
  started_at: '2026-08-30T08:00:00Z',
  finished_at: '2026-08-30T08:01:00Z',
  draft_text: '版本 B',
  winner_index: 1,
  selected_by: 'human',
  candidates: [0, 1, 2].map((index) => ({
    index,
    draft_text: `候选正文 ${index + 1}`,
    length: 6,
    score: 8 - index,
    hard_blocks: 0,
    is_winner: index === 1,
    proposal_count: 0,
    contract_score: index === 0 ? 0.75 : 0.5,
    arc_progress_score: index === 0 ? 0.42 : 0.5,
    quality_score: index === 0 ? 8.5 : 7,
  })),
};

function renderCandidateProofs(readOnlyReason?: string): string {
  return renderToStaticMarkup(createElement(CandidateProofs, {
    detail,
    selecting: null,
    onSelect: () => undefined,
    readOnlyReason,
  }));
}

describe('CandidateProofs', () => {
  it('marks historical candidates as read-only and disables every adopt button', () => {
    const markup = renderCandidateProofs('当前正文已有更新的修订。');

    expect(markup).toContain('<strong>历史候选 / 只读</strong>');
    expect(markup).toContain('当前正文已有更新的修订。');
    expect(markup).toContain('当时采用');
    expect(markup.match(/class="proof-adopt" disabled="">只读，不可采用<\/button>/g)).toHaveLength(3);
  });

  it('uses ratio meters for plan scores and a 0-10 meter for prose quality', () => {
    const markup = renderCandidateProofs();

    expect(markup).toContain('<dt>契约</dt><dd>75%</dd><span class="writing-score-meter"><i style="width:75%"></i></span>');
    expect(markup).toContain('<dt>情节弧</dt><dd>42%</dd><span class="writing-score-meter"><i style="width:42%"></i></span>');
    expect(markup).toContain('<dt>文气</dt><dd>8.5</dd><span class="writing-score-meter"><i style="width:85%"></i></span>');
  });
});
