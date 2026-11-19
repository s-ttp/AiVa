import { demoPasscode } from '../playwright.config';
import { expect, expectAccessible, test } from './support';

/**
 * The public demo's sign-ins (ADR-0019) against the local gateway, which offers
 * all three: a visitor session, the passcode sign-in and the local dev sign-in.
 */
test.describe('public demo sign-in', () => {
  test('the landing page offers every sign-in the gateway lists, accessibly', async ({ page }) => {
    await page.goto('/');
    const panel = page.locator('section').filter({ has: page.getByRole('heading', { level: 2, name: 'Sign in' }) });
    await expect(panel.getByRole('button', { name: 'Explore as a visitor' })).toBeVisible();
    await expect(panel.getByLabel('Passcode')).toBeVisible();
    await expect(panel.getByRole('heading', { name: 'Developer sign-in (local gateway)' })).toBeVisible();
    await expect(panel.getByRole('radio', { name: /AiVa Tester/ })).toBeChecked();
    await expectAccessible(page, 'sign-in options');
  });

  test('a visitor explores without an account', async ({ page }) => {
    await page.goto('/');
    const [session] = await Promise.all([
      page.waitForResponse((response) => response.url().endsWith('/v1/auth/public/session')),
      page.getByRole('button', { name: 'Explore as a visitor' }).click(),
    ]);
    expect(session.status()).toBe(201);
    await expect(page).toHaveURL(/\/console$/);
    await expect(page.getByText('Public visitor (demo)')).toBeVisible();
    await expect(page.getByRole('region', { name: 'Ten locked synthetic GCT scenarios' })).toBeVisible();
    const nav = page.getByRole('navigation', { name: 'Primary' });
    await expect(nav.getByRole('link', { name: 'Run workspace' })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'Budgets & Quotas' })).toHaveCount(0);
    await expectAccessible(page, 'scenario library as a visitor');
  });

  test('a demo user signs in with the passcode after a wrong one', async ({ page }) => {
    await page.goto('/');
    await page.getByLabel('Demo user', { exact: true }).selectOption({ label: 'Security Auditor (demo) · Security auditor' });
    const passcode = page.getByLabel('Passcode');
    await passcode.fill('not-the-passcode');
    await page.getByRole('button', { name: 'Sign in with passcode' }).click();
    await expect(page.getByRole('alert')).toHaveText('The passcode is not correct. Check it with the demo owner.');
    await expect(passcode).toHaveAttribute('aria-invalid', 'true');
    await expectAccessible(page, 'wrong passcode');

    await passcode.fill(demoPasscode);
    await page.getByRole('button', { name: 'Sign in with passcode' }).click();
    await page.waitForURL((url) => url.pathname !== '/');
    await expect(page.getByText('Security Auditor (demo)')).toBeVisible();
  });

  test('the landing page says the demo is waking up while the gateway starts, then recovers', async ({ page }) => {
    // The public demo's proxy answers this while the gateway Space starts (deploy/spaces/shared/gateway-proxy.sh).
    await page.route('**/v1/auth/sign-in-options', (route) =>
      route.fulfill({
        status: 503,
        headers: { 'Retry-After': '30', 'Access-Control-Allow-Origin': '*' },
        contentType: 'application/json',
        body: JSON.stringify({
          error: {
            code: 'GATEWAY_STARTING',
            message: 'The demo backend is starting; ready in about 3 minutes.',
            remediation: 'Retry in a minute.',
            retryable: true,
            rule_ids: [],
            run_id: null,
            details: {},
          },
        }),
      }),
    );
    await page.goto('/');
    const waking = page.getByRole('status').filter({ hasText: 'The demo is waking up' });
    await expect(waking).toBeVisible();
    await expectAccessible(page, 'demo waking up');

    await page.unroute('**/v1/auth/sign-in-options');
    await waking.getByRole('button', { name: 'Try again now' }).click();
    await expect(page.getByRole('button', { name: 'Explore as a visitor' })).toBeVisible();
  });

  test('a paused gateway Space is called paused, not waking, accessibly', async ({ page }) => {
    // Hugging Face's own page for a paused Space, reached directly (ADR-0025): it does not wake on its own.
    await page.route('**/v1/auth/sign-in-options', (route) =>
      route.fulfill({
        status: 503,
        headers: { 'Access-Control-Allow-Origin': '*' },
        contentType: 'text/html',
        body: 'The space is paused, ask a maintainer to restart it',
      }),
    );
    await page.goto('/');
    const paused = page.getByRole('status').filter({ hasText: 'The demo is paused' });
    await expect(paused).toBeVisible();
    await expect(page.getByText('The demo is waking up')).toHaveCount(0);
    await expectAccessible(page, 'demo paused');

    await page.unroute('**/v1/auth/sign-in-options');
    await paused.getByRole('button', { name: 'Try again' }).click();
    await expect(page.getByRole('button', { name: 'Explore as a visitor' })).toBeVisible();
  });
});

test.describe('public demo sign-in at 320 CSS pixels', () => {
  test.use({ viewport: { width: 320, height: 800 } });

  test('the sign-in options reflow without horizontal scrolling', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('button', { name: 'Explore as a visitor' })).toBeVisible();
    await expect(page.getByLabel('Passcode')).toBeVisible();
    await expect(page.getByRole('radio', { name: /AiVa Tester/ })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await expectAccessible(page, 'sign-in options at 320px');
  });
});
