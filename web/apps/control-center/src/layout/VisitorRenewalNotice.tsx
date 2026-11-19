import { Button } from '@aiva/ui';
import { Info } from 'lucide-react';
import { useSession } from '../auth/session';
import { t } from '../i18n';

/**
 * Says once that a public visitor's two-hour session ended and a new one
 * started. The new session is a new pseudonymous visitor (ADR-0019), so runs
 * from the earlier session no longer appear.
 */
export function VisitorRenewalNotice() {
  const { state, acknowledgeRenewal } = useSession();
  if (state.status !== 'signedIn' || !state.renewed) return null;
  return (
    <div role="status" className="mb-6 flex flex-wrap items-start gap-3 rounded-card border border-info/40 bg-info/10 p-4">
      <Info className="mt-0.5 size-5 shrink-0 text-info" />
      <p className="grow basis-64 text-secondary">{t('session.renewed')}</p>
      <Button variant="secondary" onClick={acknowledgeRenewal}>
        {t('session.renewed.dismiss')}
      </Button>
    </div>
  );
}
