import type { Page } from '@playwright/test';
import { expect, expectAccessible, signIn, test } from './support';

/**
 * Admin › Quality & Assurance against the local gateway's GET /v1/reports/quality.
 * Every check compares the page with the gateway's own response, so it holds
 * with or without traffic in the window.
 */
interface Ratio {
  numerator: number;
  denominator: number;
  value: number | null;
}
interface QualityBody {
  totals: {
    request_count: number;
    assurance: { passed: Ratio };
    evidence_coverage: { status: string };
  };
  rows: unknown[];
}

async function openQuality(page: Page, path = '/admin/quality'): Promise<QualityBody> {
  const [response] = await Promise.all([
    page.waitForResponse((r) => new URL(r.url()).pathname === '/v1/reports/quality' && r.request().method() === 'GET'),
    page.goto(path),
  ]);
  expect(response.status()).toBe(200);
  return (await response.json()) as QualityBody;
}

test.describe('Quality & Assurance', () => {
  test('shows the quality report the gateway computed, accessibly', async ({ page }) => {
    await signIn(page, 'Finance Analyst (demo)');
    const body = await openQuality(page);

    if (body.totals.request_count === 0) {
      await expect(page.getByRole('heading', { name: 'No requests in this window' })).toBeVisible();
    } else {
      const requests = page.getByRole('group', { name: 'Requests in scope' });
      await expect(requests).toContainText(body.totals.request_count.toLocaleString('en'));
      const assurance = page.getByRole('group', { name: 'Assurance checks passed' });
      if (body.totals.assurance.passed.value === null) await expect(assurance).toContainText('Not available');
      if (body.totals.evidence_coverage.status === 'not_measured') {
        await expect(page.getByRole('group', { name: 'Evidence coverage' })).toContainText('Not measured yet');
      }
      const table = page.getByRole('table', { name: 'Quality by application' });
      await expect(table.getByRole('row')).toHaveCount(body.rows.length + 1);
      await expect(page.getByText('Computed live')).toBeVisible();
    }
    await expectAccessible(page, 'quality and assurance');
  });

  test('a visitor reaches it from the menu and the page reflows at 320 CSS pixels', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: 'Explore as a visitor' }).click();
    await expect(page).toHaveURL(/\/console$/);
    const [response] = await Promise.all([
      page.waitForResponse((r) => new URL(r.url()).pathname === '/v1/reports/quality'),
      page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Quality & Assurance' }).click(),
    ]);
    expect(response.status()).toBe(200);
    await expect(page.getByRole('heading', { level: 1, name: 'Quality & Assurance' })).toBeVisible();
    await page.setViewportSize({ width: 320, height: 800 });
    await expect(page.getByLabel('User (pseudonym)')).toHaveCount(0);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await expectAccessible(page, 'quality and assurance at 320 CSS pixels');
  });
});
