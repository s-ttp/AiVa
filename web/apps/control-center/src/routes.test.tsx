import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { listViews } from './navigation';
import scenarios from './test/fixtures/scenarios.json';
import filters from './test/fixtures/insights/filters.json';
import policyVersions from './test/fixtures/policyVersions.json';
import { devSignIn, mockGateway, signedInAs } from './test/gateway';
import { renderRoute, sessionSettled } from './test/renderRoute';
import { required } from './test/required';

describe('landing page', () => {
  it('presents the brand, the value proposition in three pillars, this week a link away, and the sign-in', async () => {
    mockGateway(devSignIn);
    renderRoute('/');

    expect(screen.getByRole('heading', { level: 1, name: 'AiVa' })).toBeVisible();
    expect(screen.getByText('AI Value Assurance')).toBeVisible();
    expect(screen.getByText('Enterprise AI Intelligence & Control Platform')).toBeVisible();
    expect(screen.getByText('Govern every request. Optimize every token. Prove the value.')).toBeVisible();
    const pillars = within(required(screen.getByRole('heading', { name: 'What AiVa delivers' }).parentElement));
    expect(pillars.getAllByRole('listitem').map((item) => item.querySelector('p')?.textContent)).toEqual([
      'Cut AI spend',
      'Keep sensitive data sovereign',
      'Prove the value',
    ]);
    expect(screen.queryByRole('link', { name: 'See what AiVa did this week' })).not.toBeInTheDocument();
    expect(await screen.findByRole('group', { name: 'Demo user' })).toBeVisible();
    // Simple (owner, 2026-10-22): no stories, no guide, and no fictional tenant's name.
    expect(screen.queryByRole('heading', { name: /Pick a story|Explore AiVa in three steps/ })).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/GCT|GulfConnect/);
  });

  it('offers single sign-on as a placeholder that does not pretend to work', async () => {
    mockGateway(devSignIn);
    renderRoute('/');

    const signIn = screen.getByRole('button', { name: 'Sign in with single sign-on' });
    expect(signIn).toHaveAttribute('aria-disabled', 'true');
    expect(signIn).toHaveAccessibleDescription(/not connected in this build/);
  });
});

describe('role-aware navigation', () => {
  const linksIn = (list: HTMLElement) =>
    within(list)
      .getAllByRole('link')
      .map((link) => link.textContent);

  it('shows a platform administrator the administration areas of LLD §22.5', async () => {
    signedInAs('u-platform-admin', { 'GET /v1/reports/overview': {}, 'GET /v1/reports/filters': filters });
    renderRoute('/admin/settings');
    const nav = await screen.findByRole('navigation', { name: 'Primary' });
    const list = (name: string) => within(nav).getByRole('list', { name });
    // The Users view appears once the reporting scope has been read and allows the user dimension.
    await within(nav).findByRole('link', { name: 'Users' });

    expect(linksIn(list('AiVa Test Console'))).toEqual(['Scenario library']);
    expect(
      Array.from(list('AiVa Admin Dashboard').children, (item) => item.firstElementChild?.textContent),
    ).toEqual(['Overview', 'AiVa Govern', 'AiVa Optimize', 'AiVa Insights', 'Quality & Assurance', 'Audit', 'Settings']);
    expect(linksIn(list('AiVa Govern'))).toEqual([
      'Applications & Identities',
      'Trusted Networks',
      'Policy Rules',
      'Model Allowlist',
      'Tool Permissions',
      'Budgets & Quotas',
      'Approval Queue',
    ]);
    expect(linksIn(list('AiVa Optimize'))).toEqual([
      'Routing Policies',
      'CIM Profiles',
      'Cache Policies',
      'Model Registry',
      'Benchmarks',
    ]);
    expect(linksIn(list('AiVa Insights'))).toEqual([
      'Consumption Overview',
      'Applications',
      'Users',
      'Prompt Categories',
      'Models',
      'Savings',
      'Quotas',
      'Export Center',
    ]);
  });

  it('shows a tester the Test Console, benchmarks, audit and settings only', async () => {
    signedInAs('u-tester', { 'GET /v1/catalog/scenarios': scenarios });
    renderRoute('/console');
    const nav = await screen.findByRole('navigation', { name: 'Primary' });

    expect(linksIn(nav)).toEqual([
      'Scenario library',
      'Run workspace',
      'Document copilot',
      'Agent workflows',
      'Benchmarks',
      'Audit',
      'Settings',
    ]);
    expect(within(nav).queryByRole('list', { name: 'AiVa Govern' })).not.toBeInTheDocument();
  });

  it('shows the signed-in user and roles beside the navigation', async () => {
    signedInAs('u-auditor', { 'GET /v1/catalog/scenarios': scenarios });
    renderRoute('/console');

    expect(await screen.findByText('Security Auditor (demo)')).toBeVisible();
    expect(screen.getByText('Security auditor')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeVisible();
  });

  it('explains instead of failing when a hidden view is opened directly', async () => {
    signedInAs('u-auditor');
    renderRoute('/console/run');

    expect(await screen.findByRole('heading', { level: 1, name: 'Run workspace' })).toBeVisible();
    expect(screen.getByText('You don’t have access to this view')).toBeVisible();
    expect(screen.getByText(/do not grant the “chat:test” permission/)).toBeVisible();
  });

  it('marks only the current view with aria-current', async () => {
    signedInAs('u-platform-admin', { 'GET /v1/admin/policies': policyVersions });
    renderRoute('/admin/govern/policy-rules');

    expect(await screen.findByRole('link', { name: 'Policy Rules' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Overview' })).not.toHaveAttribute('aria-current');
    // The page sets the title once its lazily loaded module renders.
    await waitFor(() => expect(document.title).toBe('Policy Rules · AiVa Control Center'));
  });
});

describe('every route', () => {
  const paths = ['/', ...listViews().map((view) => view.path), '/no-such-page'];

  it.each(paths)('%s shows the synthetic-data banner and one page heading', async (path) => {
    signedInAs('u-platform-admin', {
      ...devSignIn,
      'GET /v1/catalog/scenarios': scenarios,
      'GET /v1/admin/policies': policyVersions,
    });
    renderRoute(path);
    await sessionSettled();

    expect(screen.getByRole('main')).toBeInTheDocument();
    expect(screen.getByRole('note', { name: 'Synthetic demo data · Illustrative UI' })).toBeVisible();
    expect(await screen.findAllByRole('heading', { level: 1 })).toHaveLength(1);
  });
});

describe('Admin overview', () => {
  it('shows an explicit error, never placeholder figures, when the gateway has no report', async () => {
    signedInAs('u-platform-admin');
    renderRoute('/admin');

    expect(await screen.findByRole('alert')).toHaveTextContent('The overview could not be loaded');
    expect(screen.queryByRole('group', { name: 'Total requests' })).not.toBeInTheDocument();
  });
});
