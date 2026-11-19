import createClient, { type Client, type Middleware } from 'openapi-fetch';
import { gatewayUnanswered, hfRateLimit, type HfRateLimit } from './browser';
import type { paths } from './schema';

export type { components, operations, paths } from './schema';
export {
  ApiError,
  clearToken,
  GATEWAY_PAUSED,
  GATEWAY_STARTING,
  hfRateLimit,
  isGatewayPaused,
  isGatewayStarting,
  isPlatformRateLimited,
  PLATFORM_RATE_LIMITED,
  readRuntimeConfig,
  readToken,
  retryAfterMinutes,
  toApiError,
  unwrap,
  writeToken,
  type ErrorEnvelopeBody,
  type HfRateLimit,
  type RuntimeConfig,
  type StoredToken,
} from './browser';

/**
 * Returns the signed-in user's or application's bearer credential, or nothing
 * when signed out. Provider API keys are gateway-side only and never reach the
 * browser, so they never pass through here.
 */
export type TokenProvider = () => string | null | undefined | Promise<string | null | undefined>;

export type AivaClient<Paths extends object = paths> = Client<Paths>;

export interface AivaClientOptions {
  /** Called when a request that carried a credential is rejected with 401 (expired or revoked). */
  onUnauthorized?: () => void;
  /**
   * Called after every response: with Hugging Face's own rate limit when its edge answered 429 (not an AiVa
   * service), else null, so the app can pause its polling until the limit resets.
   */
  onHfRateLimit?: (limit: HfRateLimit | null) => void;
  /**
   * Where the credential goes: `Authorization` (the default), or only `X-AiVa-Authorization`, for a gateway
   * reached through Hugging Face's edge, which must never see an AiVa token in `Authorization` (ADR-0025).
   */
  credentialHeader?: 'Authorization' | 'X-AiVa-Authorization';
  /**
   * The gateway is a Hugging Face Space reached directly (ADR-0025): a request that gets no readable answer
   * is taken as the Space starting (`GATEWAY_STARTING`), which views retry on their own.
   */
  unreachableMeansStarting?: boolean;
}

/** Typed client for the AiVa gateway API (types generated from its OpenAPI contract). */
export function createAivaClient<Paths extends object = paths>(
  baseUrl: string,
  getToken: TokenProvider,
  options: AivaClientOptions = {},
): AivaClient<Paths> {
  const client = createClient<Paths>({ baseUrl });
  client.use(bearerAuth(getToken, options.credentialHeader ?? 'Authorization', options.onUnauthorized));
  const { onHfRateLimit } = options;
  if (onHfRateLimit) client.use(hfRateLimits(onHfRateLimit));
  if (options.unreachableMeansStarting) client.use(unansweredIsStarting);
  return client;
}

function bearerAuth(getToken: TokenProvider, header: string, onUnauthorized?: () => void): Middleware {
  return {
    async onRequest({ request }) {
      const token = await getToken();
      if (token) request.headers.set(header, `Bearer ${token}`);
      return request;
    },
    onResponse({ request, response }) {
      if (response.status === 401 && request.headers.has(header)) onUnauthorized?.();
      return response;
    },
  };
}

const unansweredIsStarting: Middleware = {
  onError({ error }) {
    // An abort is the caller's own: it stays as it is.
    return error instanceof DOMException && error.name === 'AbortError' ? undefined : gatewayUnanswered();
  },
};

function hfRateLimits(report: (limit: HfRateLimit | null) => void): Middleware {
  return {
    async onResponse({ response }) {
      // Only a 429's body is read, from a copy: the caller still reads the response itself.
      report(response.status === 429 ? hfRateLimit(429, response.headers, await response.clone().text()) : null);
      return response;
    },
  };
}
