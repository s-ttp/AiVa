const MICROS_PER_USD = 1_000_000;
const integer = new Intl.NumberFormat('en', { maximumFractionDigits: 0 });

/**
 * Formats integer micro-USD as US dollars without floating-point arithmetic
 * (ADR-0006): at least two decimals, up to six, trailing zeros trimmed.
 * 916 → "$0.000916", 1_250_000 → "$1.25", 0 → "$0.00".
 */
export function formatMicrosUsd(micros: number): string {
  if (!Number.isSafeInteger(micros)) throw new RangeError(`Not an integer micro-USD amount: ${micros}`);
  const sign = micros < 0 ? '−' : '';
  const absolute = Math.abs(micros);
  const fraction = absolute % MICROS_PER_USD;
  const dollars = (absolute - fraction) / MICROS_PER_USD;
  const decimals = String(fraction).padStart(6, '0').replace(/0+$/, '').padEnd(2, '0');
  return `${sign}$${integer.format(dollars)}.${decimals}`;
}

export function formatCount(value: number): string {
  return integer.format(value);
}

const percent = new Intl.NumberFormat('en', { style: 'percent', maximumFractionDigits: 0 });

/** A fraction as a whole percent, e.g. 0.8 → "80%"; the locale places the sign. */
export function formatPercent(fraction: number): string {
  return percent.format(fraction);
}

const dateTime = new Intl.DateTimeFormat('en-GB', {
  dateStyle: 'medium',
  timeStyle: 'medium',
  timeZone: 'UTC',
});

/** Timestamps are shown in UTC with the zone stated, so screenshots and exports agree. */
export function formatUtc(iso: string): string {
  const time = Date.parse(iso);
  return Number.isNaN(time) ? iso : `${dateTime.format(time)} UTC`;
}
