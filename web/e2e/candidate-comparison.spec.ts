import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

const project = {
  project_id: 'candidate-book',
  name: '潮汐档案',
  genre: '科幻',
  db_path: '/tmp/candidate-book.db',
  created_at: '2026-08-30T08:00:00Z',
  chapter_count: 3,
  canon_fact_count: 11,
  archived: false,
  content_language: 'zh-CN',
};

const existingDraft = '这是服务端已经保存的第 4 章作者修订。';
const candidateTexts = [
  `版本 A 从月台上的失物箱切入。${'广播里反复念出无人认领的名字，林默沿着潮湿的灯带向前走。'.repeat(9)}【版本 A 全文终点】`,
  `版本 B 从停摆的潮汐钟切入。${'每一次秒针回落，海墙外就多出一封写给明天的信，林默决定拆开最后一封。'.repeat(9)}【版本 B 全文终点】`,
  `版本 C 从无人的末班车切入。${'车窗倒映着尚未发生的告别，林默在终点站前改写了自己的名字。'.repeat(9)}【版本 C 全文终点】`,
];

function candidates(winnerIndex: number) {
  return candidateTexts.map((draftText, index) => ({
    index,
    draft_text: draftText,
    length: draftText.length,
    score: 8.6 - index * 0.2,
    hard_blocks: 0,
    is_winner: index === winnerIndex,
    proposal_count: 2,
    plan_blocks: 0,
    contract_score: 0.91 - index * 0.03,
    arc_progress_score: 0.88 - index * 0.02,
    quality_score: 8.6 - index * 0.2,
  }));
}

function runDetail(winnerIndex: number, selectedBy: 'auto' | 'human') {
  return {
    run_id: 'run-completed-4',
    chapter: 4,
    status: 'completed',
    started_at: '2026-08-30T08:10:00Z',
    finished_at: '2026-08-30T08:11:00Z',
    word_count: candidateTexts[winnerIndex].length,
    quality_score: 9,
    draft_text: candidateTexts[winnerIndex],
    candidates: candidates(winnerIndex),
    winner_index: winnerIndex,
    selected_by: selectedBy,
  };
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function mockCandidateApi(page: Page, initialDraft: string): Promise<void> {
  let selectedCandidate = 0;
  let currentDraft = initialDraft;
  let currentRevision = 'revision-existing';

  await page.route('**/health', (route) => json(route, { status: 'ok', version: 'test' }));
  await page.route('**/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();

    if (path === '/v1/projects/candidate-book') return json(route, project);
    if (path === '/v1/candidate-book/story-map') {
      return json(route, { plan: null, arcs: [], volumes: [], chapters: [] });
    }
    if (path === '/v1/candidate-book/chapter-cards') {
      return json(route, [{ chapter: 4, title: '月台尽头', goal: '找到未来来信的寄件人', status: 'planned' }]);
    }
    if (path === '/v1/candidate-book/chapters/4/draft') {
      return json(route, {
        chapter: 4,
        revision_id: currentRevision,
        revision_round: selectedCandidate + 1,
        draft_text: currentDraft,
        word_count: currentDraft.length,
      });
    }
    if (
      path === '/v1/candidate-book/chapters/4/contract'
      || path === '/v1/candidate-book/chapters/4/alignment'
    ) {
      return json(route, { detail: 'not found' }, 404);
    }
    if (path === '/v1/candidate-book/chapters/4/plan-versions') return json(route, []);
    if (path === '/v1/candidate-book/pipeline/runs') {
      return json(route, [{
        run_id: 'run-completed-4',
        chapter: 4,
        status: 'completed',
        started_at: '2026-08-30T08:10:00Z',
        finished_at: '2026-08-30T08:11:00Z',
      }]);
    }
    if (path === '/v1/candidate-book/pipeline/runs/run-completed-4' && method === 'GET') {
      return json(route, runDetail(selectedCandidate, selectedCandidate === 0 ? 'auto' : 'human'));
    }
    if (
      path === '/v1/candidate-book/pipeline/runs/run-completed-4/select-candidate'
      && method === 'POST'
    ) {
      const body = route.request().postDataJSON() as { candidate_index: number };
      selectedCandidate = body.candidate_index;
      currentDraft = candidateTexts[selectedCandidate];
      currentRevision = `revision-selected-${selectedCandidate}`;
      return json(route, runDetail(selectedCandidate, 'human'));
    }

    return json(route, { detail: `unmocked request: ${method} ${path}` }, 404);
  });
}

test('a draft matching the run winner restores all candidates and allows another adoption', async ({ page }) => {
  await mockCandidateApi(page, candidateTexts[0]);
  await page.goto('/studio/candidate-book/write/4');

  const editor = page.getByRole('textbox', { name: '第 4 章正文' });
  await expect(editor).toHaveValue(candidateTexts[0]);

  const comparison = page.locator('#candidates');
  const candidateCards = comparison.locator('.writing-candidate-card');
  await expect(comparison).toBeVisible();
  await expect(candidateCards).toHaveCount(3);
  await expect(comparison.getByText('版本 A', { exact: true })).toBeVisible();
  await expect(comparison.getByText('版本 B', { exact: true })).toBeVisible();
  await expect(comparison.getByText('版本 C', { exact: true })).toBeVisible();

  await expect(candidateCards.nth(0).locator('.writing-candidate-copy p')).not.toContainText('【版本 A 全文终点】');
  await comparison.locator('.writing-candidate-expand-all').click();
  await expect(comparison.locator('.writing-candidate-expand-all')).toHaveText('收起全文');
  await expect(candidateCards.nth(0).locator('.writing-candidate-copy p')).toContainText('【版本 A 全文终点】');
  await expect(candidateCards.nth(1).locator('.writing-candidate-copy p')).toContainText('【版本 B 全文终点】');
  await expect(candidateCards.nth(2).locator('.writing-candidate-copy p')).toContainText('【版本 C 全文终点】');

  const selectRequest = page.waitForRequest((request) => (
    new URL(request.url()).pathname === '/v1/candidate-book/pipeline/runs/run-completed-4/select-candidate'
    && request.method() === 'POST'
  ));
  await candidateCards.nth(1).getByRole('button', { name: '采用此稿' }).click();
  expect((await selectRequest).postDataJSON()).toEqual({ candidate_index: 1 });

  await expect(editor).toHaveValue(candidateTexts[1]);
  await expect(candidateCards).toHaveCount(3);
  await expect(candidateCards.nth(1).getByRole('button', { name: '已采用' })).toBeDisabled();
  await expect(candidateCards.nth(1)).toHaveClass(/selected/);
});

test('a newer saved draft keeps completed-run candidates visible but read-only', async ({ page }) => {
  let selectionRequests = 0;
  page.on('request', (request) => {
    if (
      new URL(request.url()).pathname === '/v1/candidate-book/pipeline/runs/run-completed-4/select-candidate'
      && request.method() === 'POST'
    ) {
      selectionRequests += 1;
    }
  });
  await mockCandidateApi(page, existingDraft);
  await page.goto('/studio/candidate-book/write/4#candidates');

  await expect(page.getByRole('textbox', { name: '第 4 章正文' })).toHaveValue(existingDraft);
  const comparison = page.locator('#candidates');
  await expect(comparison.getByText('历史候选 / 只读')).toBeVisible();
  await expect(comparison.locator('.writing-candidate-card')).toHaveCount(3);

  const adoptButtons = comparison.getByRole('button', { name: '只读，不可采用' });
  await expect(adoptButtons).toHaveCount(3);
  for (let index = 0; index < 3; index += 1) {
    await expect(adoptButtons.nth(index)).toBeDisabled();
  }
  expect(selectionRequests).toBe(0);
});
