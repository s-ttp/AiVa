import { cx } from '@aiva/ui';
import { useId, useState, type ReactNode } from 'react';
import { DataTable } from '../components/DataTable';
import { t } from '../i18n';
import './charts.css';

/**
 * Accessible charts for AiVa Insights (LLD §22.4, §22.7). The drawing is
 * decorative for assistive technology: every chart has a caption, a legend
 * when it shows more than one series, and the same numbers in a data table
 * one click away. Hover readouts enhance, never gate. Series colours follow
 * the entity in a fixed order (insights/charts.css) and text keeps text colours.
 */

export interface Series {
  key: string;
  label: string;
  /** 1-based slot of the categorical palette (charts.css), fixed per entity. */
  slot: 1 | 2 | 3 | 4;
}

interface ChartFigureProps {
  title: string;
  description?: ReactNode;
  legend?: readonly Series[];
  table: { caption: string; columns: ReadonlyArray<{ label: string; numeric?: boolean }>; rows: ReadonlyArray<{ key: string; cells: readonly ReactNode[] }> };
  children: ReactNode;
}

/** A captioned chart with its legend and a data-table equivalent. */
export function ChartFigure({ title, description, legend, table, children }: ChartFigureProps) {
  const titleId = useId();
  return (
    <figure aria-labelledby={titleId} className="aiva-viz min-w-0 space-y-3">
      <figcaption>
        <span id={titleId} className="block text-body font-semibold text-brand-navy">
          {title}
        </span>
        {description && <span className="block text-secondary text-ink-muted">{description}</span>}
      </figcaption>
      {legend && legend.length > 1 && <Legend series={legend} />}
      <div aria-hidden="true">{children}</div>
      <details className="text-secondary">
        <summary className="cursor-pointer font-medium text-brand-navy underline underline-offset-2">
          {t('insights.chart.showTable')}
        </summary>
        <div className="mt-3">
          <DataTable caption={table.caption} columns={table.columns} rows={table.rows} minWidth="24rem" />
        </div>
      </details>
    </figure>
  );
}

export function Legend({ series }: { series: readonly Series[] }) {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-secondary text-ink" aria-label={t('insights.chart.legend')}>
      {series.map((item) => (
        <li key={item.key} className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className={`aiva-viz-swatch aiva-viz-slot-${item.slot}`} />
          <bdi>{item.label}</bdi>
        </li>
      ))}
    </ul>
  );
}

export interface BarDatum {
  key: string;
  label: string;
  value: number;
  /** Formatted value shown at the bar tip and in the readout. */
  display: string;
  /** Optional second line of the hover readout. */
  detail?: string;
}

/** Horizontal bars for one measure, longest first: a ranking (LLD §22.7). */
export function BarList({ data, emphasis }: { data: readonly BarDatum[]; emphasis?: string }) {
  const [active, setActive] = useState<string | null>(null);
  const max = Math.max(0, ...data.map((d) => d.value));
  const ranked = [...data].sort((a, b) => b.value - a.value);
  // Room for the widest value and the gap before it, so a narrow track (320 CSS px) shrinks every bar by the
  // same factor instead of pushing its value past the edge.
  const reserve = `${Math.max(0, ...data.map((d) => d.display.length))}ch + 0.5rem`;
  return (
    <div className="space-y-2" onPointerLeave={() => setActive(null)}>
      {ranked.map((datum) => {
        // The longest bar takes 80% of the track, leaving room for its value at the tip.
        const width = max > 0 ? Math.max(datum.value > 0 ? 0.5 : 0, (datum.value / max) * 80) : 0;
        const share = max > 0 ? datum.value / max : 0;
        const muted = emphasis !== undefined && datum.key !== emphasis;
        return (
          <div
            key={datum.key}
            className="grid grid-cols-[minmax(6rem,min(14rem,40%))_1fr] items-center gap-3"
            onPointerEnter={() => setActive(datum.key)}
          >
            <span className="truncate text-secondary text-ink" title={datum.label}>
              <bdi>{datum.label}</bdi>
            </span>
            <div className="relative flex min-w-0 items-center gap-2">
              <div
                className={cx('aiva-viz-bar shrink-0', muted ? 'aiva-viz-muted' : 'aiva-viz-slot-1', active === datum.key && 'aiva-viz-active')}
                style={{ inlineSize: `min(${width}%, max(1px, (100% - (${reserve})) * ${share}))` }}
              />
              <bdi className="shrink-0 text-secondary tabular-nums text-ink">{datum.display}</bdi>
              {active === datum.key && datum.detail && (
                <span className="aiva-viz-tooltip" style={{ insetInlineStart: `${Math.min(width, 70)}%` }}>
                  <strong className="block text-ink"><bdi>{datum.display}</bdi></strong>
                  <bdi className="block text-ink-muted">{datum.label}</bdi>
                  <bdi className="block text-ink-muted">{datum.detail}</bdi>
                </span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export interface ColumnDatum {
  key: string;
  label: string;
  values: Readonly<Record<string, number>>;
}

/** Stacked columns over time, one segment per series, 2px surface gaps between segments. */
export function StackedColumns({
  data,
  series,
  format,
}: {
  data: readonly ColumnDatum[];
  series: readonly Series[];
  format: (value: number) => string;
}) {
  const [active, setActive] = useState<string | null>(null);
  const totals = data.map((d) => series.reduce((sum, s) => sum + Math.max(0, d.values[s.key] ?? 0), 0));
  const max = Math.max(0, ...totals);
  const ticks = max > 0 ? [max, max / 2, 0] : [0];
  const labelEvery = Math.max(1, Math.ceil(data.length / 8));
  return (
    <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-2">
      <div className="flex h-48 flex-col justify-between text-end text-secondary tabular-nums text-ink-muted">
        {ticks.map((tick) => (
          <bdi key={tick}>{format(Math.round(tick))}</bdi>
        ))}
      </div>
      <div className="relative h-48 border-b border-line" onPointerLeave={() => setActive(null)}>
        <div className="aiva-viz-gridline" style={{ insetBlockStart: '0%' }} />
        <div className="aiva-viz-gridline" style={{ insetBlockStart: '50%' }} />
        <div className="absolute inset-0 flex items-end gap-0.5">
          {data.map((datum, index) => {
            const total = totals[index] ?? 0;
            return (
              <div
                key={datum.key}
                className={cx('relative flex h-full min-w-0 flex-1 flex-col-reverse items-center', active === datum.key && 'aiva-viz-active')}
                onPointerEnter={() => setActive(datum.key)}
              >
                <div className="flex w-full max-w-6 flex-col-reverse gap-0.5" style={{ blockSize: max > 0 ? `${(total / max) * 100}%` : 0 }}>
                  {series.map((s) => {
                    const value = Math.max(0, datum.values[s.key] ?? 0);
                    return value > 0 ? (
                      <div
                        key={s.key}
                        className={`aiva-viz-segment aiva-viz-slot-${s.slot}`}
                        style={{ flexGrow: value, flexBasis: 0 }}
                      />
                    ) : null;
                  })}
                </div>
                {active === datum.key && (
                  <span className="aiva-viz-tooltip bottom-full mb-1">
                    <bdi className="block font-semibold text-ink">{datum.label}</bdi>
                    {series.map((s) => (
                      <span key={s.key} className="flex items-center gap-1.5 whitespace-nowrap">
                        <span className={`aiva-viz-key aiva-viz-slot-${s.slot}`} />
                        <strong className="tabular-nums text-ink"><bdi>{format(datum.values[s.key] ?? 0)}</bdi></strong>
                        <bdi className="text-ink-muted">{s.label}</bdi>
                      </span>
                    ))}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </div>
      <span />
      <div className="mt-1 flex gap-0.5 text-secondary text-ink-muted">
        {data.map((datum, index) => (
          <span key={datum.key} className="min-w-0 flex-1 truncate text-center">
            {index % labelEvery === 0 ? <bdi>{datum.label}</bdi> : ''}
          </span>
        ))}
      </div>
    </div>
  );
}

export interface WaterfallStep {
  key: string;
  label: string;
  /** Signed change; totals carry their absolute value. */
  amount: number;
  kind: 'total' | 'change';
  display: string;
}

/** Where each step starts and ends: totals from zero, changes from the running total. */
function waterfallSpans(steps: readonly WaterfallStep[]): { step: WaterfallStep; start: number; end: number }[] {
  const spans: { step: WaterfallStep; start: number; end: number }[] = [];
  let running = 0;
  for (const step of steps) {
    const start = step.kind === 'total' ? 0 : running;
    const end = step.kind === 'total' ? step.amount : running + step.amount;
    running = end;
    spans.push({ step, start, end });
  }
  return spans;
}

/** Baseline → reductions and additions → net, as horizontal floating bars (LLD §22.7). */
export function Waterfall({ steps }: { steps: readonly WaterfallStep[] }) {
  const spans = waterfallSpans(steps);
  const low = Math.min(0, ...spans.map((s) => Math.min(s.start, s.end)));
  const high = Math.max(0, ...spans.map((s) => Math.max(s.start, s.end)));
  const range = high - low || 1;
  return (
    <div className="space-y-2">
      {spans.map(({ step, start, end }) => {
        const from = (Math.min(start, end) - low) / range;
        const size = Math.abs(end - start) / range;
        const slot = step.kind === 'total' ? 'aiva-viz-total' : step.amount < 0 ? 'aiva-viz-slot-1' : 'aiva-viz-slot-2';
        return (
          <div key={step.key} className="grid grid-cols-[minmax(6rem,12rem)_1fr_auto] items-center gap-3">
            <bdi className="text-secondary text-ink">{step.label}</bdi>
            <div className="relative h-4">
              <div
                className={cx('aiva-viz-bar absolute inset-y-0', slot)}
                style={{ insetInlineStart: `${from * 100}%`, inlineSize: `${Math.max(size * 100, step.amount ? 0.5 : 0)}%` }}
              />
            </div>
            <bdi className="text-end text-secondary tabular-nums text-ink">{step.display}</bdi>
          </div>
        );
      })}
    </div>
  );
}

/** A budget meter: spend against a cap, with alert thresholds marked (LLD §22.7 budget utilization). */
export function Meter({ fraction, thresholds, warn }: { fraction: number; thresholds: readonly number[]; warn: boolean }) {
  return (
    <div aria-hidden="true" className="relative h-2 rounded-full bg-line">
      <div
        className={cx('h-2 rounded-full', warn ? 'bg-warning' : 'bg-brand-teal')}
        style={{ inlineSize: `${Math.min(100, Math.max(fraction > 0 ? 1 : 0, fraction * 100))}%` }}
      />
      {thresholds.map((threshold) => (
        <span
          key={threshold}
          className="absolute -top-0.5 h-3 w-0.5 bg-ink"
          style={{ insetInlineStart: `${Math.min(100, threshold * 100)}%` }}
        />
      ))}
    </div>
  );
}
