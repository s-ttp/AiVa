/**
 * A one-shot request for the shell to move focus to the main content when it
 * next renders. Used after sign-in: the focused sign-in button disappears with
 * the landing page, and without this the browser decides where focus lands.
 * Kept in memory rather than in history state, so a reload does not replay it.
 */
let pending = false;

export function requestMainFocus(): void {
  pending = true;
}

export function takeMainFocusRequest(): boolean {
  const requested = pending;
  pending = false;
  return requested;
}
