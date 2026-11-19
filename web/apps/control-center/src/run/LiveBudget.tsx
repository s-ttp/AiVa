import { ProvenanceBadge } from '@aiva/ui';
import { Gauge } from 'lucide-react';
import { unwrap, type LiveBudget as LiveBudgetReadout } from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { formatPercent, formatUtc } from '../format';
import { t, tPlural } from '../i18n';
import { liveRefusalText } from './executionMode';

/**
 * The live-run readout of the public demo for a visitor (GET /v1/demo/live-budget,
 * ADR-0019): the share of the public live budget remaining today, rounded down
 * to 10%, the live runs this visitor has remaining this hour, and why a live
 * run would be refused now. Coarse by design: no amounts. `refresh` reloads it
 * after a run.
 */
export function LiveBudget({ id, refresh }: { id: string; refresh: number }) {
  const { state } = useApiQuery(`demo/live-budget#${refresh}`, (client, signal) =>
    unwrap(client.GET('/v1/demo/live-budget', { signal })),
  );
  let body;
  if (state === null || state.status === 'loading') body = <p className="text-ink-muted">{t('liveBudget.loading')}</p>;
  else if (state.status === 'error') body = <p className="text-ink-muted">{t('liveBudget.error')}</p>;
  else body = <Readout budget={state.data} />;
  return (
    <div id={id} className="flex items-start gap-3 rounded-card border border-line bg-canvas p-4 text-secondary">
      <Gauge className="mt-0.5 size-5 shrink-0 text-brand-navy" />
      <div className="space-y-1">
        <p className="font-semibold text-ink">{t('liveBudget.title')}</p>
        {body}
      </div>
    </div>
  );
}

function Readout({ budget }: { budget: LiveBudgetReadout }) {
  const runsLeft = budget.visitor_runs_remaining ?? null;
  return (
    <>
      {budget.remaining_percent == null ? (
        <p>{t('liveBudget.off')}</p>
      ) : (
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
          {t('liveBudget.remaining', { percent: formatPercent(budget.remaining_percent / 100) })}
          <ProvenanceBadge provenance={budget.provenance} />
          {budget.resets_at && (
            <span className="text-ink-muted">{t('liveBudget.resets', { time: formatUtc(budget.resets_at) })}</span>
          )}
        </p>
      )}
      {runsLeft !== null && <p>{tPlural('liveBudget.visitorRuns', runsLeft)}</p>}
      {runsLeft === 0 && budget.visitor_resets_at && (
        <p>{t('liveBudget.visitorNext', { time: formatUtc(budget.visitor_resets_at) })}</p>
      )}
      {!budget.available && budget.unavailable_reason && (
        <p className="font-semibold text-ink">{liveRefusalText(budget.unavailable_reason)}</p>
      )}
    </>
  );
}
