import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import filters from '../test/fixtures/insights/filters.json';
import overview from '../test/fixtures/insights/overview.json';
import qualityApplication from '../test/fixtures/quality/qualityApplication.json';
import qualityContextProfile from '../test/fixtures/quality/qualityContextProfile.json';
import qualityEmpty from '../test/fixtures/quality/qualityEmpty.json';
import qualityExecutionPath from '../test/fixtures/quality/qualityExecutionPath.json';
import { json, signedInAs, type Route } from '../test/gateway';
import { renderRoute, sessionSettled } from '../test/renderRoute';
import { required } from '../test/required';

/**
 * Admin › Quality & Assurance on GET /v1/reports/quality. The reports are
 * recorded from a local gateway with Insights' endpoint (simulated inference,
 * synthetic traffic). A measured evidence coverage and Guard failures cannot
 * be produced by simulated runs, so those cases change only those fields of a
 * recorded report.
 */
function quality(byGroup: Record<string, unknown> = {}): Route {
  const reports: Record<string, unknown> = {
    application: qualityApplication,
    context_profile: qualityContextProfile,
    execution_path: qualityExecutionPath,
    ...byGroup,
  };
  return (_request, url) => json(reports[url.searchParams.get('group_by') ?? 'application']);
}

async function open(path = '/admin/quality?from=2026-10-10&to=2026-10-16', route: Route = quality()) {
  const gateway = signedInAs('u-finance', { 'GET /v1/reports/filters': filters, 'GET /v1/reports/quality': route });
  renderRoute(path);
  await sessionSettled();
  return gateway;
}

const tile = (name: string) => screen.getByRole('group', { name });

describe('Quality & Assurance', () => {
  it('shows the totals with their provenance, computed live, and coverage not measured yet', async () => {
    await open();

    await screen.findByRole('heading', { name: 'Quality at a glance' });
    expect(tile('Requests in scope')).toHaveTextContent('734');
    expect(tile('Requests in scope')).toHaveTextContent('Synthetic');
    expect(tile('Assurance checks passed')).toHaveTextContent('100%');
    expect(tile('Assurance checks passed')).toHaveTextContent('Simulated');
    expect(tile('Context sufficiency verified')).toHaveTextContent('100%');
    expect(tile('Context Guard passed')).toHaveTextContent('100%');
    // Not measured is never 0%, and carries no provenance badge since it is not a number.
    const coverage = tile('Evidence coverage');
    expect(coverage).toHaveTextContent('Not measured yet');
    expect(coverage).not.toHaveTextContent('0%');
    expect(coverage).not.toHaveTextContent('Simulated');
    expect(screen.getByText('Computed live')).toBeVisible();
    expect(screen.queryByText(/Never refreshed/i)).not.toBeInTheDocument();
  });

  it('switches the grouping and asks the gateway for it', async () => {
    const gateway = await open();

    const table = await screen.findByRole('table', { name: 'Quality by application' });
    expect(within(table).getAllByRole('row')).toHaveLength(qualityApplication.rows.length + 1);
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Group by' }), 'context_profile');

    const byProfile = await screen.findByRole('table', { name: 'Quality by context profile' });
    expect(within(byProfile).getAllByRole('row')).toHaveLength(qualityContextProfile.rows.length + 1);
    const last = gateway.requests.filter((r) => r.url.includes('/v1/reports/quality')).at(-1);
    expect(new URL(required(last).url).searchParams.get('group_by')).toBe('context_profile');
  });

  it('reads "Not available" for a rate with nothing to measure, never 0% or 100%', async () => {
    await open('/admin/quality?from=2026-10-10&to=2026-10-16&group=execution_path');

    const table = await screen.findByRole('table', { name: 'Quality by execution path' });
    // The cache-return row has no checked runs, no CIM and no Guard package: 0 / 0 everywhere.
    const row = required(within(table).getByText('Cache return').closest('tr'));
    expect(within(row).getAllByText('Not available')).toHaveLength(3);
    expect(within(row).queryByText('Simulated')).not.toBeInTheDocument();
    expect(within(row).getByText('Not measured yet')).toBeVisible();
  });

  it('says when a window has no requests rather than showing empty rates', async () => {
    await open('/admin/quality?from=2026-01-01&to=2026-01-02', quality({ application: qualityEmpty }));

    expect(await screen.findByRole('heading', { name: 'No requests in this window' })).toBeVisible();
    expect(screen.queryByRole('group', { name: 'Assurance checks passed' })).not.toBeInTheDocument();
  });

  it('shows a measured evidence coverage with its provenance, and Guard failures by kind without messages', async () => {
    const measured = {
      ...qualityApplication,
      totals: {
        ...qualityApplication.totals,
        evidence_coverage: {
          status: 'measured',
          measured_runs: 6,
          mean: 0.75,
          with_unresolved: { numerator: 1, denominator: 6, value: 1 / 6 },
          provenance: 'simulated',
        },
        guard: { ...qualityApplication.totals.guard, failures: [{ kind: 'dangling_citation', count: 2 }] },
      },
    };
    await open(undefined, quality({ application: measured }));

    await screen.findByRole('heading', { name: 'Quality at a glance' });
    expect(tile('Evidence coverage')).toHaveTextContent('75%');
    expect(tile('Evidence coverage')).toHaveTextContent('Simulated');
    const failures = screen.getByRole('table', { name: 'Failures by kind' });
    expect(within(failures).getByText('Citation with no passage')).toBeVisible();
    expect(within(failures).getByText('2')).toBeVisible();
  });

  it('is linked from the Overview’s quality pass rate', async () => {
    signedInAs('u-finance', { 'GET /v1/reports/overview': overview, 'GET /v1/reports/filters': filters });
    renderRoute('/admin?from=2026-10-10&to=2026-10-16');

    const link = await screen.findByRole('link', { name: 'See the quality pass rate in detail in Quality & Assurance' });
    await waitFor(() => expect(link.getAttribute('href')).toBe('/admin/quality?from=2026-10-10&to=2026-10-16'));
  });
});
