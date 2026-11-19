import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import policyVersions from '../test/fixtures/policyVersions.json';
import runDetail from '../test/fixtures/runDetail.json';
import runTrace from '../test/fixtures/runTrace.json';
import { errorEnvelope, signedInAs } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';
import { required } from '../test/required';

describe('AiVa Govern policy versions', () => {
  it('lists policy versions with status, bundle hash and UTC timestamps', async () => {
    signedInAs('u-auditor', { 'GET /v1/admin/policies': policyVersions });
    renderRoute('/admin/govern/policy-rules');

    const table = await screen.findByRole('table', { name: 'AI-PEL policy versions, newest first' });
    const row = within(table).getByRole('row', { name: /^1 / });
    expect(row).toHaveTextContent('Published');
    expect(row).toHaveTextContent(`${required(policyVersions[0]).bundle_hash.slice(0, 12)}…`);
    expect(row).toHaveTextContent('14 Oct 2026, 09:15:07 UTC');
    expect(row).toHaveTextContent('Seeded from config/policies');
    expect(screen.getByText(/Choose “Review” on a version/)).toBeVisible();
  });

  it('shows the gateway’s refusal instead of an empty table', async () => {
    signedInAs('u-auditor', {
      'GET /v1/admin/policies': errorEnvelope(403, 'PERMISSION_DENIED', 'You do not have permission for this action.'),
    });
    renderRoute('/admin/govern/policy-rules');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Policy versions could not be loaded');
    expect(alert).toHaveTextContent('You do not have permission for this action.');
  });

  it('shows an explicit empty state', async () => {
    signedInAs('u-auditor', { 'GET /v1/admin/policies': [] });
    renderRoute('/admin/govern/policy-rules');

    expect(await screen.findByText('No policy versions')).toBeVisible();
  });
});

describe('run and trace explorer', () => {
  const routes = {
    [`GET /v1/runs/${runDetail.run_id}`]: runDetail,
    [`GET /v1/runs/${runDetail.run_id}/trace`]: runTrace,
  };

  it('opens a run by ID with its attempts, policy events and execution graph', async () => {
    const gateway = signedInAs('u-auditor', routes);
    const { router } = renderRoute('/admin/audit');

    expect(await screen.findByText(/No run open/)).toBeVisible();
    await userEvent.type(screen.getByRole('textbox', { name: 'Run ID' }), runDetail.run_id);
    await userEvent.click(screen.getByRole('button', { name: 'Open run' }));

    expect(router.state.location.search).toBe(`?run=${runDetail.run_id}`);
    const record = required((await screen.findByRole('heading', { name: 'Run record' })).closest('section'));
    expect(record).toHaveTextContent('Completed · Success');
    expect(record).toHaveTextContent('Sovereign direct (CIM bypassed)');
    expect(record).toHaveTextContent('Simulated test identity · Simulated source IP');
    const attempts = screen.getByRole('table', { name: 'Model attempts' });
    expect(within(attempts).getAllByRole('row')).toHaveLength(2);
    expect(attempts).toHaveTextContent('gct-qwen-finetuned');
    expect(attempts).toHaveTextContent('Simulated');
    expect(screen.getByRole('table', { name: 'Policy events' })).toHaveTextContent('GCT-CONF-001');
    expect(screen.getByRole('heading', { name: 'Pipeline stages' })).toBeVisible();
    expect(gateway.requests.map((r) => new URL(r.url).pathname)).toEqual(
      expect.arrayContaining([`/v1/runs/${runDetail.run_id}`, `/v1/runs/${runDetail.run_id}/trace`]),
    );
  });

  it('reports a run the caller may not read as not found', async () => {
    signedInAs('u-auditor', {
      'GET /v1/runs/*': errorEnvelope(404, 'NOT_FOUND', 'Run not found.'),
      'GET /v1/runs/*/trace': errorEnvelope(404, 'NOT_FOUND', 'Run not found.'),
    });
    renderRoute('/admin/audit?run=run_someone_else');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The run could not be opened');
    expect(alert).toHaveTextContent('Run not found.');
  });
});
