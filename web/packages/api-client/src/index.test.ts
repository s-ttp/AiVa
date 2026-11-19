import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ApiError,
  createAivaClient,
  GATEWAY_PAUSED,
  GATEWAY_STARTING,
  isGatewayPaused,
  isGatewayStarting,
  PLATFORM_RATE_LIMITED,
  retryAfterMinutes,
  unwrap,
} from './index';

interface TestPaths {
  '/v1/runs/{id}': {
    get: {
      parameters: { path: { id: string } };
      responses: { 200: { content: { 'application/json': { run_id: string } } } };
    };
  };
}

function stubFetch(status = 200) {
  const fetchMock = vi.fn<(request: Request) => Promise<Response>>(async () =>
    Response.json({ run_id: 'run-1' }, { status }),
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function sentRequest(fetchMock: ReturnType<typeof stubFetch>): Request {
  const request = fetchMock.mock.lastCall?.[0];
  if (!request) throw new Error('fetch was not called');
  return request;
}

describe('createAivaClient', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('sends the bearer token to the gateway base URL', async () => {
    const fetchMock = stubFetch();
    const client = createAivaClient<TestPaths>('https://gateway.example', async () => 'app-token');

    const { data } = await client.GET('/v1/runs/{id}', { params: { path: { id: 'run-1' } } });

    expect(data).toEqual({ run_id: 'run-1' });
    expect(sentRequest(fetchMock).url).toBe('https://gateway.example/v1/runs/run-1');
    expect(sentRequest(fetchMock).headers.get('Authorization')).toBe('Bearer app-token');
  });

  it('omits the Authorization header when signed out', async () => {
    const fetchMock = stubFetch();
    const client = createAivaClient<TestPaths>('https://gateway.example', () => null);

    await client.GET('/v1/runs/{id}', { params: { path: { id: 'run-1' } } });

    expect(sentRequest(fetchMock).headers.has('Authorization')).toBe(false);
  });

  it('reports a rejected credential so the caller can end the session', async () => {
    stubFetch(401);
    const onUnauthorized = vi.fn();
    const client = createAivaClient<TestPaths>('https://gateway.example', () => 'expired', { onUnauthorized });

    await client.GET('/v1/runs/{id}', { params: { path: { id: 'run-1' } } });

    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it('does not report 401 for requests sent without a credential', async () => {
    stubFetch(401);
    const onUnauthorized = vi.fn();
    const client = createAivaClient<TestPaths>('https://gateway.example', () => null, { onUnauthorized });

    await client.GET('/v1/runs/{id}', { params: { path: { id: 'run-1' } } });

    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});

/** Hugging Face's own rate-limit page, as its edge answers when the demo makes too many requests. */
const HF_PAGE = '<!doctype html><html><body><h1>429</h1><p>We had to rate limit you. If you think it is an error, send us an email.</p></body></html>';
/** nginx's own 429 page: the platform proxy's per-visitor limit. */
const NGINX_PAGE = '<html>\r\n<head><title>429 Too Many Requests</title></head>\r\n<body>\r\n<center><h1>429 Too Many Requests</h1></center>\r\n<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n';

describe('Hugging Face rate limits', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function answer(response: Response) {
    vi.stubGlobal('fetch', vi.fn(async () => response));
    const onHfRateLimit = vi.fn();
    const client = createAivaClient<TestPaths>('https://gateway.example', () => 'token', { onHfRateLimit });
    return { onHfRateLimit, call: () => unwrap(client.GET('/v1/runs/{id}', { params: { path: { id: 'run-1' } } })) };
  }

  it('reports its HTML 429 with the reset the RateLimit header gives, and names it in the error', async () => {
    const { onHfRateLimit, call } = answer(
      new Response(HF_PAGE, { status: 429, headers: { 'Content-Type': 'text/html; charset=utf-8', RateLimit: '"api";r=0;t=42', 'Retry-After': '300' } }),
    );

    const error = await call().catch((cause: unknown) => cause);

    expect(onHfRateLimit).toHaveBeenCalledWith({ resetSeconds: 42 });
    expect(error).toMatchObject({ status: 429, code: PLATFORM_RATE_LIMITED });
    expect(retryAfterMinutes(error as never)).toBe(1);
  });

  it('falls back to Retry-After, and says nothing when neither is sent (or, cross-origin, exposed)', async () => {
    const seconds = answer(new Response(HF_PAGE, { status: 429, headers: { 'Content-Type': 'text/plain', 'Retry-After': '120' } }));
    await seconds.call().catch(() => undefined);
    expect(seconds.onHfRateLimit).toHaveBeenCalledWith({ resetSeconds: 120 });

    const unsaid = answer(new Response(HF_PAGE, { status: 429, headers: { 'Content-Type': 'text/html' } }));
    await unsaid.call().catch(() => undefined);
    expect(unsaid.onHfRateLimit).toHaveBeenCalledWith({ resetSeconds: null });
  });

  it("leaves the gateway's JSON 429 and the proxy's own 429 as they were", async () => {
    const envelope = { error: { code: 'PUBLIC_SESSION_LIMITED', message: 'Too many sessions.', details: { retry_after_seconds: 600 } } };
    const gateway = answer(Response.json(envelope, { status: 429, headers: { 'Retry-After': '600' } }));
    const error = await gateway.call().catch((cause: unknown) => cause);
    expect(gateway.onHfRateLimit).toHaveBeenCalledWith(null);
    expect(error).toMatchObject({ status: 429, code: 'PUBLIC_SESSION_LIMITED', message: 'Too many sessions.' });

    const proxy = answer(new Response(NGINX_PAGE, { status: 429, headers: { 'Content-Type': 'text/html' } }));
    const refused = await proxy.call().catch((cause: unknown) => cause);
    expect(proxy.onHfRateLimit).toHaveBeenCalledWith(null);
    expect(refused).toMatchObject({ status: 429, code: null, message: 'The gateway responded with HTTP 429.' });
  });

  it('reports every other answer as no limit, and still hands the body to the caller', async () => {
    const { onHfRateLimit, call } = answer(Response.json({ run_id: 'run-1' }));

    await expect(call()).resolves.toEqual({ run_id: 'run-1' });
    expect(onHfRateLimit).toHaveBeenCalledWith(null);
  });
});

describe('a gateway reached directly on Hugging Face (ADR-0025)', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const get = (client: ReturnType<typeof createAivaClient<TestPaths>>, signal?: AbortSignal) =>
    unwrap(client.GET('/v1/runs/{id}', { params: { path: { id: 'run-1' } }, signal }));

  it('sends the credential only in X-AiVa-Authorization, and reports its rejection', async () => {
    const fetchMock = stubFetch(401);
    const onUnauthorized = vi.fn();
    const client = createAivaClient<TestPaths>('https://gateway.example', () => 'token', {
      credentialHeader: 'X-AiVa-Authorization',
      onUnauthorized,
    });

    await client.GET('/v1/runs/{id}', { params: { path: { id: 'run-1' } } });

    expect(sentRequest(fetchMock).headers.get('X-AiVa-Authorization')).toBe('Bearer token');
    expect(sentRequest(fetchMock).headers.has('Authorization')).toBe(false);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("takes Hugging Face's own 503 page as the Space starting, with its Retry-After", async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>Space is paused</html>', { status: 503, headers: { 'Content-Type': 'text/html', 'Retry-After': '30' } })));
    const client = createAivaClient<TestPaths>('https://gateway.example', () => 'token');

    const error = (await get(client).catch((cause: unknown) => cause)) as ApiError;

    expect(isGatewayStarting(error)).toBe(true);
    expect(error).toMatchObject({ status: 503, details: { retry_after_seconds: 30 } });
  });

  it("takes Hugging Face's paused page as paused, not starting", async () => {
    const page = 'The space is paused, ask a maintainer to restart it';
    vi.stubGlobal('fetch', vi.fn(async () => new Response(page, { status: 503, headers: { 'Content-Type': 'text/html' } })));
    const client = createAivaClient<TestPaths>('https://gateway.example', () => 'token');

    const error = (await get(client).catch((cause: unknown) => cause)) as ApiError;

    expect(isGatewayPaused(error)).toBe(true);
    expect(isGatewayStarting(error)).toBe(false);
    expect(error).toMatchObject({ status: 503, code: GATEWAY_PAUSED });
  });

  it("keeps the gateway's own JSON 503s as they are", async () => {
    const envelope = { error: { code: 'SOVEREIGN_UNAVAILABLE', message: 'The sovereign model is cold.', details: {} } };
    vi.stubGlobal('fetch', vi.fn(async () => Response.json(envelope, { status: 503 })));
    const client = createAivaClient<TestPaths>('https://gateway.example', () => 'token');

    await expect(get(client)).rejects.toMatchObject({ status: 503, code: 'SOVEREIGN_UNAVAILABLE' });
  });

  it('takes no readable answer as the Space starting only when told to, and leaves an abort alone', async () => {
    vi.stubGlobal('fetch', vi.fn(async (request: Request) => {
      if (request.signal.aborted) throw new DOMException('Aborted', 'AbortError');
      throw new TypeError('Failed to fetch');
    }));
    const direct = createAivaClient<TestPaths>('https://gateway.example', () => 'token', { unreachableMeansStarting: true });
    const local = createAivaClient<TestPaths>('https://gateway.example', () => 'token');

    await expect(get(direct)).rejects.toMatchObject({ kind: 'network', code: GATEWAY_STARTING });
    await expect(get(local)).rejects.toMatchObject({ kind: 'network', code: null, message: 'The AiVa gateway could not be reached.' });
    const controller = new AbortController();
    controller.abort();
    await expect(get(direct, controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
  });
});
