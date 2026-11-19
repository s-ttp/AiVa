import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import consumptionApplication from '../test/fixtures/insights/consumptionApplication.json';
import consumptionDepartment from '../test/fixtures/insights/consumptionDepartment.json';
import filtersVisitor from '../test/fixtures/demo/filtersVisitor.json';
import scenarios from '../test/fixtures/scenarios.json';
import { errorEnvelope, json, signedInAs, type Route } from '../test/gateway';
import { renderRoute, sessionSettled } from '../test/renderRoute';

/**
 * What the public demo's visitor sees (ADR-0019): its permissions hide
 * approvals, budgets, exports and administration, and its reporting scope
 * (`users_visible: false`, recorded) withholds the user dimension.
 */
function consumption(): Route {
  return (_request, url) => {
    const group = url.searchParams.get('group_by');
    if (group === 'department') return json(consumptionDepartment);
    if (group === 'application') return json(consumptionApplication);
    return errorEnvelope(404, 'NOT_FOUND', `No fixture for group_by=${group ?? ''}`);
  };
}

async function asVisitor(path: string) {
  const gateway = signedInAs('u-public-visitor', {
    'GET /v1/catalog/scenarios': scenarios,
    'GET /v1/reports/filters': filtersVisitor,
    'GET /v1/reports/consumption': consumption(),
  });
  renderRoute(path);
  await sessionSettled();
  return gateway;
}

describe('the public visitor', () => {
  it('sees the console and reports, but no approvals, budgets, exports or user views', async () => {
    await asVisitor('/console');

    const nav = await screen.findByRole('navigation', { name: 'Primary' });
    const links = within(nav).getAllByRole('link').map((link) => link.textContent);
    expect(links).toEqual(expect.arrayContaining(['Run workspace', 'Consumption Overview', 'Savings']));
    for (const hidden of ['Approval Queue', 'Budgets & Quotas', 'Quotas', 'Export Center', 'Users', 'Benchmarks']) {
      expect(links).not.toContain(hidden);
    }
  });

  it('is told the user dimension is not shown when opening the users view directly', async () => {
    const gateway = await asVisitor('/admin/insights/users');

    expect(await screen.findByRole('heading', { level: 1, name: 'Users' })).toBeVisible();
    // The page waits for the reporting scope, then says the dimension is not shown.
    expect(await screen.findByText('Not shown for your role')).toBeVisible();
    expect(gateway.requests.some((r) => r.url.includes('group_by=user'))).toBe(false);
  });

  it('gets no user filter and drills from an application straight to prompt categories', async () => {
    const gateway = await asVisitor('/admin/insights?from=2026-10-09&to=2026-10-15&level=application');

    const table = await screen.findByRole('table', { name: 'Consumption By application' });
    expect(screen.queryByRole('combobox', { name: 'User (pseudonym)' })).not.toBeInTheDocument();
    const [first] = within(table).getAllByRole('button', { name: /^Drill down into / });
    await userEvent.click(first as HTMLElement);

    await waitFor(() => expect(gateway.requests.some((r) => r.url.includes('group_by=category'))).toBe(true));
    expect(gateway.requests.some((r) => r.url.includes('group_by=user'))).toBe(false);
  });

  it('links each application to the explorer at the category level', async () => {
    await asVisitor('/admin/insights/applications');

    const table = await screen.findByRole('table', { name: 'Consumption per application' });
    const [link] = within(table).getAllByRole('link');
    expect(link?.getAttribute('href')).toContain('level=category');
  });

  it('follows the gateway’s reporting scope, not the role', async () => {
    signedInAs('u-finance', { 'GET /v1/reports/filters': filtersVisitor });
    renderRoute('/admin/insights/users');
    await sessionSettled();

    // Only a scope that was read (users_visible: false) gives this answer; an unknown scope keeps loading.
    expect(await screen.findByText('Not shown for your role')).toBeVisible();
    const nav = screen.getByRole('navigation', { name: 'Primary' });
    expect(within(nav).queryByRole('link', { name: 'Users' })).not.toBeInTheDocument();
    expect(within(nav).getByRole('link', { name: 'Export Center' })).toBeVisible();
  });

  it('shows no user dimension while the reporting scope is still unknown', async () => {
    signedInAs('u-finance', { 'GET /v1/reports/filters': () => new Promise<Response>(() => undefined) });
    renderRoute('/admin/insights/users');
    await sessionSettled();

    const nav = await screen.findByRole('navigation', { name: 'Primary' });
    expect(within(nav).getByRole('link', { name: 'Consumption Overview' })).toBeVisible();
    expect(within(nav).queryByRole('link', { name: 'Users' })).not.toBeInTheDocument();
    expect(await screen.findByText('Checking your reporting scope…')).toBeVisible();
    expect(screen.queryByText('Not shown for your role')).not.toBeInTheDocument();
  });

  it('keeps the user dimension hidden from a visitor whose reporting scope cannot be read', async () => {
    const gateway = signedInAs('u-public-visitor', {
      'GET /v1/reports/filters': errorEnvelope(500, 'INTERNAL_ERROR', 'Synthetic failure.'),
    });
    renderRoute('/admin/insights/users');
    await sessionSettled();

    expect(await screen.findByText('Not shown for your role')).toBeVisible();
    const nav = screen.getByRole('navigation', { name: 'Primary' });
    expect(within(nav).queryByRole('link', { name: 'Users' })).not.toBeInTheDocument();
    expect(gateway.requests.some((r) => r.url.includes('group_by=user'))).toBe(false);
  });

  it('gives other roles the contract default when the reporting scope cannot be read', async () => {
    signedInAs('u-finance', {
      'GET /v1/reports/filters': errorEnvelope(500, 'INTERNAL_ERROR', 'Synthetic failure.'),
    });
    renderRoute('/console');
    await sessionSettled();

    const nav = await screen.findByRole('navigation', { name: 'Primary' });
    expect(await within(nav).findByRole('link', { name: 'Users' })).toBeVisible();
  });

  it('never asks for the user level when a visitor deep-links to it', async () => {
    const gateway = await asVisitor('/admin/insights?from=2026-10-09&to=2026-10-15&level=user');

    expect(await screen.findByRole('table', { name: 'Consumption By department' })).toBeVisible();
    expect(gateway.requests.some((r) => r.url.includes('group_by=user'))).toBe(false);
  });
});
