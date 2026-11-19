import { buttonClasses, EmptyState } from '@aiva/ui';
import { Link } from 'react-router';
import { t } from '../i18n';
import { Page } from '../layout/Page';

export function NotFoundPage() {
  return (
    <Page title={t('notFound.title')}>
      <EmptyState title={t('notFound.title')} description={t('notFound.body')}>
        <Link to="/" className={buttonClasses('secondary')}>
          {t('notFound.home')}
        </Link>
      </EmptyState>
    </Page>
  );
}
