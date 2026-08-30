import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

const projects = [
  {
    project_id: 'draft-agent',
    name: '月海来信',
    genre: '科幻',
    primary_genre: '科幻',
    story_tags: ['悬疑', '成长'],
    premise: '失忆的信使在月面城市收到一封来自未来的信。',
    db_path: '/tmp/draft-agent.db',
    created_at: '2026-08-30T08:00:00Z',
    chapter_count: 3,
    canon_fact_count: 18,
    archived: false,
  },
  {
    project_id: 'post-agent',
    name: '雨夜修补匠',
    genre: '都市奇幻',
    premise: '她修补旧伞，也修补被雨冲散的记忆。',
    db_path: '/tmp/post-agent.db',
    created_at: '2026-08-29T08:00:00Z',
    chapter_count: 8,
    canon_fact_count: 27,
    archived: false,
  },
  {
    project_id: 'ready-book',
    name: '边城慢火',
    genre: '武侠',
    premise: '一间边城酒馆等来了十年前失踪的掌柜。',
    db_path: '/tmp/ready-book.db',
    created_at: '2026-08-28T08:00:00Z',
    chapter_count: 12,
    canon_fact_count: 34,
    archived: false,
  },
  {
    project_id: 'new-book',
    name: '未命名的夏天',
    genre: '青春',
    premise: '两个陌生人在毕业前共同保守一个秘密。',
    db_path: '/tmp/new-book.db',
    // Deliberately newest: the recommendation should still prefer an older
    // manuscript that is waiting for an author decision.
    created_at: '2026-08-31T08:00:00Z',
    chapter_count: 0,
    canon_fact_count: 0,
    archived: false,
  },
];

const emptyStoryMap = { plan: null, arcs: [], volumes: [], chapters: [] };

function agentSession(projectId: string, stage: 'draft_choice' | 'post_chapter', chapter: number) {
  return {
    session_id: `session-${projectId}`,
    project_id: projectId,
    interaction_mode: 'guided',
    stage,
    status: 'awaiting_choice',
    decision: null,
    last_decision: null,
    state: { current_chapter: chapter },
    story_map: emptyStoryMap,
    error: null,
  };
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function mockShelfApi(page: Page): Promise<void> {
  await page.route('**/health', (route) => json(route, { status: 'ok', version: 'test' }));
  await page.route('**/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/v1/projects') return json(route, projects);

    const projectMatch = path.match(/^\/v1\/projects\/([^/]+)$/);
    if (projectMatch) {
      const project = projects.find((item) => item.project_id === decodeURIComponent(projectMatch[1]));
      return project ? json(route, project) : json(route, { detail: 'not found' }, 404);
    }

    if (path === '/v1/draft-agent/agent/sessions') {
      return json(route, [agentSession('draft-agent', 'draft_choice', 4)]);
    }
    if (path === '/v1/post-agent/agent/sessions') {
      return json(route, [agentSession('post-agent', 'post_chapter', 9)]);
    }
    if (path.endsWith('/agent/sessions')) return json(route, []);
    if (path.endsWith('/story-map')) return json(route, emptyStoryMap);
    return json(route, []);
  });
}

test.beforeEach(async ({ page }) => {
  await mockShelfApi(page);
});

test('the shelf surfaces the next author action and preserves the Agent checkpoint', async ({ page }) => {
  await page.goto('/studio');

  const spotlight = page.locator('.shelf-next-card');
  await expect(spotlight.getByRole('heading', { name: '月海来信' })).toBeVisible();
  await expect(spotlight.getByText('等待你选正文')).toBeVisible();
  await spotlight.getByRole('button', { name: '比较候选' }).click();

  await expect(page).toHaveURL('/studio/draft-agent/plan?session=session-draft-agent');
  await expect(page.getByRole('heading', { name: '规划室' })).toBeVisible();
});

test('search, status filters, sorting, and all active Agent routes stay actionable', async ({ page }) => {
  await page.goto('/studio');

  await page.getByLabel('筛选作品状态').selectOption('needs_decision');
  await expect(page.locator('.shelf-project-card')).toHaveCount(2);
  await expect(page.locator('.shelf-project-card h2')).toHaveText(['月海来信', '雨夜修补匠']);

  await page.getByLabel('搜索作品').fill('雨夜');
  await expect(page.locator('.shelf-project-card')).toHaveCount(1);
  await expect(page.locator('.shelf-next-card').getByRole('heading')).toHaveText('雨夜修补匠');
  await page.locator('.shelf-next-card').getByRole('button', { name: '核对本章' }).click();
  await expect(page).toHaveURL('/studio/post-agent/plan?session=session-post-agent');

  await page.goto('/studio');
  await page.getByRole('button', { name: '进度' }).click();
  await expect(page.locator('.shelf-project-card h2')).toHaveText([
    '边城慢火',
    '雨夜修补匠',
    '月海来信',
    '未命名的夏天',
  ]);
});

test('the author-first shelf stays usable on a narrow screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/studio');

  await expect(page.getByRole('heading', { name: '今天，从最顺手的下一步开始。' })).toBeVisible();
  await expect(page.getByRole('button', { name: '＋ 新建一本书' })).toBeVisible();
  await expect(page.locator('.shelf-next-card').getByRole('button', { name: '比较候选' })).toBeVisible();
  await expect(page.getByLabel('搜索作品')).toBeVisible();

  const hasHorizontalPageOverflow = await page.evaluate(() => (
    document.documentElement.scrollWidth > document.documentElement.clientWidth
  ));
  expect(hasHorizontalPageOverflow).toBe(false);
});
