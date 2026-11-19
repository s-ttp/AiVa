import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { readToken } from '../auth/tokenStore';
import devUsers from '../test/fixtures/devUsers.json';
import profiles from '../test/fixtures/profiles.json';
import scenarios from '../test/fixtures/scenarios.json';
import demoSignInFailed from '../test/fixtures/signIn/demoSignInFailed.json';
import demoSignInLocked from '../test/fixtures/signIn/demoSignInLocked.json';
import gatewayStarting from '../test/fixtures/signIn/gatewayStarting.json';
import publicSessionLimited from '../test/fixtures/signIn/publicSessionLimited.json';
import { errorEnvelope, json, mockGateway } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';

/** What the public demo's gateway offers: visitor sessions and the passcode sign-in, never dev sign-in. */
const publicDemo = { public: true, passcode: true, dev: false };
const minted = { access_token: 'minted-token', token_type: 'Bearer', expires_at: '2099-01-01T00:00:00Z' };

async function signInPanel() {
  const heading = await screen.findByRole('heading', { level: 2, name: 'Sign in' });
  return within(heading.closest('section') as HTMLElement);
}

describe('sign-in options', () => {
  it('offers the visitor session and the passcode sign-in, and no dev sign-in, on the public demo', async () => {
    const gateway = mockGateway({ 'GET /v1/auth/sign-in-options': publicDemo, 'GET /v1/auth/demo/users': devUsers });
    renderRoute('/');

    const panel = await signInPanel();
    expect(await panel.findByRole('button', { name: 'Explore as a visitor' })).toBeVisible();
    expect(await panel.findByRole('combobox', { name: 'Demo user' })).toHaveDisplayValue(
      'AiVa Tester (demo) · Tester',
    );
    expect(panel.getByLabelText('Passcode')).toHaveAttribute('type', 'password');
    expect(panel.queryByRole('heading', { name: 'Developer sign-in (local gateway)' })).not.toBeInTheDocument();
    expect(panel.queryByRole('radio')).not.toBeInTheDocument();
    expect(gateway.requests.some((request) => request.url.includes('/v1/auth/dev/'))).toBe(false);
  });

  it('shows the dev sign-in only when the gateway lists it', async () => {
    mockGateway({
      'GET /v1/auth/sign-in-options': { public: false, passcode: false, dev: true },
      'GET /v1/auth/dev/users': devUsers,
    });
    renderRoute('/');

    const panel = await signInPanel();
    expect(await panel.findByRole('heading', { name: 'Developer sign-in (local gateway)' })).toBeVisible();
    expect(await panel.findAllByRole('radio')).toHaveLength(devUsers.length);
    expect(panel.queryByRole('button', { name: 'Explore as a visitor' })).not.toBeInTheDocument();
    expect(panel.queryByLabelText('Passcode')).not.toBeInTheDocument();
  });

  it('says so when the gateway offers no demo sign-in', async () => {
    mockGateway({ 'GET /v1/auth/sign-in-options': { public: false, passcode: false, dev: false } });
    renderRoute('/');

    expect(await screen.findByText(/This gateway offers no demo sign-in/)).toBeVisible();
  });

  it('says the demo is waking up while its gateway starts, and tries again', async () => {
    let starting = true;
    const gateway = mockGateway({
      'GET /v1/auth/sign-in-options': () =>
        starting ? json(gatewayStarting, 503) : json({ public: true, passcode: false, dev: false }),
    });
    renderRoute('/');

    const panel = await signInPanel();
    const waking = await panel.findByRole('status');
    expect(within(waking).getByText('The demo is waking up')).toBeVisible();
    expect(panel.queryByRole('alert')).not.toBeInTheDocument();
    starting = false;
    await userEvent.click(within(waking).getByRole('button', { name: 'Try again now' }));

    expect(await panel.findByRole('button', { name: 'Explore as a visitor' })).toBeVisible();
    expect(gateway.requests.filter((request) => request.url.endsWith('/v1/auth/sign-in-options'))).toHaveLength(2);
  });
});

describe('visitor session', () => {
  it('starts a two-hour visitor session and lands on the first view the visitor may open', async () => {
    const gateway = mockGateway({
      'GET /v1/auth/sign-in-options': { public: true, passcode: false, dev: false },
      'POST /v1/auth/public/session': json(minted, 201),
      'GET /v1/auth/me': profiles['u-public-visitor'],
      'GET /v1/catalog/scenarios': scenarios,
    });
    const { router } = renderRoute('/');

    await userEvent.click(await screen.findByRole('button', { name: 'Explore as a visitor' }));

    await waitFor(() => expect(router.state.location.pathname).toBe('/console'));
    expect(readToken()?.token).toBe('minted-token');
    const session = gateway.requests.find((request) => request.url.endsWith('/v1/auth/public/session'));
    expect(session?.headers.get('X-AiVa-Authorization')).toBeNull();
    expect(await screen.findByText('Public visitor')).toBeVisible();
  });

  it('gives the time until a new visitor session may start when the limit is reached', async () => {
    mockGateway({
      'GET /v1/auth/sign-in-options': publicDemo,
      'GET /v1/auth/demo/users': devUsers,
      'POST /v1/auth/public/session': json(publicSessionLimited, 429),
    });
    renderRoute('/');

    await userEvent.click(await screen.findByRole('button', { name: 'Explore as a visitor' }));

    // retry_after_seconds 2926 rounds up to 49 minutes.
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Too many visitor sessions were started recently. Try again in 49 minutes.',
    );
    expect(readToken()).toBeNull();
  });

  it('explains a gateway that issues no visitor sessions', async () => {
    mockGateway({
      'GET /v1/auth/sign-in-options': { public: true, passcode: false, dev: false },
      'POST /v1/auth/public/session': errorEnvelope(
        403,
        'PUBLIC_SESSION_UNAVAILABLE',
        'This gateway has no public demo visitor.',
      ),
    });
    renderRoute('/');

    await userEvent.click(await screen.findByRole('button', { name: 'Explore as a visitor' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Visitor sessions are not available on this gateway. Sign in instead.',
    );
  });
});

describe('passcode sign-in', () => {
  it('signs a demo user in with the passcode and returns to the page that asked', async () => {
    const gateway = mockGateway({
      'GET /v1/auth/sign-in-options': publicDemo,
      'GET /v1/auth/demo/users': devUsers,
      'POST /v1/auth/demo/token': minted,
      'GET /v1/auth/me': profiles['u-auditor'],
    });
    const { router } = renderRoute('/admin/audit?run=run_123');

    const panel = await signInPanel();
    await userEvent.selectOptions(await panel.findByRole('combobox', { name: 'Demo user' }), 'u-auditor');
    await userEvent.type(panel.getByLabelText('Passcode'), 'synthetic-passcode');
    await userEvent.click(panel.getByRole('button', { name: 'Sign in with passcode' }));

    await waitFor(() => expect(router.state.location.pathname).toBe('/admin/audit'));
    const tokenRequest = gateway.requests.find((request) => request.url.endsWith('/v1/auth/demo/token'));
    expect(await tokenRequest?.json()).toEqual({ user_id: 'u-auditor', passcode: 'synthetic-passcode' });
  });

  it('marks a wrong passcode and leaves no token behind', async () => {
    mockGateway({
      'GET /v1/auth/sign-in-options': publicDemo,
      'GET /v1/auth/demo/users': devUsers,
      'POST /v1/auth/demo/token': json(demoSignInFailed, 401),
    });
    renderRoute('/');

    const panel = await signInPanel();
    const passcode = await panel.findByLabelText('Passcode');
    await userEvent.type(passcode, 'wrong');
    await userEvent.click(panel.getByRole('button', { name: 'Sign in with passcode' }));

    const alert = await panel.findByRole('alert');
    expect(alert).toHaveTextContent('The passcode is not correct. Check it with the demo owner.');
    expect(passcode).toHaveAttribute('aria-invalid', 'true');
    expect(passcode).toHaveAccessibleDescription('The passcode is not correct. Check it with the demo owner.');
    expect(readToken()).toBeNull();
  });

  it('gives the time until the next attempt after a lockout', async () => {
    mockGateway({
      'GET /v1/auth/sign-in-options': publicDemo,
      'GET /v1/auth/demo/users': devUsers,
      'POST /v1/auth/demo/token': json(demoSignInLocked, 429),
    });
    renderRoute('/');

    const panel = await signInPanel();
    await userEvent.type(await panel.findByLabelText('Passcode'), 'another-guess');
    await userEvent.click(panel.getByRole('button', { name: 'Sign in with passcode' }));

    expect(await panel.findByRole('alert')).toHaveTextContent('Too many sign-in attempts. Try again in 15 minutes.');
    expect(panel.getByLabelText('Passcode')).not.toHaveAttribute('aria-invalid');
  });
});
