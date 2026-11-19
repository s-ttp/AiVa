import { EmptyState } from '@aiva/ui';
import { t } from '../i18n';
import { Page } from '../layout/Page';
import type { RoutedView } from '../navigation';

/** Honest placeholder for a view whose gateway API is not available yet. */
export function ViewPlaceholderPage({ view }: { view: RoutedView }) {
  return (
    <Page
      title={t(`view.${view.id}.title`)}
      eyebrow={t(`area.${view.area}`)}
      description={t(`view.${view.id}.description`)}
    >
      <EmptyState icon={view.icon} title={t('placeholder.title')} description={t('placeholder.body')} />
    </Page>
  );
}
