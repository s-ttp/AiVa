import { Badge, ProvenanceBadge } from '@aiva/ui';
import { CircleCheck, CircleX, Info } from 'lucide-react';
import { FactList } from '../components/FactList';
import { t, tPlural } from '../i18n';
import { ChartFigure, Waterfall, type WaterfallStep } from './charts';
import { MoneyCell } from './figures';
import { formatCount, formatMicrosUsd, provenanceOf } from './numbers';
import type { BaselineComparison, SavingsWaterfall } from './types';

const TOTALS = new Set(['frontier_baseline', 'net']);

/**
 * Estimated savings versus the pinned frontier baseline (LLD §21.4, §22.7):
 * baseline → cache → context → routing → prompt cache → overheads → net. The
 * components are separated so nothing is counted twice; the steps are
 * modelled with the pricing snapshot and labelled estimated. The opt-in
 * baseline comparison follows the net as a measurement cost, never
 * subtracted from the savings.
 */
/**
 * A step's label. Context packaging can shrink or grow the context, so its
 * label says which, matching the sign: never a "reduction" that added cost.
 */
function stepLabel(step: SavingsWaterfall['steps'][number]): string {
  if (step.step === 'context' && step.amount_micros !== 0) {
    return t(step.amount_micros < 0 ? 'insights.savings.step.context.reduced' : 'insights.savings.step.context.added');
  }
  return t(`insights.savings.step.${step.step}`);
}

export function SavingsChart({ savings }: { savings: SavingsWaterfall }) {
  const steps: WaterfallStep[] = savings.steps.map((step) => ({
    key: step.step,
    label: stepLabel(step),
    amount: step.amount_micros,
    kind: TOTALS.has(step.step) ? 'total' : 'change',
    display: formatMicrosUsd(step.amount_micros),
  }));
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <ProvenanceBadge provenance={provenanceOf(savings)} />
        <Badge icon={Info}>{t('insights.savings.notQualityAdjusted')}</Badge>
      </div>
      <ChartFigure
        title={t('insights.savings.chart.title', { model: savings.frontier_model_id })}
        description={t('insights.savings.chart.description', {
          net: formatMicrosUsd(savings.net_savings_micros),
          pricing: savings.pricing_snapshot,
        })}
        legend={[
          { key: 'reduces', label: t('insights.savings.legend.reduces'), slot: 1 },
          { key: 'adds', label: t('insights.savings.legend.adds'), slot: 2 },
        ]}
        table={{
          caption: t('insights.savings.table.caption'),
          columns: [{ label: t('insights.col.step') }, { label: t('insights.col.amount'), numeric: true }, { label: t('insights.col.provenance') }],
          rows: savings.steps.map((step) => ({
            key: step.step,
            cells: [
              stepLabel(step),
              formatMicrosUsd(step.amount_micros),
              <ProvenanceBadge key="p" provenance={provenanceOf(step)} />,
            ],
          })),
        }}
      >
        <Waterfall steps={steps} />
      </ChartFigure>
      <div className="space-y-1">
        <p className="flex flex-wrap items-center gap-2 font-medium text-ink">
          <span>{t('insights.savings.measurement', { amount: formatMicrosUsd(savings.measurement_cost.value) })}</span>
          <ProvenanceBadge provenance={provenanceOf(savings.measurement_cost)} />
        </p>
        <p className="text-secondary text-ink-muted">{t('insights.savings.measurement.detail')}</p>
        <p className="text-secondary text-ink-muted">{t('insights.savings.gpuReplay')}</p>
      </div>
      <details className="text-secondary">
        <summary className="cursor-pointer font-medium text-brand-navy underline underline-offset-2">
          {t('insights.savings.methodology')}
        </summary>
        <ul className="mt-2 list-disc space-y-1 ps-5 text-ink">
          <li>{t('insights.savings.method.baseline')}</li>
          <li>{t('insights.savings.method.cache')}</li>
          <li>{t('insights.savings.method.context')}</li>
          <li>{t('insights.savings.method.routing')}</li>
          <li>{t('insights.savings.method.overheads')}</li>
          <li>{t('insights.savings.method.gpu')}</li>
          <li>{t('insights.savings.method.limits')}</li>
        </ul>
      </details>
    </div>
  );
}

/** The opt-in, metered frontier comparison on the same payload, with quality pass/fail (PLAN §4.3). */
export function BaselineCard({ baseline }: { baseline: BaselineComparison }) {
  if (baseline.runs === 0) {
    return <p className="text-ink-muted">{t('insights.baseline.empty', { model: baseline.frontier_model_id })}</p>;
  }
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <Badge icon={CircleCheck} tone="success">
          {tPlural('insights.baseline.passed', baseline.quality_passed_runs)}
        </Badge>
        <Badge icon={CircleX} tone={baseline.quality_failed_runs ? 'danger' : 'neutral'}>
          {tPlural('insights.baseline.failed', baseline.quality_failed_runs)}
        </Badge>
        <Badge icon={Info}>{t('insights.baseline.notGraded')}</Badge>
      </div>
      <FactList
        facts={[
          { label: t('insights.baseline.runs'), value: formatCount(baseline.runs) },
          { label: t('insights.baseline.frontierCost', { model: baseline.frontier_model_id }), value: <MoneyCell amount={baseline.baseline} /> },
          { label: t('insights.baseline.actualCost'), value: <MoneyCell amount={baseline.actual} /> },
          { label: t('insights.baseline.savings'), value: <MoneyCell amount={baseline.savings} /> },
          { label: t('insights.baseline.gated'), value: <MoneyCell amount={baseline.quality_gated_savings} /> },
        ]}
      />
      <p className="text-secondary text-ink-muted">{t('insights.baseline.note')}</p>
    </div>
  );
}
