import { Button, SelectField, TextField } from '@aiva/ui';
import { RotateCcw, X } from 'lucide-react';
import { useSession } from '../auth/session';
import { t, tRich } from '../i18n';
import { dimensionLabel, filterLabel } from './labels';
import { DEFAULT_FILTERS, lastDays, type FilterKey, type ReportParams } from './params';
import { useFilterOptions } from './useFilterOptions';

const PRESETS = [
  { days: 1, key: 'insights.window.today' },
  { days: 7, key: 'insights.window.last7' },
  { days: 30, key: 'insights.window.last30' },
] as const;

/**
 * One row of filters above the content they scope: time range first, then the
 * dimensions. Options come from the gateway for the caller's reporting scope,
 * so a department manager is only offered their own department's values.
 */
export function FilterBar({ report, keys = DEFAULT_FILTERS }: { report: ReportParams; keys?: readonly FilterKey[] }) {
  const { window, filters } = report;
  const { usersVisible } = useSession();
  const { options: loaded, label: optionLabel } = useFilterOptions(window);
  // No user filter without the user dimension (public demo visitors, ADR-0019), or until the scope says so.
  const shown = usersVisible === true ? keys : keys.filter((key) => key !== 'user');
  const active = (Object.entries(filters) as [FilterKey, string][]).filter(([, value]) => value);

  return (
    <section aria-label={t('insights.filters.label')} className="rounded-card border border-line bg-card p-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-wrap gap-2" role="group" aria-label={t('insights.window.presets')}>
          {PRESETS.map((preset) => {
            const range = lastDays(preset.days);
            const selected = range.from === window.from && range.to === window.to;
            return (
              <Button
                key={preset.days}
                variant={selected ? 'primary' : 'secondary'}
                aria-pressed={selected}
                onClick={() => report.setWindow(range)}
              >
                {t(preset.key)}
              </Button>
            );
          })}
        </div>
        <TextField
          type="date"
          label={t('insights.window.from')}
          value={window.from}
          max={window.to}
          onChange={(event) => event.target.value && report.setWindow({ from: event.target.value, to: window.to })}
        />
        <TextField
          type="date"
          label={t('insights.window.to')}
          value={window.to}
          min={window.from}
          onChange={(event) => event.target.value && report.setWindow({ from: window.from, to: event.target.value })}
        />
        <p className="pb-2 text-secondary text-ink-muted">{t('insights.window.utc')}</p>
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        {shown.map((key) => {
          const values = loaded?.[key] ?? [];
          const current = filters[key];
          return (
            <SelectField
              key={key}
              label={filterLabel(key)}
              value={current ?? ''}
              onChange={(event) => report.setFilter(key, event.target.value || null)}
            >
              <option value="">{t('insights.filters.all')}</option>
              {current && !values.some((option) => option.value === current) && (
                <option value={current}>{optionLabel(key, current)}</option>
              )}
              {values.map((option) => (
                <option key={option.value} value={option.value}>
                  {dimensionLabel(key, option.value, option.label)}
                </option>
              ))}
            </SelectField>
          );
        })}
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-2" aria-live="polite">
        {active.length === 0 ? (
          <p className="text-secondary text-ink-muted">{t('insights.filters.none')}</p>
        ) : (
          <>
            <h3 className="text-secondary font-semibold text-ink">{t('insights.filters.active')}</h3>
            <ul className="flex flex-wrap gap-2">
              {active.map(([key, value]) => (
                <li key={key}>
                  <button
                    type="button"
                    onClick={() => report.setFilter(key, null)}
                    aria-label={t('insights.filters.remove', { filter: filterLabel(key), value: optionLabel(key, value) })}
                    className="inline-flex items-center gap-1.5 rounded-full border border-line bg-canvas px-2.5 py-0.5 text-secondary text-ink hover:border-ink-muted"
                  >
                    <span>
                      {tRich('insights.filters.chip', {
                        filter: filterLabel(key),
                        value: (
                          <strong className="font-semibold">
                            <bdi>{optionLabel(key, value)}</bdi>
                          </strong>
                        ),
                      })}
                    </span>
                    <X className="size-3.5" aria-hidden="true" />
                  </button>
                </li>
              ))}
            </ul>
            <Button variant="secondary" onClick={report.resetFilters}>
              <RotateCcw className="size-4" aria-hidden="true" />
              {t('insights.filters.reset')}
            </Button>
          </>
        )}
      </div>
    </section>
  );
}
