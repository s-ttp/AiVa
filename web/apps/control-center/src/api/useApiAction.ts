import { useCallback, useEffect, useRef, useState } from 'react';
import { useSession } from '../auth/session';
import { ApiError, type GatewayClient } from './client';

export type ActionState<T> =
  | { status: 'idle' }
  | { status: 'pending' }
  | { status: 'error'; error: ApiError }
  | { status: 'success'; data: T };

/**
 * Runs a gateway write on demand (publish, approve, simulate). Only the latest
 * call updates the state; results that arrive after unmount are dropped.
 * `run` resolves to the data, or null when the call failed.
 */
export function useApiAction<Args extends unknown[], T>(
  perform: (client: GatewayClient, ...args: Args) => Promise<T>,
): { state: ActionState<T>; run: (...args: Args) => Promise<T | null>; reset: () => void } {
  const { client } = useSession();
  const [state, setState] = useState<ActionState<T>>({ status: 'idle' });
  const latest = useRef(0);
  const mounted = useRef(true);
  const performRef = useRef(perform);
  useEffect(() => {
    performRef.current = perform;
  });
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = useCallback(
    async (...args: Args): Promise<T | null> => {
      const call = ++latest.current;
      const settle = (next: ActionState<T>) => {
        if (mounted.current && call === latest.current) setState(next);
      };
      if (client === null) {
        settle({
          status: 'error',
          error: new ApiError({ kind: 'network', message: 'No AiVa gateway endpoint is configured.' }),
        });
        return null;
      }
      settle({ status: 'pending' });
      try {
        const data = await performRef.current(client, ...args);
        settle({ status: 'success', data });
        return data;
      } catch (cause) {
        const error =
          cause instanceof ApiError
            ? cause
            : new ApiError({ kind: 'network', message: 'The AiVa gateway could not be reached.' });
        settle({ status: 'error', error });
        return null;
      }
    },
    [client],
  );

  const reset = useCallback(() => {
    latest.current += 1;
    setState({ status: 'idle' });
  }, []);

  return { state, run, reset };
}
