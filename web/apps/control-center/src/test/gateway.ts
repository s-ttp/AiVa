import { vi } from 'vitest';
import type { CallerProfile } from '../api/client';
import devUsers from './fixtures/devUsers.json';
import profiles from './fixtures/profiles.json';

/**
 * A fake AiVa gateway for component tests. Fixtures in ./fixtures are
 * responses recorded from a local gateway in simulated mode (synthetic data).
 */
export const TEST_GATEWAY_URL = 'http://gateway.test';

export type Route = (request: Request, url: URL) => Response | Promise<Response>;

export function json(body: unknown, status = 200): Response {
  return Response.json(body, { status });
}

export function errorEnvelope(status: number, code: string, message: string, remediation?: string): Response {
  return json({ error: { code, message, remediation, rule_ids: [], retryable: false, details: {} } }, status);
}

/** Hugging Face's own rate-limit page (its edge, not the gateway), with the reset its `RateLimit` header gives. */
export function hfRateLimited(resetSeconds?: number): Response {
  const page = '<!doctype html><html><body><h1>429</h1><p>We had to rate limit you. If you think it is an error, send us an email.</p></body></html>';
  const headers = new Headers({ 'Content-Type': 'text/html; charset=utf-8' });
  if (resetSeconds !== undefined) headers.set('RateLimit', `"api";r=0;t=${resetSeconds}`);
  return new Response(page, { status: 429, headers });
}

export interface MockGateway {
  /** Every request the UI sent, in order. */
  requests: Request[];
  set: (route: string, handler: Route | unknown) => void;
}

/**
 * Stubs fetch with routes keyed "METHOD /path" (path parameters as `*`). A
 * route is a handler, a Response (replayed) or a JSON body. Unknown routes
 * answer 404 so a missing mock fails loudly.
 */
export function mockGateway(routes: Record<string, Route | unknown> = {}): MockGateway {
  const table = new Map<string, Route | unknown>(Object.entries(routes));
  const requests: Request[] = [];
  const match = (method: string, pathname: string) => {
    for (const [key, handler] of table) {
      const [routeMethod, routePath] = key.split(' ');
      const pattern = new RegExp(`^${(routePath ?? '').replace(/\*/g, '[^/]+')}$`);
      if (routeMethod === method && pattern.test(pathname)) return handler;
    }
    return undefined;
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: Request) => {
      const request = input.clone();
      requests.push(request);
      const url = new URL(input.url);
      const handler = match(input.method, url.pathname);
      if (handler === undefined) return errorEnvelope(404, 'NOT_FOUND', `No mock for ${input.method} ${url.pathname}`);
      if (handler instanceof Response) return handler.clone();
      return typeof handler === 'function' ? (handler as Route)(input, url) : json(handler);
    }),
  );
  return { requests, set: (route, handler) => table.set(route, handler) };
}

/** A gateway that offers only the local dev sign-in, with its seeded demo users. */
export const devSignIn = {
  'GET /v1/auth/sign-in-options': { public: false, passcode: false, dev: true },
  'GET /v1/auth/dev/users': devUsers,
} as const;

export type DemoUserId = keyof typeof profiles;

/** Starts the test signed in: a stored token plus GET /v1/auth/me for the given demo user. */
export function signedInAs(user: DemoUserId, routes: Record<string, Route | unknown> = {}): MockGateway {
  sessionStorage.setItem('aiva.session', JSON.stringify({ token: `test-token-${user}`, expiresAt: Date.now() + 3_600_000 }));
  return mockGateway({ 'GET /v1/auth/me': profiles[user] as CallerProfile, ...routes });
}
