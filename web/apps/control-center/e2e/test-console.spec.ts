import type { Page, Response } from '@playwright/test';
import { expect, expectAccessible, GATEWAY_URL, signIn, signInToConsole, test } from './support';

const PROVENANCE_LABELS: Record<string, string> = {
  measured: 'Measured',
  estimated: 'Estimated',
  simulated: 'Simulated',
  replayed: 'Replayed',
  synthetic: 'Synthetic',
};

function provenanceLabel(provenance: string): string {
  const label = PROVENANCE_LABELS[provenance];
  if (!label) throw new Error(`Unknown provenance from the gateway: ${provenance}`);
  return label;
}

interface ChatBody {
  run_id: string;
  trace_id: string;
  status: string;
  execution_path: string;
  cache: { hit: boolean; upstream_call: boolean; cache_type: string | null };
  response: { provenance: string } | null;
  usage: { provenance: string; upstream_submitted_input_tokens: number };
  cost: { provenance: string; baseline_micros: number };
  policy: { action: string; policy_version: string };
  trace: Array<{ name: string; status: string }>;
}

/**
 * Runs the form and returns the gateway's own response, to compare the UI against. By default the
 * prompt gets a unique suffix so the gateway's response cache cannot answer it (runs repeat across
 * suites); pass `fresh: false` to submit the prompt exactly as it is.
 */
async function run(page: Page, { fresh = true }: { fresh?: boolean } = {}): Promise<ChatBody> {
  if (fresh) {
    const prompt = page.getByRole('textbox', { name: 'Synthetic prompt' });
    await prompt.fill(`${await prompt.inputValue()} [e2e ${crypto.randomUUID()}]`);
  }
  const [response] = await Promise.all([
    page.waitForResponse((r: Response) => r.url().endsWith('/v1/ai/chat') && r.request().method() === 'POST'),
    page.getByRole('button', { name: 'Run through AiVa' }).click(),
  ]);
  const body = (await response.json()) as ChatBody;
  await expect(page.getByRole('heading', { level: 2, name: 'Run result' })).toBeVisible();
  return body;
}

test.describe('AiVa Test Console against the local gateway', () => {
  test('the sign-in page is accessible', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('radio', { name: /AiVa Tester/ })).toBeVisible();
    await expect(page.getByText('Public demo: synthetic data. Do not enter real or personal information.')).toBeVisible();
    // Simple: the value proposition and the sign-in, with no fictional tenant's name.
    await expect(page.getByRole('heading', { name: 'What AiVa delivers' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'See what AiVa did this week' })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Explore AiVa in three steps' })).toHaveCount(0);
    await expect(page.getByText(/GCT|GulfConnect/)).toHaveCount(0);
    await expectAccessible(page, 'sign-in');
  });

  test('a signed-in tester continues from the landing page to their start page', async ({ page }) => {
    await signInToConsole(page, 'AiVa Tester (demo)');
    await page.goto('/');
    await expect(page.getByText('Signed in as AiVa Tester (demo).')).toBeVisible();
    await expectAccessible(page, 'landing page, signed in');
    await page.getByRole('link', { name: 'Continue to AiVa Control Center' }).click();
    await expect(page).toHaveURL(/\/console$/);
  });

  test('a tester signs in and lands on the gateway’s scenario catalog', async ({ page }) => {
    await signIn(page, 'AiVa Tester (demo)');
    await expect(page).toHaveURL(/\/console$/);
    const rows = page.getByRole('table', { name: 'Ten locked synthetic GCT scenarios' }).getByRole('row');
    await expect(rows).toHaveCount(11);
    await expect(page.getByRole('row', { name: /Arabic–English Customer Support/ })).toContainText('Deferred');
    const nav = page.getByRole('navigation', { name: 'Primary' });
    await expect(nav.getByRole('link', { name: 'Run workspace' })).toBeVisible();
    await expect(nav.getByRole('link', { name: 'Policy Rules' })).toHaveCount(0);
    await expectAccessible(page, 'scenario library');
  });

  test('a confidential scenario takes the sovereign direct route with its full trace', async ({ page }) => {
    await signInToConsole(page, 'AiVa Tester (demo)');
    await page.getByRole('link', { name: 'Open Telecom Fraud Management in the run workspace' }).click();
    await expect(page.getByRole('combobox', { name: 'Scenario' })).toHaveValue('fraud-management');
    await expectAccessible(page, 'run workspace (form)');

    const body = await run(page);

    expect(body.execution_path).toBe('sovereign-direct');
    const result = page.getByRole('region', { name: 'Run result' });
    await expect(result.getByText('Allowed by policy', { exact: true })).toBeVisible();
    await expect(result.getByText('Sovereign direct route required')).toBeVisible();
    await expect(result.getByText('CIM bypassed by mandatory sovereignty policy')).toBeVisible();
    await expect(result.getByText(`Allowed by policy · policy version ${body.policy.policy_version}`)).toBeVisible();
    await expect(result.getByText(body.trace_id)).toBeVisible();

    // Provenance is exactly what the gateway reported, never upgraded to "Measured".
    const tokens = result.getByRole('group', { name: 'Upstream input tokens' });
    await expect(tokens).toContainText(String(body.usage.upstream_submitted_input_tokens));
    await expect(tokens).toContainText(provenanceLabel(body.usage.provenance));
    const cost = result.getByRole('group', { name: 'Provider API cost (this route)' });
    await expect(cost).toContainText(provenanceLabel(body.cost.provenance));

    // The 13-stage execution graph comes from the trace in the response.
    const stages = page.getByRole('region', { name: 'Pipeline stages' }).getByRole('listitem').filter({
      has: page.locator('p.font-semibold'),
    });
    await expect(stages).toHaveCount(13);
    for (const span of body.trace.filter((s) => s.status === 'skipped')) {
      expect(['retrieval', 'cim', 'context_guard']).toContain(span.name);
    }
    await expect(stages.nth(5)).toContainText('Skipped');
    await expect(stages.nth(5)).toContainText('CIM bypassed by mandatory sovereignty policy');
    await expectAccessible(page, 'run workspace (result)');

    for (const tab of ['Prompt & Context Inspector', 'Routing Decision', 'Cache & Tokens', 'Savings Analysis']) {
      await page.getByRole('tab', { name: tab }).click();
      await expect(page.getByRole('tabpanel', { name: tab })).toBeVisible();
      await expectAccessible(page, `run tab ${tab}`);
    }
    const routing = page.getByRole('tab', { name: 'Routing Decision' });
    await routing.click();
    await expect(page.getByRole('tabpanel', { name: 'Routing Decision' })).toContainText('GCT-CONF-001');

    // The same run, read back through GET /v1/runs/{id} and /trace.
    await page.getByRole('link', { name: 'Open in trace explorer' }).click();
    await expect(page).toHaveURL(new RegExp(`/admin/audit\\?run=${body.run_id}$`));
    const record = page.getByRole('region', { name: 'Run record' });
    await expect(record).toContainText(body.run_id);
    await expect(record).toContainText('Sovereign direct (CIM bypassed)');
    await expect(page.getByRole('region', { name: 'Pipeline stages' })).toBeVisible();
    await expectAccessible(page, 'trace explorer');
  });

  test('a source outside every trusted network is blocked with rule ID and remediation', async ({ page }) => {
    await signInToConsole(page, 'AiVa Tester (demo)');
    await page.goto('/console/run?scenario=customer-support');
    await page.getByRole('textbox', { name: 'Simulated source IP' }).fill('198.51.100.7');

    const body = await run(page);

    expect(body.status).toBe('blocked');
    const result = page.getByRole('region', { name: 'Run result' });
    await expect(result.getByText('Blocked by AI policy', { exact: true })).toBeVisible();
    await expect(result.getByText('AIPEL-NET-001')).toBeVisible();
    await expect(result.getByText(/^What to do:/)).toBeVisible();
    await expect(result.getByRole('group', { name: 'Upstream input tokens' })).toContainText('No upstream call');
    await expect(page.getByText('The run stopped before an execution path was chosen.')).toBeVisible();
    await expectAccessible(page, 'run workspace (blocked)');
  });

  test('scenario 5 sends its synthetic alarm records from the catalog, shown read-only', async ({ page, request }) => {
    // The catalog straight from the gateway, as the tester it signs in as.
    const minted = await request.post(`${GATEWAY_URL}/v1/auth/dev/token`, { data: { user_id: 'u-tester' } });
    const { access_token: token } = (await minted.json()) as { access_token: string };
    const catalog = await request.get(`${GATEWAY_URL}/v1/catalog/scenarios`, { headers: { Authorization: `Bearer ${token}` } });
    const scenario = ((await catalog.json()) as Array<{ id: string; sample_evidence?: unknown[] | null }>).find(
      (candidate) => candidate.id === 'network-operations',
    );
    const evidence = scenario?.sample_evidence ?? [];
    expect(evidence.length).toBeGreaterThan(0);

    await signInToConsole(page, 'AiVa Tester (demo)');
    await page.goto('/console/run?scenario=network-operations');
    await page.getByText(`Evidence sent with the prompt (${evidence.length})`).click();
    await expect(page.getByText('Synthetic sample from the scenario catalog')).toBeVisible();
    await expectAccessible(page, 'run form with scenario evidence');
    await page.setViewportSize({ width: 320, height: 800 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, 'horizontal overflow at 320 CSS pixels').toBe(0);

    const [sent] = await Promise.all([
      page.waitForRequest((r) => r.url().endsWith('/v1/ai/chat') && r.method() === 'POST'),
      run(page, { fresh: false }),
    ]);
    expect((sent.postDataJSON() as { evidence?: unknown }).evidence).toEqual(evidence);
  });

  test('a baseline comparison yields an estimated saving labelled with its provenance', async ({ page }) => {
    await signInToConsole(page, 'AiVa Tester (demo)');
    await page.goto('/console/run?scenario=customer-support');
    await page.getByText('Advanced options').click();
    await page.getByRole('checkbox', { name: 'Compare with the pinned frontier baseline' }).check();

    const body = await run(page);

    await page.getByRole('tab', { name: 'Savings Analysis' }).click();
    const panel = page.getByRole('tabpanel', { name: 'Savings Analysis' });
    if (body.cost.baseline_micros > 0) {
      const savings = panel.getByRole('group', { name: 'Estimated savings versus pinned baseline' });
      await expect(savings).toBeVisible();
      const label = body.cost.provenance === 'measured' ? 'Estimated' : provenanceLabel(body.cost.provenance);
      await expect(savings).toContainText(label);
    } else {
      await expect(panel).toContainText('No savings figure is shown');
    }
  });

  test('a repeated request is answered from the response cache without an upstream call', async ({ page }) => {
    await signInToConsole(page, 'AiVa Tester (demo)');
    await page.goto('/console/run?scenario=repeated-queries');
    const first = await run(page);
    expect(first.cache.hit).toBe(false);

    const second = await run(page, { fresh: false });

    expect(second.cache.hit, 'the gateway serves an identical repeated request from its response cache').toBe(true);
    expect(second.cache.upstream_call).toBe(false);
    const result = page.getByRole('region', { name: 'Run result' });
    await expect(result.getByText(/^Full response-cache hit \(.+\) · No upstream inference call$/)).toBeVisible();
    await expect(result.getByRole('group', { name: 'Upstream input tokens' })).toContainText('No upstream call');
    // The cached answer keeps the provenance of the run that produced it.
    const answer = page.getByRole('region', { name: 'Actual response' });
    await expect(answer).toContainText(provenanceLabel(second.response?.provenance ?? 'missing'));
    await page.getByRole('tab', { name: 'Prompt & Context Inspector' }).click();
    await expect(page.getByText('No upstream inference call: nothing was submitted to a model.')).toBeVisible();
    await expectAccessible(page, 'run workspace (cache hit)');
  });

  test('sign-in works with the keyboard alone', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('radio', { name: /AiVa Tester/ })).toBeChecked();
    await page.getByRole('radio', { name: /AiVa Tester/ }).focus();
    await page.keyboard.press('Tab');
    await expect(page.getByRole('button', { name: 'Sign in as demo user' })).toBeFocused();
    await page.keyboard.press('Enter');
    // The tester lands on the Test Console; the sign-in button disappears with the landing page and focus moves
    // to the main content.
    await expect(page).toHaveURL(/\/console$/);
    await expect(page.locator('#main')).toBeFocused();
    const catalog = page.getByRole('region', { name: 'Ten locked synthetic GCT scenarios' });
    await expect(catalog).toBeVisible(); // the catalog loads after the page renders

    // On a fresh page load the skip link stays the first stop.
    await page.reload();
    await expect(page.getByRole('heading', { level: 1, name: 'Scenario library' })).toBeVisible();
    await page.keyboard.press('Tab');
    await expect(page.getByRole('link', { name: 'Skip to main content' })).toBeFocused();
  });
});

test.describe('responsive layout', () => {
  test.use({ viewport: { width: 320, height: 800 } });

  test('the landing page reflows at 320 CSS pixels', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'What AiVa delivers' })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await expectAccessible(page, 'landing page at 320px');
  });

  test('the run workspace reflows at 320 CSS pixels without page-level horizontal scrolling', async ({ page }) => {
    await signInToConsole(page, 'AiVa Tester (demo)');
    await page.goto('/console/run?scenario=fraud-management');
    await run(page);

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await expectAccessible(page, 'run workspace at 320px');
  });

  test('an answer with long unbroken lines (code) reflows at 320 CSS pixels', async ({ page }) => {
    // Scenario 2's recorded answer is code; its identifiers are longer than a 320 px line.
    const code = '```python\ndef flag_large_roaming_sessions(cdr_records):\n    return [r for r in cdr_records if r["roaming_usage_bytes"] > 2_000_000_000]\n```';
    await page.route('**/v1/ai/chat', async (route) => {
      const response = await route.fetch();
      const body = (await response.json()) as { response: { content: string } | null };
      if (body.response) body.response.content = code;
      await route.fulfill({ response, json: body });
    });
    await signInToConsole(page, 'AiVa Tester (demo)');
    await page.goto('/console/run?scenario=coding-assistant');
    await run(page);

    await expect(page.getByText('def flag_large_roaming_sessions(cdr_records):', { exact: false })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });
});

test.describe('AiVa Admin Dashboard', () => {
  test('an auditor sees policy versions but not the run workspace', async ({ page }) => {
    await signIn(page, 'Security Auditor (demo)');
    const nav = page.getByRole('navigation', { name: 'Primary' });
    await expect(nav.getByRole('link', { name: 'Run workspace' })).toHaveCount(0);

    await nav.getByRole('link', { name: 'Policy Rules' }).click();
    const table = page.getByRole('table', { name: 'AI-PEL policy versions, newest first' });
    await expect(table.getByRole('row')).not.toHaveCount(1);
    await expect(table).toContainText('Published');
    await expectAccessible(page, 'policy versions');

    await page.goto('/console/run');
    await expect(page.getByText('You don’t have access to this view')).toBeVisible();
    await expectAccessible(page, 'no-access view');
  });

  test('a finance analyst sees live budgets and FinOps alerts', async ({ page }) => {
    await signIn(page, 'Finance Analyst (demo)');
    await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Budgets & Quotas' }).click();

    const budgets = page.getByRole('table', { name: 'Budgets with live status' });
    await expect(budgets.getByRole('row').filter({ hasText: 'Tenant' })).toContainText('$2,000.00');
    await expect(page.getByRole('region', { name: 'FinOps alerts', exact: true })).toBeVisible();
    await expectAccessible(page, 'budgets and quotas');
  });

  test('the trace explorer reports runs a caller cannot read as not found', async ({ page }) => {
    await signIn(page, 'Finance Analyst (demo)');
    await page.goto('/admin/audit?run=run_does_not_exist');
    await expect(page.getByText('You don’t have access to this view')).toBeVisible();

    await page.getByRole('button', { name: 'Sign out' }).click();
    await signIn(page, 'Security Auditor (demo)');
    await page.goto('/admin/audit?run=run_does_not_exist');
    await expect(page.getByRole('alert')).toContainText('Run not found.');
    await expectAccessible(page, 'trace explorer (not found)');
  });
});
