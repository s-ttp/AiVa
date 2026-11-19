import { createContext, useContext } from 'react';
import type { ApiError, CallerProfile, GatewayClient } from '../api/client';
import type { Permission } from './permissions';

export type SessionState =
  /** A stored token is being verified with GET /v1/auth/me. */
  | { status: 'restoring' }
  /** The stored token could not be verified: the gateway was unreachable or still starting. */
  | { status: 'unverified'; error: ApiError }
  | { status: 'signedOut'; reason: 'none' | 'signedOut' | 'expired' }
  /**
   * `renewed` is set when a public visitor's two-hour session ended and a new
   * one started quietly; the new session is a new pseudonymous visitor.
   */
  | { status: 'signedIn'; profile: CallerProfile; expiresAt: number; renewed?: boolean };

export interface Session {
  /** Gateway base URL from the runtime configuration, or null when not configured. */
  gatewayUrl: string | null;
  /** Typed gateway client that sends the session's bearer token; null without a gateway URL. */
  client: GatewayClient | null;
  state: SessionState;
  /** Starts a public demo session as the seeded visitor (POST /v1/auth/public/session). Throws ApiError. */
  startVisitorSession: () => Promise<CallerProfile>;
  /** Passcode sign-in as a seeded demo user (POST /v1/auth/demo/token). Throws ApiError. */
  signInWithPasscode: (userId: string, passcode: string) => Promise<CallerProfile>;
  /** Dev sign-in as a seeded demo user (POST /v1/auth/dev/token), local gateways only. Throws ApiError. */
  signInAsDemoUser: (userId: string) => Promise<CallerProfile>;
  /**
   * Starts a session from a bearer token obtained elsewhere, e.g. an OIDC
   * authorization-code flow. Roles and permissions always come from the gateway.
   */
  signInWithToken: (token: string, expiresAt: number) => Promise<CallerProfile>;
  signOut: () => void;
  retry: () => void;
  /** Hides the notice that a visitor session was renewed. */
  acknowledgeRenewal: () => void;
  can: (permission: Permission) => boolean;
  /**
   * Whether reports may show the user dimension (user filter, grouping and
   * principal): the gateway's reporting scope says (`users_visible`, false for
   * public demo visitors, ADR-0019), so the UI offers nothing it would refuse.
   * Null until the scope has been read: callers treat that as hidden, so the
   * dimension never appears before the gateway has allowed it.
   */
  usersVisible: boolean | null;
}

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error('useSession must be used inside <SessionProvider>');
  return session;
}

export function useProfile(): CallerProfile {
  const { state } = useSession();
  if (state.status !== 'signedIn') throw new Error('useProfile requires a signed-in session');
  return state.profile;
}

/** A visitor session is renewed this long before its token expires, so no request meets an expired token. */
export const VISITOR_RENEWAL_LEAD_MS = 60_000;

/** The public demo's pseudonymous visitor (ADR-0019), as opposed to a named demo or real user. */
export function isPublicVisitor(profile: CallerProfile): boolean {
  return (profile.roles ?? []).includes('public_visitor');
}
