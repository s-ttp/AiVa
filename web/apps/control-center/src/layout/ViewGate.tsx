import { EmptyState, LoadingState } from '@aiva/ui';
import { Lock } from 'lucide-react';
import type { ReactNode } from 'react';
import { useProfile, useSession } from '../auth/session';
import { formatList } from '../format';
import { t } from '../i18n';
import { roleLabel } from '../labels';
import { viewAllowed, type RoutedView } from '../navigation';
import { Page } from './Page';

/**
 * Shows a view only when the caller may use it. Opening a hidden view directly
 * explains why it is unavailable instead of failing silently.
 */
export function ViewGate({ view, children }: { view: RoutedView; children: ReactNode }) {
  const { can, usersVisible } = useSession();
  const profile = useProfile();
  if (viewAllowed(view, can, usersVisible === true)) return children;
  if (!view.permission || can(view.permission)) {
    if (usersVisible === null) {
      // Allowed by permission; the reporting scope decides once it has been read.
      return (
        <Page title={t(`view.${view.id}.title`)} eyebrow={t(`area.${view.area}`)}>
          <LoadingState label={t('reportScope.loading')} />
        </Page>
      );
    }
    // Allowed by permission, but about the user dimension, which this caller does not see.
    return (
      <Page title={t(`view.${view.id}.title`)} eyebrow={t(`area.${view.area}`)}>
        <EmptyState icon={Lock} title={t('noAccess.userDimension.title')} description={t('noAccess.userDimension.body')} />
      </Page>
    );
  }
  return (
    <Page title={t(`view.${view.id}.title`)} eyebrow={t(`area.${view.area}`)}>
      <EmptyState
        icon={Lock}
        title={t('noAccess.title')}
        description={t('noAccess.body', {
          roles: formatList((profile.roles ?? []).map(roleLabel), 'and') || t('noAccess.noRoles'),
          permission: view.permission,
        })}
      />
    </Page>
  );
}
