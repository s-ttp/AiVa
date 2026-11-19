import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ApiError, unwrap, type ApprovalRequest } from '../api/client';
import { hfPausedUntil, subscribeHfPause } from '../api/hfBackoff';
import { useSession } from '../auth/session';
import { nextPollDelay, PENDING_LIMIT, PendingApprovalsContext, type PendingApprovals } from './pendingApprovals';

interface Snapshot {
  list: readonly ApprovalRequest[] | null;
  fetchedAt: number | null;
  error: ApiError | null;
  loading: boolean;
}

/**
 * Polls the pending Approval Queue for callers with `approvals:read` and shares
 * the result, so the navigation badge and the queue table never disagree.
 * Polls every 30 s while the page is visible, pauses while it is hidden,
 * refetches when the page becomes visible or the window regains focus, and
 * backs off after failures. While Hugging Face rate-limits the demo it waits
 * for the shared back-off, then polls again. A failed poll keeps the last list
 * and records the error, so a stale count is never presented as a fresh one.
 */
export function PendingApprovalsProvider({ children }: { children: ReactNode }) {
  const { client, can } = useSession();
  const enabled = client !== null && can('approvals:read');
  const [snapshot, setSnapshot] = useState<Snapshot>({ list: null, fetchedAt: null, error: null, loading: enabled });
  const pollNow = useRef<() => void>(() => undefined);

  useEffect(() => {
    if (!enabled || client === null) return;
    let failures = 0;
    let nextAllowedAt = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let inFlight: AbortController | null = null;

    function schedule() {
      clearTimeout(timer);
      const delay = nextPollDelay(failures);
      nextAllowedAt = failures > 0 ? Date.now() + delay : 0;
      if (document.visibilityState !== 'hidden' && hfPausedUntil() === null) timer = setTimeout(() => void poll(true), delay);
    }

    async function poll(force: boolean) {
      if (client === null || inFlight || hfPausedUntil() !== null || (!force && Date.now() < nextAllowedAt)) return;
      clearTimeout(timer);
      const controller = new AbortController();
      inFlight = controller;
      try {
        const data = await unwrap(
          client.GET('/v1/admin/approvals', {
            params: { query: { status: 'pending', limit: PENDING_LIMIT } },
            signal: controller.signal,
          }),
        );
        failures = 0;
        setSnapshot({ list: data.approvals, fetchedAt: Date.now(), error: null, loading: false });
      } catch (cause) {
        if (controller.signal.aborted) return;
        failures += 1;
        const error =
          cause instanceof ApiError
            ? cause
            : new ApiError({ kind: 'network', message: 'The AiVa gateway could not be reached.' });
        setSnapshot((previous) => ({ ...previous, error, loading: false }));
      } finally {
        if (inFlight === controller) inFlight = null;
      }
      schedule();
    }

    function onVisibility() {
      if (document.visibilityState === 'hidden') clearTimeout(timer);
      else void poll(false);
    }
    function onFocus() {
      void poll(false);
    }
    // The shared back-off: stop while Hugging Face rate-limits the demo, and poll once its limit resets.
    function onHfPause() {
      if (hfPausedUntil() !== null) clearTimeout(timer);
      else if (document.visibilityState !== 'hidden') void poll(true);
    }

    pollNow.current = () => void poll(true);
    void poll(true);
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('focus', onFocus);
    const unsubscribe = subscribeHfPause(onHfPause);
    return () => {
      pollNow.current = () => undefined;
      unsubscribe();
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('focus', onFocus);
      clearTimeout(timer);
      inFlight?.abort();
    };
  }, [enabled, client]);

  const refresh = useCallback(() => pollNow.current(), []);
  const value = useMemo<PendingApprovals | null>(
    () => (enabled ? { ...snapshot, refresh } : null),
    [enabled, snapshot, refresh],
  );
  return <PendingApprovalsContext value={value}>{children}</PendingApprovalsContext>;
}
