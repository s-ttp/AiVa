import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import approvalStatusApproved from '../test/fixtures/govern/approvalStatusApproved.json';
import workflowPost from '../test/fixtures/console/workflowPost.json';
import workflowStored from '../test/fixtures/console/workflowStored.json';
import scenarios from '../test/fixtures/scenarios.json';
import { errorEnvelope, signedInAs, type Route } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';
import { required } from '../test/required';

const notImplemented = () => errorEnvelope(501, 'NOT_IMPLEMENTED', 'Agent workflows are not implemented yet.');
const routes = (extra: Record<string, Route | unknown> = {}) => ({
  'GET /v1/catalog/scenarios': scenarios,
  'GET /v1/agents/workflows': { items: [], page: { next_cursor: null, limit: 50 } },
  ...extra,
});
const approvalId = required(workflowPost.pending_approval_ids[0]);

async function startWorkflow() {
  await userEvent.click(await screen.findByRole('button', { name: 'Run workflow' }));
}

/** A step of the timeline by its heading, e.g. "2. incident.lookup". */
const step = async (name: string) =>
  within(required((await screen.findByRole('heading', { level: 4, name })).closest('li')));

describe('agent workflows', () => {
  it('prefills scenario 6 and sends the workflow as a Test Console identity', async () => {
    const gateway = signedInAs('u-tester', routes({ 'POST /v1/agents/workflows': notImplemented() }));
    renderRoute('/console/agents');

    expect(await screen.findByRole('textbox', { name: 'Incident ID' })).toHaveValue('INC-7781');
    await startWorkflow();
    await screen.findByText('The incident agent is not available yet');
    const post = required(gateway.requests.find((r) => r.method === 'POST'));
    expect(await post.json()).toEqual({
      agent: 'service-incident',
      incident_id: 'INC-7781',
      instructions: 'Triage synthetic service incident INC-7781, check affected sites and draft a customer notice.',
      data_classification: 'internal',
      quality_tier: 'standard',
      max_output_tokens: 1024,
      scenario_id: 'agentic-workflow',
      test_profile: { application_id: 'gct-incident-agent', simulated_source_ip: '192.0.2.10' },
    });
  });

  it('shows the not-implemented state for the list', async () => {
    signedInAs('u-tester', routes({ 'GET /v1/agents/workflows': notImplemented() }));
    renderRoute('/console/agents');

    const state = (await screen.findByText('The workflow list is not available yet')).closest('[role="status"]');
    expect(state).toHaveTextContent('Agent workflows are not implemented yet.');
  });

  it('shows each step with its policy decision, output and the workflow budget', async () => {
    signedInAs('u-tester', routes({ 'POST /v1/agents/workflows': workflowPost }));
    renderRoute('/console/agents');
    await startWorkflow();

    const card = within(required((await screen.findByRole('heading', { level: 2, name: 'Workflow' })).closest('section')));
    // The workflow's status badge comes first; the paused step repeats it.
    expect(required(card.getAllByText('Approval required')[0])).toBeVisible();
    const plan = await step('1. plan');
    expect(plan.getByText('Model step')).toBeVisible();
    expect(plan.getByText('Allowed by policy')).toBeVisible();
    expect(plan.getByText('[Simulated] Look up INC-7781, then check affected sites.')).toBeVisible();
    const lookup = await step('2. incident.lookup');
    expect(lookup.getByText('Tool call')).toBeVisible();
    expect(lookup.getByText(/incident_id: INC-7781\s+status: open/)).toBeVisible();
    const notify = await step('9. notification.send');
    expect(notify.getByText('Approval required', { selector: 'span' })).toBeVisible();
    expect(notify.getByText('Approval required', { selector: 'dd bdi' })).toBeVisible();
    expect(notify.getByText('GCT-TOOL-005')).toBeVisible();
    expect(notify.getByText(approvalId)).toBeVisible();
    // A tester cannot open the Approval Queue, so the step says where it is decided instead of linking.
    expect(notify.getByText('An approver decides it in the AiVa Govern Approval Queue.')).toBeVisible();
    expect(notify.getByText('Not executed')).toBeVisible();
    expect(
      screen.getByText('A cooperative cap shared by every step. The request cap and application budgets stay the hard limits.'),
    ).toBeVisible();
    // What happens at the cap reads as words, not the gateway's code.
    const budget = within(screen.getByRole('region', { name: 'Workflow budget' }));
    expect(budget.getByText('Approval required')).toBeVisible();
    expect(budget.queryByText('approval_required')).not.toBeInTheDocument();
  });

  it('says output was not retained for a stored workflow', async () => {
    signedInAs('u-tester', routes({ 'GET /v1/agents/workflows/*': workflowStored }));
    renderRoute(`/console/agents?workflow=${workflowStored.workflow_id}`);

    const plan = await step('1. plan');
    expect(plan.getByText(/^Output not retained/)).toBeVisible();
    const lookup = await step('2. incident.lookup');
    expect(lookup.getByText(/^Output not retained/)).toBeVisible();
    expect(lookup.getByText(required(workflowStored.steps[1]?.tool?.output_sha256))).toBeVisible();
  });

  it('resumes the workflow once the approval is approved', async () => {
    const resumed = { ...workflowPost, status: 'completed', pending_approval_ids: [], completed_at: '2026-10-14T13:55:00Z', report: 'Customer notice drafted.' };
    const gateway = signedInAs(
      'u-tester',
      routes({
        'POST /v1/agents/workflows': workflowPost,
        'GET /v1/approvals/*': { ...approvalStatusApproved, approval_id: approvalId },
        'POST /v1/agents/workflows/*/resume': resumed,
      }),
    );
    renderRoute('/console/agents');
    await startWorkflow();

    const resume = within(required((await screen.findByRole('heading', { name: 'Resume after approval' })).closest('section')));
    await userEvent.click(resume.getByRole('button', { name: 'Check approval status' }));
    await userEvent.click(await resume.findByRole('button', { name: 'Resume workflow' }));

    expect(await screen.findByText('Customer notice drafted.')).toBeVisible();
    const post = required(gateway.requests.find((r) => r.url.endsWith('/resume')));
    expect(new URL(post.url).pathname).toBe(`/v1/agents/workflows/${workflowPost.workflow_id}/resume`);
    expect(await post.json()).toEqual({
      approval_id: approvalId,
      test_profile: { application_id: 'gct-incident-agent', simulated_source_ip: '192.0.2.10' },
    });
  });

  it('reads a 404 as not found or not visible', async () => {
    signedInAs('u-tester', routes({ 'GET /v1/agents/workflows/*': errorEnvelope(404, 'NOT_FOUND', 'Workflow not found.') }));
    renderRoute('/console/agents?workflow=wf_unknown');

    expect(await screen.findByText('Not found or not visible to you')).toBeVisible();
  });
});
