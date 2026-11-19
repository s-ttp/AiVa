import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import highImpactDraft from '../test/fixtures/govern/policyVersionDetail.json';
import draft from '../test/fixtures/govern/policyDraft.json';
import draftInvalid from '../test/fixtures/govern/policyDraftInvalid.json';
import publishResult from '../test/fixtures/govern/policyPublish.json';
import rollbackResult from '../test/fixtures/govern/policyRollback.json';
import separationOfDuties from '../test/fixtures/govern/policySeparationOfDuties.json';
import simulation from '../test/fixtures/govern/policySimulation.json';
import versions from '../test/fixtures/govern/policyVersions.json';
import scenarios from '../test/fixtures/scenarios.json';
import { json, signedInAs, type Route } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';
import { required } from '../test/required';

const published = { ...draft, version: 2, status: 'published', changes_from_published: [], published_version: 2 };
const archived = { ...draft, status: 'archived' };

function routes(detail: unknown, extra: Record<string, Route | unknown> = {}) {
  return {
    'GET /v1/admin/policies': versions,
    'GET /v1/admin/policies/*': detail,
    'GET /v1/catalog/scenarios': scenarios,
    ...extra,
  };
}

/** A card by its heading, once present. */
async function card(name: string | RegExp) {
  return within(required((await screen.findByRole('heading', { level: 2, name })).closest('section')));
}

const posts = (gateway: { requests: Request[] }, path: string) =>
  gateway.requests.filter((r) => r.method === 'POST' && new URL(r.url).pathname === path);

describe('policy version review', () => {
  it('opens a version from the list', async () => {
    signedInAs('u-auditor', routes(highImpactDraft));
    const { router } = renderRoute('/admin/govern/policy-rules');

    await userEvent.click(await screen.findByRole('link', { name: 'Review version 4' }));
    expect(router.state.location.search).toBe('?version=4');
    expect(await screen.findByRole('heading', { level: 2, name: 'Version 4' })).toBeVisible();
  });

  it('shows changes from the published version with high-impact changes marked', async () => {
    signedInAs('u-auditor', routes(highImpactDraft));
    renderRoute('/admin/govern/policy-rules?version=4');

    const review = await card('Version 4');
    const changes = await review.findByRole('table', { name: 'Changes in version 4 compared with the published version' });
    const row = within(changes).getByRole('row', { name: /GCT-SEC-002/ });
    expect(row).toHaveTextContent('Removed');
    expect(row).toHaveTextContent('High impact');
    expect(row).toHaveTextContent('Policy removed: its restrictions no longer apply.');
    expect(review.getByRole('heading', { name: 'Changes from published version 2' })).toBeVisible();
  });

  it('describes each policy’s conditions and restrictions in plain language, highest priority first', async () => {
    signedInAs('u-auditor', routes(highImpactDraft));
    renderRoute('/admin/govern/policy-rules?version=4');

    const review = await card('Version 4');
    const table = await review.findByRole('table', { name: 'Policies in version 4' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows[0]).toHaveTextContent('GCT-CONF-001');
    expect(rows[0]).toHaveTextContent('Classification is Confidential, Restricted, Sovereign-only');
    expect(rows[0]).toHaveTextContent('Mandatory path: Sovereign direct (CIM bypassed)');
    expect(rows[0]).toHaveTextContent('Models limited to gct-qwen-finetuned');
    expect(rows[0]).toHaveTextContent('External providers denied');
  });

  it('keeps simulation, publishing and drafting read-only for roles without those permissions', async () => {
    signedInAs('u-auditor', routes(highImpactDraft));
    renderRoute('/admin/govern/policy-rules?version=4');

    await card('Version 4');
    expect(screen.getByText(/Your roles cannot simulate policy versions/)).toBeVisible();
    expect(screen.getByText(/Your roles cannot publish policy versions/)).toBeVisible();
    expect(screen.getByText(/Your roles cannot create policy drafts/)).toBeVisible();
    expect(screen.queryByRole('button', { name: /Publish version/ })).not.toBeInTheDocument();
  });

  it('reports a version that cannot be loaded', async () => {
    signedInAs('u-auditor', routes({}, { 'GET /v1/admin/policies/*': json({ error: { code: 'NOT_FOUND', message: 'Policy version 9 not found.', rule_ids: [], retryable: false } }, 404) }));
    renderRoute('/admin/govern/policy-rules?version=9');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Policy version 9 could not be loaded');
    expect(alert).toHaveTextContent('Policy version 9 not found.');
  });
});

describe('policy simulation', () => {
  it('simulates a scenario against the draft and the published version and compares the decisions', async () => {
    const gateway = signedInAs(
      'u-platform-admin',
      routes(draft, {
        'POST /v1/admin/policies/test': async (request: Request) => {
          const body = (await request.json()) as { version: number };
          if (body.version === 3) return json(simulation);
          const results = simulation.results.map((result) => ({
            ...result,
            decision: { ...result.decision, action: 'allow', policy_version: '2', matched_rule_ids: [] },
          }));
          return json({ ...simulation, version: 2, status: 'published', results });
        },
      }),
    );
    renderRoute('/admin/govern/policy-rules?version=3');

    const simulator = await card('Simulate version 3');
    await userEvent.selectOptions(await simulator.findByRole('combobox', { name: 'Scenario' }), 'customer-support');
    await userEvent.type(simulator.getByRole('textbox', { name: 'Additional routing tags' }), 'needs-approval');
    await userEvent.selectOptions(simulator.getByRole('combobox', { name: 'Expected decision' }), 'approval_required');
    await userEvent.click(simulator.getByRole('button', { name: 'Simulate scenario' }));

    const heading = await simulator.findByRole('heading', { name: 'Simulation results for version 3' });
    expect(heading).toHaveFocus();
    const table = simulator.getByRole('table', { name: 'Simulated decisions for version 3' });
    const row = within(table).getByRole('row', { name: /Tagged support request/ });
    expect(row).toHaveTextContent('Approval required');
    expect(row).toHaveTextContent('GCT-E2E-APPROVAL');
    expect(row).toHaveTextContent('Allowed by policy');
    expect(row).toHaveTextContent('Changed');
    expect(row).toHaveTextContent('Passed');
    expect(simulator.getByText(/2 passed, 0 failed/)).toBeVisible();

    const bodies = await Promise.all(posts(gateway, '/v1/admin/policies/test').map((r) => r.json()));
    expect(bodies.map((body) => body.version).sort()).toEqual([2, 3]);
    expect(bodies[0].cases).toEqual([
      {
        name: '1. Customer Support Chatbot',
        expected_action: 'approval_required',
        sample: expect.objectContaining({
          application_id: 'gct-support-chatbot',
          identity_mode: 'simulated',
          source_mode: 'simulated',
          source_ip: '192.0.2.10',
          scenario_id: 'customer-support',
          stage: 'request',
          tags: expect.arrayContaining(['needs-approval']),
        }),
      },
    ]);
  });

  it('simulates every catalog scenario without expectations', async () => {
    const gateway = signedInAs('u-platform-admin', routes(published, { 'POST /v1/admin/policies/test': simulation }));
    renderRoute('/admin/govern/policy-rules?version=2');

    const simulator = await card('Simulate version 2');
    await userEvent.click(await simulator.findByRole('button', { name: 'Simulate all 10 scenarios' }));

    await simulator.findByRole('heading', { name: /Simulation results/ });
    const requests = posts(gateway, '/v1/admin/policies/test');
    expect(requests).toHaveLength(1); // the published version has nothing to compare with
    const body = await required(requests[0]).json();
    expect(body.cases).toHaveLength(10);
    expect(body.cases.every((c: { expected_action: unknown }) => c.expected_action === null)).toBe(true);
  });

  it('shows a failed simulation explicitly', async () => {
    signedInAs(
      'u-platform-admin',
      routes(published, {
        'POST /v1/admin/policies/test': json(
          { error: { code: 'POLICY_ENGINE_UNAVAILABLE', message: 'The policy engine is unavailable.', rule_ids: [], retryable: true } },
          503,
        ),
      }),
    );
    renderRoute('/admin/govern/policy-rules?version=2');

    const simulator = await card('Simulate version 2');
    await userEvent.click(await simulator.findByRole('button', { name: 'Simulate scenario' }));
    const alert = await simulator.findByRole('alert');
    expect(alert).toHaveTextContent('The simulation did not run');
    expect(alert).toHaveTextContent('The policy engine is unavailable.');
  });
});

describe('publishing and rollback', () => {
  it('publishes a draft with an approval note and shows the enforced bundle', async () => {
    const gateway = signedInAs('u-platform-admin', routes(draft, { 'POST /v1/admin/policies/publish': publishResult }));
    renderRoute('/admin/govern/policy-rules?version=3');

    const panel = await card('Publish version 3');
    expect(panel.getByText('Publishing replaces version 2 with version 3 for every new request.')).toBeVisible();
    await userEvent.type(panel.getByRole('textbox', { name: 'Approval note' }), 'Adds an approval step; restricts only.');
    await userEvent.click(panel.getByRole('button', { name: 'Publish version 3' }));

    const outcome = await screen.findByRole('heading', { level: 2, name: 'Version 3 published' });
    expect(outcome).toHaveFocus();
    const section = within(required(outcome.closest('section')));
    expect(section.getByText('Version 3 is now enforced. Version 2 is archived.')).toBeVisible();
    expect(section.getByText(publishResult.bundle_hash)).toBeVisible();
    expect(section.getByRole('table', { name: 'Changes from version 2' })).toHaveTextContent('GCT-E2E-APPROVAL');
    expect(await required(posts(gateway, '/v1/admin/policies/publish')[0]).json()).toEqual({
      version: 3,
      approval_note: 'Adds an approval step; restricts only.',
    });
    await waitFor(() =>
      expect(gateway.requests.filter((r) => new URL(r.url).pathname === '/v1/admin/policies')).toHaveLength(2),
    );
  });

  it('tells the author of a high-impact draft up front that someone else must publish it', async () => {
    const gateway = signedInAs('u-platform-admin', routes(highImpactDraft));
    renderRoute('/admin/govern/policy-rules?version=4');

    const panel = await card('Publish version 4');
    expect(panel.getByText('Separation of duties: you cannot publish this version')).toBeVisible();
    expect(panel.getByText(/You authored version 4, and it makes high-impact changes/)).toBeVisible();
    expect(panel.getByText('GCT-SEC-002')).toBeVisible();
    expect(panel.queryByRole('button', { name: 'Publish version 4' })).not.toBeInTheDocument();
    expect(posts(gateway, '/v1/admin/policies/publish')).toHaveLength(0);
  });

  it('lets another administrator publish a high-impact draft and still shows a refusal', async () => {
    signedInAs(
      'u-tenant-admin',
      routes(
        { ...highImpactDraft, authored_by_viewer: false },
        { 'POST /v1/admin/policies/publish': json(separationOfDuties, 403) },
      ),
    );
    renderRoute('/admin/govern/policy-rules?version=4');

    const panel = await card('Publish version 4');
    expect(panel.getByText('High-impact changes', { exact: true })).toBeVisible();
    expect(panel.getByText(/You did not author this version, so you may publish it/)).toBeVisible();
    await userEvent.type(panel.getByRole('textbox', { name: 'Approval note' }), 'Drop the secrets rule.');
    await userEvent.click(panel.getByRole('button', { name: 'Publish version 4' }));

    // The gateway decides; if it refuses after all, the refusal is shown as it states it.
    const alert = await panel.findByRole('alert');
    expect(alert).toHaveTextContent('Separation of duties: publish refused');
    expect(alert).toHaveTextContent('GCT-SEC-002');
    expect(alert).toHaveTextContent('Ask another administrator to review the changes and publish this version.');
  });

  it('rolls back to an archived version', async () => {
    const gateway = signedInAs(
      'u-platform-admin',
      routes(archived, { 'POST /v1/admin/policies/publish': { ...rollbackResult, version: 3, previous_version: 2 } }),
    );
    renderRoute('/admin/govern/policy-rules?version=3');

    const panel = await card('Roll back to version 3');
    expect(panel.getByText(/re-publishes the archived bundle of version 3 unchanged/)).toBeVisible();
    await userEvent.type(panel.getByRole('textbox', { name: 'Approval note' }), 'Restore the approval rule.');
    await userEvent.click(panel.getByRole('button', { name: 'Roll back to version 3' }));

    expect(await screen.findByRole('heading', { level: 2, name: 'Rolled back to version 3' })).toBeVisible();
    expect(screen.getByText('Version 3 is enforced again. Version 2 is archived.')).toBeVisible();
    expect(posts(gateway, '/v1/admin/policies/publish')).toHaveLength(1);
  });

  it('does not offer to publish the published version', async () => {
    signedInAs('u-platform-admin', routes(published));
    renderRoute('/admin/govern/policy-rules?version=2');

    await card('Version 2');
    expect(screen.getByText(/Version 2 is published and enforced/)).toBeVisible();
    expect(screen.getByText(/This is the published version/)).toBeVisible();
    expect(screen.queryByRole('textbox', { name: 'Approval note' })).not.toBeInTheDocument();
  });
});

describe('drafting', () => {
  it('saves an edited copy as a new draft and opens it', async () => {
    const created = { ...draft, version: 5, notes: 'Tighten audit' };
    const gateway = signedInAs('u-platform-admin', routes(published, { 'POST /v1/admin/policies': json(created, 201) }));
    const { router } = renderRoute('/admin/govern/policy-rules?version=2');

    const editor = await card('New draft');
    const policies = editor.getByRole('textbox', { name: 'Policies (JSON)' });
    expect(JSON.parse((policies as HTMLTextAreaElement).value)).toEqual(published.policies);
    await userEvent.type(editor.getByRole('textbox', { name: 'Notes' }), 'Tighten audit');
    await userEvent.click(editor.getByRole('button', { name: 'Validate and save draft' }));

    await waitFor(() => expect(router.state.location.search).toBe('?version=5'));
    expect(await required(posts(gateway, '/v1/admin/policies')[0]).json()).toEqual({
      policies: published.policies,
      notes: 'Tighten audit',
    });
  });

  it('rejects text that is not a JSON array before calling the gateway', async () => {
    const gateway = signedInAs('u-platform-admin', routes(published));
    renderRoute('/admin/govern/policy-rules?version=2');

    const editor = await card('New draft');
    const policies = editor.getByRole('textbox', { name: 'Policies (JSON)' });
    await userEvent.clear(policies);
    await userEvent.type(policies, '{{"policy_id": "x"}');
    await userEvent.click(editor.getByRole('button', { name: 'Validate and save draft' }));

    const alert = await editor.findByRole('alert');
    expect(alert).toHaveTextContent('The policies could not be read');
    expect(alert).toHaveTextContent('The policies must be a JSON array of policy documents.');
    expect(posts(gateway, '/v1/admin/policies')).toHaveLength(0);
  });

  it('lists the gateway’s validation errors', async () => {
    signedInAs('u-platform-admin', routes(published, { 'POST /v1/admin/policies': json(draftInvalid, 422) }));
    renderRoute('/admin/govern/policy-rules?version=2');

    const editor = await card('New draft');
    await userEvent.click(editor.getByRole('button', { name: 'Validate and save draft' }));

    const alert = await editor.findByRole('alert');
    expect(alert).toHaveTextContent('The draft has invalid references.');
    expect(within(alert).getByRole('listitem')).toHaveTextContent('policy GCT-BAD: unknown model no-such-model');
  });
});
