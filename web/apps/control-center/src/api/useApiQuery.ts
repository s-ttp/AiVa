import { useCallback, useEffect, useState } from 'react';
import { useSession } from '../auth/session';
import { ApiError, type GatewayClient } from './client';

export type QueryState<T> =
  | { status: 'loading' }
  | { status: 'error'; error: ApiError }
  | { status: 'success'; data: T };

interface Settled<T> {
  key: string;
  state: QueryState<T>;
}

/**
 * Loads gateway data for the current session. `key` identifies the request:
 * when it changes the previous result is discarded and the state is `loading`
 * until the new response arrives. Pass `null` as the key to skip loading.
 */
export function useApiQuery<T>(
  key: string | null,
  load: (client: GatewayClient, signal: AbortSignal) => Promise<T>,
): { state: QueryState<T> | null; reload: () => void } {
  const { client } = useSession();
  const [attempt, setAttempt] = useState(0);
  const [settled, setSettled] = useState<Settled<T> | null>(null);
  const requestKey = key === null ? null : `${key}#${attempt}`;

  useEffect(() => {
    if (requestKey === null || client === null) return;
    const controller = new AbortController();
    load(client, controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setSettled({ key: requestKey, state: { status: 'success', data } });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        const apiError =
          error instanceof ApiError
            ? error
            : new ApiError({ kind: 'network', message: 'The AiVa gateway could not be reached.' });
        setSettled({ key: requestKey, state: { status: 'error', error: apiError } });
      },
    );
    return () => controller.abort();
    // `load` is identified by `key`: callers pass a fresh closure on every render.
  }, [requestKey, client]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  if (requestKey === null) return { state: null, reload };
  if (client === null) {
    return {
      state: {
        status: 'error',
        error: new ApiError({ kind: 'network', message: 'No AiVa gateway endpoint is configured.' }),
      },
      reload,
    };
  }
  return { state: settled?.key === requestKey ? settled.state : { status: 'loading' }, reload };
}
