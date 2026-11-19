/** Narrows away null/undefined in tests, failing with a message instead of a non-null assertion. */
export function required<T>(value: T | null | undefined, what = 'value'): T {
  if (value === null || value === undefined) throw new Error(`Expected ${what} to be present`);
  return value;
}
