import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

const project = {
  project_id: 'safe-book',
  name: '雾港来客',
  genre: '悬疑',
  premise: '一名失踪多年的船员，在同一场雾里第二次靠岸。',
  db_path: '/tmp/safe-book.db',
  created_at: '2026-08-30T08:00:00Z',
  chapter_count: 6,
  canon_fact_count: 12,
  archived: false,
};

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function mockHealth(page: Page): Promise<void> {
  await page.route('**/health', (route) => json(route, { status: 'ok', version: 'test' }));
}

test('continue actions stay locked until the Agent checkpoint is known', async ({ page }) => {
  await mockHealth(page);
  let releaseSession!: () => void;
  const sessionGate = new Promise<void>((resolve) => { releaseSession = resolve; });

  await page.route('**/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/v1/projects') return json(route, [project]);
    if (path === '/v1/safe-book/agent/sessions') {
      await sessionGate;
      return json(route, []);
    }
    return json(route, []);
  });

  await page.goto('/studio');

  const spotlightButton = page.locator('.shelf-next-card').getByRole('button', { name: '正在同步' });
  const cardButton = page.locator('.shelf-project-card').getByRole('button', { name: '正在同步' });
  await expect(spotlightButton).toBeDisabled();
  await expect(cardButton).toBeDisabled();

  releaseSession();

  await expect(page.locator('.shelf-next-card').getByRole('button', { name: '继续写作' })).toBeEnabled();
  await expect(page.locator('.shelf-project-card').getByRole('button', { name: '继续写作' })).toBeEnabled();
});

for (const failure of [
  { label: 'permission failure', status: 403 },
  { label: 'server failure', status: 500 },
  { label: 'network failure', status: null },
]) {
  test(`an Agent ${failure.label} is shown as unknown and cannot resume`, async ({ page }) => {
    await mockHealth(page);
    await page.route('**/v1/**', async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/v1/projects') return json(route, [project]);
      if (path === '/v1/safe-book/agent/sessions') {
        if (failure.status == null) return route.abort('failed');
        return json(route, { detail: 'checkpoint unavailable' }, failure.status);
      }
      return json(route, []);
    });

    await page.goto('/studio');

    await expect(page.locator('.shelf-project-card').getByText('状态未知')).toBeVisible();
    await expect(page.locator('.shelf-next-card').getByRole('button', { name: '暂不可继续' })).toBeDisabled();
    await expect(page.locator('.shelf-project-card').getByRole('button', { name: '暂不可继续' })).toBeDisabled();
    await expect(page.getByRole('button', { name: '重试同步状态' })).toBeVisible();
  });
}

test('an explicit missing Agent endpoint uses the chapter-count compatibility fallback', async ({ page }) => {
  await mockHealth(page);
  await page.route('**/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/v1/projects') return json(route, [project]);
    if (path === '/v1/safe-book/agent/sessions') {
      return json(route, { detail: 'not found' }, 404);
    }
    return json(route, []);
  });

  await page.goto('/studio');

  const continueButton = page.locator('.shelf-next-card').getByRole('button', { name: '继续写作' });
  await expect(continueButton).toBeEnabled();
  await continueButton.click();
  await expect(page).toHaveURL('/studio/safe-book/write/7');
});

test('a project-list error has its own retry state instead of masquerading as an empty shelf', async ({ page }) => {
  await mockHealth(page);
  let projectsAvailable = false;

  await page.route('**/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/v1/projects') {
      return projectsAvailable
        ? json(route, [project])
        : json(route, { detail: 'temporary shelf failure' }, 500);
    }
    if (path === '/v1/safe-book/agent/sessions') return json(route, []);
    return json(route, []);
  });

  await page.goto('/studio');

  await expect(page.getByRole('heading', { name: '书架暂时没能打开' })).toBeVisible();
  await expect(page.getByText('书架空空')).toHaveCount(0);
  projectsAvailable = true;
  await page.getByRole('button', { name: '重试加载' }).click();

  await expect(page.locator('.shelf-project-card').getByRole('heading', { name: '雾港来客' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '书架暂时没能打开' })).toHaveCount(0);
});
