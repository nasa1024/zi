import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

const project = {
  project_id: 'flow-book',
  name: '流程验证书',
  genre: '悬疑',
  db_path: '/tmp/flow-book.db',
  created_at: '2026-08-30T08:00:00Z',
  chapter_count: 4,
  canon_fact_count: 9,
  archived: false,
  content_language: 'zh-CN',
};

const candidateTexts = [
  '稿 A：林默在潮汐钟下拆开了第一封来自明天的信。',
  '稿 B：末班车进站时，所有乘客都忘记了自己的名字。',
  '稿 C：广播只播报明天的失踪者，而第一个名字是林默。',
];

const alignmentReport = {
  chapter: 4,
  goal_score: 0.94,
  beat_score: 0.9,
  arc_score: 0.88,
  volume_score: 0.91,
  reveal_score: 1,
  divergence_score: 0.08,
  level: 'normal',
  recommended_plan_action: 'keep',
  decision: '正文与章节契约一致，可按原路线继续。',
  hard_reveal: false,
  detail: {},
  evidence_spans: [],
};

interface ApiState {
  currentDraft: string;
  currentRevision: string;
  selectedCandidate: number;
  includeCompletedRun: boolean;
  generationStarted: boolean;
  backgroundRunStatus: 'running' | 'completed';
  runPolls: number;
}

function candidateList(winnerIndex: number) {
  return candidateTexts.map((draftText, index) => ({
    index,
    draft_text: draftText,
    length: draftText.length,
    score: 8.8 - index * 0.2,
    quality_score: 8.8 - index * 0.2,
    hard_blocks: 0,
    plan_blocks: 0,
    contract_score: 0.94 - index * 0.03,
    arc_progress_score: 0.9 - index * 0.03,
    proposal_count: 2,
    is_winner: index === winnerIndex,
  }));
}

function completedRunDetail(
  winnerIndex: number,
  selectedBy: 'auto' | 'human',
  runId = 'run-completed-4',
) {
  return {
    run_id: runId,
    chapter: 4,
    status: 'completed',
    started_at: '2026-08-30T08:10:00Z',
    finished_at: '2026-08-30T08:11:00Z',
    word_count: candidateTexts[winnerIndex].length,
    quality_score: 9,
    draft_text: candidateTexts[winnerIndex],
    candidates: candidateList(winnerIndex),
    winner_index: winnerIndex,
    selected_by: selectedBy,
    alignment_report: alignmentReport,
  };
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function mockWritingApi(page: Page, overrides: Partial<ApiState> = {}): Promise<ApiState> {
  const state: ApiState = {
    currentDraft: candidateTexts[0],
    currentRevision: 'revision-4-auto',
    selectedCandidate: 0,
    includeCompletedRun: true,
    generationStarted: false,
    backgroundRunStatus: 'running',
    runPolls: 0,
    ...overrides,
  };

  await page.route('**/health', (route) => json(route, { status: 'ok', version: 'test' }));
  await page.route('**/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();

    if (path === '/v1/projects/flow-book') return json(route, project);
    if (path === '/v1/flow-book/story-map') {
      return json(route, {
        plan: null,
        arcs: [],
        volumes: [],
        chapters: [
          { chapter: 4, title: '潮汐钟', goal: '读懂来自未来的信', status: 'drafted', plan_version: 2, locked: false },
          { chapter: 5, title: '失名列车', goal: '找回被抹去的名字', status: 'planned', plan_version: 2, locked: false },
        ],
      });
    }
    if (path === '/v1/flow-book/chapter-cards') {
      return json(route, [
        { chapter: 4, title: '潮汐钟', goal: '读懂来自未来的信', status: 'drafted', plan_version: 2 },
        { chapter: 5, title: '失名列车', goal: '找回被抹去的名字', status: 'planned', plan_version: 2 },
      ]);
    }
    if (path === '/v1/flow-book/chapters/4/draft' && method === 'GET') {
      return json(route, {
        chapter: 4,
        revision_id: state.currentRevision,
        revision_round: 2,
        draft_text: state.currentDraft,
        word_count: state.currentDraft.length,
      });
    }
    if (path === '/v1/flow-book/chapters/4/contract') {
      return json(route, { detail: 'not found' }, 404);
    }
    if (path === '/v1/flow-book/chapters/4/alignment') return json(route, alignmentReport);
    if (path === '/v1/flow-book/chapters/4/plan-versions') return json(route, []);
    if (path === '/v1/flow-book/pipeline/runs') {
      state.runPolls += 1;
      if (state.generationStarted) {
        return json(route, [{
          run_id: 'run-background-4',
          chapter: 4,
          status: state.backgroundRunStatus,
          started_at: '2026-08-30T09:00:00Z',
          finished_at: state.backgroundRunStatus === 'completed'
            ? '2026-08-30T09:01:00Z'
            : null,
        }]);
      }
      return json(route, state.includeCompletedRun ? [{
        run_id: 'run-completed-4',
        chapter: 4,
        status: 'completed',
        started_at: '2026-08-30T08:10:00Z',
        finished_at: '2026-08-30T08:11:00Z',
      }] : []);
    }
    if (path === '/v1/flow-book/pipeline/runs/run-completed-4' && method === 'GET') {
      return json(
        route,
        completedRunDetail(state.selectedCandidate, state.selectedCandidate === 0 ? 'auto' : 'human'),
      );
    }
    if (path === '/v1/flow-book/pipeline/runs/run-background-4' && method === 'GET') {
      return json(
        route,
        completedRunDetail(state.selectedCandidate, 'auto', 'run-background-4'),
      );
    }
    if (
      path === '/v1/flow-book/pipeline/runs/run-completed-4/select-candidate'
      && method === 'POST'
    ) {
      const body = route.request().postDataJSON() as { candidate_index: number };
      state.selectedCandidate = body.candidate_index;
      state.currentDraft = candidateTexts[state.selectedCandidate];
      state.currentRevision = `revision-4-human-${state.selectedCandidate}`;
      return json(route, completedRunDetail(state.selectedCandidate, 'human'));
    }
    if (path.endsWith('/agent/sessions') || path.endsWith('/volumes')) return json(route, []);

    return json(route, { detail: `unmocked request: ${method} ${path}` }, 404);
  });

  return state;
}

test('dirty navigation stays on the chapter until the author explicitly leaves', async ({ page }) => {
  await mockWritingApi(page);
  await page.goto('/studio/flow-book/write/4?session=checkpoint-42');

  const editor = page.getByRole('textbox', { name: '第 4 章正文' });
  await editor.fill(`${candidateTexts[0]}\n作者新增了一句。`);
  const planLink = page.locator('.workspace-primary-nav a[href*="/plan"]');
  await planLink.click();

  const guard = page.getByRole('dialog', { name: '当前工作还没有安全落盘' });
  await expect(guard).toBeVisible();
  await expect(guard).toContainText('正文还有未保存修改');
  await expect(page).toHaveURL('/studio/flow-book/write/4?session=checkpoint-42');

  await guard.getByRole('button', { name: '留在本章' }).click();
  await expect(guard).toBeHidden();
  await expect(page).toHaveURL('/studio/flow-book/write/4?session=checkpoint-42');

  await planLink.click();
  await guard.getByRole('button', { name: '仍然离开' }).click();
  await expect(page).toHaveURL('/studio/flow-book/plan?session=checkpoint-42');
});

test('adoption advances candidates to proofread and keep advances to continue', async ({ page }) => {
  await mockWritingApi(page);
  await page.goto('/studio/flow-book/write/4?session=checkpoint-42#candidates');

  await expect(page.locator('.story-flow-item[data-step="select"] a')).toHaveAttribute('aria-current', 'step');
  const candidateCards = page.locator('#candidates .writing-candidate-card');
  const selectRequest = page.waitForRequest((request) => (
    new URL(request.url()).pathname === '/v1/flow-book/pipeline/runs/run-completed-4/select-candidate'
    && request.method() === 'POST'
  ));
  await candidateCards.nth(1).getByRole('button', { name: '采用此稿' }).click();
  expect((await selectRequest).postDataJSON()).toEqual({ candidate_index: 1 });

  await expect(page).toHaveURL('/studio/flow-book/write/4?session=checkpoint-42#proofread');
  await expect(page.locator('.story-flow-item[data-step="proofread"] a')).toHaveAttribute('aria-current', 'step');
  await expect(page.getByRole('textbox', { name: '第 4 章正文' })).toHaveValue(candidateTexts[1]);

  await page.getByRole('button', { name: '保持原计划' }).click();
  await expect(page).toHaveURL('/studio/flow-book/write/4?session=checkpoint-42#continue');
  await expect(page.locator('.story-flow-item[data-step="continue"]')).toHaveClass(/is-current/);
  await expect(page.getByRole('status')).toContainText('校对完成：保持原计划，可以继续下一章');
});

test('workspace, flow rail, and chapter navigation retain the checkpoint session', async ({ page }) => {
  await mockWritingApi(page);
  await page.goto('/studio/flow-book/write/4?session=checkpoint-42');

  const primaryHrefs = await page.locator('.workspace-primary-nav a').evaluateAll((links) => (
    links.map((link) => link.getAttribute('href'))
  ));
  expect(primaryHrefs).toHaveLength(5);
  expect(primaryHrefs.every((href) => href?.includes('?session=checkpoint-42'))).toBe(true);

  const railHrefs = await page.locator('.story-flow-link').evaluateAll((links) => (
    links.slice(1).map((link) => link.getAttribute('href'))
  ));
  expect(railHrefs.every((href) => href?.includes('?session=checkpoint-42'))).toBe(true);

  const chapterHrefs = await page.locator('.story-spine a[href*="/write/"]').evaluateAll((links) => (
    links.map((link) => link.getAttribute('href'))
  ));
  expect(chapterHrefs.length).toBeGreaterThan(0);
  expect(chapterHrefs.every((href) => href?.includes('?session=checkpoint-42'))).toBe(true);

  await page.locator('.workspace-primary-nav a[href*="/world"]').click();
  await expect(page).toHaveURL('/studio/flow-book/world?session=checkpoint-42');
});

test('a new generation keeps the previous candidates visible and temporarily read-only', async ({ page }) => {
  await page.addInitScript(() => {
    const runtime = window as typeof window & { __failPipeline?: () => void };
    const originalFetch = window.fetch.bind(window);
    let rejectPipeline: ((reason?: unknown) => void) | null = null;
    runtime.__failPipeline = () => rejectPipeline?.(new TypeError('synthetic stream failure'));
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
      if (url.includes('/pipeline/run/stream')) {
        return new Promise<Response>((_resolve, reject) => { rejectPipeline = reject; });
      }
      return originalFetch(input, init);
    }) as typeof window.fetch;
  });
  await mockWritingApi(page);
  await page.goto('/studio/flow-book/write/4#candidates');

  const comparison = page.locator('#candidates');
  const candidateCards = comparison.locator('.writing-candidate-card');
  await expect(candidateCards).toHaveCount(3);

  await page.getByRole('button', { name: /生成候选/ }).click();
  await expect(candidateCards).toHaveCount(3);
  await expect(comparison).toContainText('新候选生成中');
  await expect(comparison.getByRole('button', { name: '只读，不可采用' })).toHaveCount(3);

  await page.evaluate(() => {
    (window as typeof window & { __failPipeline?: () => void }).__failPipeline?.();
  });
  await expect(page.getByRole('alert')).toContainText('生成失败');
  await expect(page.getByRole('alert')).not.toContainText('synthetic stream failure');
  await expect(candidateCards).toHaveCount(3);
  await expect(comparison.getByText('新候选生成中')).toBeHidden();
  await expect(comparison.getByRole('button', { name: '采用此稿' })).toHaveCount(2);
});

test('refresh recovers a persisted run, locks duplicates, and reconciles through completion', async ({ page }) => {
  let streamRequests = 0;
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.endsWith('/pipeline/run/stream')) streamRequests += 1;
  });
  const state = await mockWritingApi(page, {
    includeCompletedRun: false,
    generationStarted: true,
    backgroundRunStatus: 'running',
  });
  await page.goto('/studio/flow-book/write/4');

  const lockedGenerate = page.getByRole('button', { name: '正在确认后台结果' });
  await expect(lockedGenerate).toBeDisabled();
  await expect(page.getByRole('alert')).toContainText('检测到刷新前尚未结束的后台任务');
  await expect(page.getByRole('status')).toContainText('已恢复后台生成对账');
  await lockedGenerate.evaluate((button) => { (button as HTMLButtonElement).click(); });
  expect(streamRequests).toBe(0);

  await expect.poll(() => state.runPolls, { timeout: 5_000 }).toBeGreaterThan(1);
  state.currentDraft = candidateTexts[2];
  state.currentRevision = 'revision-background-complete';
  state.selectedCandidate = 2;
  state.backgroundRunStatus = 'completed';

  await expect(page.getByRole('button', { name: /生成候选/ })).toBeEnabled({ timeout: 8_000 });
  await expect(page.getByRole('textbox', { name: '第 4 章正文' })).toHaveValue(candidateTexts[2]);
  await expect(page).toHaveURL('/studio/flow-book/write/4#candidates');
  await expect(page.getByRole('alert')).toContainText('刷新前的后台任务已完成，最新结果已同步');
  expect(streamRequests).toBe(0);
});

test('stopping the page wait keeps duplicate generation locked while the backend run is active', async ({ page }) => {
  await page.addInitScript(() => {
    const runtime = window as typeof window & { __pipelineAttempts?: number };
    const originalFetch = window.fetch.bind(window);
    runtime.__pipelineAttempts = 0;
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
      if (url.includes('/pipeline/run/stream')) {
        runtime.__pipelineAttempts = (runtime.__pipelineAttempts ?? 0) + 1;
        return new Promise<Response>((_resolve, reject) => {
          const abort = (): void => reject(new DOMException('The operation was aborted.', 'AbortError'));
          if (init?.signal?.aborted) {
            abort();
            return;
          }
          init?.signal?.addEventListener('abort', abort, { once: true });
        });
      }
      return originalFetch(input, init);
    }) as typeof window.fetch;
  });
  const state = await mockWritingApi(page, { includeCompletedRun: false });
  await page.goto('/studio/flow-book/write/4');

  await page.getByRole('button', { name: /生成候选/ }).click();
  const stopButton = page.getByRole('button', { name: '停止等待' });
  await expect(stopButton).toBeVisible();
  state.generationStarted = true;
  await stopButton.click();

  const lockedGenerate = page.getByRole('button', { name: '正在确认后台结果' });
  await expect(lockedGenerate).toBeDisabled();
  await expect(page.getByRole('status')).toContainText(
    '页面已停止等待；后台仍在生成，本章暂不可重复运行',
    { timeout: 5_000 },
  );

  await lockedGenerate.evaluate((button) => { (button as HTMLButtonElement).click(); });
  await page.waitForTimeout(100);
  const attempts = await page.evaluate(() => (
    window as typeof window & { __pipelineAttempts?: number }
  ).__pipelineAttempts);
  expect(attempts).toBe(1);
  expect(state.runPolls).toBeGreaterThan(1);
});
