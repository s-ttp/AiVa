import type { Provenance } from '@aiva/ui';

/** Shared with the Context Lab; money stays integer micro-USD end to end (ADR-0006). */
export { formatCount, formatMicrosUsd, formatPercent, formatUtc } from '@aiva/ui';

/** Whole percent of a cap by integer arithmetic (ADR-0006); null when there is no cap to compare with. */
export function percentOfCap(micros: number, cap: number): number | null {
  if (cap <= 0) return null;
  const scaled = micros * 100;
  return (scaled - (scaled % cap)) / cap;
}

const sizes = {
  byte: new Intl.NumberFormat('en', { style: 'unit', unit: 'byte', unitDisplay: 'long' }),
  kilobyte: new Intl.NumberFormat('en', { style: 'unit', unit: 'kilobyte', maximumFractionDigits: 1 }),
  megabyte: new Intl.NumberFormat('en', { style: 'unit', unit: 'megabyte', maximumFractionDigits: 1 }),
};

/** A file size in bytes, KB or MB (1 KB = 1,024 bytes). */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return sizes.byte.format(bytes);
  if (bytes < 1024 * 1024) return sizes.kilobyte.format(bytes / 1024);
  return sizes.megabyte.format(bytes / (1024 * 1024));
}

const lists = {
  and: new Intl.ListFormat('en', { style: 'long', type: 'conjunction' }),
  plain: new Intl.ListFormat('en', { style: 'short', type: 'unit' }),
};

/**
 * Joins items the way the locale writes lists: `and` for prose ("Tester and
 * Finance analyst"), `plain` for identifiers and values ("GCT-001, GCT-002").
 */
export function formatList(items: readonly string[], kind: keyof typeof lists = 'plain'): string {
  return lists[kind].format(items);
}

/** Milliseconds with precision suited to sub-millisecond pipeline stages. */
export function formatDurationMs(ms: number): string {
  if (ms < 1) return `${ms.toFixed(2)} ms`;
  if (ms < 1000) return `${ms.toFixed(1)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

/** Least trustworthy first, mirroring the gateway's run provenance order. */
const TRUST_ORDER: readonly Provenance[] = ['simulated', 'replayed', 'synthetic', 'estimated', 'measured'];

export function isProvenance(value: unknown): value is Provenance {
  return typeof value === 'string' && (TRUST_ORDER as readonly string[]).includes(value);
}

/**
 * The least trustworthy of the inputs: a value derived from a simulated input
 * is simulated, one derived from an estimate is at best estimated.
 */
export function weakestProvenance(...labels: readonly Provenance[]): Provenance {
  return labels.reduce<Provenance>(
    (weakest, label) => (TRUST_ORDER.indexOf(label) < TRUST_ORDER.indexOf(weakest) ? label : weakest),
    'measured',
  );
}
