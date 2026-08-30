import { expect, test } from '@playwright/test';
import type { Route } from '@playwright/test';

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

test('the rolling plan starts at the next chapter and follows refreshed project progress', async ({ page }) => {
  let chapterCount = 6;

  await page.route('**/health', (route) => json(route, { status: 'ok', version: 'test' }));
  await page.route('**/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/v1/projects/demo') {
      return json(route, {
        project_id: 'demo',
        name: '演示长篇',
        genre: '玄幻',
        db_path: '/tmp/demo.db',
        created_at: '2026-07-15T00:00:00Z',
        chapter_count: chapterCount,
        canon_fact_count: 12,
        archived: false,
      });
    }
    if (path === '/v1/demo/planning/windows/generate') {
      return json(route, {
        start_chapter: 7,
        horizon: 5,
        decision: {
          decision_id: 'decision-next',
          recommended_option_id: 'route-a',
          options: [{
            id: 'route-a',
            title: '稳步推进',
            summary: '先让主角拿到下一条线索。',
            payload: {
              chapters: [{ chapter: 7, title: '雾中灯塔', status: 'candidate', beats: [] }],
            },
          }],
        },
      }, 201);
    }
    if (path === '/v1/demo/planning/windows/decision-next/approve') {
      chapterCount = 7;
      return json(route, {
        decision_id: 'decision-next',
        selected_option_id: 'route-a',
        planned: [7],
        skipped: [],
        version_ids: { '7': 'version-7' },
      });
    }
    if (path.endsWith('/story-map')) {
      return json(route, { plan: null, arcs: [], volumes: [], chapters: [] });
    }
    if (path.endsWith('/agent/sessions') || path.endsWith('/volumes')) return json(route, []);
    return json(route, []);
  });

  await page.goto('/studio/demo/plan');

  const startChapter = page.getByLabel('起始章');
  await expect(startChapter).toHaveValue('7');
  await page.getByRole('button', { name: '生成三条路线' }).click();
  await expect(page.getByRole('heading', { name: '稳步推进' })).toBeVisible();
  await page.getByRole('button', { name: '采用这条路线' }).click();

  await expect(startChapter).toHaveValue('8');
  await expect(page.getByText('第 8 章往哪里走')).toBeVisible();
});
