import AxeBuilder from '@axe-core/playwright';
import { expect, test as base, type Page } from '@playwright/test';

export const GATEWAY_URL = process.env.AIVA_E2E_GATEWAY_URL ?? 'http://localhost:8000';

/** WCAG 2.0, 2.1 and 2.2 level A and AA rules. */
const WCAG_22_AA = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22a', 'wcag22aa'];

/** Fails with the full violation list so a regression names the rule and the element. */
export async function expectAccessible(page: Page, label: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(WCAG_22_AA).analyze();
  const violations = results.violations.map((violation) => ({
    rule: violation.id,
    impact: violation.impact,
    help: violation.help,
    targets: violation.nodes.map((node) => node.target.join(' ')),
  }));
  expect(violations, `WCAG 2.2 AA violations on ${label}`).toEqual([]);
}

/** Every page talks to the gateway under test, whatever config.js was built with. */
export const test = base.extend({
  page: async ({ page }, use) => {
    await page.route('**/config.js', (route) =>
      route.fulfill({
        contentType: 'text/javascript',
        body: `window.__AIVA_CONFIG__ = Object.freeze(${JSON.stringify({ gatewayUrl: GATEWAY_URL })});`,
      }),
    );
    await use(page);
  },
});

export { expect };

/** Signs in through the gateway's demo sign-in as the named demo user. */
export async function signIn(page: Page, displayName: string): Promise<void> {
  await page.goto('/');
  await page.getByRole('radio', { name: displayName }).check();
  await page.getByRole('button', { name: 'Sign in as demo user' }).click();
  await page.waitForURL((url) => url.pathname !== '/');
}

/** Signs in and opens the Test Console. */
export async function signInToConsole(page: Page, displayName: string): Promise<void> {
  await signIn(page, displayName);
  if (new URL(page.url()).pathname !== '/console') {
    await page.goto('/console');
    await page.waitForURL((url) => url.pathname === '/console');
  }
}
