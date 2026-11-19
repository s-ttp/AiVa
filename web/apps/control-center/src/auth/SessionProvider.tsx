import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ApiError, createGatewayClient, isPlatformRateLimited, unwrap, type CallerProfile } from '../api/client';
import { readRuntimeConfig, type RuntimeConfig } from '../config';
import { permissionChecker } from './permissions';
import { isPublicVisitor, SessionContext, VISITOR_RENEWAL_LEAD_MS, type Session, type SessionState } from './session';
import { clearToken, readToken, writeToken } from './tokenStore';

const MAX_TIMER_DELAY_MS = 2_147_483_647;
/** The client saw the gateway reject the session token (HTTP 401). */
const REJECTED = 'rejected';

interface SessionProviderProps {
  config?: RuntimeConfig;
  children: ReactNode;
}

function noGateway(): ApiError {
  return new ApiError({ kind: 'network', message: 'No AiVa gateway endpoint is configured.' });
}

/**
 * The gateway could not answer yet: unreachable, its platform proxy reports it is still starting, or Hugging
 * Face rate-limits the demo.
 */
function gatewayNotAnswering(error: unknown): error is ApiError {
  return error instanceof ApiError && (error.kind === 'network' || (error.status ?? 0) >= 500 || isPlatformRateLimited(error));
}

/** Owns the bearer token and the caller profile for the whole Control Center (ADR-0010, ADR-0019). */
export function SessionProvider({ config, children }: SessionProviderProps) {
  const [gatewayUrl] = useState(() => (config ?? readRuntimeConfig()).gatewayUrl);
  const [state, setState] = useState<SessionState>(() =>
    gatewayUrl && readToken() ? { status: 'restoring' } : { status: 'signedOut', reason: 'none' },
  );
  // The client is created once; it reports a rejected token as an event, which the
  // current session handles (see below).
  const [rejections] = useState(() => new EventTarget());
  const [client] = useState(() =>
    gatewayUrl
      ? createGatewayClient(
          gatewayUrl,
          () => readToken()?.token ?? null,
          () => rejections.dispatchEvent(new Event(REJECTED)),
        )
      : null,
  );

  const endSession = useCallback(() => {
    clearToken();
    setState({ status: 'signedOut', reason: 'expired' });
  }, []);

  // Verify a token restored from storage. A gateway that is unreachable or still
  // starting keeps the token, so the session survives the demo waking up.
  useEffect(() => {
    if (state.status !== 'restoring' || client === null) return;
    const controller = new AbortController();
    unwrap(client.GET('/v1/auth/me', { signal: controller.signal })).then(
      (profile) => {
        const stored = readToken();
        if (stored) setState({ status: 'signedIn', profile, expiresAt: stored.expiresAt });
        else setState({ status: 'signedOut', reason: 'expired' });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        if (gatewayNotAnswering(error)) setState({ status: 'unverified', error });
        else endSession();
      },
    );
    return () => controller.abort();
  }, [state.status, client, endSession]);

  // The two-hour session of a public visitor is renewed quietly when it ends: a new
  // session is a new pseudonymous visitor, so the shell says so once. If no new
  // session can be started (limits, gateway down), the visitor is signed out.
  const renewal = useRef<Promise<void> | null>(null);
  const renewVisitorSession = useCallback(() => {
    if (client === null) return;
    // The old token stays stored until the new one replaces it: it is still valid, and a reload while
    // the request is in flight must not lose the session.
    renewal.current ??= (async () => {
      try {
        const minted = await unwrap(client.POST('/v1/auth/public/session'));
        const expiresAt = Date.parse(minted.expires_at);
        writeToken({ token: minted.access_token, expiresAt });
        setState((current) =>
          current.status === 'signedIn' ? { ...current, expiresAt, renewed: true } : current,
        );
      } catch {
        endSession();
      } finally {
        renewal.current = null;
      }
    })();
  }, [client, endSession]);

  const visitor = state.status === 'signedIn' && isPublicVisitor(state.profile);
  useEffect(() => {
    const onRejected = () => {
      // Renew only a visitor token that is due; a fresh one rejected means it was revoked.
      const stored = readToken();
      if (visitor && stored && stored.expiresAt - Date.now() <= VISITOR_RENEWAL_LEAD_MS) renewVisitorSession();
      else if (renewal.current === null) endSession();
    };
    rejections.addEventListener(REJECTED, onRejected);
    return () => rejections.removeEventListener(REJECTED, onRejected);
  }, [rejections, visitor, renewVisitorSession, endSession]);

  // End (or, for a visitor, renew) the session when the token expires, even
  // without a request. setTimeout fires at once for delays beyond 2^31-1 ms,
  // so long-lived tokens are left to the gateway's 401 instead.
  const expiresAt = state.status === 'signedIn' ? state.expiresAt : null;
  useEffect(() => {
    if (expiresAt === null) return;
    const delay = Math.max(0, expiresAt - (visitor ? VISITOR_RENEWAL_LEAD_MS : 0) - Date.now());
    if (delay > MAX_TIMER_DELAY_MS) return;
    const timer = window.setTimeout(visitor ? renewVisitorSession : endSession, delay);
    return () => window.clearTimeout(timer);
  }, [expiresAt, visitor, renewVisitorSession, endSession]);

  const signInWithToken = useCallback(
    async (token: string, tokenExpiresAt: number): Promise<CallerProfile> => {
      if (client === null) throw noGateway();
      writeToken({ token, expiresAt: tokenExpiresAt });
      try {
        const profile = await unwrap(client.GET('/v1/auth/me'));
        setState({ status: 'signedIn', profile, expiresAt: tokenExpiresAt });
        return profile;
      } catch (error) {
        clearToken();
        throw error;
      }
    },
    [client],
  );

  const startVisitorSession = useCallback(async () => {
    if (client === null) throw noGateway();
    const minted = await unwrap(client.POST('/v1/auth/public/session'));
    return signInWithToken(minted.access_token, Date.parse(minted.expires_at));
  }, [client, signInWithToken]);

  const signInWithPasscode = useCallback(
    async (userId: string, passcode: string) => {
      if (client === null) throw noGateway();
      const minted = await unwrap(client.POST('/v1/auth/demo/token', { body: { user_id: userId, passcode } }));
      return signInWithToken(minted.access_token, Date.parse(minted.expires_at));
    },
    [client, signInWithToken],
  );

  const signInAsDemoUser = useCallback(
    async (userId: string) => {
      if (client === null) throw noGateway();
      const minted = await unwrap(client.POST('/v1/auth/dev/token', { body: { user_id: userId } }));
      return signInWithToken(minted.access_token, Date.parse(minted.expires_at));
    },
    [client, signInWithToken],
  );

  const signOut = useCallback(() => {
    clearToken();
    setState({ status: 'signedOut', reason: 'signedOut' });
  }, []);

  const retry = useCallback(() => setState({ status: 'restoring' }), []);

  // Whether reports show the user dimension comes from the gateway's reporting
  // scope (`users_visible`, false for public demo visitors), read once per
  // signed-in user who may read reports. It is unknown (null) until it arrives,
  // and callers hide the dimension meanwhile. If it cannot be read, a public
  // visitor keeps it hidden and anyone else gets the contract's default; the
  // gateway refuses the dimension anyway.
  const reportUser =
    state.status === 'signedIn' && state.profile.permissions?.includes('reports:read')
      ? (state.profile.user_id ?? null)
      : null;
  const [reportScope, setReportScope] = useState<{ user: string; usersVisible: boolean } | null>(null);
  useEffect(() => {
    if (reportUser === null || client === null) return;
    const controller = new AbortController();
    const today = new Date().toISOString().slice(0, 10);
    const query = { from: today, to: today };
    unwrap(client.GET('/v1/reports/filters', { params: { query }, signal: controller.signal })).then(
      (filters) => {
        if (controller.signal.aborted) return;
        setReportScope({ user: reportUser, usersVisible: filters.scope.users_visible ?? true });
      },
      () => {
        if (!controller.signal.aborted) setReportScope({ user: reportUser, usersVisible: !visitor });
      },
    );
    return () => controller.abort();
  }, [reportUser, client, visitor]);
  const usersVisible =
    reportUser === null ? false : reportScope?.user === reportUser ? reportScope.usersVisible : null;

  const acknowledgeRenewal = useCallback(
    () => setState((current) => (current.status === 'signedIn' ? { ...current, renewed: false } : current)),
    [],
  );

  const session = useMemo<Session>(
    () => ({
      gatewayUrl,
      client,
      state,
      startVisitorSession,
      signInWithPasscode,
      signInAsDemoUser,
      signInWithToken,
      signOut,
      retry,
      acknowledgeRenewal,
      can: permissionChecker(state.status === 'signedIn' ? (state.profile.permissions ?? []) : []),
      usersVisible,
    }),
    [
      gatewayUrl,
      client,
      state,
      startVisitorSession,
      signInWithPasscode,
      signInAsDemoUser,
      signInWithToken,
      signOut,
      retry,
      acknowledgeRenewal,
      usersVisible,
    ],
  );

  return <SessionContext value={session}>{children}</SessionContext>;
}
