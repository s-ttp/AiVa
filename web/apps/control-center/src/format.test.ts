import { describe, expect, it } from 'vitest';
import { formatDurationMs, formatMicrosUsd, formatUtc, percentOfCap, weakestProvenance } from './format';

describe('formatMicrosUsd', () => {
  it.each([
    [0, '$0.00'],
    [916, '$0.000916'],
    [20_556, '$0.020556'],
    [1_250_000, '$1.25'],
    [2_000_000_000, '$2,000.00'],
    [1, '$0.000001'],
    [-19_640, '−$0.01964'],
  ])('%i micros → %s', (micros, expected) => {
    expect(formatMicrosUsd(micros)).toBe(expected);
  });

  it('refuses non-integer amounts instead of rounding them', () => {
    expect(() => formatMicrosUsd(0.5)).toThrow(RangeError);
  });
});

describe('formatDurationMs', () => {
  it.each([
    [0.04, '0.04 ms'],
    [5.384, '5.4 ms'],
    [1530, '1.53 s'],
  ])('%f → %s', (ms, expected) => {
    expect(formatDurationMs(ms)).toBe(expected);
  });
});

describe('formatUtc', () => {
  it('states the UTC zone', () => {
    expect(formatUtc('2026-10-14T09:30:05Z')).toBe('14 Oct 2026, 09:30:05 UTC');
  });
});

describe('weakestProvenance', () => {
  it('never upgrades a simulated or estimated input', () => {
    expect(weakestProvenance('measured', 'estimated')).toBe('estimated');
    expect(weakestProvenance('estimated', 'simulated')).toBe('simulated');
    expect(weakestProvenance('measured')).toBe('measured');
  });
});

describe('percentOfCap', () => {
  it('rounds down to whole percent without floats and refuses a zero cap', () => {
    expect(percentOfCap(2_668, 3_000)).toBe(88);
    expect(percentOfCap(3_000, 3_000)).toBe(100);
    expect(percentOfCap(0, 400_000_000)).toBe(0);
    expect(percentOfCap(5, 0)).toBeNull();
  });
});
