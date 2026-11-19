import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import devUsers from '../test/fixtures/devUsers.json';
import profiles from '../test/fixtures/profiles.json';
import scenarios from '../test/fixtures/scenarios.json';
import gatewayStarting from '../test/fixtures/signIn/gatewayStarting.json';
import publicSessionLimited from '../test/fixtures/signIn/publicSessionLimited.json';
import { devSignIn, errorEnvelope, hfRateLimited, json, mockGateway, signedInAs } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';
import { safeNextPath } from './permissions';
import { readToken } from './tokenStore';

const minted = { access_token: 'minted-token', token_type: 'Bearer', expires_at: '2099-01-01T00:00:00Z' };

describe('demo sign-in', () => {
  it('lists the gateway’s demo users with their roles, the tester preselected', async () => {
    mockGateway(devSignIn);
    renderRoute('/');

    const tester = await screen.findByRole('radio', { name: /AiVa Tester \(demo\)/ });
    expect(tester).toBeChecked();
    expect(screen.getAllByRole('radio')).toHaveLength(devUsers.length);
    expect(screen.getByRole('radio', { name: /Security Auditor \(demo\)/ })).not.toBeChecked();
  });

  it('signs in through the gateway and lands on the role’s home: the Test Console for the tester', async () => {
    const gateway = mockGateway({
      ...devSignIn,
      'POST /v1/auth/dev/token': minted,
      'GET /v1/auth/me': profiles['u-tester'],
      'GET /v1/catalog/scenarios': scenarios,
    });
    const { router } = renderRoute('/');

    await userEvent.click(await screen.findByRole('button', { name: 'Sign in as demo user' }));

    await waitFor(() => expect(router.state.location.pathname).toBe('/console'));
    // The sign-in button is gone, so focus moves to the main content instead of being lost.
    await waitFor(() => expect(document.getElementById('main')).toHaveFocus());
    const tokenRequest = gateway.requests.find((r) => r.url.endsWith('/v1/auth/dev/token'));
    expect(await tokenRequest?.json()).toEqual({ user_id: 'u-tester' });
    const me = gateway.requests.find((r) => r.url.endsWith('/v1/auth/me'));
    expect(me?.headers.get('X-AiVa-Authorization')).toBe('Bearer minted-token');
    expect(readToken()?.token).toBe('minted-token');
  });

  it('returns to the page that asked for sign-in', async () => {
    mockGateway({
      ...devSignIn,
      'POST /v1/auth/dev/token': minted,
      'GET /v1/auth/me': profiles['u-auditor'],
    });
    const { router } = renderRoute('/admin/audit?run=run_123');

    await waitFor(() => expect(router.state.location.pathname).toBe('/'));
    expect(router.state.location.search).toBe(`?next=${encodeURIComponent('/admin/audit?run=run_123')}`);
    await userEvent.click(await screen.findByRole('radio', { name: /Security Auditor/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Sign in as demo user' }));

    await waitFor(() => expect(router.state.location.pathname).toBe('/admin/audit'));
    expect(router.state.location.search).toBe('?run=run_123');
  });

  it('says so when demo sign-in is disabled on the gateway', async () => {
    mockGateway({
      ...devSignIn,
      'GET /v1/auth/dev/users': errorEnvelope(
        404,
        'DEV_SIGN_IN_DISABLED',
        'Demo sign-in is not enabled on this gateway.',
        'Sign in with single sign-on.',
      ),
    });
    renderRoute('/');

    expect(await screen.findByText('Demo sign-in is not enabled on this gateway.')).toBeVisible();
    expect(screen.getByText('Sign in with single sign-on.')).toBeVisible();
    expect(screen.queryByRole('radio')).not.toBeInTheDocument();
  });

  it('shows an explicit error when the gateway cannot be reached', async () => {
    mockGateway({
      'GET /v1/auth/sign-in-options': () => {
        throw new TypeError('Failed to fetch');
      },
    });
    renderRoute('/');

    expect(await screen.findByRole('alert')).toHaveTextContent('The AiVa gateway could not be reached.');
    expect(screen.getByRole('button', { name: 'Try again' })).toBeVisible();
  });

  it('reports a failed sign-in without leaving a token behind', async () => {
    mockGateway({
      ...devSignIn,
      'POST /v1/auth/dev/token': errorEnvelope(404, 'NOT_FOUND', 'Unknown demo user.'),
    });
    renderRoute('/');

    await userEvent.click(await screen.findByRole('button', { name: 'Sign in as demo user' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Unknown demo user.');
    expect(readToken()).toBeNull();
  });
});

describe('session lifecycle', () => {
  it('ends the session when the gateway rejects the token', async () => {
    signedInAs('u-tester', {
      'GET /v1/catalog/scenarios': errorEnvelope(401, 'AUTHENTICATION_REQUIRED', 'Invalid or missing credentials.'),
      ...devSignIn,
    });
    const { router } = renderRoute('/console');

    expect(await screen.findByText('Your session has ended. Sign in again to continue.')).toBeVisible();
    expect(router.state.location.pathname).toBe('/');
    expect(readToken()).toBeNull();
  });

  it('drops an expired stored token without calling the gateway', async () => {
    sessionStorage.setItem('aiva.session', JSON.stringify({ token: 'old', expiresAt: Date.now() - 1 }));
    const gateway = mockGateway(devSignIn);
    const { router } = renderRoute('/console');

    await waitFor(() => expect(router.state.location.pathname).toBe('/'));
    expect(gateway.requests.some((r) => r.url.endsWith('/v1/auth/me'))).toBe(false);
  });

  it('signs out and returns to the start page', async () => {
    signedInAs('u-tester', { 'GET /v1/catalog/scenarios': scenarios, ...devSignIn });
    const { router } = renderRoute('/console');

    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }));

    expect(await screen.findByText('You have signed out.')).toBeVisible();
    expect(router.state.location.pathname).toBe('/');
    expect(readToken()).toBeNull();
  });

  it('keeps the session but offers a retry when the gateway is unreachable at start', async () => {
    let up = false;
    signedInAs('u-tester', {
      'GET /v1/auth/me': () => {
        if (!up) throw new TypeError('Failed to fetch');
        return json(profiles['u-tester']);
      },
      'GET /v1/catalog/scenarios': scenarios,
    });
    renderRoute('/console');

    expect(await screen.findByText('Your session could not be verified')).toBeVisible();
    up = true;
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Scenario library' })).toBeVisible();
  });
});

describe('public visitor session', () => {
  /** A stored visitor token that expires within the renewal lead, so renewal starts at once. */
  function visitorDue(routes: Record<string, unknown>) {
    sessionStorage.setItem('aiva.session', JSON.stringify({ token: 'visitor-old', expiresAt: Date.now() + 30_000 }));
    return mockGateway({ 'GET /v1/auth/me': profiles['u-public-visitor'], 'GET /v1/catalog/scenarios': scenarios, ...routes });
  }

  it('renews quietly before the two-hour token expires and says once that a new session started', async () => {
    const gateway = visitorDue({ 'POST /v1/auth/public/session': json(minted, 201) });
    const { router } = renderRoute('/console');

    const notice = await screen.findByText(/Your two-hour visitor session ended, so a new one started/);
    expect(notice).toBeVisible();
    expect(router.state.location.pathname).toBe('/console');
    expect(readToken()).toEqual({ token: 'minted-token', expiresAt: Date.parse(minted.expires_at) });
    // The renewal carries the old token, which is still valid; the route does not need it.
    const renewal = gateway.requests.find((request) => request.url.endsWith('/v1/auth/public/session'));
    expect(renewal?.headers.get('X-AiVa-Authorization')).toBe('Bearer visitor-old');

    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByText(/Your two-hour visitor session ended/)).not.toBeInTheDocument();
  });

  it('keeps the current token while the new session is being started, so a reload cannot lose it', async () => {
    let finish: (response: Response) => void = () => undefined;
    const gateway = visitorDue({
      'POST /v1/auth/public/session': () => new Promise<Response>((resolve) => (finish = resolve)),
    });
    renderRoute('/console');

    await waitFor(() =>
      expect(gateway.requests.some((request) => request.url.endsWith('/v1/auth/public/session'))).toBe(true),
    );
    expect(readToken()?.token).toBe('visitor-old');
    finish(json(minted, 201));
    await waitFor(() => expect(readToken()?.token).toBe('minted-token'));
  });

  it('signs the visitor out when no new session can start', async () => {
    visitorDue({
      'POST /v1/auth/public/session': json(publicSessionLimited, 429),
      'GET /v1/auth/sign-in-options': { public: true, passcode: false, dev: false },
    });
    const { router } = renderRoute('/console');

    expect(await screen.findByText('Your session has ended. Sign in again to continue.')).toBeVisible();
    expect(router.state.location.pathname).toBe('/');
    expect(readToken()).toBeNull();
  });

  it('does not renew a visitor token the gateway rejects long before it expires', async () => {
    sessionStorage.setItem('aiva.session', JSON.stringify({ token: 'revoked', expiresAt: Date.now() + 3_600_000 }));
    const gateway = mockGateway({
      'GET /v1/auth/me': profiles['u-public-visitor'],
      'GET /v1/catalog/scenarios': errorEnvelope(401, 'AUTHENTICATION_REQUIRED', 'Invalid or missing credentials.'),
      'GET /v1/auth/sign-in-options': { public: true, passcode: false, dev: false },
    });
    renderRoute('/console');

    expect(await screen.findByText('Your session has ended. Sign in again to continue.')).toBeVisible();
    expect(gateway.requests.some((request) => request.url.endsWith('/v1/auth/public/session'))).toBe(false);
  });

  it('keeps a stored session while Hugging Face rate-limits the demo, and says so', async () => {
    signedInAs('u-tester', { 'GET /v1/auth/me': hfRateLimited(60), 'GET /v1/catalog/scenarios': scenarios });
    renderRoute('/console');

    expect(await screen.findByText('Your session could not be verified')).toBeVisible();
    expect(screen.getByText('The hosting platform is rate-limiting requests to the demo backend.')).toBeVisible();
    expect(readToken()?.token).toBe('test-token-u-tester');
  });

  it('keeps a stored session while the demo wakes up, then continues', async () => {
    let starting = true;
    signedInAs('u-tester', {
      'GET /v1/auth/me': () => (starting ? json(gatewayStarting, 503) : json(profiles['u-tester'])),
      'GET /v1/catalog/scenarios': scenarios,
    });
    renderRoute('/console');

    expect(await screen.findByText('The demo is waking up')).toBeVisible();
    expect(readToken()?.token).toBe('test-token-u-tester');
    starting = false;
    await userEvent.click(screen.getByRole('button', { name: 'Try again now' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Scenario library' })).toBeVisible();
  });
});

describe('a demo that is waking up', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows a view as waiting, not failed, and loads it on its own once the gateway answers', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let starting = true;
    const gateway = signedInAs('u-tester', {
      'GET /v1/catalog/scenarios': () => (starting ? json(gatewayStarting, 503) : json(scenarios)),
    });
    renderRoute('/console');

    expect(await screen.findByText('The demo is waking up')).toBeVisible();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    starting = false;
    await vi.advanceTimersByTimeAsync(30_000);

    expect(await screen.findByRole('table')).toBeVisible();
    expect(gateway.requests.filter((request) => request.url.endsWith('/v1/catalog/scenarios'))).toHaveLength(2);
  });

  it("takes Hugging Face's own 503 page as waking, and retries it on its own", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let starting = true;
    signedInAs('u-tester', {
      'GET /v1/catalog/scenarios': () =>
        starting ? new Response('<html>Starting</html>', { status: 503, headers: { 'Content-Type': 'text/html' } }) : json(scenarios),
    });
    renderRoute('/console');

    expect(await screen.findByText('The demo is waking up')).toBeVisible();
    starting = false;
    await vi.advanceTimersByTimeAsync(30_000);

    expect(await screen.findByRole('table')).toBeVisible();
  });

  it('says a paused demo is paused, and asks again only when told to', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let paused = true;
    const gateway = signedInAs('u-tester', {
      'GET /v1/catalog/scenarios': () =>
        paused
          ? new Response('The space is paused, ask a maintainer to restart it', { status: 503, headers: { 'Content-Type': 'text/html' } })
          : json(scenarios),
    });
    const reads = () => gateway.requests.filter((request) => request.url.endsWith('/v1/catalog/scenarios')).length;
    renderRoute('/console');

    expect(await screen.findByText('The demo is paused')).toBeVisible();
    expect(screen.queryByText('The demo is waking up')).not.toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    expect(reads()).toBe(1);

    paused = false;
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('table')).toBeVisible();
  });
});

describe('safeNextPath', () => {
  it.each([
    ['/admin/audit?run=1', '/admin/audit?run=1'],
    ['//evil.example', null],
    ['/\\evil.example', null],
    ['https://evil.example', null],
    [null, null],
  ])('%s → %s', (next, expected) => {
    expect(safeNextPath(next)).toBe(expected);
  });
});
