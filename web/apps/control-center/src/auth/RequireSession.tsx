import { ErrorState, LoadingState } from '@aiva/ui';
import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router';
import { isGatewayDown } from '../api/client';
import { GatewayDown } from '../components/GatewayDown';
import { t } from '../i18n';
import { useSession } from './session';

/** Renders its children only for a signed-in caller; otherwise sends them to sign in and back. */
export function RequireSession({ children }: { children: ReactNode }) {
  const { state, retry } = useSession();
  const location = useLocation();

  if (state.status === 'signedIn') return children;
  if (state.status === 'signedOut') {
    const next = `${location.pathname}${location.search}`;
    return <Navigate to={`/?next=${encodeURIComponent(next)}`} replace />;
  }
  return (
    <main id="main" className="mx-auto w-full max-w-3xl grow px-4 py-10">
      <title>{t('app.documentTitle', { page: t('session.checking') })}</title>
      <h1 className="sr-only">{t('session.checking')}</h1>
      {state.status === 'restoring' ? (
        <LoadingState label={t('session.checking')} />
      ) : isGatewayDown(state.error) ? (
        <GatewayDown error={state.error} onRetry={retry} />
      ) : (
        <ErrorState title={t('session.unverified.title')} description={state.error.message} onRetry={retry} />
      )}
    </main>
  );
}
