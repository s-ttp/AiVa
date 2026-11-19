/**
 * Browser plumbing shared by the AiVa web apps: the gateway's error envelope,
 * the runtime configuration and the per-tab bearer-token store. The Context
 * Lab answers with the same error envelope as the gateway.
 */

/** The `{ error: {...} }` body every AiVa service returns on failure. */
export interface ErrorEnvelopeBody {
  code: string;
  message: string;
  remediation?: string | null;
  rule_ids?: string[];
  details?: Record<string, unknown>;
}

/** A failed API call, normalised from the error envelope (or the lack of a response). */
export class ApiError extends Error {
  readonly kind: 'http' | 'network';
  readonly status: number | null;
  readonly code: string | null;
  readonly remediation: string | null;
  readonly ruleIds: readonly string[];
  /** Machine-readable specifics from the error envelope, e.g. validation errors. */
  readonly details: Readonly<Record<string, unknown>>;

  constructor(init: {
    kind: 'http' | 'network';
    message: string;
    status?: number;
    code?: string;
    remediation?: string | null;
    ruleIds?: readonly string[];
    details?: Readonly<Record<string, unknown>>;
  }) {
    super(init.message);
    this.name = 'ApiError';
    this.kind = init.kind;
    this.status = init.status ?? null;
    this.code = init.code ?? null;
    this.remediation = init.remediation ?? null;
    this.ruleIds = init.ruleIds ?? [];
    this.details = init.details ?? {};
  }
}

export const GATEWAY_STARTING = 'GATEWAY_STARTING';

/**
 * The public demo's gateway Space is asleep or waking: its proxy or front door answered 503 GATEWAY_STARTING
 * (deploy/spaces/shared/gateway-proxy.sh, ADR-0019), or, reached directly (ADR-0025), Hugging Face answered
 * its own 503 page or no readable answer at all.
 */
export function isGatewayStarting(error: ApiError): boolean {
  return error.code === GATEWAY_STARTING;
}

export const GATEWAY_PAUSED = 'GATEWAY_PAUSED';
const PAUSED_TEXT = /the space is paused/i;

/**
 * The demo's gateway Space is paused on Hugging Face: unlike a sleeping one it does not wake on its own, so
 * nothing retries it until someone restarts it.
 */
export function isGatewayPaused(error: ApiError): boolean {
  return error.code === GATEWAY_PAUSED;
}

/**
 * A gateway reached directly on Hugging Face (ADR-0025) gave no readable answer: the fetch failed, as it does
 * when the platform answers without CORS headers while the Space sleeps or wakes. Taken as starting.
 */
export function gatewayUnanswered(): ApiError {
  return new ApiError({
    kind: 'network',
    code: GATEWAY_STARTING,
    message: 'The demo backend is not answering yet; it may be starting.',
    remediation: 'Retry in a minute.',
  });
}

/** Whole minutes until a rate-limited request may be retried (`retry_after_seconds`), at least one. */
export function retryAfterMinutes(error: ApiError): number | null {
  const seconds = error.details.retry_after_seconds;
  return typeof seconds === 'number' && Number.isFinite(seconds) ? Math.max(1, Math.ceil(seconds / 60)) : null;
}

/**
 * The code a client error carries for Hugging Face's own HTTP 429: the hosting platform refusing the hop to the
 * demo backend, not an AiVa service's limit.
 */
export const PLATFORM_RATE_LIMITED = 'PLATFORM_RATE_LIMITED';

/** Hugging Face's own rate limit on the demo. */
export interface HfRateLimit {
  /** Seconds until the limit resets: the `RateLimit` header's `t=`, else `Retry-After`; null when neither says. */
  resetSeconds: number | null;
}

const HF_LIMIT_TEXT = 'We had to rate limit you';
/** nginx's own error page (the platform proxy's limit before it answered JSON): never Hugging Face's. */
const NGINX_PAGE = '<center>nginx</center>';

/**
 * Seconds until a 429 without the error envelope may be retried: Hugging Face's edge refusing the hop to the
 * gateway Space with its own page sends `RateLimit: "pages";r=0;t=<seconds>`, others may send Retry-After.
 */
function platformRetryAfterSeconds(headers: Headers | undefined): number | null {
  const reset = /\bt=(\d+)/.exec(headers?.get('ratelimit') ?? '');
  const seconds = Number(reset?.[1] ?? headers?.get('retry-after') ?? Number.NaN);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

/**
 * Hugging Face's own 429, told apart from AiVa's: the gateway and the platform proxy answer a JSON error
 * envelope, while Hugging Face's edge answers an HTML page that says "We had to rate limit you". Content-Type
 * and the body are readable cross-origin too; the reset headers only when CORS exposes them, else it is null.
 * Null for any other response.
 */
export function hfRateLimit(status: number, headers: Headers, body: string): HfRateLimit | null {
  if (status !== 429) return null;
  const html = /^\s*text\/html/i.test(headers.get('content-type') ?? '');
  if (!body.includes(HF_LIMIT_TEXT) && !(html && !body.includes(NGINX_PAGE))) return null;
  return { resetSeconds: platformRetryAfterSeconds(headers) };
}

/** The call was refused by Hugging Face's own rate limit, not by an AiVa service. */
export function isPlatformRateLimited(error: ApiError): boolean {
  return error.code === PLATFORM_RATE_LIMITED;
}

function isErrorEnvelope(value: unknown): value is { error: ErrorEnvelopeBody } {
  return (
    typeof value === 'object' &&
    value !== null &&
    'error' in value &&
    typeof value.error === 'object' &&
    value.error !== null &&
    'code' in value.error
  );
}

/** Builds an ApiError from a non-2xx response body; its headers tell Hugging Face's own 429 apart. */
export function toApiError(status: number, body: unknown, service = 'The gateway', headers?: Headers): ApiError {
  if (isErrorEnvelope(body)) {
    return new ApiError({
      kind: 'http',
      status,
      code: body.error.code,
      message: body.error.message,
      remediation: body.error.remediation,
      ruleIds: body.error.rule_ids,
      details: body.error.details,
    });
  }
  // Hugging Face's own 503 page while the Space is asleep, waking, building or paused (ADR-0025).
  const html = /^\s*text\/html/i.test(headers?.get('content-type') ?? '');
  if (status === 503 && typeof body === 'string' && PAUSED_TEXT.test(body)) {
    return new ApiError({
      kind: 'http',
      status,
      code: GATEWAY_PAUSED,
      message: 'The demo backend is paused on Hugging Face; ask a maintainer to restart it.',
    });
  }
  if (status === 503 && typeof body === 'string' && html) {
    const seconds = platformRetryAfterSeconds(headers);
    return new ApiError({
      kind: 'http',
      status,
      code: GATEWAY_STARTING,
      message: 'The hosting platform reports the demo backend is not running yet.',
      remediation: 'Retry in a minute.',
      details: seconds === null ? {} : { retry_after_seconds: seconds },
    });
  }
  // Not the gateway's own limit (that one has the envelope), nor the proxy's: the hosting platform's.
  const limit = typeof body === 'string' ? hfRateLimit(status, headers ?? new Headers(), body) : null;
  if (limit) {
    return new ApiError({
      kind: 'http',
      status,
      code: PLATFORM_RATE_LIMITED,
      message: 'The hosting platform is rate-limiting requests to the demo backend.',
      remediation: 'Wait a few minutes; the page retries on its own.',
      details: limit.resetSeconds === null ? {} : { retry_after_seconds: limit.resetSeconds },
    });
  }
  return new ApiError({ kind: 'http', status, message: `${service} responded with HTTP ${status}.` });
}

interface ApiResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/**
 * Awaits an openapi-fetch call and returns its data, or throws an ApiError.
 * A fetch that never reached the service becomes a `network` error, unless the
 * client already said what it means (`unreachableMeansStarting`).
 */
export async function unwrap<T>(
  call: Promise<ApiResult<T>>,
  unreachable = 'The AiVa gateway could not be reached.',
): Promise<T> {
  let result: ApiResult<T>;
  try {
    result = await call;
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    if (cause instanceof ApiError) throw cause;
    throw new ApiError({ kind: 'network', message: unreachable });
  }
  if (result.response.ok && result.data !== undefined) return result.data;
  throw toApiError(result.response.status, result.error, undefined, result.response.headers);
}

export interface RuntimeConfig {
  /** Gateway base URL without a trailing slash, or null when absent or invalid. */
  gatewayUrl: string | null;
  /** The Control Center's story-led visitor experience; off unless the Space sets it to `true`. */
  storyMode: boolean;
}

/**
 * Reads the runtime configuration that /config.js assigns to
 * `window.__AIVA_CONFIG__` (written at container start), so one build serves
 * every Space.
 */
export function readRuntimeConfig(
  raw: unknown = (globalThis as { __AIVA_CONFIG__?: unknown }).__AIVA_CONFIG__,
): RuntimeConfig {
  const config = typeof raw === 'object' && raw !== null ? (raw as Record<string, unknown>) : {};
  const value = config.gatewayUrl;
  return { gatewayUrl: typeof value === 'string' ? toHttpUrl(value) : null, storyMode: config.storyMode === true };
}

function toHttpUrl(value: string): string | null {
  if (!URL.canParse(value.trim())) return null;
  const url = new URL(value.trim());
  return url.protocol === 'http:' || url.protocol === 'https:' ? url.href.replace(/\/+$/, '') : null;
}

/**
 * The bearer token for this browser tab. sessionStorage is per tab and
 * cleared when the tab closes; when storage is unavailable (private mode,
 * blocked site data) the token lives in memory until the page reloads.
 */
export interface StoredToken {
  token: string;
  /** Expiry as epoch milliseconds. */
  expiresAt: number;
}

const TOKEN_KEY = 'aiva.session';
let memory: StoredToken | null = null;

function parseToken(raw: string | null): StoredToken | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (
      typeof value === 'object' &&
      value !== null &&
      'token' in value &&
      'expiresAt' in value &&
      typeof value.token === 'string' &&
      typeof value.expiresAt === 'number'
    ) {
      return { token: value.token, expiresAt: value.expiresAt };
    }
  } catch {
    // Corrupt entry: treated as signed out.
  }
  return null;
}

/** The stored token, or null when absent or expired (an expired token is removed). */
export function readToken(now: number = Date.now()): StoredToken | null {
  let stored: StoredToken | null;
  try {
    stored = parseToken(sessionStorage.getItem(TOKEN_KEY));
  } catch {
    stored = memory; // Storage blocked: fall back to memory.
  }
  if (stored && stored.expiresAt <= now) {
    clearToken();
    return null;
  }
  return stored;
}

export function writeToken(value: StoredToken): void {
  memory = value;
  try {
    sessionStorage.setItem(TOKEN_KEY, JSON.stringify(value));
  } catch {
    // Storage blocked: memory only.
  }
}

export function clearToken(): void {
  memory = null;
  try {
    sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage blocked: nothing persisted.
  }
}
