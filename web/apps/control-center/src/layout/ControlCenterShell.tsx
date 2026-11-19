import { AppShell, Wordmark } from '@aiva/ui';
import { useEffect, useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router';
import { RequireSession } from '../auth/RequireSession';
import { formatCount } from '../format';
import { awaitingDecision, usePendingApprovals } from '../govern/pendingApprovals';
import { PendingApprovalsProvider } from '../govern/PendingApprovalsProvider';
import { t } from '../i18n';
import { AccountPanel } from './AccountPanel';
import { takeMainFocusRequest } from './arrival';
import { ControlCenterNav } from './ControlCenterNav';
import { VisitorRenewalNotice } from './VisitorRenewalNotice';

/** Moves focus to the main content when sign-in asked for it on the way in. */
function FocusMainOnArrival() {
  const { key } = useLocation();
  useEffect(() => {
    if (takeMainFocusRequest()) document.getElementById('main')?.focus();
  }, [key]);
  return null;
}

/**
 * Announces a change in the number of requests awaiting the viewer's decision,
 * once per change: the badge itself is not a live region, so polls stay silent.
 */
function PendingApprovalsAnnouncer() {
  const pending = usePendingApprovals();
  const count = pending?.list && !pending.error ? awaitingDecision(pending.list).count : null;
  const [seen, setSeen] = useState<{ count: number | null; message: string }>({ count: null, message: '' });
  if (count !== null && count !== seen.count) {
    setSeen({
      count,
      message: seen.count === null ? '' : t('approvals.badge.announce', { count: formatCount(count) }),
    });
  }
  return (
    <div aria-live="polite" className="sr-only">
      {seen.message}
    </div>
  );
}

export function ControlCenterShell() {
  return (
    <RequireSession>
      <PendingApprovalsProvider>
        <AppShell
          brand={
            <Link to="/" className="flex items-baseline gap-2 rounded-lg">
              <Wordmark size="sm" tone="onDark" />
              <span className="text-secondary font-medium text-on-brand/80">{t('app.surface')}</span>
            </Link>
          }
          navLabel={t('nav.label')}
          nav={<ControlCenterNav />}
          account={<AccountPanel />}
        >
          <FocusMainOnArrival />
          <PendingApprovalsAnnouncer />
          <VisitorRenewalNotice />
          <Outlet />
        </AppShell>
      </PendingApprovalsProvider>
    </RequireSession>
  );
}
