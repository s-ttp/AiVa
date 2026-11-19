import { ProvenanceBadge } from '@aiva/ui';
import type { CSSProperties } from 'react';
import { t, tPlural } from '../i18n';
import { ChartFigure } from './charts';
import { formatCount, provenanceOf, weakestProvenance } from './numbers';
import type { PackingSummary, TokenUsage, TrafficFlow } from './types';

/*
 * The data figures of Story mode's "What AiVa did this week" (the Value screen).
 * UI lays the screen out; each figure here takes a report payload as it comes
 * from the gateway and carries its own copy, provenance and data table. The
 * savings figure is FrontierComparison (./frontier). Chart colours follow the
 * Insights palette; counts are text, never written on a series colour.
 */

type FlowBucket = TrafficFlow['stopped'][number]['bucket'] | TrafficFlow['routed'][number]['bucket'];

const ROUTE_COLOURS: Record<TrafficFlow['routed'][number]['bucket'], string> = {
  cache: 'aiva-viz-slot-4',
  sovereign: 'aiva-viz-slot-1',
  local_fast: 'aiva-viz-total',
  cim_packaged: 'aiva-viz-slot-3',
  full_context: 'aiva-viz-muted',
};
const STOPPED_COLOUR = 'aiva-viz-slot-2';

/** A segment as wide as its share of the row, never so narrow it disappears. */
function share(count: number, minimum = '0.5rem'): CSSProperties {
  return { flex: `${count} 1 0`, minInlineSize: minimum };
}

/**
 * Where the requests went, as an icicle: all requests; those stopped before
 * any route against those routed; then each route. Widths are to scale.
 */
export function TrafficFlowChart({ flow }: { flow: TrafficFlow }) {
  const stopped = flow.stopped.reduce((sum, bucket) => sum + bucket.count, 0);
  const routed = flow.requests - stopped;
  const legend = [
    ...flow.stopped.filter((bucket) => bucket.count > 0).map((bucket) => ({ ...bucket, colour: STOPPED_COLOUR })),
    ...flow.routed.map((bucket) => ({ ...bucket, colour: ROUTE_COLOURS[bucket.bucket] })),
  ];
  return (
    <div className="space-y-4">
      <ChartFigure
        title={t('insights.flow.title')}
        description={t('insights.flow.description')}
        table={{
          caption: t('insights.flow.table'),
          columns: [{ label: t('insights.flow.col.path') }, { label: t('insights.col.requests'), numeric: true }],
          rows: [
            ...flow.stopped.map((bucket) => ({ key: bucket.bucket, cells: [flowLabel(bucket.bucket), formatCount(bucket.count)] })),
            ...flow.routed.map((bucket) => ({ key: bucket.bucket, cells: [flowLabel(bucket.bucket), formatCount(bucket.count)] })),
          ],
        }}
      >
        <div className="space-y-2">
          <div className="flex min-h-11 items-center rounded-lg bg-brand-navy px-4 font-semibold text-on-brand">
            {tPlural('insights.flow.arrive', flow.requests)}
          </div>
          <div className="flex min-h-11 gap-1">
            {stopped > 0 && <span className={`rounded-s-lg ${STOPPED_COLOUR}`} style={share(stopped, '1.5rem')} />}
            {routed > 0 && (
              <span
                className={`flex items-center bg-brand-navy px-4 font-semibold text-on-brand ${stopped > 0 ? 'rounded-e-lg' : 'rounded-lg'}`}
                style={share(routed)}
              >
                {tPlural('insights.flow.routed', routed)}
              </span>
            )}
          </div>
          <div className="flex min-h-14 gap-1">
            {stopped > 0 && <span style={share(stopped, '1.5rem')} />}
            {flow.routed
              .filter((bucket) => bucket.count > 0)
              .map((bucket) => (
                <span key={bucket.bucket} className={`first:rounded-s-lg last:rounded-e-lg ${ROUTE_COLOURS[bucket.bucket]}`} style={share(bucket.count)} />
              ))}
          </div>
        </div>
      </ChartFigure>
      <ul className="grid gap-x-8 gap-y-3 sm:grid-cols-2" aria-label={t('insights.flow.legend')}>
        {legend.map((bucket) => (
          <li key={bucket.bucket} className="flex items-start gap-3">
            <span aria-hidden="true" className={`aiva-viz-swatch mt-1 shrink-0 ${bucket.colour}`} />
            <span>
              <strong className="font-semibold text-ink">
                {t('insights.flow.item', { count: formatCount(bucket.count), label: flowLabel(bucket.bucket) })}
              </strong>{' '}
              <span className="text-ink-muted">{t(`insights.flow.about.${bucket.bucket}`)}</span>
            </span>
          </li>
        ))}
      </ul>
      <ProvenanceBadge provenance={provenanceOf(flow)} />
    </div>
  );
}

function flowLabel(bucket: FlowBucket): string {
  return t(`insights.flow.label.${bucket}`);
}

/**
 * What the CIM's packing did: of the passages it was given on the requests
 * that sent its package, how many it kept, and the tokens the answering model
 * read there (the run view's counting rule).
 */
export function PassagesKept({ packing }: { packing: PackingSummary }) {
  if (packing.packed_runs === 0) return <p className="text-ink-muted">{t('insights.packing.none')}</p>;
  const retrieved = packing.passages_retrieved;
  const setAside = retrieved - packing.passages_kept;
  return (
    <figure className="space-y-3" aria-label={t('insights.packing.title')}>
      <p className="text-body font-semibold text-ink">
        {retrieved
          ? t('insights.packing.kept', { kept: formatCount(packing.passages_kept), retrieved: formatCount(retrieved) })
          : t('insights.packing.noPassages')}
      </p>
      {retrieved > 0 && (
        <span aria-hidden="true" className="flex h-4 gap-0.5">
          {packing.passages_kept > 0 && <span className="rounded-s-full aiva-viz-slot-3 last:rounded-e-full" style={share(packing.passages_kept)} />}
          {setAside > 0 && <span className="rounded-e-full aiva-viz-muted first:rounded-s-full" style={share(setAside)} />}
        </span>
      )}
      {setAside > 0 && <p className="text-secondary text-ink-muted">{tPlural('insights.packing.setAside', setAside)}</p>}
      <p className="text-secondary text-ink-muted">
        {tPlural('insights.packing.read', packing.packed_runs, { tokens: formatCount(packing.model_input_tokens.total) })}
      </p>
      <ProvenanceBadge provenance={weakestProvenance(provenanceOf(packing), provenanceOf(packing.model_input_tokens))} />
    </figure>
  );
}

type Stage = 'answering' | 'packer' | 'retries' | 'baselines' | 'quality' | 'other';

/**
 * Where the tokens went, by stage: the answering models' first calls, the
 * CIM's packing, retries of any stage, the opt-in frontier baselines, which
 * are never shown to users, and presenters' answer-quality checks. The parts
 * sum to all tokens in and out.
 */
export function TokensByStage({ tokens }: { tokens: TokenUsage }) {
  const stages: { key: Stage; value: number; slot: number }[] = [
    { key: 'answering', value: tokens.upstream_input_tokens + tokens.upstream_output_tokens, slot: 1 },
    { key: 'packer', value: tokens.cim_input_tokens + tokens.cim_output_tokens, slot: 2 },
    { key: 'retries', value: tokens.retry_input_tokens + tokens.retry_output_tokens, slot: 3 },
    { key: 'baselines', value: tokens.baseline_input_tokens + tokens.baseline_output_tokens, slot: 4 },
    { key: 'quality', value: (tokens.quality_comparison_input_tokens ?? 0) + (tokens.quality_comparison_output_tokens ?? 0), slot: 0 },
    { key: 'other', value: (tokens.other_input_tokens ?? 0) + (tokens.other_output_tokens ?? 0), slot: 0 },
  ];
  // Answer-quality checks and other stages are rare: listed only when they used tokens.
  const parts = stages.filter((part) => (part.key !== 'quality' && part.key !== 'other') || part.value > 0);
  const total = tokens.input_tokens.total + tokens.output_tokens.total;
  return (
    <div className="space-y-4">
      <ChartFigure
        title={t('insights.stages.title')}
        description={t('insights.stages.description', { total: formatCount(total) })}
        table={{
          caption: t('insights.stages.table'),
          columns: [{ label: t('insights.stages.col.stage') }, { label: t('insights.stages.col.tokens'), numeric: true }],
          rows: parts.map((part) => ({ key: part.key, cells: [t(`insights.stages.${part.key}`), formatCount(part.value)] })),
        }}
      >
        <span className="flex h-10 gap-1">
          {parts
            .filter((part) => part.value > 0)
            .map((part) => (
              <span
                key={part.key}
                className={`first:rounded-s-lg last:rounded-e-lg ${part.slot ? `aiva-viz-slot-${part.slot}` : 'aiva-viz-muted'}`}
                style={share(part.value)}
              />
            ))}
        </span>
      </ChartFigure>
      <dl className="grid gap-x-8 gap-y-3 sm:grid-cols-2">
        {parts.map((part) => (
          // A <dl> group holds only its <dt> and <dd>; the swatch sits inside the <dt>.
          <div key={part.key} className="space-y-0.5">
            <dt className="flex items-start gap-3 font-semibold text-ink">
              <span aria-hidden="true" className={`aiva-viz-swatch mt-1 shrink-0 ${part.slot ? `aiva-viz-slot-${part.slot}` : 'aiva-viz-muted'}`} />
              {t('insights.stages.item', { label: t(`insights.stages.${part.key}`), count: formatCount(part.value) })}
            </dt>
            <dd className="ps-7 text-ink-muted">{t(`insights.stages.about.${part.key}`)}</dd>
          </div>
        ))}
      </dl>
      <ProvenanceBadge provenance={weakestProvenance(provenanceOf(tokens.input_tokens), provenanceOf(tokens.output_tokens))} />
    </div>
  );
}
