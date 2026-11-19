import { expect, expectAccessible, signInToConsole, test } from './support';

/**
 * The document copilot and agent workflow views against the local gateway.
 * Both are built: each view runs a real (simulated-model) request and shows
 * what the gateway returned, never sample data.
 */
test.describe('Test Console document copilot and agent workflows', () => {
  test('the document copilot drafts an owner-only document and previews it', async ({ page }) => {
    await signInToConsole(page, 'AiVa Tester (demo)');
    await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Document copilot' }).click();

    await expect(page.getByRole('heading', { level: 1, name: 'Document copilot' })).toBeVisible();
    // The list is served by the gateway: empty on a fresh database, never the not-implemented state.
    await expect(
      page.getByText('No artifacts yet').or(page.getByRole('table', { name: 'Your artifacts, newest first' })),
    ).toBeVisible();
    await expect(page.getByText('The artifact list is not available yet')).toHaveCount(0);
    // Scenario 10's synthetic sample from the catalog fills the whole request.
    await expect(page.getByRole('textbox', { name: 'Document title' })).toHaveValue(
      'Synthetic Q3 network capex board summary',
    );
    await expect(page.getByText('Synthetic sample from the scenario catalog')).toHaveCount(2);
    const [response] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/v1/artifacts') && r.request().method() === 'POST'),
      page.getByRole('button', { name: 'Draft document' }).click(),
    ]);
    expect(response.status()).toBe(200);
    // The gateway built the file and checked it; with simulated GCT-Qwen the draft fails honestly.
    await expect(page.getByRole('heading', { level: 2, name: 'Document', exact: true })).toBeVisible();
    await expect(page.getByRole('table', { name: 'Document output checks' })).toBeVisible();
    await page.getByRole('button', { name: 'Show preview' }).click();
    await expect(page.getByText('The preview could not be rendered')).toHaveCount(0);
    await expect(page.getByRole('table', { name: 'Your artifacts, newest first' })).toBeVisible();
    await expectAccessible(page, 'document copilot');
  });

  test('the incident agent runs a governed workflow and lists it', async ({ page }) => {
    await signInToConsole(page, 'AiVa Tester (demo)');
    await page.goto('/console/agents');

    await expect(page.getByRole('heading', { level: 1, name: 'Agent workflows' })).toBeVisible();
    // The list is served by the gateway: empty on a fresh database, never the not-implemented state.
    await expect(
      page.getByText('No workflows yet').or(page.getByRole('table', { name: 'Agent workflows, newest first' })),
    ).toBeVisible();
    await expect(page.getByText('The workflow list is not available yet')).toHaveCount(0);
    await expect(page.getByRole('textbox', { name: 'Incident ID' })).toHaveValue('INC-7781');
    const [response] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/v1/agents/workflows') && r.request().method() === 'POST'),
      page.getByRole('button', { name: 'Run workflow' }).click(),
    ]);
    expect(response.status()).toBe(200);
    // The agent drafts the notice and stops: the external send waits for a human approver.
    await expect(page.getByRole('heading', { level: 2, name: 'Workflow', exact: true })).toBeVisible();
    await expect(page.getByRole('region', { name: 'Workflow budget' })).toBeVisible();
    await expect(page.getByRole('heading', { level: 3, name: 'Steps' })).toBeVisible();
    await expect(page.getByRole('heading', { level: 4, name: '9. notification.send' })).toBeVisible();
    await expect(page.getByText('Approval required').first()).toBeVisible();
    await expect(page.getByRole('table', { name: 'Agent workflows, newest first' })).toBeVisible();
    await expectAccessible(page, 'agent workflows');
  });
});
