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
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === '/v1/catalogs/writing-profiles') {
      const english = url.searchParams.get('language')?.startsWith('en');
      return json(route, {
        catalog_version: 'test',
        items: english ? [{
          id: 'en_progression_serial_v1',
          version: '1.0.0',
          display_name: 'Progression Fantasy Serial',
          description: 'Visible progress and forward-driving chapter endings.',
          language: 'en-US',
          market: 'global_english',
          publication_format: 'web_serial',
          default_story_tags: ['progression_fantasy'],
          defaults: { pov: 'third_person_limited', tense: 'past', pace: 'fast', chapter_word_range: [1500, 2500] },
        }] : [{
          id: 'cn_web_novel_v1',
          version: '1.0.0',
          display_name: '中文网文',
          description: '兼容现有中文项目。',
          language: 'zh-CN',
          market: 'cn_web',
          publication_format: 'web_serial',
          defaults: { pov: 'third_person_limited', tense: 'past', pace: 'fast' },
        }],
      });
    }
    if (path === '/v1/catalogs/genres') {
      const english = url.searchParams.get('language')?.startsWith('en');
      return json(route, { items: english ? [{ id: 'fantasy', label: 'Fantasy' }] : [{ id: 'xuanhuan', label: '玄幻' }] });
    }
    if (path === '/v1/projects/setup/suggest') {
      const body = route.request().postDataJSON() as { content_language?: string };
      const english = body.content_language?.startsWith('en');
      return json(route, {
        candidates: [{
          genre: english ? 'fantasy' : 'xuanhuan',
          premise: english ? 'English premise from the selected profile.' : '中文候选设定。',
          power_ranks: [],
        }],
        degraded: false,
      });
    }
    if (path === '/v1/projects' && route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      const writingProfileId = String(body.writing_profile_id ?? 'cn_web_novel_v1');
      return json(route, {
        ...project,
        project_id: 'created',
        name: body.name || 'Untitled',
        genre: body.primary_genre || body.genre,
        primary_genre: body.primary_genre,
        content_language: body.content_language,
        target_market: body.target_market,
        publication_format: body.publication_format,
        writing_profile_id: writingProfileId,
        writing_profile_version: 1,
        writing_profile_summary: {
          id: writingProfileId,
          display_name: writingProfileId,
          version: 1,
          content_language: body.content_language,
        },
      }, 201);
    }
    if (path === '/v1/projects') return json(route, []);
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

test('the project wizard submits an explicit English writing profile', async ({ page }) => {
  await page.goto('/studio');
  await page.getByRole('button', { name: '＋ 新建一本书' }).click();
  await page.locator('#cp-language').selectOption('en-US');
  await page.getByRole('button', { name: '下一步 →' }).click();

  await expect(page.getByText('Progression Fantasy Serial', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: '下一步 →' })).toBeEnabled();
  await page.getByRole('button', { name: '下一步 →' }).click();
  await page.getByRole('tab', { name: '✍️ 自己填' }).click();

  const createRequest = page.waitForRequest((request) => (
    new URL(request.url()).pathname === '/v1/projects' && request.method() === 'POST'
  ));
  await page.getByRole('button', { name: '✨ 零输入开始创作' }).click();
  const request = await createRequest;
  expect(request.postDataJSON()).toMatchObject({
    content_language: 'en-US',
    target_market: 'global_english',
    publication_format: 'web_serial',
    primary_genre: 'fantasy',
    writing_profile_id: 'en_progression_serial_v1',
  });
  await expect(page).toHaveURL(/\/studio\/created\/plan$/);
});

test('changing content language clears generated plot candidates', async ({ page }) => {
  await page.goto('/studio');
  await page.getByRole('button', { name: '＋ 新建一本书' }).click();
  await page.locator('#cp-language').selectOption('en-US');
  await page.getByRole('button', { name: '下一步 →' }).click();
  await expect(page.getByRole('button', { name: '下一步 →' })).toBeEnabled();
  await page.getByRole('button', { name: '下一步 →' }).click();
  await page.getByRole('button', { name: '✨ 生成 3 个候选' }).click();
  await expect(page.getByText('English premise from the selected profile.')).toBeVisible();

  await page.getByRole('button', { name: '← 上一步' }).click();
  await page.getByRole('button', { name: '← 上一步' }).click();
  page.once('dialog', (dialog) => dialog.accept());
  await page.locator('#cp-language').selectOption('zh-CN');
  await page.getByRole('button', { name: '下一步 →' }).click();
  await expect(page.getByRole('button', { name: '下一步 →' })).toBeEnabled();
  await page.getByRole('button', { name: '下一步 →' }).click();

  await expect(page.getByText('English premise from the selected profile.')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '✨ 生成 3 个候选' })).toBeVisible();
});
