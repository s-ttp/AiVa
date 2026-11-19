import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router';

/**
 * Report window and global filters (LLD §22.6), kept in the URL so every
 * drilldown step is a link that can be shared, reloaded and walked back.
 * Windows are whole UTC days, inclusive.
 */
export const FILTER_KEYS = [
  'department_id',
  'application_id',
  'user',
  'model_id',
  'provider',
  'business_function',
  'task_category',
  'intent',
  'complexity',
  'sensitivity',
  'language',
  'execution_path',
  'source_policy',
  'environment',
  'outcome',
  'data_designation',
] as const;

export type FilterKey = (typeof FILTER_KEYS)[number];

/** The global filters most views offer (LLD §22.6); a view may pass its own list. */
export const DEFAULT_FILTERS: readonly FilterKey[] = [
  'department_id',
  'application_id',
  'user',
  'model_id',
  'task_category',
  'sensitivity',
  'execution_path',
  'source_policy',
  'language',
  'environment',
];
export type Filters = Partial<Record<FilterKey, string>>;

export interface ReportWindow {
  from: string;
  to: string;
}

const DAY_MS = 86_400_000;
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

export function utcDay(time: number): string {
  return new Date(time).toISOString().slice(0, 10);
}

/** The last `days` UTC days, today included. */
export function lastDays(days: number, now: number = Date.now()): ReportWindow {
  return { from: utcDay(now - (days - 1) * DAY_MS), to: utcDay(now) };
}

export const DEFAULT_DAYS = 7;

export function parseWindow(params: URLSearchParams, now: number = Date.now()): ReportWindow {
  const fallback = lastDays(DEFAULT_DAYS, now);
  const from = params.get('from');
  const to = params.get('to');
  if (!from || !to || !ISO_DAY.test(from) || !ISO_DAY.test(to) || from > to) return fallback;
  return { from, to };
}

export function parseFilters(params: URLSearchParams): Filters {
  const filters: Filters = {};
  for (const key of FILTER_KEYS) {
    const value = params.get(key);
    if (value) filters[key] = value;
  }
  return filters;
}

/** Query parameters for a report call: window, filters and any extras (group_by, cursor…). */
export function reportQuery(
  window: ReportWindow,
  filters: Filters,
  extra: Record<string, string | number | undefined> = {},
): Record<string, string | number> {
  const query: Record<string, string | number> = { from: window.from, to: window.to };
  for (const [key, value] of Object.entries({ ...filters, ...extra })) {
    if (value !== undefined && value !== '') query[key] = value;
  }
  return query;
}

/** A link to an Insights view with the given window and filters. */
export function reportLink(path: string, window: ReportWindow, filters: Filters, extra: Record<string, string> = {}): string {
  const params = new URLSearchParams({ from: window.from, to: window.to });
  for (const [key, value] of Object.entries({ ...filters, ...extra })) {
    if (value) params.set(key, value);
  }
  return `${path}?${params.toString()}`;
}

export interface ReportParams {
  window: ReportWindow;
  filters: Filters;
  params: URLSearchParams;
  setWindow: (window: ReportWindow) => void;
  setFilter: (key: FilterKey, value: string | null) => void;
  setParams: (changes: Record<string, string | null>) => void;
  resetFilters: () => void;
}

/** URL-backed report state for the current view. */
export function useReportParams(): ReportParams {
  const [params, setSearchParams] = useSearchParams();
  const window = useMemo(() => parseWindow(params), [params]);
  const filters = useMemo(() => parseFilters(params), [params]);

  const setParams = useCallback(
    (changes: Record<string, string | null>) => {
      setSearchParams((current) => {
        const next = new URLSearchParams(current);
        for (const [key, value] of Object.entries(changes)) {
          if (value === null || value === '') next.delete(key);
          else next.set(key, value);
        }
        return next;
      });
    },
    [setSearchParams],
  );

  return {
    window,
    filters,
    params,
    setParams,
    setWindow: (next) => setParams({ from: next.from, to: next.to }),
    setFilter: (key, value) => setParams({ [key]: value }),
    resetFilters: () => setParams(Object.fromEntries(FILTER_KEYS.map((key) => [key, null]))),
  };
}
