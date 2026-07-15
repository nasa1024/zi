import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

const project = {
  project_id: 'demo',
  name: '演示长篇',
  genre: '玄幻',
  db_path: '/tmp/demo.db',
  created_at: '2026-07-15T00:00:00Z',
  chapter_count: 6,
  canon_fact_count: 12,
  archived: false,
};

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function mockApi(page: Page): Promise<void> {
  await page.route('**/health', (route) => json(route, { status: 'ok', version: 'test' }));
  await page.route('**/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/v1/projects/demo') return json(route, project);
    if (path.endsWith('/story-map')) {
      return json(route, { plan: null, arcs: [], volumes: [], chapters: [] });
    }
    if (path.includes('/chapter-cards')) return json(route, []);
    if (path.endsWith('/chapters/7/draft')) {
      return json(route, {
        chapter: 7,
        revision_id: null,
        revision_round: -1,
        draft_text: '',
        word_count: 0,
        status: 'empty',
      });
    }
    if (path.endsWith('/contract') || path.endsWith('/alignment')) {
      return json(route, { detail: 'not found' }, 404);
    }
    if (path.endsWith('/plan-versions')) return json(route, []);
    if (path.endsWith('/pipeline/runs')) return json(route, []);
    if (path.endsWith('/agent/sessions')) return json(route, []);
    if (path.endsWith('/volumes')) return json(route, []);
    return json(route, []);
  });
}

test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await page.addInitScript(() => {
    localStorage.setItem('nf_active_project', 'stale-project');
  });
});

test('the URL project id wins over stale local state', async ({ page }) => {
  await page.goto('/studio/demo/plan');

  await expect(page).toHaveURL(/\/studio\/demo\/plan$/);
  await expect(page.getByRole('heading', { name: '规划室' })).toBeVisible();
  await expect(page.getByRole('link', { name: '返回书架切换项目' })).toContainText('演示长篇');
  await expect(page.locator('a[href="/studio/demo/world"]')).toHaveCount(1);
});

test('the chapter URL drives the writing desk and revision guard', async ({ page }) => {
  await page.goto('/studio/demo/write/7');

  await expect(page).toHaveURL(/\/studio\/demo\/write\/7$/);
  await expect(page.getByRole('heading', { name: '第 7 章' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '第 7 章正文' })).toBeVisible();
  await expect(page.getByRole('button', { name: '等待修订' })).toBeDisabled();
});
