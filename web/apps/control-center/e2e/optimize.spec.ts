import { expect, expectAccessible, signIn, test } from './support';

const caption = 'Registered models, their pinned versions and residency';

test.describe('AiVa Optimize against the local gateway', () => {
  test('the model registry lists every model the gateway reports, with its pinned revision', async ({ page }) => {
    await signIn(page, 'Tenant Administrator (demo)');
    const [response] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/v1/admin/models') && r.request().method() === 'GET'),
      page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Model Registry' }).click(),
    ]);
    expect(response.status()).toBe(200);
    const registry = (await response.json()) as {
      models: {
        id: string;
        catalog_configured: boolean;
        provider_model_id: string | null;
        revision: string | null;
        sovereign: boolean;
        context_window?: number | null;
        rate_card?: { id: string; version: string; effective_from: string } | null;
      }[];
    };

    await expect(page.getByRole('heading', { level: 1, name: 'Model Registry' })).toBeVisible();
    const table = page.getByRole('table', { name: caption });
    await expect(table.getByRole('row')).toHaveCount(registry.models.length + 1);
    const prices = page.getByRole('table', { name: 'Pinned prices per model, in US dollars' });
    for (const model of registry.models) {
      const row = table.getByRole('row').filter({ hasText: model.id });
      if (model.catalog_configured) {
        await expect(row).toContainText(model.provider_model_id ?? '');
        await expect(row).toContainText(`Revision ${model.revision ?? ''}`);
      } else {
        await expect(row).toContainText('Not configured in the model catalog');
      }
      if (model.sovereign) await expect(row).toContainText('Sovereign');
      if (model.context_window != null) {
        await expect(row).toContainText(`${model.context_window.toLocaleString('en')} tokens`);
      }
      const price = prices.getByRole('row').filter({ hasText: model.id });
      if (model.rate_card) {
        await expect(price).toContainText(model.rate_card.id);
        await expect(price).toContainText(`Version ${model.rate_card.version}`);
        await expect(price).toContainText(`Effective from ${model.rate_card.effective_from}`);
      } else {
        await expect(price).toContainText('No rate card configured');
      }
    }
    await expectAccessible(page, 'model registry');
  });

  // The local gateway cannot produce these states without editing the catalog or unpublishing every
  // policy version, so the browser alters the gateway's real response (one model) or answers with the
  // gateway's own error code.
  test('a model missing from the catalog and an unpublished registry read as not configured', async ({ page }) => {
    await signIn(page, 'Tenant Administrator (demo)');
    let missing = '';
    await page.route('**/v1/admin/models', async (route) => {
      const response = await route.fetch();
      const body = (await response.json()) as { models: { id: string }[] };
      const [first, ...rest] = body.models;
      missing = first?.id ?? '';
      const unconfigured = {
        ...first,
        catalog_configured: false,
        provider: null,
        provider_model_id: null,
        revision: null,
        context_window: null,
        provider_route: null,
        adapter: null,
        rate_card: null,
      };
      await route.fulfill({ response, json: { ...body, models: [unconfigured, ...rest] } });
    });
    await page.goto('/admin/optimize/model-registry');
    const table = page.getByRole('table', { name: caption });
    await expect(table).toBeVisible();

    expect(missing).not.toBe('');
    const row = table.getByRole('row').filter({ hasText: missing });
    await expect(row).toContainText('Not configured in the model catalog');
    await expect(row).not.toContainText('Revision');
    const price = page
      .getByRole('table', { name: 'Pinned prices per model, in US dollars' })
      .getByRole('row')
      .filter({ hasText: missing });
    await expect(price).toContainText('No rate card configured');
    await expectAccessible(page, 'model registry with a model missing from the catalog');

    await page.unroute('**/v1/admin/models');
    await page.route('**/v1/admin/models', (route) =>
      route.fulfill({
        status: 503,
        json: {
          error: {
            code: 'POLICY_BUNDLE_MISSING',
            message: 'No policy version is published.',
            remediation: 'Publish a policy version in AiVa Govern.',
            rule_ids: [],
            retryable: false,
            details: {},
          },
        },
      }),
    );
    await page.reload();
    await expect(page.getByRole('heading', { name: 'The model registry is not configured' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open Policy Rules' })).toBeVisible();
    await expectAccessible(page, 'model registry not configured');
  });

  test('the model registry reflows at 320 CSS pixels', async ({ page }) => {
    await signIn(page, 'Tenant Administrator (demo)');
    await page.setViewportSize({ width: 320, height: 800 });
    await page.goto('/admin/optimize/model-registry');
    await expect(page.getByRole('table', { name: caption })).toBeVisible();

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await expectAccessible(page, 'model registry at 320 CSS pixels');
  });
});
