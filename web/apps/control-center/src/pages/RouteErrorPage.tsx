import { ErrorState } from '@aiva/ui';
import { t } from '../i18n';

/** Rendered by React Router when a route throws; keeps the global banner visible. */
export function RouteErrorPage() {
  return (
    <main id="main" className="mx-auto w-full max-w-3xl grow px-4 py-10">
      <title>{t('app.documentTitle', { page: t('error.title') })}</title>
      <ErrorState title={t('error.title')} description={t('error.body')} onRetry={() => window.location.reload()} />
    </main>
  );
}
