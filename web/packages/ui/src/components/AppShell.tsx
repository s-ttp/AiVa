import { Menu, X } from 'lucide-react';
import { useId, useState, type ReactNode } from 'react';
import { useLocation } from 'react-router';
import { cx } from '../cx';
import { t } from '../strings';
import { SkipLink } from './SkipLink';

interface AppShellProps {
  /** Product identity shown above the navigation (wordmark and surface name). */
  brand: ReactNode;
  /** Accessible name of the primary navigation landmark. */
  navLabel: string;
  /** NavSection / NavItem elements. */
  nav: ReactNode;
  /** Signed-in account and sign-out, shown above the navigation (inside the mobile menu). */
  account?: ReactNode;
  children: ReactNode;
}

/**
 * Left navigation (LLD §22.4: 240–264px) beside a content column capped at
 * ~1440px. Below the `lg` breakpoint the navigation stacks above the content
 * behind a disclosure button.
 */
export function AppShell({ brand, navLabel, nav, account, children }: AppShellProps) {
  const navId = useId();
  const { pathname } = useLocation();
  // Remember where the menu was opened so that navigating closes it.
  const [openedAt, setOpenedAt] = useState<string | null>(null);
  const menuOpen = openedAt === pathname;

  return (
    <div className="grow lg:grid lg:grid-cols-[var(--aiva-nav-width)_minmax(0,1fr)]">
      <SkipLink />
      <div className="bg-brand-navy text-on-brand [--aiva-focus-ring:var(--aiva-brand-teal-light)] lg:sticky lg:top-0 lg:h-dvh lg:overflow-y-auto">
        <header className="flex items-center justify-between gap-4 px-6 py-5">
          {brand}
          <button
            type="button"
            className="inline-flex items-center gap-2 rounded-lg px-3 py-2 text-secondary font-semibold hover:bg-on-brand/10 lg:hidden"
            aria-expanded={menuOpen}
            aria-controls={navId}
            onClick={() => setOpenedAt(menuOpen ? null : pathname)}
          >
            {menuOpen ? <X className="size-4" /> : <Menu className="size-4" />}
            {t('shell.menu')}
          </button>
        </header>
        <div id={navId} className={cx(menuOpen ? 'block' : 'hidden', 'px-3 pb-8 lg:block')}>
          {account && <div className="mb-6 border-b border-on-brand/15 px-3 pb-5">{account}</div>}
          <nav aria-label={navLabel}>
            <ul className="space-y-6">{nav}</ul>
          </nav>
        </div>
      </div>
      <main
        id="main"
        tabIndex={-1}
        className="mx-auto w-full max-w-(--aiva-content-max-width) px-4 py-6 outline-none sm:px-6 lg:px-8 lg:py-8"
      >
        {children}
      </main>
    </div>
  );
}
