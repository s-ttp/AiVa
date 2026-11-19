import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { APIRequestContext, Page } from '@playwright/test';
import { expect, expectAccessible, GATEWAY_URL, signIn, test } from './support';

/**
 * AiVa Insights against the local gateway: synthetic traffic is generated
 * through the real pipeline first (`aiva-admin traffic`, simulated inference,
 * designated synthetic), then every Insights view is checked for content,
 * row-level scope and WCAG 2.2 AA. Numbers on screen are compared with the
 * gateway's own report, so the suite stays valid with live inference.
 */

const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url));

const VIEWS = [
  { path: '/admin', heading: 'Overview' },
  { path: '/admin/insights', heading: 'Consumption Overview' },
  { path: '/admin/insights/applications', heading: 'Applications' },
  { path: '/admin/insights/users', heading: 'Users' },
  { path: '/admin/insights/prompt-categories', heading: 'Prompt Categories' },
  { path: '/admin/insights/models', heading: 'Models' },
  { path: '/admin/insights/savings', heading: 'Savings' },
  { path: '/admin/insights/quotas', heading: 'Quotas' },
  { path: '/admin/insights/export-center', heading: 'Export Center' },
] as const;

test.describe.configure({ mode: 'serial' });

test.beforeAll(() => {
  execFileSync('uv', ['run', 'aiva-admin', 'traffic', '--runs', '30', '--seed', '21'], {
    cwd: repoRoot,
    stdio: 'inherit',
  });
});

async function apiToken(request: APIRequestContext, userId: string): Promise<string> {
  const response = await request.post(`${GATEWAY_URL}/v1/auth/dev/token`, { data: { user_id: userId } });
  expect(response.ok()).toBe(true);
  return ((await response.json()) as { access_token: string }).access_token;
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

async function settled(page: Page) {
  await page.waitForLoadState('networkidle');
  await expect(page.getByText(/^Loading /)).toHaveCount(0);
}

test('every Insights view renders data and meets WCAG 2.2 AA', async ({ page }) => {
  await signIn(page, 'Tenant Administrator (demo)');
  for (const view of VIEWS) {
    await page.goto(view.path);
    await expect(page.getByRole('heading', { level: 1, name: view.heading })).toBeVisible();
    await settled(page);
    await expect(page.getByRole('alert')).toHaveCount(0);
    await expectAccessible(page, view.path);
  }
});

test('every Insights view reflows at 320 CSS pixels without page-level horizontal scrolling', async ({ page }) => {
  await signIn(page, 'Tenant Administrator (demo)');
  await page.setViewportSize({ width: 320, height: 800 });
  for (const view of VIEWS) {
    await page.goto(view.path);
    await expect(page.getByRole('heading', { level: 1, name: view.heading })).toBeVisible();
    await settled(page);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, `horizontal overflow on ${view.path}`).toBe(0);
  }
});

test('the executive overview shows the gateway totals with their provenance', async ({ page, request }) => {
  const token = await apiToken(request, 'u-finance');
  const response = await request.get(`${GATEWAY_URL}/v1/reports/overview`, {
    params: { from: today(), to: today() },
    headers: { Authorization: `Bearer ${token}` },
  });
  const report = (await response.json()) as { usage: { request_count: number; provenance: string } };
  expect(report.usage.request_count).toBeGreaterThan(0);

  await signIn(page, 'Finance Analyst (demo)');
  await page.goto('/admin');
  await page.getByRole('button', { name: 'Today' }).click();
  const requests = page.getByRole('group', { name: 'Total requests' });
  await expect(requests).toContainText(report.usage.request_count.toLocaleString('en'));
  const label = report.usage.provenance.charAt(0).toUpperCase() + report.usage.provenance.slice(1);
  await expect(requests.getByText(label, { exact: true })).toBeVisible();
  // Simulated inference is never presented as a measured provider cost.
  await expect(page.getByRole('group', { name: 'Measured provider API cost' })).toHaveCount(0);
});

test('drill down from the enterprise to a trace', async ({ page }) => {
  await signIn(page, 'Tenant Administrator (demo)');
  await page.goto(`/admin/insights?from=${today()}&to=${today()}`);
  const steps = [
    ['Consumption By department', 'Customer Care'],
    ['Consumption By application', 'Customer Support Chatbot'],
  ] as const;
  for (const [table, row] of steps) {
    await page.getByRole('table', { name: table }).getByRole('button', { name: `Drill down into ${row}` }).click();
  }
  // Users, categories and models: take the first row with a drilldown each time.
  for (const table of ['Consumption By user or service principal', 'Consumption By prompt category', 'Consumption By model']) {
    await page.getByRole('table', { name: table }).getByRole('button', { name: /^Drill down into / }).first().click();
  }
  const traces = page.getByRole('table', { name: 'Requests matching the filters' });
  await expect(traces).toBeVisible();
  await traces.getByRole('link', { name: /^Open trace / }).first().click();
  await expect(page.getByRole('table', { name: 'Model attempts of this request' })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Drilldown path' })).toContainText('Customer Care');
  await expectAccessible(page, 'trace drilldown');
});

test('a department manager sees only their department, in the UI and through the API', async ({ page, request }) => {
  await signIn(page, 'Customer Care Manager (demo)');
  await page.goto(`/admin/insights/applications?from=${today()}&to=${today()}`);
  const table = page.getByRole('table', { name: 'Consumption per application' });
  await expect(table.getByRole('link', { name: /^Explore / })).toHaveCount(1);
  await expect(table).toContainText('Customer Support Chatbot');

  const token = await apiToken(request, 'u-care-manager');
  const other = await request.get(`${GATEWAY_URL}/v1/reports/consumption`, {
    params: { from: today(), to: today(), group_by: 'user', department_id: 'fraud-risk' },
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(other.status()).toBe(403);
});

test('a finance analyst exports a filtered CSV and downloads it', async ({ page }) => {
  await signIn(page, 'Finance Analyst (demo)');
  await page.goto(`/admin/insights/export-center?from=${today()}&to=${today()}&application_id=gct-support-chatbot`);
  await page.getByRole('combobox', { name: 'Group by' }).selectOption('user');
  await page.getByRole('button', { name: 'Request CSV export' }).click();
  const table = page.getByRole('table', { name: 'Your CSV exports, newest first' });
  const ready = table.getByRole('row').filter({ hasText: 'Ready' }).first();
  await expect(ready).toBeVisible({ timeout: 15_000 });
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    ready.getByRole('button', { name: /^Download export / }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^aiva-consumption-user-.*\.csv$/);
  const path = await download.path();
  const csv = readFileSync(path, 'utf-8');
  expect(csv).toContain('# Data designation: synthetic');
  expect(csv).toContain('# Filters: application_id=gct-support-chatbot');
  expect(csv).toContain('usr_');
  expect(csv).not.toContain('syn-care');
  await expectAccessible(page, 'export center with an export');
});
