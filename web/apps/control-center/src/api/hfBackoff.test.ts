import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { hfPausedUntil, observeHfRateLimit, subscribeHfPause } from './hfBackoff';

const unsaid = { resetSeconds: null };

/** Reports a 429 now and returns how long the pause it starts lasts. */
function limited(limit: { resetSeconds: number | null } = unsaid): number | null {
  const now = Date.now();
  observeHfRateLimit(limit, now);
  const until = hfPausedUntil();
  return until === null ? null : until - now;
}

describe('the shared Hugging Face back-off', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('pauses until the reset Hugging Face gives, then lifts and tells every poller both times', () => {
    const heard = vi.fn();
    const stop = subscribeHfPause(heard);

    expect(limited({ resetSeconds: 42 })).toBe(42_000);
    vi.advanceTimersByTime(41_999);
    expect(hfPausedUntil()).not.toBeNull();
    vi.advanceTimersByTime(1);
    expect(hfPausedUntil()).toBeNull();
    expect(heard).toHaveBeenCalledTimes(2);
    stop();
  });

  it('without a reset, waits 30 s, doubling for each limit in a row up to 5 min', () => {
    const waits = Array.from({ length: 6 }, () => {
      const wait = limited() ?? 0;
      vi.advanceTimersByTime(wait);
      return wait;
    });

    expect(waits).toEqual([30_000, 60_000, 120_000, 240_000, 300_000, 300_000]);
  });

  it('starts the doubling over after an answer that follows a pause, not one to a request sent before it', () => {
    vi.advanceTimersByTime(limited() ?? 0);
    expect(limited()).toBe(60_000);
    observeHfRateLimit(null); // sent before the pause
    vi.advanceTimersByTime(60_000);
    expect(limited()).toBe(120_000);
    vi.advanceTimersByTime(120_000);

    observeHfRateLimit(null);

    expect(limited()).toBe(30_000);
  });

  it('never shortens a pause, and never pauses for less than a second', () => {
    expect(limited({ resetSeconds: 120 })).toBe(120_000);
    expect(limited({ resetSeconds: 10 })).toBe(120_000);
    vi.advanceTimersByTime(120_000);

    expect(limited({ resetSeconds: 0 })).toBe(1_000);
  });
});
