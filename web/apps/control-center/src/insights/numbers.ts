import type { Provenance } from '@aiva/ui';
import { formatCount, formatList, formatMicrosUsd, isProvenance, weakestProvenance } from '../format';
import { t } from '../i18n';
import type { Amount, ProvenanceSplit, Ratio, ReportMetadata } from './types';

const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
const percent = {
  whole: new Intl.NumberFormat('en', { style: 'percent', maximumFractionDigits: 0 }),
  tenth: new Intl.NumberFormat('en', { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1 }),
};

/** Large counts compactly (12.9K), small ones exactly. */
export function formatCompact(value: number): string {
  return Math.abs(value) < 10_000 ? formatCount(value) : compact.format(value);
}

/** A ratio as a whole or one-decimal percentage; null when undefined (0 / 0). */
export function formatRatio(ratio: Ratio): string | null {
  if (ratio.value === null || ratio.value === undefined) return null;
  const value = ratio.value;
  return (Math.abs(value) >= 0.1 || value === 0 ? percent.whole : percent.tenth).format(value);
}

const score = new Intl.NumberFormat('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** A calibrated score in [0, 1], e.g. a classifier confidence, to two decimals. */
export function formatScore(value: number): string {
  return score.format(value);
}

export function provenanceOf(value: { provenance?: string | null } | null | undefined): Provenance {
  return isProvenance(value?.provenance) ? value.provenance : 'simulated';
}

/** Counts made at the gateway are measured facts, unless the traffic itself was synthetic (ADR-0006). */
export function countProvenance(designation: string): Provenance {
  return designation === 'actual' ? 'measured' : designation === 'none' ? 'measured' : 'synthetic';
}

/** The rate cards of the charges counted, or the pricing snapshot when there are none. */
export function rateCards(metadata: ReportMetadata): string {
  const versions = metadata.rate_card_versions?.length ? metadata.rate_card_versions : [metadata.pricing_snapshot];
  return formatList(versions);
}

export function amountText(amount: Amount): string {
  return formatMicrosUsd(amount.value);
}

/** The non-zero parts of a provenance split, least trustworthy first. */
export function splitParts(split: ProvenanceSplit): { provenance: Provenance; value: number }[] {
  const order: Provenance[] = ['simulated', 'replayed', 'synthetic', 'estimated', 'measured'];
  return order.map((provenance) => ({ provenance, value: split[provenance] ?? 0 })).filter((part) => part.value !== 0);
}

/** Bucket labels: the hour for windows of up to two days, the day otherwise (both UTC). */
export function bucketLabel(bucket: string, granularity: 'hour' | 'day'): string {
  return granularity === 'hour'
    ? t('insights.bucket.hour', { date: bucket.slice(5, 10), time: bucket.slice(11, 16) })
    : bucket.slice(0, 10);
}

export { formatCount, formatMicrosUsd, weakestProvenance };
