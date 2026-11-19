/**
 * Permission names granted by the gateway (`aiva_gateway/auth/rbac.py`). The
 * gateway computes them from the caller's roles and returns them from
 * GET /v1/auth/me; the UI only reads them to decide what to show.
 */
export const PERMISSIONS = [
  'chat:test',
  'chat:live',
  'runs:read',
  'policies:read',
  'policies:write',
  'policies:publish',
  'budgets:read',
  'budgets:write',
  'reports:read',
  'reports:export',
  'reports:identity',
  'evaluations:run',
  'catalog:read',
  'approvals:read',
  'approvals:decide',
  // Optimize: wake and put to sleep the inference endpoints (granted by the gateway; no page uses it here).
  'demo:wake',
  // Optimize: compare a live answer with the frontier reference, AI-judged (ADR-0023).
  'demo:compare',
  // Govern: issue and revoke application keys, and read their metadata (ADR-0027).
  'credentials:manage',
  'credentials:read',
] as const;

export type Permission = (typeof PERMISSIONS)[number];

/** A permission check over the list GET /v1/auth/me returned. */
export function permissionChecker(granted: readonly string[]): (permission: Permission) => boolean {
  const set = new Set(granted);
  return (permission) => set.has(permission);
}

/** Accepts only same-origin paths as a post-sign-in destination (no open redirects). */
export function safeNextPath(next: string | null): string | null {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/\\')) return null;
  return next;
}
