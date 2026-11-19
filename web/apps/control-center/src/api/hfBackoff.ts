import { useSyncExternalStore } from 'react';
import type { HfRateLimit } from '@aiva/api-client';

/** The first wait when Hugging Face rate-limits the demo without saying when its limit resets. */
export const FIRST_BACKOFF_MS = 30_000;
/** The longest such wait: each one in a row doubles the last, up to this. */
export const MAX_BACKOFF_MS = 300_000;
/** The shortest pause, so a limit that says it resets now is not asked again in a burst. */
const MIN_PAUSE_MS = 1_000;

let pausedUntil: number | null = null;
/** Hugging Face 429s since the demo last got an answer after a pause. */
let strikes = 0;
let lift: ReturnType<typeof setTimeout> | undefined;
const listeners = new Set<() => void>();

function notify() {
  for (const listener of listeners) listener();
}

/**
 * One back-off for every poller (the demo's nodes, the approvals badge, the export list). The client reports
 * each response: Hugging Face's own 429 pauses them all until its limit resets (its `RateLimit` `t=`, else
 * `Retry-After`, else 30 s doubling up to 5 min); any other answer after the pause starts the doubling over.
 * Answers that arrive during the pause were sent before it and say nothing about the limit.
 */
export function observeHfRateLimit(limit: HfRateLimit | null, now = Date.now()): void {
  if (limit === null) {
    if (pausedUntil === null) strikes = 0;
    return;
  }
  const wait = limit.resetSeconds !== null ? limit.resetSeconds * 1000 : Math.min(FIRST_BACKOFF_MS * 2 ** strikes, MAX_BACKOFF_MS);
  strikes += 1;
  const until = now + Math.max(wait, MIN_PAUSE_MS);
  if (pausedUntil !== null && until <= pausedUntil) return;
  pausedUntil = until;
  clearTimeout(lift);
  lift = setTimeout(() => {
    pausedUntil = null;
    notify();
  }, until - now);
  notify();
}

/** When polling may start again (epoch ms), or null when Hugging Face is not rate-limiting the demo. */
export function hfPausedUntil(): number | null {
  return pausedUntil;
}

export function subscribeHfPause(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The shared pause for a component: re-renders when it starts and when it ends. */
export function useHfPause(): number | null {
  return useSyncExternalStore(subscribeHfPause, hfPausedUntil);
}

/** Forgets any pause, for tests. */
export function resetHfBackoff(): void {
  clearTimeout(lift);
  pausedUntil = null;
  strikes = 0;
  notify();
}
