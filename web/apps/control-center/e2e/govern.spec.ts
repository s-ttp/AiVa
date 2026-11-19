import type { APIRequestContext, Browser, Page, Response } from '@playwright/test';
import { expect, expectAccessible, GATEWAY_URL, signIn, test } from './support';

/**
 * AiVa Govern against the local gateway: the policy lifecycle (draft,
 * simulate, publish, separation of duties, rollback) and the Approval Queue
 * (a tester's request pauses for approval, an approver decides it, the tester
 * resumes). The approval rule matches a tag unique to this run, so other
 * suites and earlier runs are unaffected; the suite rolls back what it published.
 */

const TAG = `e2e-approval-${crypto.randomUUID().slice(0, 8)}`;
const RULE_ID = 'GCT-E2E-APPROVAL';

interface PolicyVersion {
  version: number;
  status: 'draft' | 'published' | 'archived';
  bundle_hash: string;
}

/** Shared across the serial tests below. */
const state: { original?: PolicyVersion; draft?: number } = {};

async function apiToken(request: APIRequestContext, userId: string): Promise<string> {
  const response = await request.post(`${GATEWAY_URL}/v1/auth/dev/token`, { data: { user_id: userId } });
  expect(response.ok()).toBe(true);
  return ((await response.json()) as { access_token: string }).access_token;
}

async function publishedVersion(request: APIRequestContext, token: string): Promise<PolicyVersion> {
  const response = await request.get(`${GATEWAY_URL}/v1/admin/policies`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const versions = (await response.json()) as PolicyVersion[];
  const published = versions.find((version) => version.status === 'published');
  if (!published) throw new Error('The gateway has no published policy version; run `make seed` first.');
  return published;
}

/** A page in its own browser context: its own session, like a second person at another desk. */
async function personPage(browser: Browser, displayName: string): Promise<Page> {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  await page.route('**/config.js', (route) =>
    route.fulfill({
      contentType: 'text/javascript',
      body: `window.__AIVA_CONFIG__ = Object.freeze(${JSON.stringify({ gatewayUrl: GATEWAY_URL })});`,
    }),
  );
  await signIn(page, displayName);
  return page;
}

const card = (page: Page, name: string | RegExp) =>
  page.locator('section').filter({ has: page.getByRole('heading', { level: 2, name, exact: typeof name === 'string' }) });

/** Runs the customer-support scenario with this suite's tag and a unique prompt (no cache hits). */
async function runTagged(page: Page) {
  await page.goto('/console/run?scenario=customer-support');
  const prompt = page.getByRole('textbox', { name: 'Synthetic prompt' });
  await expect(prompt).not.toHaveValue('');
  await prompt.fill(`${await prompt.inputValue()} [e2e ${crypto.randomUUID()}]`);
  const tags = page.getByRole('textbox', { name: 'Routing tags' });
  await tags.fill([await tags.inputValue(), TAG].filter(Boolean).join(', '));
  const [response] = await Promise.all([
    page.waitForResponse((r: Response) => r.url().endsWith('/v1/ai/chat') && r.request().method() === 'POST'),
    page.getByRole('button', { name: 'Run through AiVa' }).click(),
  ]);
  const body = (await response.json()) as { status: string; approval: { approval_id: string | null } | null };
  expect(body.status).toBe('approval_required');
  const approvalId = body.approval?.approval_id;
  if (!approvalId) throw new Error('The gateway returned no approval ID');
  await expect(page.getByRole('heading', { name: 'Approval request' })).toBeVisible();
  return approvalId;
}

async function decide(page: Page, approvalId: string, verdict: 'Approve' | 'Deny', note: string) {
  await page.goto(`/admin/govern/approval-queue?approval=${approvalId}`);
  const detail = card(page, 'Approval request');
  await expect(detail.getByRole('heading', { name: 'Request', exact: true })).toBeVisible();
  await detail.getByRole('textbox', { name: 'Decision note' }).fill(note);
  await detail.getByRole('button', { name: verdict, exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Decision recorded' })).toBeFocused();
}

test.describe.serial('AiVa Govern against the local gateway', () => {
  test.beforeAll(async ({ request }) => {
    state.original = await publishedVersion(request, await apiToken(request, 'u-tenant-admin'));
  });

  test.afterAll(async ({ request }) => {
    // Restore the bundle that was published before the suite, if a failure left ours enforced.
    const original = state.original;
    if (!original) return;
    const token = await apiToken(request, 'u-tenant-admin');
    if ((await publishedVersion(request, token)).version === original.version) return;
    await request.post(`${GATEWAY_URL}/v1/admin/policies/publish`, {
      headers: { Authorization: `Bearer ${token}` },
      data: { version: original.version, approval_note: 'Restore after the Govern end-to-end suite.' },
    });
  });

  test('an administrator drafts an approval rule, simulates it and publishes it', async ({ page }) => {
    const original = state.original;
    if (!original) throw new Error('No published version recorded');
    await signIn(page, 'Tenant Administrator (demo)');
    await page.goto('/admin/govern/policy-rules');
    await page.getByRole('link', { name: `Review version ${original.version}`, exact: true }).click();
    await expect(page.getByText(/This is the published version/)).toBeVisible();
    await expectAccessible(page, 'policy version review');

    // Draft: a copy of the published policies plus an approval rule for this run's tag.
    const editor = card(page, 'New draft');
    const textarea = editor.getByRole('textbox', { name: 'Policies (JSON)' });
    const policies = JSON.parse(await textarea.inputValue()) as unknown[];
    policies.push({
      policy_id: RULE_ID,
      title: 'End-to-end: tagged requests need an approver',
      priority: 500,
      match: { tags_any: [TAG] },
      enforce: {
        action: 'approval_required',
        reason: `Requests tagged ${TAG} need an approver.`,
        remediation: 'Ask an approver to review the request in the AiVa Govern Approval Queue.',
      },
    });
    await textarea.fill(JSON.stringify(policies, null, 2));
    await editor.getByRole('textbox', { name: 'Notes' }).fill(`E2E approval rule for ${TAG}`);
    await editor.getByRole('button', { name: 'Validate and save draft' }).click();
    await page.waitForURL((url) => url.searchParams.get('version') !== String(original.version));
    const draft = Number(new URL(page.url()).searchParams.get('version'));
    state.draft = draft;

    const review = card(page, `Version ${draft}`);
    const added = review
      .getByRole('table', { name: `Changes in version ${draft} compared with the published version` })
      .getByRole('row', { name: new RegExp(RULE_ID) });
    await expect(added).toContainText('Added');
    await expect(added).toContainText('Standard');

    // Simulate: the tagged scenario now needs approval; the published version allowed it.
    const simulator = card(page, `Simulate version ${draft}`);
    await simulator.getByRole('combobox', { name: 'Scenario' }).selectOption('customer-support');
    await simulator.getByRole('textbox', { name: 'Additional routing tags' }).fill(TAG);
    await simulator.getByRole('combobox', { name: 'Expected decision' }).selectOption('approval_required');
    await simulator.getByRole('button', { name: 'Simulate scenario' }).click();
    const results = simulator.getByRole('table', { name: `Simulated decisions for version ${draft}` });
    const row = results.getByRole('row', { name: /Customer Support/ });
    await expect(row).toContainText('Approval required');
    await expect(row).toContainText('Changed');
    await expect(row).toContainText('Passed');
    await expectAccessible(page, 'policy simulation results');

    // Publish with an approval note.
    const publish = card(page, `Publish version ${draft}`);
    await publish.getByRole('textbox', { name: 'Approval note' }).fill('Adds an approval step for the e2e tag; restricts only.');
    await publish.getByRole('button', { name: `Publish version ${draft}` }).click();
    await expect(page.getByRole('heading', { level: 2, name: `Version ${draft} published` })).toBeFocused();
    await expect(page.getByText(`Version ${draft} is now enforced. Version ${original.version} is archived.`)).toBeVisible();
    await expectAccessible(page, 'policy publish outcome');
  });

  test('a tagged request pauses for approval, the application owner approves it and the tester resumes', async ({
    browser,
  }) => {
    const tester = await personPage(browser, 'AiVa Tester (demo)');
    const approvalId = await runTagged(tester);
    await expect(tester.getByText(approvalId, { exact: true })).toBeVisible();
    await expectAccessible(tester, 'run paused for approval');

    const owner = await personPage(browser, 'Support Chatbot Owner (demo)');
    await owner.goto('/admin/govern/approval-queue');
    const queue = owner.getByRole('table', { name: 'Pending approval requests' });
    await expect(queue.getByRole('link', { name: `Review ${approvalId}` })).toBeVisible();
    // The navigation badge counts requests awaiting this approver (exactly this one on a fresh database).
    const badgeLink = owner.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: /^Approval Queue/ });
    await expect(badgeLink).toHaveAccessibleName(/^Approval Queue, \d+ awaiting your decision$/);
    const before = Number(/, (\d+) awaiting/.exec((await badgeLink.getAttribute('aria-label')) ?? '')?.[1]);
    await expectAccessible(owner, 'approval queue');
    await decide(owner, approvalId, 'Approve', 'Reviewed: synthetic support question.');
    await expect(badgeLink).toHaveAccessibleName(
      before === 1 ? 'Approval Queue' : `Approval Queue, ${before - 1} awaiting your decision`,
    );
    const outcome = card(owner, 'Approval request');
    await expect(outcome.getByRole('heading', { name: /Re-evaluation against current policy/ })).toBeVisible();
    await expect(outcome.getByText('Approval required').first()).toBeVisible();
    await expectAccessible(owner, 'approval decision outcome');

    await tester.getByRole('button', { name: 'Check approval status' }).click();
    await expect(tester.getByText('Approver’s note: Reviewed: synthetic support question.')).toBeVisible();
    const [response] = await Promise.all([
      tester.waitForResponse((r: Response) => r.url().endsWith('/v1/ai/chat') && r.request().method() === 'POST'),
      tester.getByRole('button', { name: 'Resubmit with approval' }).click(),
    ]);
    const resumed = (await response.json()) as { status: string; approval: { satisfied_by: string | null } };
    expect(resumed.status).toBe('completed');
    expect(resumed.approval.satisfied_by).toBe(approvalId);
    const result = card(tester, 'Run result');
    await expect(result.getByText('Allowed by policy', { exact: true })).toBeVisible();
    await expect(result.getByText(approvalId, { exact: true })).toBeVisible();
  });

  test('a denied request stays blocked', async ({ browser }) => {
    const tester = await personPage(browser, 'AiVa Tester (demo)');
    const approvalId = await runTagged(tester);

    const admin = await personPage(browser, 'Platform Administrator (demo)');
    await decide(admin, approvalId, 'Deny', 'Not needed for this demo.');
    await expect(card(admin, 'Approval request').getByText('The request stays blocked.', { exact: false })).toBeVisible();
    await admin.getByRole('combobox', { name: 'Show requests' }).selectOption('denied');
    await expect(
      admin.getByRole('table', { name: 'Denied approval requests' }).getByRole('link', { name: `Review ${approvalId}` }),
    ).toBeVisible();

    await tester.getByRole('button', { name: 'Check approval status' }).click();
    await expect(tester.getByText('The request was denied. It stays blocked.')).toBeVisible();
    await expect(tester.getByRole('button', { name: 'Resubmit with approval' })).toHaveCount(0);
  });

  test('separation of duties: only another administrator may publish a high-impact draft', async ({ page, browser }) => {
    await signIn(page, 'Tenant Administrator (demo)');
    await page.goto(`/admin/govern/policy-rules?version=${state.draft}`);
    const editor = card(page, 'New draft');
    const textarea = editor.getByRole('textbox', { name: 'Policies (JSON)' });
    const policies = JSON.parse(await textarea.inputValue()) as Array<{ policy_id: string }>;
    // Removing a restriction relaxes policy: a high-impact change.
    await textarea.fill(JSON.stringify(policies.filter((policy) => policy.policy_id !== RULE_ID), null, 2));
    await editor.getByRole('textbox', { name: 'Notes' }).fill('E2E: remove the approval rule (high impact)');
    await editor.getByRole('button', { name: 'Validate and save draft' }).click();
    await page.waitForURL((url) => url.searchParams.get('version') !== String(state.draft));
    const version = Number(new URL(page.url()).searchParams.get('version'));

    // The author is told up front, with no publish action to try.
    const own = card(page, `Publish version ${version}`);
    await expect(own.getByText('Separation of duties: you cannot publish this version')).toBeVisible();
    await expect(own.getByText(RULE_ID, { exact: true })).toBeVisible();
    await expect(own.getByRole('button', { name: `Publish version ${version}` })).toHaveCount(0);
    await expectAccessible(page, 'separation of duties for the author');

    // Another administrator may publish it (not done here: the rollback test restores the original bundle).
    const other = await personPage(browser, 'Platform Administrator (demo)');
    await other.goto(`/admin/govern/policy-rules?version=${version}`);
    const publish = card(other, `Publish version ${version}`);
    await expect(publish.getByText(/You did not author this version, so you may publish it/)).toBeVisible();
    await expect(publish.getByRole('button', { name: `Publish version ${version}` })).toBeEnabled();
  });

  test('an administrator rolls back to the original bundle', async ({ page }) => {
    const original = state.original;
    if (!original) throw new Error('No published version recorded');
    await signIn(page, 'Platform Administrator (demo)');
    await page.goto(`/admin/govern/policy-rules?version=${original.version}`);
    const rollback = card(page, `Roll back to version ${original.version}`);
    await expect(rollback.getByText('High-impact changes', { exact: true })).toBeVisible();
    await rollback.getByRole('textbox', { name: 'Approval note' }).fill('Remove the end-to-end approval rule.');
    await rollback.getByRole('button', { name: `Roll back to version ${original.version}` }).click();

    await expect(page.getByRole('heading', { level: 2, name: `Rolled back to version ${original.version}` })).toBeFocused();
    await expect(page.getByText(original.bundle_hash).first()).toBeVisible();
    const row = page
      .getByRole('table', { name: 'AI-PEL policy versions, newest first' })
      .getByRole('row', { name: new RegExp(`^${original.version} `) });
    await expect(row).toContainText('Published');
  });

  test('the administrative read views are accessible', async ({ page }) => {
    await signIn(page, 'Security Auditor (demo)');
    for (const [path, table] of [
      ['/admin/govern/applications', 'Registered applications and their grants'],
      ['/admin/govern/trusted-networks', 'Trusted networks and the applications that accept them'],
      ['/admin/govern/model-allowlist', 'Models and the applications granted them'],
      ['/admin/govern/tool-permissions', 'Registered tools and the applications granted them'],
    ] as const) {
      await page.goto(path);
      await expect(page.getByRole('table', { name: table })).toBeVisible();
      await expectAccessible(page, path);
    }
  });

  test('the Govern views reflow at 320 CSS pixels', async ({ page }) => {
    await signIn(page, 'Tenant Administrator (demo)');
    await page.setViewportSize({ width: 320, height: 800 });
    const views = [
      [`/admin/govern/policy-rules?version=${state.original?.version ?? 1}`, page.getByRole('heading', { name: 'Policies', exact: true })],
      ['/admin/govern/approval-queue', page.getByRole('combobox', { name: 'Show requests' })],
      ['/admin/govern/applications', page.getByRole('table', { name: 'Registered applications and their grants' })],
      ['/admin/govern/trusted-networks', page.getByRole('heading', { name: 'Client address' })],
      ['/admin/govern/model-allowlist', page.getByRole('heading', { name: 'Built-in invariants' })],
    ] as const;
    for (const [path, ready] of views) {
      await page.goto(path);
      await expect(ready).toBeVisible();
      await expect(page.getByRole('status')).toHaveCount(0);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, `horizontal scroll on ${path}`).toBeLessThanOrEqual(0);
      await expectAccessible(page, `${path} at 320 CSS pixels`);
    }
  });
});

test.describe('application keys', () => {
  test('an administrator issues a key, sees it once, copies it and revokes it, accessibly', async ({ page, context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await signIn(page, 'Platform Administrator (demo)');
    await page.goto('/admin/govern/applications');
    await page.getByRole('button', { name: 'Manage keys for Coding Assistant' }).click();
    const keys = page.getByRole('region', { name: 'Keys for Coding Assistant' });
    await expect(keys.getByRole('heading', { name: 'Keys for Coding Assistant' })).toBeFocused();

    // A run of its own each time: the local database keeps earlier runs' keys.
    const label = `End-to-end key ${Date.now()} (synthetic)`;
    await keys.getByRole('textbox', { name: 'Label' }).fill(label);
    await keys.getByRole('spinbutton', { name: 'Expires in (days)' }).fill('7');
    await keys.getByRole('button', { name: 'Issue key' }).click();

    const dialog = page.getByRole('dialog', { name: 'Key issued for Coding Assistant' });
    await expect(dialog).toBeVisible();
    const field = dialog.getByRole('textbox', { name: `Key “${label}”` });
    // The modal focuses the key, selected for copying.
    await expect(field).toBeFocused();
    const key = await field.inputValue();
    expect(key).toMatch(/^aiva_[0-9a-f]{16}\.\S+$/);
    await expectAccessible(page, 'application keys, a new key shown once');
    await dialog.getByRole('button', { name: 'Copy key' }).click();
    await expect(dialog.getByRole('status')).toHaveText('Copied.');
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(key);

    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
    expect(await page.content()).not.toContain(key);
    expect(page.url()).not.toContain(key);

    const row = keys.getByRole('table', { name: 'Issued keys and their status' }).getByRole('row', { name: label });
    await expect(row).toContainText('Active');
    await row.getByRole('button', { name: `Revoke ${label}` }).click();
    const confirm = keys.getByRole('group', { name: `Revoke ${label}` });
    await expect(confirm.getByRole('button', { name: 'Cancel' })).toBeFocused();
    await confirm.getByRole('button', { name: 'Revoke key' }).click();
    await expect(keys.getByText(`Key “${label}” revoked.`)).toBeVisible();
    await expect(row).toContainText('Revoked');
    // How to call the API with such a key: this gateway's OpenAI-compatible base URL, never the key itself.
    const example = keys.getByRole('region', { name: 'Use the API' }).getByRole('region', { name: 'Python example' });
    await expect(example).toContainText(`base_url="${GATEWAY_URL}/v1"`);
    await expect(example).not.toContainText(key);
    await expectAccessible(page, 'application keys after a revoke');

    await page.setViewportSize({ width: 320, height: 800 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, 'horizontal scroll on the keys at 320 CSS pixels').toBeLessThanOrEqual(0);
    await expectAccessible(page, 'application keys at 320 CSS pixels');
  });

  test('an auditor reads an application’s keys but cannot issue or revoke them', async ({ page }) => {
    await signIn(page, 'Security Auditor (demo)');
    await page.goto('/admin/govern/applications');
    await page.getByRole('button', { name: 'Manage keys for Coding Assistant' }).click();
    const keys = page.getByRole('region', { name: 'Keys for Coding Assistant' });
    await expect(keys.getByText('Read-only: you can see this application’s keys but not issue or revoke them.')).toBeVisible();
    await expect(keys.getByRole('button', { name: 'Issue key' })).toHaveCount(0);
    await expect(keys.getByRole('button', { name: /^Revoke / })).toHaveCount(0);
    await expectAccessible(page, 'application keys, read-only');
  });
});

test.describe('model switches', () => {
  test('an administrator drafts switching the sovereign model off, sees the warning, and opens the draft to test and publish', async ({
    page,
  }) => {
    await signIn(page, 'Platform Administrator (demo)');
    await page.goto('/admin/govern/model-allowlist');
    const models = page.getByRole('table', { name: 'Models and the applications granted them' });
    await models.getByRole('button', { name: 'Draft switching GCT Sovereign Qwen off for gct-fraud-investigator' }).click();

    const draft = page.getByRole('region', { name: /^Draft version \d+: GCT Sovereign Qwen switched off for gct-fraud-investigator$/ });
    await expect(draft.getByRole('heading', { level: 2 })).toBeFocused();
    // The gateway's own warning: confidential requests are blocked, never rerouted.
    await expect(draft.getByRole('region', { name: 'Before you publish' })).toContainText('blocked by AI policy');
    const confidential = draft.getByRole('table', { name: 'What the draft changes, by classification' }).getByRole('row', { name: /^Confidential/ });
    await expect(confidential).toContainText('Blocked by AI policy');
    // Nothing is published: the page still shows the model on for the application.
    const entry = models.getByRole('listitem').filter({ hasText: 'gct-fraud-investigator' }).filter({
      has: page.getByRole('button', { name: 'Draft switching GCT Sovereign Qwen off for gct-fraud-investigator' }),
    });
    await expect(entry).toContainText('On');
    await expectAccessible(page, 'model allowlist, a drafted switch');

    await page.setViewportSize({ width: 320, height: 800 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, 'horizontal scroll on a drafted switch at 320 CSS pixels').toBeLessThanOrEqual(0);
    await expectAccessible(page, 'model allowlist, a drafted switch at 320 CSS pixels');

    const open = draft.getByRole('link', { name: /^Test and publish version \d+$/ });
    const version = (await open.textContent())?.match(/\d+$/)?.[0];
    await open.click();
    await expect(page).toHaveURL(new RegExp(`/admin/govern/policy-rules\\?version=${version}$`));
    await expect(page.getByRole('heading', { level: 2, name: new RegExp(`Version ${version}`) }).first()).toBeVisible();
  });

  test('an auditor sees which applications each model is on for, with no switches', async ({ page }) => {
    await signIn(page, 'Security Auditor (demo)');
    await page.goto('/admin/govern/model-allowlist');
    const models = page.getByRole('table', { name: 'Models and the applications granted them' });
    await expect(models.getByText('On').first()).toBeVisible();
    await expect(models.getByRole('button', { name: /^Draft switching/ })).toHaveCount(0);
  });
});
