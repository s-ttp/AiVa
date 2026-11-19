import type { Page } from '@playwright/test';
import { expect, expectAccessible, signInToConsole, test } from './support';

/**
 * The public demo's run choices and visitor views (ADR-0019) against the local
 * gateway, which runs simulated inference: a live request is refused as not
 * live-capable, and a recorded replay that finds no recording is answered by
 * the simulated adapter. Nothing here calls a provider.
 */
async function startVisitorSession(page: Page): Promise<void> {
  await page.goto('/');
  await page.getByRole('button', { name: 'Explore as a visitor' }).click();
  await expect(page).toHaveURL(/\/console$/);
}

async function run(page: Page): Promise<void> {
  await Promise.all([
    page.waitForResponse((response) => response.url().endsWith('/v1/ai/chat')),
    page.getByRole('button', { name: 'Run through AiVa' }).click(),
  ]);
}

test.describe('public demo run choices', () => {
  test('a visitor runs the recorded replay and sees the public live budget', async ({ page }) => {
    await startVisitorSession(page);
    await page.goto('/console/run?scenario=customer-support');

    const mode = page.getByRole('group', { name: 'Execution mode' });
    await expect(mode.getByRole('radio', { name: /^Recorded replay/ })).toBeChecked();
    await expect(page.getByText('Live runs in the public demo')).toBeVisible();
    await expect(page.getByText(/^Public live budget left today: \d+%/)).toBeVisible();
    await expectAccessible(page, 'run form for a visitor');

    await run(page);
    const result = page.locator('section').filter({ has: page.getByRole('heading', { level: 2, name: 'Run result' }) });
    await expect(result.getByText('Recorded replay', { exact: true })).toBeVisible();
    await expect(result.getByText(/No recording matched this request/)).toBeVisible();
  });

  test('a tester asking for a live run on a simulated gateway is offered the replay, never downgraded', async ({ page }) => {
    await signInToConsole(page, 'AiVa Tester (demo)');
    await page.goto('/console/run?scenario=fraud-management');

    await page.getByRole('group', { name: 'Execution mode' }).getByRole('radio', { name: /^Live/ }).check();
    await run(page);
    await expect(page.getByText('Live runs aren’t available on this gateway.')).toBeVisible();
    await expect(page.getByRole('group', { name: 'Execution mode' })).toHaveCount(0);
    await expectAccessible(page, 'refused live run');

    const [replay] = await Promise.all([
      page.waitForRequest((request) => request.url().endsWith('/v1/ai/chat')),
      page.getByRole('button', { name: 'Run the recorded replay' }).click(),
    ]);
    expect(replay.postDataJSON()).toMatchObject({ execution_mode: 'replay' });
    await expect(page.getByText('Recorded replay', { exact: true })).toBeVisible();
  });
});

test.describe('public visitor views', () => {
  test('a visitor sees reports without the user dimension', async ({ page }) => {
    await startVisitorSession(page);
    const nav = page.getByRole('navigation', { name: 'Primary' });
    await expect(nav.getByRole('link', { name: 'Consumption Overview' })).toBeVisible();
    for (const hidden of ['Users', 'Approval Queue', 'Budgets & Quotas', 'Export Center']) {
      await expect(nav.getByRole('link', { name: hidden, exact: true })).toHaveCount(0);
    }

    await page.goto('/admin/insights');
    await expect(page.getByRole('heading', { level: 1, name: 'Consumption Overview' })).toBeVisible();
    await expect(page.getByLabel('User (pseudonym)')).toHaveCount(0);
    await expectAccessible(page, 'consumption overview for a visitor');

    await page.goto('/admin/insights/users');
    await expect(page.getByText('Not shown for your role')).toBeVisible();
  });
});
