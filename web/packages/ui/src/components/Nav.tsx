import type { LucideIcon } from 'lucide-react';
import { useId, type ReactNode } from 'react';
import { NavLink } from 'react-router';
import { cx } from '../cx';

interface NavSectionProps {
  title: string;
  /** 1 = top-level area (e.g. "AiVa Admin Dashboard"), 2 = group within it. */
  level?: 1 | 2;
  children: ReactNode;
}

/** A labelled list of navigation items; render inside AppShell's `nav`. */
export function NavSection({ title, level = 1, children }: NavSectionProps) {
  const titleId = useId();
  return (
    <li className={level === 2 ? 'pt-3' : undefined}>
      <span
        id={titleId}
        className={cx(
          'block px-3 font-semibold',
          level === 1 ? 'text-secondary text-brand-teal-light' : 'text-secondary text-on-brand/75',
        )}
      >
        {title}
      </span>
      <ul aria-labelledby={titleId} className="mt-2 space-y-0.5">
        {children}
      </ul>
    </li>
  );
}

interface NavItemProps {
  to: string;
  label: string;
  icon?: LucideIcon;
  /** Visual-only marker after the label, e.g. a count; describe it in `accessibleLabel`. */
  badge?: ReactNode;
  /** Accessible name when the badge adds information; it must start with the visible label. */
  accessibleLabel?: string;
}

/** A navigation link; React Router sets aria-current="page" when it is active. */
export function NavItem({ to, label, icon: Icon, badge, accessibleLabel }: NavItemProps) {
  return (
    <li>
      <NavLink
        to={to}
        end
        aria-label={accessibleLabel}
        className={({ isActive }) =>
          cx(
            'flex items-center gap-3 rounded-lg border-s-[3px] px-3 py-2 text-body',
            isActive
              ? 'border-brand-teal bg-on-brand/10 font-semibold text-on-brand'
              : 'border-transparent text-on-brand/80 hover:bg-on-brand/5 hover:text-on-brand',
          )
        }
      >
        {Icon && <Icon className="size-4 shrink-0" />}
        <span>{label}</span>
        {badge && (
          <span aria-hidden="true" className="ms-auto">
            {badge}
          </span>
        )}
      </NavLink>
    </li>
  );
}
