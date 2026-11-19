import { describe, expect, it } from 'vitest';
import { retryAfterMinutes, toApiError, unwrap } from './browser';

const HF_PAGE = '<html><body>We had to rate limit you.</body></html>';

describe('toApiError on a 429 without the error envelope', () => {
  it("reads the reset from Hugging Face's RateLimit header", () => {
    const headers = new Headers({ ratelimit: '"pages";r=0;t=212', 'retry-after': '30' });
    const error = toApiError(429, HF_PAGE, undefined, headers);
    expect(error.code).toBe('PLATFORM_RATE_LIMITED');
    expect(error.details.retry_after_seconds).toBe(212);
    expect(retryAfterMinutes(error)).toBe(4);
  });

  it('falls back to Retry-After, then to no reset', () => {
    expect(toApiError(429, HF_PAGE, undefined, new Headers({ 'retry-after': '30' })).details.retry_after_seconds).toBe(30);
    expect(toApiError(429, HF_PAGE).details).toEqual({});
  });

  it("keeps the gateway's own 429 as the gateway sent it", () => {
    const body = { error: { code: 'PUBLIC_SESSION_LIMITED', message: 'Too many.', details: { retry_after_seconds: 60 } } };
    const error = toApiError(429, body, undefined, new Headers({ ratelimit: '"pages";r=0;t=212' }));
    expect(error.code).toBe('PUBLIC_SESSION_LIMITED');
    expect(error.details.retry_after_seconds).toBe(60);
  });

  it('gets the headers through unwrap', async () => {
    const response = new Response(HF_PAGE, { status: 429, headers: { ratelimit: '"pages";r=0;t=90' } });
    const failure = await unwrap(Promise.resolve({ error: HF_PAGE, response })).catch((e: unknown) => e);
    expect(failure).toMatchObject({ status: 429, code: 'PLATFORM_RATE_LIMITED', details: { retry_after_seconds: 90 } });
  });
});
