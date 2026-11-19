import { isPublicVisitor, useSession } from '../auth/session';

/** Whether the signed-in caller is the public demo's pseudonymous visitor (ADR-0019). */
export function usePublicVisitor(): boolean {
  const { state } = useSession();
  return state.status === 'signedIn' && isPublicVisitor(state.profile);
}
