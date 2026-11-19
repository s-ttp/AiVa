import { useId } from 'react';
import { t } from '../strings';
import { ProvenanceBadge, type Provenance } from './badges';
import { Ltr } from './Ltr';

/** The "Data provenance" facts every KPI must disclose (LLD §22.9). */
export interface ProvenanceDetail {
  source: string;
  calculation: string;
  window: string;
  pricingVersion?: string;
  cacheAccounting?: string;
  limitations?: string;
}

const DETAIL_FIELDS = [
  'source',
  'calculation',
  'window',
  'pricingVersion',
  'cacheAccounting',
  'limitations',
] as const satisfies ReadonlyArray<keyof ProvenanceDetail>;

interface KpiTileProps {
  label: string;
  /** Pre-formatted value; `null` renders an explicit "No data yet". */
  value: string | null;
  /** Where the value comes from; `null` when the value is not a number (e.g. "Not applicable"). */
  provenance: Provenance | null;
  detail: ProvenanceDetail;
}

export function KpiTile({ label, value, provenance, detail }: KpiTileProps) {
  const labelId = useId();
  return (
    <div
      role="group"
      aria-labelledby={labelId}
      className="flex flex-col gap-3 rounded-card border border-line bg-card p-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p id={labelId} className="text-secondary font-medium text-ink-muted">
          {label}
        </p>
        {provenance && <ProvenanceBadge provenance={provenance} />}
      </div>
      {value === null ? (
        <p className="text-body text-ink-muted">{t('kpi.noData')}</p>
      ) : (
        <p className="text-kpi font-semibold tabular-nums text-brand-navy">
          <Ltr>{value}</Ltr>
        </p>
      )}
      <details className="text-secondary">
        <summary className="cursor-pointer font-medium text-brand-navy underline underline-offset-2">
          {t('provenance.label')}
        </summary>
        <dl className="mt-2 space-y-2">
          {DETAIL_FIELDS.map((field) =>
            detail[field] ? (
              <div key={field}>
                <dt className="font-medium text-ink">{t(`provenance.${field}`)}</dt>
                <dd className="text-ink-muted">{detail[field]}</dd>
              </div>
            ) : null,
          )}
        </dl>
      </details>
    </div>
  );
}
