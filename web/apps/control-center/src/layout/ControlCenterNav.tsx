import { cx, NavItem, NavSection } from '@aiva/ui';
import { Clock } from 'lucide-react';
import type { ReactElement } from 'react';
import { useSession } from '../auth/session';
import { approvalBadge, usePendingApprovals, type ApprovalBadge } from '../govern/pendingApprovals';
import { t } from '../i18n';
import { permittedNavigation, type NavEntry } from '../navigation';

/** Count of requests awaiting the viewer's decision; dashed with a clock when it is from an earlier poll. */
function CountBadge({ badge }: { badge: ApprovalBadge }) {
  return (
    <span
      className={cx(
        'inline-flex items-center gap-1 rounded-full px-2 text-secondary font-semibold tabular-nums',
        badge.stale ? 'border border-dashed border-on-brand/70 text-on-brand' : 'bg-brand-teal-light text-brand-navy',
      )}
    >
      {badge.stale && <Clock className="size-3" />}
      {badge.text}
    </span>
  );
}

function renderEntry(entry: NavEntry, level: 1 | 2, approvals: ApprovalBadge | null): ReactElement {
  if ('path' in entry) {
    const badge = entry.id === 'approval-queue' ? approvals : null;
    return (
      <NavItem
        key={entry.id}
        to={entry.path}
        label={t(`view.${entry.id}.title`)}
        icon={entry.icon}
        badge={badge && <CountBadge badge={badge} />}
        accessibleLabel={badge?.label}
      />
    );
  }
  return (
    <NavSection key={entry.id} title={t(`area.${entry.id}`)} level={level}>
      {entry.items.map((child) => renderEntry(child, 2, approvals))}
    </NavSection>
  );
}

/** Role-aware navigation: only the views the signed-in caller's permissions allow. */
export function ControlCenterNav() {
  const { can, usersVisible } = useSession();
  const approvals = approvalBadge(usePendingApprovals());
  const groups = permittedNavigation(can, usersVisible === true);
  return (
    <>
      {groups.map((group) => renderEntry(group, 1, approvals))}
    </>
  );
}
