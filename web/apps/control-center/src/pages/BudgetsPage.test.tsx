import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import budgetAlerts from '../test/fixtures/budgetAlerts.json';
import budgets from '../test/fixtures/budgets.json';
import { errorEnvelope, signedInAs } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';
import { required } from '../test/required';

const routes = { 'GET /v1/admin/budgets': budgets, 'GET /v1/admin/budgets/alerts': budgetAlerts };

/** The body row whose cells contain every given text. */
function rowWith(table: HTMLElement, ...texts: string[]): HTMLElement {
  return required(
    within(table)
      .getAllByRole('row')
      .find((row) => texts.every((text) => row.textContent?.includes(text))),
    `row with ${texts.join(', ')}`,
  );
}

describe('AiVa Govern budgets and quotas', () => {
  it('shows cap, spent, reserved, remaining and forecast with their provenance', async () => {
    signedInAs('u-finance', routes);
    renderRoute('/admin/govern/budgets-quotas');

    const table = await screen.findByRole('table', { name: 'Budgets with live status' });
    const daily = rowWith(table, 'gct-support-chatbot', 'Daily');
    expect(daily).toHaveTextContent('$0.003'); // cap 3,000 micros
    expect(daily).toHaveTextContent('$0.002668'); // spent
    expect(daily).toHaveTextContent('$0.000332'); // remaining
    expect(daily).toHaveTextContent('$0.006066'); // forecast
    expect(within(daily).getAllByText('Simulated').length).toBeGreaterThan(0);
    expect(within(daily).getByText('Estimated')).toBeInTheDocument();
    expect(daily).toHaveTextContent('88% of cap spent');
    expect(daily).toHaveTextContent('Alerts at 80%, 95%');
    expect(daily).toHaveTextContent('80% reached');
    // No spend yet: zero is a measured fact, not a simulated one.
    expect(rowWith(table, 'gct-coding-assistant')).toHaveTextContent('Measured');
  });

  it('shows per-request ceilings without a running total', async () => {
    signedInAs('u-finance', routes);
    renderRoute('/admin/govern/budgets-quotas');

    const table = await screen.findByRole('table', { name: 'Budgets with live status' });
    const perRequest = rowWith(table, 'Per request', 'default');
    expect(perRequest).toHaveTextContent('Ceiling applied to each request or workflow; no running total');
    expect(perRequest).not.toHaveTextContent('% of cap spent');
  });

  it('lists FinOps alerts with the budget, amount and provenance', async () => {
    signedInAs('u-finance', routes);
    renderRoute('/admin/govern/budgets-quotas');

    const table = await screen.findByRole('table', { name: 'FinOps alerts, newest first' });
    const row = within(table).getAllByRole('row')[1];
    expect(row).toHaveTextContent('Hard cap reached');
    expect(row).toHaveTextContent('bud-application-gct-support-chatbot-daily');
    expect(row).toHaveTextContent('$0.002668 of $0.003');
    expect(row).toHaveTextContent('Estimated');
    expect(row).toHaveTextContent('14 Oct 2026');
  });

  it('shows explicit empty and error states', async () => {
    signedInAs('u-finance', {
      'GET /v1/admin/budgets': errorEnvelope(503, 'DEPENDENCY_UNAVAILABLE', 'The quota store is unavailable.'),
      'GET /v1/admin/budgets/alerts': { alerts: [] },
    });
    renderRoute('/admin/govern/budgets-quotas');

    expect(await screen.findByRole('alert')).toHaveTextContent('The quota store is unavailable.');
    expect(await screen.findByText('No alerts')).toBeVisible();
  });
});
