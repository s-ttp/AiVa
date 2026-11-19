import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import chatBaseline from '../test/fixtures/chatBaseline.json';
import chatBlocked from '../test/fixtures/chatBlocked.json';
import chatQuotaDenied from '../test/fixtures/chatQuotaDenied.json';
import chatSovereign from '../test/fixtures/chatSovereign.json';
import approvalStatusApproved from '../test/fixtures/govern/approvalStatusApproved.json';
import chatApprovalRequired from '../test/fixtures/govern/chatApprovalRequired.json';
import chatResumed from '../test/fixtures/govern/chatResumed.json';
import chatCitationsNone from '../test/fixtures/quality/chatCitationsNone.json';
import chatCitationsNotChecked from '../test/fixtures/quality/chatCitationsNotChecked.json';
import scenarios from '../test/fixtures/scenarios.json';
import { errorEnvelope, json, signedInAs } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';
import { required } from '../test/required';

async function runScenario(response: unknown, path = '/console/run?scenario=fraud-management') {
  const gateway = signedInAs('u-tester', { 'GET /v1/catalog/scenarios': scenarios, 'POST /v1/ai/chat': response });
  renderRoute(path);
  await userEvent.click(await screen.findByRole('button', { name: 'Run through AiVa' }));
  return gateway;
}

/** Synthetic records for scenario 5, as the catalog would serve them. */
const sampleEvidence = [
  { id: 'alarm-ALM-0001', text: 'Synthetic alarm ALM-0001: site DXB-017, LINK_DOWN, major, 2026-10-12T06:41Z.' },
  { id: 'alarm-ALM-0002', text: 'Synthetic alarm ALM-0002: site DXB-017, HIGH_BER, minor, 2026-10-12T06:39Z.' },
];
const withEvidence = scenarios.map((scenario) =>
  scenario.id === 'network-operations' ? { ...scenario, sample_evidence: sampleEvidence } : scenario,
);

const kpi = (name: string) => screen.getByRole('group', { name });
const resultCard = async () => within(required((await screen.findByRole('heading', { level: 2, name: 'Run result' })).closest('section')));

describe('run workspace form', () => {
  it('prefills the scenario from the catalog and keeps deferred scenarios unselectable', async () => {
    signedInAs('u-tester', { 'GET /v1/catalog/scenarios': scenarios });
    renderRoute('/console/run?scenario=fraud-management');

    expect(await screen.findByRole('combobox', { name: 'Scenario' })).toHaveDisplayValue('3. Telecom Fraud Management');
    expect(screen.getByRole('combobox', { name: 'Application profile' })).toHaveValue('gct-fraud-investigator');
    expect(screen.getByRole('combobox', { name: 'Declared classification' })).toHaveValue('confidential');
    expect(screen.getByRole('textbox', { name: 'Synthetic prompt' })).toHaveValue(
      'Investigate synthetic SIM-swap alert FR-2026-0412 and summarise the evidence timeline.',
    );
    expect(screen.getByRole('option', { name: '7. Arabic–English Customer Support (deferred)' })).toBeDisabled();
    expect(screen.getByText('No run yet')).toBeVisible();
  });

  it('shows a scenario’s sample evidence read-only and sends it with the prompt', async () => {
    const gateway = signedInAs('u-tester', { 'GET /v1/catalog/scenarios': withEvidence, 'POST /v1/ai/chat': chatSovereign });
    renderRoute('/console/run?scenario=network-operations');

    const summary = await screen.findByText('Evidence sent with the prompt (2)');
    await userEvent.click(summary);
    const evidence = within(required(summary.closest('details')));
    expect(evidence.getByText('Synthetic sample from the scenario catalog')).toBeVisible();
    expect(evidence.getByText('alarm-ALM-0001')).toBeVisible();
    expect(evidence.getByText(required(sampleEvidence[1]).text)).toBeVisible();
    // Read-only: an edited record would no longer match the scenario's recorded replay.
    expect(evidence.queryAllByRole('textbox')).toHaveLength(0);
    await userEvent.click(screen.getByRole('button', { name: 'Run through AiVa' }));

    const request = gateway.requests.find((r) => r.url.endsWith('/v1/ai/chat'));
    expect(((await request?.json()) as { evidence?: unknown }).evidence).toEqual(sampleEvidence);
  });

  it('sends no evidence for a scenario without it, also after switching away from one', async () => {
    const gateway = signedInAs('u-tester', { 'GET /v1/catalog/scenarios': withEvidence, 'POST /v1/ai/chat': chatSovereign });
    renderRoute('/console/run?scenario=network-operations');

    expect(await screen.findByText('Evidence sent with the prompt (2)')).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Scenario' }), '3. Telecom Fraud Management');
    expect(screen.queryByText(/^Evidence sent with the prompt/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Run through AiVa' }));

    const request = gateway.requests.find((r) => r.url.endsWith('/v1/ai/chat'));
    expect(await request?.json()).not.toHaveProperty('evidence');
  });

  it('sends a traced, idempotent Test Console request as the signed-in tester', async () => {
    const gateway = await runScenario(chatSovereign);
    const request = gateway.requests.find((r) => r.url.endsWith('/v1/ai/chat'));

    expect(request?.headers.get('X-AiVa-Authorization')).toBe('Bearer test-token-u-tester');
    // Never in Authorization, which a platform edge on the way could read (ADR-0025).
    expect(request?.headers.get('Authorization')).toBeNull();
    expect(request?.headers.get('Idempotency-Key')).toMatch(/^[0-9a-f-]{36}$/);
    expect(await request?.json()).toMatchObject({
      trace: true,
      scenario_id: 'fraud-management',
      data_classification: 'confidential',
      routing: { tags: ['fraud-investigation'] },
      test_profile: { application_id: 'gct-fraud-investigator', simulated_source_ip: '192.0.2.10' },
    });
  });
});

describe('run result', () => {
  it('shows the policy decision, sovereign route, CIM copy and simulated provenance', async () => {
    await runScenario(chatSovereign);
    const result = await resultCard();

    expect(result.getByText('Allowed by policy')).toBeVisible();
    expect(result.getByText('Sovereign direct route required')).toBeVisible();
    expect(screen.getAllByText('CIM bypassed by mandatory sovereignty policy').length).toBeGreaterThan(0);
    expect(screen.getByText(`Allowed by policy · policy version ${chatSovereign.policy.policy_version}`)).toBeVisible();
    expect(screen.getByText('Confidential (declared: Confidential)')).toBeVisible();
    expect(kpi('Upstream input tokens')).toHaveTextContent('22');
    expect(kpi('Upstream input tokens')).toHaveTextContent('Simulated');
    expect(kpi('Provider API cost (this route)')).toHaveTextContent('$0.00');
    expect(kpi('Gateway time')).toHaveTextContent('Simulated');
    expect(kpi('Estimated local GPU cost')).toHaveTextContent('$0.00');
    expect(kpi('Estimated local GPU cost')).toHaveTextContent('Simulated');
    // Simulated tokens, costs and times never read as measured (the budget reservation is a ledger fact).
    for (const name of ['Upstream input tokens', 'Provider API cost (this route)', 'Gateway time', 'Estimated local GPU cost']) {
      expect(kpi(name)).not.toHaveTextContent('Measured');
    }
    expect(screen.getByText(chatSovereign.run_id)).toBeVisible();
    expect(screen.getByText(chatSovereign.trace_id)).toBeVisible();
  });

  it('draws the 13-stage execution graph from the trace with skip reasons', async () => {
    await runScenario(chatSovereign);
    const graph = required((await screen.findByRole('heading', { name: 'Pipeline stages' })).closest('section'));

    const stages = within(graph).getAllByRole('listitem').filter((item) => item.parentElement?.tagName === 'OL');
    expect(stages).toHaveLength(13);
    expect(stages.map((stage) => stage.querySelector('p')?.textContent)).toEqual([
      'Ingress',
      'AI-PEL policy',
      'Exact cache',
      'RLCD initial assessment',
      'Retrieval',
      'Enterprise CIM',
      'Context sufficiency guard',
      'RLCD final plan',
      'Budget reservation',
      'Preflight',
      'Inference',
      'Assurance',
      'Reconciliation',
    ]);
    expect(stages[5]).toHaveTextContent('Skipped');
    expect(stages[5]).toHaveTextContent('CIM bypassed by mandatory sovereignty policy');
    const path = required(screen.getByRole('heading', { name: 'Execution path' }).closest('section'));
    expect(within(path).getByText('Sovereign direct (CIM bypassed)').parentElement).toHaveTextContent('Taken');
    expect(within(path).getByText('CIM-assisted').parentElement).toHaveTextContent('Not taken');
  });

  it('explains a denial with its rule ID and remediation and reports no upstream usage', async () => {
    await runScenario(chatBlocked);
    const result = await resultCard();

    expect(result.getByText('Blocked by AI policy.')).toBeVisible();
    expect(result.getByText('AIPEL-NET-001')).toBeVisible();
    expect(
      screen.getByText('What to do: Call from an approved network or ask an administrator to register it.'),
    ).toBeVisible();
    expect(kpi('Upstream input tokens')).toHaveTextContent('No upstream call');
    expect(kpi('Budget reserved')).toHaveTextContent('No reservation');
    expect(result.getByText('No trusted network matched', { exact: false })).toBeVisible();
    expect(screen.getByText('Run ended here')).toBeVisible();
    expect(screen.getByText('The run stopped before an execution path was chosen.')).toBeVisible();
  });

  it('names a key call accepted from the internet as such, not as a trusted network', async () => {
    await runScenario({ ...chatSovereign, identity: { ...chatSovereign.identity, matched_network_id: 'internet' } });
    const result = await resultCard();
    expect(result.getByText('Internet (application key)', { exact: false })).toBeVisible();
  });

  it('tells a budget-cap denial apart from a policy block', async () => {
    await runScenario(chatQuotaDenied, '/console/run?scenario=customer-support');
    const result = await resultCard();

    expect(result.getByText('Blocked by budget cap')).toBeVisible();
    expect(result.queryByText('Blocked by AI policy')).not.toBeInTheDocument();
    expect(result.getByText('Allowed by policy · policy version 1')).toBeVisible();
    expect(result.getByText('bud-application-gct-support-chatbot-daily')).toBeVisible();
    expect(result.getByText('gct-qwen-finetuned (planned by RLCD; not called)')).toBeVisible();
    expect(kpi('Upstream input tokens')).toHaveTextContent('No upstream call');
    const graph = required((await screen.findByRole('heading', { name: 'Pipeline stages' })).closest('section'));
    const quota = within(graph).getByText('Budget reservation').closest('li');
    expect(quota).toHaveTextContent('Run ended here');
    expect(quota).toHaveTextContent('Budget reservation denied');
    expect(screen.getByText('No response: a budget cap did not cover the reservation. No model was called.')).toBeInTheDocument();
  });

  it('never shows submitted-model tokens for a full response-cache hit', async () => {
    const cacheHit = {
      ...chatSovereign,
      execution_path: 'cache-return',
      cim: { ...chatSovereign.cim, state: 'skipped-cache', message: 'Full response-cache hit: no upstream inference call' },
      cache: { checked: true, hit: true, cache_type: 'semantic', upstream_call: false },
    };
    await runScenario(cacheHit);

    expect(
      await screen.findByText('Full response-cache hit (Semantic) · No upstream inference call'),
    ).toBeVisible();
    // A hit carries the provenance of the run that produced the cached answer, never "measured" by default.
    const response = required(screen.getByRole('heading', { name: 'Actual response' }).closest('section'));
    expect(within(response).getByText('Simulated')).toBeInTheDocument();
    expect(within(response).queryByText('Measured')).not.toBeInTheDocument();
    expect(kpi('Upstream input tokens')).toHaveTextContent('No upstream call');
    expect(kpi('Upstream output tokens')).toHaveTextContent('No upstream call');
    await userEvent.click(screen.getByRole('tab', { name: 'Prompt & Context Inspector' }));
    expect(screen.getByText('No upstream inference call: nothing was submitted to a model.')).toBeVisible();
    await userEvent.click(screen.getByRole('tab', { name: 'Savings Analysis' }));
    expect(screen.getByText(/The avoided cost is not returned by the gateway/)).toBeVisible();
  });

  it('estimates savings only against a baseline comparison, labelled with its provenance', async () => {
    await runScenario(chatBaseline, '/console/run?scenario=customer-support');

    await userEvent.click(await screen.findByRole('tab', { name: 'Savings Analysis' }));
    const savings = screen.getByRole('group', { name: 'Estimated savings versus pinned baseline' });
    expect(savings).toHaveTextContent('$0.000916');
    expect(savings).toHaveTextContent('Simulated');
    const costs = screen.getByRole('table', { name: 'Cost components for this run' });
    expect(within(costs).getByRole('row', { name: /Frontier baseline/ })).toHaveTextContent('$0.000916');
  });

  it('shows the GPU estimate as not applicable when no GPU endpoint ran, never as $0', async () => {
    const noGpu = { ...chatBaseline, cost: { ...chatBaseline.cost, gpu_estimated_micros: 0, gpu_estimated_provenance: null } };
    await runScenario(noGpu, '/console/run?scenario=customer-support');

    const tile = kpi('Estimated local GPU cost');
    expect(tile).toHaveTextContent('Not applicable');
    expect(tile).not.toHaveTextContent('$0');
    expect(tile).not.toHaveTextContent('Simulated');
    await userEvent.click(screen.getByRole('tab', { name: 'Savings Analysis' }));
    const costs = screen.getByRole('table', { name: 'Cost components for this run' });
    const gpu = within(costs).getByRole('row', { name: /Estimated local GPU cost/ });
    expect(gpu).toHaveTextContent('Not applicable');
    expect(gpu).not.toHaveTextContent('$');
  });

  it('says a CIM-selected context was not verified', async () => {
    const selected = { ...chatBaseline, cim: { ...chatBaseline.cim, sufficiency: 'cim-selected' } };
    await runScenario(selected, '/console/run?scenario=customer-support');

    expect((await screen.findAllByText('CIM selected the passages; relevance not verified'))[0]).toBeVisible();
    expect(screen.queryByText(/sufficiency verified/)).not.toBeInTheDocument();
  });

  it('withholds a savings figure when no baseline was run', async () => {
    await runScenario(chatSovereign);

    await userEvent.click(await screen.findByRole('tab', { name: 'Savings Analysis' }));
    expect(screen.queryByRole('group', { name: 'Estimated savings versus pinned baseline' })).not.toBeInTheDocument();
    expect(screen.getByText(/No savings figure is shown without a baseline comparison/)).toBeVisible();
  });

  it('shows a run that failed closed like any other run', async () => {
    const failed = {
      ...chatBlocked,
      status: 'failed',
      outcome: 'error',
      error: { ...chatBlocked.error, code: 'DEPENDENCY_UNAVAILABLE', message: 'Policy engine unavailable.', rule_ids: [] },
    };
    await runScenario(json(failed, 503));

    expect(await screen.findByText('Run failed; the gateway failed closed')).toBeVisible();
    expect(screen.getByText('Policy engine unavailable.')).toBeVisible();
    expect(screen.getByText('DEPENDENCY_UNAVAILABLE')).toBeVisible();
  });

  it('shows the gateway’s error envelope when no run was recorded', async () => {
    await runScenario(
      errorEnvelope(422, 'VALIDATION_FAILED', 'simulated_source_ip is not a valid IP address.', 'Use an IPv4 or IPv6 address.'),
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The run could not be started');
    expect(alert).toHaveTextContent('simulated_source_ip is not a valid IP address.');
    expect(alert).toHaveTextContent('Use an IPv4 or IPv6 address.');
    expect(alert).toHaveTextContent('HTTP 422, VALIDATION_FAILED');
  });
});

describe('runs that need approval', () => {
  it('checks the approval and resubmits the identical request with its ID', async () => {
    let chats = 0;
    const gateway = signedInAs('u-tester', {
      'GET /v1/catalog/scenarios': scenarios,
      'POST /v1/ai/chat': () => json(chats++ === 0 ? chatApprovalRequired : chatResumed),
      'GET /v1/approvals/*': approvalStatusApproved,
    });
    renderRoute('/console/run?scenario=customer-support');
    await userEvent.click(await screen.findByRole('button', { name: 'Run through AiVa' }));

    const panel = within(required((await screen.findByRole('heading', { name: 'Approval request' })).closest('section')));
    const approvalId = required(chatApprovalRequired.approval.approval_id);
    expect(panel.getByText(approvalId)).toBeVisible();
    expect(panel.getByText('GCT-E2E-APPROVAL')).toBeVisible();
    expect(panel.getByText(/Pending when the run ended/)).toBeVisible();
    expect(panel.queryByRole('button', { name: 'Resubmit with approval' })).not.toBeInTheDocument();

    await userEvent.click(panel.getByRole('button', { name: 'Check approval status' }));
    expect(await panel.findByText('Approved')).toBeVisible();
    expect(panel.getByText('Approver’s note: Reviewed: synthetic billing question.')).toBeVisible();
    await userEvent.click(panel.getByRole('button', { name: 'Resubmit with approval' }));

    const result = await resultCard();
    expect(await result.findByText('Allowed by policy')).toBeVisible();
    expect(result.getByText(approvalId)).toBeVisible();
    const [first, second] = await Promise.all(
      gateway.requests.filter((r) => r.url.endsWith('/v1/ai/chat')).map((r) => r.json()),
    );
    expect(first.approval_id).toBeNull();
    expect(second).toEqual({ ...first, approval_id: approvalId });
  });

  it('explains a denied approval and offers no resubmission', async () => {
    signedInAs('u-tester', {
      'GET /v1/catalog/scenarios': scenarios,
      'POST /v1/ai/chat': chatApprovalRequired,
      'GET /v1/approvals/*': { ...approvalStatusApproved, status: 'denied', decision_note: 'Not needed.' },
    });
    renderRoute('/console/run?scenario=customer-support');
    await userEvent.click(await screen.findByRole('button', { name: 'Run through AiVa' }));

    const panel = within(required((await screen.findByRole('heading', { name: 'Approval request' })).closest('section')));
    await userEvent.click(panel.getByRole('button', { name: 'Check approval status' }));
    expect(await panel.findByText('The request was denied. It stays blocked.')).toBeVisible();
    expect(panel.queryByRole('button', { name: 'Resubmit with approval' })).not.toBeInTheDocument();
  });
});

describe('answer citations', () => {
  const citationsOf = async (response: unknown) => {
    await runScenario(response);
    const result = await resultCard();
    return required(result.getByText('Answer citations').parentElement);
  };
  const withCitations = (citations: object) => ({
    ...chatCitationsNone,
    assurance: { ...chatCitationsNone.assurance, answer_citations: citations },
  });

  it('says when an answer cites nothing, and when there was no model call to check', async () => {
    expect(await citationsOf(chatCitationsNone)).toHaveTextContent('No citations');
  });

  it('says citations were not checked when no model was called', async () => {
    expect(await citationsOf(chatCitationsNotChecked)).toHaveTextContent('Citations not checked (no model call)');
  });

  // Simulated runs never cite, so these two change only the recorded run's answer_citations.
  it('confirms citations that match the sources the model was given', async () => {
    const fact = await citationsOf(withCitations({ status: 'resolved', cited: 3, unresolved: 0, unresolved_refs: [] }));
    expect(fact).toHaveTextContent('Citations match the sources it was given');
  });

  it('names the citations that were not in the context, without blocking the run', async () => {
    const fact = await citationsOf(
      withCitations({ status: 'unresolved', cited: 3, unresolved: 1, unresolved_refs: ['KB-GCT-0042 §2'] }),
    );
    expect(fact).toHaveTextContent('Cites a source that was not in its context (1 of 3)');
    expect(within(fact).getByText('KB-GCT-0042 §2')).toBeVisible();
    expect(screen.getAllByText('Allowed by policy').length).toBeGreaterThan(0);
    expect(screen.queryByText('Blocked by AI policy')).not.toBeInTheDocument();
  });
});
