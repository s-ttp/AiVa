import { SyntheticDataBanner } from '@aiva/ui';
import { Outlet } from 'react-router';
import { SessionProvider } from '../auth/SessionProvider';

/** Wraps every route: the session and the synthetic-data notice that is always visible. */
export function RootLayout() {
  return (
    <SessionProvider>
      <div className="flex min-h-dvh flex-col">
        <SyntheticDataBanner />
        <Outlet />
      </div>
    </SessionProvider>
  );
}
