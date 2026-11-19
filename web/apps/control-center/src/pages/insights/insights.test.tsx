import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import alerts from '../../test/fixtures/insights/alerts.json';
import budgets from '../../test/fixtures/insights/budgets.json';
import classificationTask from '../../test/fixtures/insights/classificationTask.json';
import consumptionApplication from '../../test/fixtures/insights/consumptionApplication.json';
import consumptionApplicationCareManager from '../../test/fixtures/insights/consumptionApplicationCareManager.json';
import consumptionDepartment from '../../test/fixtures/insights/consumptionDepartment.json';
import consumptionUser from '../../test/fixtures/insights/consumptionUser.json';
import exportsList from '../../test/fixtures/insights/exports.json';
import filters from '../../test/fixtures/insights/filters.json';
import governanceApplication from '../../test/fixtures/insights/governanceApplication.json';
import optimizationApplication from '../../test/fixtures/insights/optimizationApplication.json';
import optimizationModel from '../../test/fixtures/insights/optimizationModel.json';
import optimizationStrategy from '../../test/fixtures/insights/optimizationStrategy.json';
import overview from '../../test/fixtures/insights/overview.json';
import profileCareManager from '../../test/fixtures/insights/profileCareManager.json';
import profileTenantAdmin from '../../test/fixtures/insights/profileTenantAdmin.json';
import runDetailSovereign from '../../test/fixtures/insights/runDetailSovereign.json';
import runsFraud from '../../test/fixtures/insights/runsFraud.json';
import { errorEnvelope, hfRateLimited, json, signedInAs, type Route } from '../../test/gateway';
import { renderRoute, sessionSettled } from '../../test/renderRoute';
import { required } from '../../test/required';
import filtersVisitor from '../../test/fixtures/demo/filtersVisitor.json';
import { formatCount, formatMicrosUsd } from '../../format';

/** Answers by the query's group_by, so one route serves every grouping a view asks for. */
function byGroup(groups: Record<string, unknown>): Route {
  return (_request, url) => {
    const fixture = groups[url.searchParams.get('group_by') ?? ''];
    return fixture ? json(fixture) : errorEnvelope(404, 'NOT_FOUND', 'No fixture for this grouping');
  };
}

const ROUTES = {
  'GET /v1/reports/filters': filters,
  'GET /v1/reports/overview': overview,
  'GET /v1/reports/consumption': byGroup({
    department: consumptionDepartment,
    application: consumptionApplication,
    user: consumptionUser,
  }),
  'GET /v1/reports/classification': classificationTask,
  'GET /v1/reports/optimization': byGroup({
    model: optimizationModel,
    strategy: optimizationStrategy,
    application: optimizationApplication,
  }),
  'GET /v1/reports/governance': governanceApplication,
  'GET /v1/reports/runs': runsFraud,
  'GET /v1/reports/runs/*': runDetailSovereign,
  'GET /v1/reports/exports': exportsList,
  'GET /v1/admin/budgets': budgets,
  'GET /v1/admin/budgets/alerts': alerts,
};

async function open(path: string, user: Parameters<typeof signedInAs>[0] = 'u-finance', extra: Record<string, unknown> = {}) {
  const gateway = signedInAs(user, { ...ROUTES, ...extra });
  renderRoute(path);
  await sessionSettled();
  return gateway;
}

describe('Admin › Overview (executive view)', () => {
  it('shows totals with provenance, and never labels simulated spend as measured', async () => {
    await open('/admin');
    const requests = await screen.findByRole('group', { name: 'Total requests' });
    expect(requests).toHaveTextContent('60');
    expect(within(requests).getByText('Synthetic')).toBeInTheDocument();
    const spend = screen.getByRole('group', { name: 'Provider API cost (not measured)' });
    expect(spend).toHaveTextContent('$0.018105');
    expect(within(spend).getByText('Simulated')).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Measured provider API cost' })).not.toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Estimated local GPU cost' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Avoided by full response cache' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Full response-cache hit rate' })).toHaveTextContent('1.7%');
    expect(screen.getByText('Synthetic demo data', { selector: 'span' })).toBeInTheDocument();
  });

  it('shows the savings waterfall as estimated, not quality-adjusted, with the measured baseline comparison', async () => {
    await open('/admin');
    expect(await screen.findByText('Not quality-adjusted')).toBeInTheDocument();
    expect(screen.getByRole('figure', { name: /Savings waterfall against claude-flagship/ })).toBeInTheDocument();
    const steps = screen.getByRole('table', { name: 'Savings waterfall steps' });
    expect(within(steps).getByText('Frontier baseline')).toBeInTheDocument();
    expect(within(steps).getByText('Net cost')).toBeInTheDocument();
    // The context step grew the context in this fixture (+$0.003): its label says it added cost.
    expect(within(steps).getByText('Context packaging: added cost')).toBeInTheDocument();
    expect(within(steps).queryByText(/reduction/i)).not.toBeInTheDocument();
    // The opt-in baseline comparison is not a step: it follows the net, never subtracted from the savings.
    expect(within(steps).queryByText(/Baseline comparison/)).not.toBeInTheDocument();
    expect(
      screen.getByText(`Measurement cost, not counted in savings: ${formatMicrosUsd(overview.savings.measurement_cost.value)}`),
    ).toBeInTheDocument();
    expect(screen.getByText('GPU time is not estimated on replayed runs.')).toBeInTheDocument();
    expect(screen.getByText('Baseline outputs are not graded in this phase')).toBeInTheDocument();
    expect(screen.getByText(`${overview.baseline.quality_passed_runs} passed quality checks`)).toBeInTheDocument();
  });

  it('every chart has a data-table equivalent', async () => {
    await open('/admin');
    await screen.findByRole('figure', { name: 'Token consumption' });
    for (const figure of screen.getAllByRole('figure')) {
      expect(within(figure).getByText('Show the data as a table')).toBeInTheDocument();
      expect(within(figure).getByRole('table')).toBeInTheDocument();
    }
  });

  it('shows an explicit error instead of figures when the gateway refuses', async () => {
    await open('/admin', 'u-finance', {
      'GET /v1/reports/overview': errorEnvelope(403, 'PERMISSION_DENIED', 'Reports are limited to your own tenant.'),
    });
    expect(await screen.findByRole('alert')).toHaveTextContent('The overview could not be loaded');
    expect(screen.queryByRole('group', { name: 'Total requests' })).not.toBeInTheDocument();
  });
});

describe('Insights › Consumption Overview (explorer)', () => {
  it('drills from the enterprise into a department and sends the filter to the gateway', async () => {
    const gateway = await open('/admin/insights?from=2026-10-09&to=2026-10-15');
    const table = await screen.findByRole('table', { name: 'Consumption By department' });
    expect(within(table).getByText('Customer Care')).toBeInTheDocument();
    await userEvent.click(within(table).getByRole('button', { name: 'Drill down into Customer Care' }));
    await screen.findByRole('table', { name: 'Consumption By application' });
    const last = gateway.requests.filter((r) => r.url.includes('/v1/reports/consumption')).at(-1);
    const query = new URL(last?.url ?? 'http://x').searchParams;
    expect(query.get('department_id')).toBe('customer-care');
    expect(query.get('group_by')).toBe('application');
    expect(screen.getByRole('navigation', { name: 'Drilldown path' })).toHaveTextContent('Enterprise');
    expect(await screen.findByRole('button', { name: 'Remove filter Department: Customer Care' })).toBeInTheDocument();
  });

  it('lists traces and opens one: a confidential request with no CIM and no external attempt', async () => {
    await open('/admin/insights?from=2026-10-09&to=2026-10-15&level=runs&application_id=gct-fraud-investigator');
    const traces = await screen.findByRole('table', { name: 'Requests matching the filters' });
    const first = runsFraud.runs[0];
    await userEvent.click(within(traces).getByRole('link', { name: `Open trace ${first?.trace_id}` }));
    const attempts = await screen.findByRole('table', { name: 'Model attempts of this request' });
    expect(within(attempts).getAllByRole('row')).toHaveLength(2); // header + the single sovereign call
    expect(attempts).toHaveTextContent('gct-qwen-finetuned');
    const detail = screen.getByRole('region', { name: 'Trace detail' });
    expect(within(detail).getByText('Sovereign direct route required')).toBeInTheDocument();
    expect(within(detail).getAllByText('CIM bypassed by mandatory sovereignty policy').length).toBeGreaterThan(0);
  });

  it('names the calls and charges of an answer-quality check on the trace', async () => {
    const served = required(runDetailSovereign.attempts[0]);
    const comparison = (stage: string, model_id: string, amount_micros: number) => ({
      ...served,
      attempt_id: `att_${stage}`,
      stage,
      model_id,
      provenance: 'measured',
      charges: [{ ...required(served.charges[0]), attempt_id: `att_${stage}`, charge_type: stage, amount_micros, provenance: 'measured' }],
    });
    const compared = {
      ...runDetailSovereign,
      attempts: [
        ...runDetailSovereign.attempts,
        comparison('quality_reference', 'claude-flagship', 28_000),
        comparison('quality_grading', 'gemini-flash-lite', 700),
      ],
    };
    await open('/admin/insights?from=2026-10-09&to=2026-10-15&level=runs&application_id=gct-fraud-investigator', 'u-finance', {
      'GET /v1/reports/runs/*': compared,
    });
    const traces = await screen.findByRole('table', { name: 'Requests matching the filters' });
    await userEvent.click(within(traces).getByRole('link', { name: `Open trace ${runsFraud.runs[0]?.trace_id}` }));
    const attempts = await screen.findByRole('table', { name: 'Model attempts of this request' });
    // The stage and its charge carry the same name, never "Other".
    expect(within(attempts).getAllByText('Quality check: frontier reference')).toHaveLength(2);
    expect(within(attempts).getAllByText('Quality check: grader')).toHaveLength(2);
    expect(within(attempts).queryByText(/^Other/)).not.toBeInTheDocument();
  });
});

describe('Insights › Applications, Users, Prompt Categories, Models, Savings, Quotas', () => {
  it('ranks applications and links each to the explorer at user level', async () => {
    await open('/admin/insights/applications');
    const table = await screen.findByRole('table', { name: 'Consumption per application' });
    const link = within(table).getByRole('link', { name: 'Explore Customer Support Chatbot by user' });
    expect(link.getAttribute('href')).toContain('application_id=gct-support-chatbot');
    expect(link.getAttribute('href')).toContain('level=user');
  });

  it('shows a department manager only their department, as the gateway scoped it', async () => {
    await open('/admin/insights/applications', 'u-finance', {
      'GET /v1/auth/me': profileCareManager,
      'GET /v1/reports/consumption': consumptionApplicationCareManager,
    });
    const table = await screen.findByRole('table', { name: 'Consumption per application' });
    expect(within(table).getAllByRole('row')).toHaveLength(2);
    expect(screen.getByText('Department customer-care')).toBeInTheDocument();
  });

  it('lists users by pseudonym and offers audited identity resolution only with reports:identity', async () => {
    await open('/admin/insights/users');
    await screen.findByRole('table', { name: 'Consumption per user or service principal' });
    expect(screen.queryByRole('heading', { name: 'Resolve a pseudonym' })).not.toBeInTheDocument();
  });

  it('resolves pseudonyms for a tenant administrator, with a reason', async () => {
    const gateway = await open('/admin/insights/users', 'u-tenant-admin', {
      'GET /v1/auth/me': profileTenantAdmin,
      'POST /v1/reports/identities/resolve': {
        identities: [{ pseudonym: consumptionUser.rows[0]?.group, kind: 'synthetic', user_id: 'syn-care-01', display_name: 'Synthetic traffic user' }],
      },
    });
    expect(await screen.findByRole('heading', { name: 'Resolve a pseudonym' })).toBeInTheDocument();
    const pseudonym = consumptionUser.rows[0]?.group ?? '';
    await userEvent.click(await screen.findByRole('checkbox', { name: pseudonym }));
    const submit = screen.getByRole('button', { name: 'Resolve and audit' });
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByRole('textbox', { name: 'Reason' }), 'Investigating an anomaly alert');
    await userEvent.click(submit);
    expect(await screen.findByRole('table', { name: 'Resolved identities' })).toHaveTextContent('syn-care-01');
    const sent = gateway.requests.find((r) => r.url.endsWith('/v1/reports/identities/resolve'));
    expect(await sent?.json()).toEqual({ pseudonyms: [pseudonym], reason: 'Investigating an anomaly alert' });
  });

  it('reports the unclassified bucket as low confidence, with the taxonomy version', async () => {
    await open('/admin/insights/prompt-categories');
    expect(await screen.findByRole('group', { name: 'Unclassified or review-needed share' })).toHaveTextContent('5.0%');
    expect(screen.getAllByText('tax-2026.10.1', { selector: 'bdi' }).length).toBeGreaterThan(0);
    const table = screen.getByRole('table', { name: 'Requests, tokens and spend per group' });
    expect(within(table).getByText('Unclassified (low confidence)')).toBeInTheDocument();
  });

  it('shows model efficiency per model and per context strategy', async () => {
    await open('/admin/insights/models');
    const table = await screen.findByRole('table', { name: 'Efficiency per model' });
    expect(within(table).getByText('GCT Sovereign Qwen')).toBeInTheDocument();
    expect(await screen.findByRole('table', { name: 'Efficiency per context strategy' })).toBeInTheDocument();
  });

  it('measures packing as passages kept, with the tokens the model read', async () => {
    await open('/admin/insights/models');
    const table = await screen.findByRole('table', { name: 'Efficiency per model' });
    const headers = within(table).getAllByRole('columnheader').map((cell) => cell.textContent);
    expect(headers).toContain('Passages kept by the CIM');
    expect(headers).toContain('Prompt → sent tokens');
    const qwen = required(optimizationModel.rows.find((row) => row.group === 'gct-qwen-finetuned')).packing;
    const row = required(within(table).getByRole('rowheader', { name: /GCT Sovereign Qwen/ }).closest('tr'));
    expect(row).toHaveTextContent(`${qwen.passages_kept} of ${qwen.passages_retrieved} passages kept`);
    expect(row).toHaveTextContent(`${qwen.packed_runs} packed requests; the model read ${formatCount(qwen.model_input_tokens.total)} tokens`);
  });

  it('shows savings by application with their provenance', async () => {
    await open('/admin/insights/savings');
    const table = await screen.findByRole('table', { name: 'Estimated savings components per application' });
    expect(within(table).getAllByText('Simulated').length).toBeGreaterThan(0);
    expect(screen.getByRole('heading', { name: 'Pinned frontier comparison' })).toBeInTheDocument();
  });

  it('shows every waterfall step per application, so each row sums to its net cost', async () => {
    await open('/admin/insights/savings');
    const table = await screen.findByRole('table', { name: 'Estimated savings components per application' });
    const headers = within(table).getAllByRole('columnheader').map((cell) => cell.textContent);
    expect(headers).toEqual([
      'Application',
      'Provenance',
      'Frontier baseline',
      'Full response cache',
      'Context packaging',
      'Model-rate differential',
      'Provider prompt cache',
      'CIM overhead',
      'Retries and failed calls',
      'Tool calls (web search)',
      'Ledger corrections',
      'Local GPU (estimated)',
      'Net cost',
      'Estimated net savings',
      'Measurement cost',
    ]);
    const first = required(optimizationApplication.rows[0]);
    const steps = first.savings.steps.map((step) => step.amount_micros);
    expect(steps.slice(0, -1).reduce((sum, amount) => sum + amount, 0)).toBe(steps.at(-1));
    const row = required(within(table).getByRole('rowheader', { name: first.label }).closest('tr'));
    expect(within(row).getAllByRole('cell').slice(1, -2).map((cell) => cell.textContent)).toEqual(
      steps.map(formatMicrosUsd),
    );
    expect(within(row).getAllByRole('cell').at(-1)).toHaveTextContent(formatMicrosUsd(first.savings.measurement_cost.value));
  });

  it('shows budgets, alerts and governance with local-only enforcement for confidential traffic', async () => {
    await open('/admin/insights/quotas');
    expect(await screen.findByRole('table', { name: 'Budgets in your scope, this period' })).toBeInTheDocument();
    expect(
      await screen.findByText(/Local-only enforcement held: \d+ confidential-or-above requests, zero CIM and zero external/),
    ).toBeInTheDocument();
    const governance = screen.getByRole('table', { name: 'Governance outcomes by Application' });
    expect(within(governance).getByText('All requests')).toBeInTheDocument();
  });
});

describe('Insights › Export Center', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('requests a filtered export with the window and lists ready exports for download', async () => {
    const gateway = await open('/admin/insights/export-center?from=2026-10-09&to=2026-10-15&application_id=gct-support-chatbot', 'u-finance', {
      'POST /v1/reports/exports': json({ ...exportsList.exports[0], status: 'queued' }, 202),
    });
    expect(await screen.findByText(/no prompt text, subscriber identifiers or resolved identities/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Request CSV export' }));
    const sent = await waitFor(() =>
      required(gateway.requests.find((r) => r.method === 'POST' && r.url.endsWith('/v1/reports/exports'))),
    );
    expect(await sent.json()).toEqual({
      report: 'consumption',
      group_by: 'application',
      window: { from: '2026-10-09', to: '2026-10-15', timezone: 'UTC' },
      filters: { application_id: 'gct-support-chatbot' },
    });
    const table = await screen.findByRole('table', { name: 'Your CSV exports, newest first' });
    expect(within(table).getByText('Ready')).toBeInTheDocument();
    expect(within(table).getByRole('button', { name: `Download export ${exportsList.exports[0]?.export_id}` })).toBeInTheDocument();
  });

  it('stops polling a running export while Hugging Face rate-limits the demo, and asks again once the limit resets', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const running = { exports: [{ ...exportsList.exports[0], status: 'running' }] };
    const answers: Response[] = [json(running), hfRateLimited(30), json(exportsList)];
    const gateway = await open('/admin/insights/export-center', 'u-finance', {
      'GET /v1/reports/exports': () => required(answers.length > 1 ? answers.shift() : answers[0]).clone(),
    });
    const reads = () => gateway.requests.filter((r) => r.url.endsWith('/v1/reports/exports')).length;
    await waitFor(() => expect(reads()).toBe(2));

    await act(() => vi.advanceTimersByTimeAsync(25_000));
    expect(reads()).toBe(2);

    await act(() => vi.advanceTimersByTimeAsync(6_000));
    const table = await screen.findByRole('table', { name: 'Your CSV exports, newest first' });
    expect(within(table).getByText('Ready')).toBeInTheDocument();
    expect(reads()).toBe(3);
  });
});

describe('Insights rendered right-to-left', () => {
  beforeEach(() => {
    document.documentElement.dir = 'rtl';
  });
  afterEach(() => {
    document.documentElement.dir = 'ltr';
  });

  const isolatedLtr = () => [...document.querySelectorAll('bdi[dir="ltr"]')].map((node) => node.textContent);

  it('keeps application, user and model IDs left-to-right in the trace list and the trace', async () => {
    await open('/admin/insights?from=2026-10-09&to=2026-10-15&level=runs&application_id=gct-fraud-investigator');
    const traces = await screen.findByRole('table', { name: 'Requests matching the filters' });
    const first = required(runsFraud.runs[0]);
    expect(isolatedLtr()).toEqual(expect.arrayContaining([first.application_id, first.principal]));
    expect(screen.getByText(`${runsFraud.page.total_rows} rows`)).toBeInTheDocument();
    await userEvent.click(within(traces).getByRole('link', { name: `Open trace ${first.trace_id}` }));
    const policy = await screen.findByRole('table', { name: 'AI-PEL evaluations of this request' });
    // One translatable message, with the egress model isolated inside it.
    expect(policy).toHaveTextContent('egress → gct-qwen-finetuned');
    expect(within(policy).getByText('gct-qwen-finetuned', { selector: 'bdi[dir="ltr"]' })).toBeInTheDocument();
  });

  it('uses plural messages and locale lists for governance outcomes', async () => {
    const totals = { ...governanceApplication.totals, confidential_requests: 1 };
    await open('/admin/insights/quotas', 'u-finance', {
      'GET /v1/reports/governance': { ...governanceApplication, totals },
    });
    expect(
      await screen.findByText(
        'Local-only enforcement held: 1 confidential-or-above request, zero CIM and zero external provider attempts',
      ),
    ).toBeInTheDocument();
    const governance = screen.getByRole('table', { name: 'Governance outcomes by Application' });
    expect(within(governance).getAllByText('GCT-INJ-003 (1)').length).toBeGreaterThan(0);
  });

  it('reports answer-quality checks under the scenario 11 exception apart from the served path', async () => {
    const totals = { ...governanceApplication.totals, confidential_requests: 3, confidential_comparison_attempts: 2 };
    await open('/admin/insights/quotas', 'u-finance', {
      'GET /v1/reports/governance': { ...governanceApplication, totals },
    });
    expect(
      await screen.findByText(
        'Served paths stayed sovereign: 3 confidential-or-above requests, zero CIM and zero external provider attempts while serving',
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText('2 comparison calls sent scenario 11’s synthetic sample to external providers under the ADR-0023 exception'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^Local-only enforcement held/)).not.toBeInTheDocument();
  });

  it('isolates filter values and report versions on the executive overview', async () => {
    await open('/admin?application_id=gct-support-chatbot');
    expect(await screen.findByRole('group', { name: 'Total requests' })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Remove filter Application: Customer Support Chatbot' })).toHaveTextContent(
      'Application: Customer Support Chatbot',
    );
    expect(isolatedLtr()).toContain(overview.metadata.pricing_snapshot);
  });
});

describe('Insights for a public demo visitor', () => {
  it('shows a visitor passages kept, never the prompt-to-sent token change', async () => {
    await open('/admin/insights/models', 'u-public-visitor', { 'GET /v1/reports/filters': filtersVisitor });
    const table = await screen.findByRole('table', { name: 'Efficiency per model' });
    const headers = within(table).getAllByRole('columnheader').map((cell) => cell.textContent);
    expect(headers).toContain('Passages kept by the CIM');
    expect(headers).not.toContain('Prompt → sent tokens');
    expect(within(table).queryByText(/fewer$|more$/)).not.toBeInTheDocument();
  });

  // The gateway hides every principal from a scope without the user dimension (ADR-0019).
  const hiddenRuns = {
    ...runsFraud,
    metadata: { ...runsFraud.metadata, scope: { ...runsFraud.metadata.scope, users_visible: false } },
    runs: runsFraud.runs.map((run) => ({ ...run, principal: 'hidden' })),
  };
  const hiddenDetail = { ...runDetailSovereign, run: { ...runDetailSovereign.run, principal: 'hidden' } };

  it('lists and opens traces without a user column or principal', async () => {
    await open('/admin/insights?from=2026-10-09&to=2026-10-15&level=runs', 'u-public-visitor', {
      'GET /v1/reports/filters': filtersVisitor,
      'GET /v1/reports/runs': hiddenRuns,
      'GET /v1/reports/runs/*': hiddenDetail,
    });
    const traces = await screen.findByRole('table', { name: 'Requests matching the filters' });
    const headers = within(traces).getAllByRole('columnheader').map((cell) => cell.textContent);
    expect(headers).not.toContain('User');
    expect(within(traces).queryByText('hidden')).not.toBeInTheDocument();
    await userEvent.click(within(traces).getAllByRole('link', { name: /^Open trace / })[0] as HTMLElement);
    const detail = await screen.findByRole('region', { name: 'Trace detail' });
    expect(await within(detail).findByText('Not shown for your role')).toBeInTheDocument();
    expect(within(detail).queryByText('hidden')).not.toBeInTheDocument();
  });

  const METHOD =
    'Simulated pricing at rate card rc-2026-10-14. All 11 AiVa answers passed their checks; the baseline answers were not graded. ' +
    'The full savings waterfall is shown to administrators.';

  it('gives a visitor one savings figure: the frontier comparison with its method, never the waterfall', async () => {
    await open('/admin', 'u-public-visitor', { 'GET /v1/reports/filters': filtersVisitor });
    const comparison = await screen.findByRole('figure', { name: 'Against the frontier model' });
    expect(comparison).toHaveTextContent('The 11 requests that also asked Claude Opus 5.5 for a baseline answer');
    const bars = within(comparison).getAllByRole('definition');
    expect(bars[0]).toHaveTextContent(formatMicrosUsd(overview.baseline.baseline.value));
    expect(bars[1]).toHaveTextContent(formatMicrosUsd(overview.baseline.actual.value));
    expect(within(comparison).getAllByText('Simulated')).toHaveLength(2);
    // The in-country side has no per-token price, and replays carry no GPU estimate: said, not hidden.
    expect(bars[1]).toHaveTextContent('In-country model: no per-token price · GPU time not estimated on replay');
    expect(comparison).toHaveTextContent(METHOD);
    const tile = screen.getByRole('group', { name: 'Saved against the frontier model' });
    expect(tile).toHaveTextContent(formatMicrosUsd(overview.baseline.savings.value));
    expect(screen.queryByRole('group', { name: 'Estimated savings versus pinned baseline' })).not.toBeInTheDocument();
    expect(screen.queryByRole('figure', { name: /Savings waterfall/ })).not.toBeInTheDocument();
  });

  it('shows a visitor only the frontier comparison on the savings view', async () => {
    await open('/admin/insights/savings', 'u-public-visitor', { 'GET /v1/reports/filters': filtersVisitor });
    const comparison = await screen.findByRole('figure', { name: 'Against the frontier model' });
    expect(comparison).toHaveTextContent(METHOD);
    expect(screen.queryByRole('figure', { name: /Savings waterfall/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('table', { name: 'Savings waterfall steps' })).not.toBeInTheDocument();
    expect(screen.queryByRole('table', { name: 'Estimated savings components per application' })).not.toBeInTheDocument();
  });

  it('says there is nothing to compare when no request sent a baseline', async () => {
    const none = { ...overview, baseline: { ...overview.baseline, runs: 0 } };
    await open('/admin', 'u-public-visitor', { 'GET /v1/reports/filters': filtersVisitor, 'GET /v1/reports/overview': none });
    expect(
      await screen.findByText('No request in this window asked Claude Opus 5.5 for a baseline answer, so there is nothing to compare yet.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Saved against the frontier model' })).toHaveTextContent('No data yet');
  });
});
