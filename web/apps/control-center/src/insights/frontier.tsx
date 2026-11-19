import { Ltr, ProvenanceBadge } from '@aiva/ui';
import { t, tPlural } from '../i18n';
import { formatMicrosUsd, provenanceOf, rateCards, weakestProvenance } from './numbers';
import type { Amount, BaselineComparison, ReportMetadata } from './types';

/**
 * The one savings figure a public visitor sees (Story mode): what the pinned
 * frontier model cost against what AiVa's route cost, on the requests that
 * really sent it a baseline, with the method in one line. Both bars are
 * ledger amounts with their own provenance. The modelled savings waterfall
 * is for administrators and finance (Story mode).
 */
export function FrontierComparison({
  baseline,
  metadata,
  waterfallHidden = false,
}: {
  baseline: BaselineComparison;
  metadata: ReportMetadata;
  waterfallHidden?: boolean;
}) {
  const model = baseline.frontier_model_name;
  if (baseline.runs === 0) {
    return <p className="text-ink-muted">{t('insights.frontier.empty', { model })}</p>;
  }
  const bars: { key: string; label: string; amount: Amount }[] = [
    { key: 'frontier', label: t('insights.frontier.frontier'), amount: baseline.baseline },
    { key: 'aiva', label: t('insights.frontier.aiva'), amount: baseline.actual },
  ];
  const scale = Math.max(...bars.map((bar) => bar.amount.value), 1);
  return (
    <figure className="space-y-4" aria-label={t('insights.frontier.title')}>
      <figcaption className="text-secondary text-ink-muted">
        {tPlural('insights.frontier.caption', baseline.runs, { model })}
      </figcaption>
      <dl className="grid grid-cols-[minmax(0,9rem)_minmax(0,1fr)_auto] items-center gap-x-4 gap-y-3">
        {bars.map((bar) => (
          <div key={bar.key} className="contents">
            <dt className="font-semibold text-brand-navy">{bar.label}</dt>
            <dd className="contents">
              <span aria-hidden="true" className="block h-5 rounded-full bg-canvas">
                <span
                  className={`block h-5 rounded-full ${bar.key === 'frontier' ? 'bg-ink-muted' : 'bg-brand-teal'}`}
                  style={{ inlineSize: `${barPercent(bar.amount.value, scale)}%` }}
                />
              </span>
              <span className="inline-flex items-center gap-2 justify-self-end">
                <Ltr className="text-body font-semibold tabular-nums">{formatMicrosUsd(bar.amount.value)}</Ltr>
                <ProvenanceBadge provenance={provenanceOf(bar.amount)} />
              </span>
              {bar.key === 'aiva' && (
                <span className="col-start-2 col-end-4 text-secondary text-ink-muted">{t('insights.frontier.inCountry')}</span>
              )}
            </dd>
          </div>
        ))}
      </dl>
      <p className="text-secondary text-ink-muted">
        {t('insights.frontier.method', { pricing: pricingText(baseline, metadata), quality: qualityText(baseline) })}
        {waterfallHidden ? ` ${t('insights.frontier.waterfallHidden')}` : ''}
      </p>
    </figure>
  );
}

/** A bar's share of the larger amount; a non-zero amount stays visible however small. */
function barPercent(value: number, scale: number): number {
  if (value <= 0) return 0;
  return Math.max((value / scale) * 100, 1);
}

/** How the two amounts were priced, from their weakest provenance and the charges' rate cards. */
function pricingText(baseline: BaselineComparison, metadata: ReportMetadata): string {
  const provenance = weakestProvenance(provenanceOf(baseline.baseline), provenanceOf(baseline.actual));
  return t(`insights.frontier.pricing.${provenance}`, { version: rateCards(metadata) });
}

function qualityText(baseline: BaselineComparison): string {
  return baseline.quality_passed_runs === baseline.runs
    ? tPlural('insights.frontier.quality.all', baseline.runs)
    : t('insights.frontier.quality.some', {
        passed: baseline.quality_passed_runs,
        runs: baseline.runs,
      });
}
