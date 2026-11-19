import { unwrap } from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { dimensionLabel } from './labels';
import { reportQuery, type FilterKey, type ReportWindow } from './params';
import type { ReportFilterOptions } from './types';

/**
 * Filter values and their names for the window, as the gateway offers them in
 * the caller's scope. `label` falls back to the value until they arrive.
 */
export function useFilterOptions(window: ReportWindow): {
  options: ReportFilterOptions['options'] | null;
  label: (key: FilterKey, value: string) => string;
} {
  const { state } = useApiQuery(`reports/filters?${window.from}&${window.to}`, (client, signal) =>
    unwrap(
      client.GET('/v1/reports/filters', {
        params: { query: reportQuery(window, {}) as { from: string; to: string } },
        signal,
      }),
    ),
  );
  const options = state?.status === 'success' ? state.data.options : null;
  return {
    options,
    label: (key, value) => dimensionLabel(key, value, options?.[key]?.find((option) => option.value === value)?.label),
  };
}
