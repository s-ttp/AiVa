import { Construction } from 'lucide-react';
import type { ApiError } from '../api/client';
import { t } from '../i18n';
import { Mono } from './inline';

/**
 * The gateway answered 501: the endpoint exists in the contract but is not
 * built yet. Shown as its own state, never as an error or as sample data.
 */
export function NotImplementedState({ error, feature }: { error: ApiError; feature: string }) {
  return (
    <div role="status" className="flex items-start gap-3 rounded-card border border-info/40 bg-info/10 p-4">
      <Construction className="mt-0.5 size-5 shrink-0 text-info" />
      <div className="space-y-1">
        <p className="font-semibold text-ink">{t('notImplemented.title', { feature })}</p>
        <p className="text-secondary">{t('notImplemented.body')}</p>
        <p className="text-secondary">{error.message}</p>
        <p className="text-secondary">
          <Mono>{error.code ?? 'NOT_IMPLEMENTED'}</Mono>
        </p>
      </div>
    </div>
  );
}
