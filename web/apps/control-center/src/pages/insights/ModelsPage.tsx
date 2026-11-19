import { Card, Ltr, ProvenanceBadge } from '@aiva/ui';
import { unwrap } from '../../api/client';
import { useApiQuery } from '../../api/useApiQuery';
import { DataTable } from '../../components/DataTable';
import { QueryView } from '../../components/QueryView';
import { formatDurationMs } from '../../format';
import { t, tPlural } from '../../i18n';
import { BarList, ChartFigure } from '../../insights/charts';
import { FilterBar } from '../../insights/FilterBar';
import { ReportMeta } from '../../insights/figures';
import { dimensionLabel } from '../../insights/labels';
import { formatCount, formatMicrosUsd, formatRatio, provenanceOf, weakestProvenance } from '../../insights/numbers';
import { reportQuery, useReportParams } from '../../insights/params';
import type { OptimizationReport, OptimizationRow } from '../../insights/types';
import { usePublicVisitor } from '../../insights/visitor';
import { Page } from '../../layout/Page';

/**
 * AiVa Insights › Models: model and context efficiency (LLD §21.7). CIM
 * invocation and local bypass rates, tokens before and after context
 * construction, cache hits and provider prompt-cache tokens, latency and
 * retries per model and per context strategy.
 */
export function ModelsPage() {
  const report = useReportParams();
  const byModel = useOptimization(report, 'model');
  const byStrategy = useOptimization(report, 'strategy');
  return (
    <Page title={t('view.models.title')} eyebrow={t('area.insights')} description={t('view.models.description')}>
      <FilterBar report={report} />
      <Card title={t('insights.models.title')} description={t('insights.models.description')}>
        <QueryView
          state={byModel.state}
          onRetry={byModel.reload}
          loadingLabel={t('insights.optimization.loading')}
          errorTitle={t('insights.optimization.error')}
          empty={{ title: t('insights.consumption.emptyTitle'), description: t('insights.consumption.emptyBody') }}
          isEmpty={(data) => data.rows.length === 0}
        >
          {(data) => <ModelMix data={data} />}
        </QueryView>
      </Card>
      <Card title={t('insights.strategy.title')} description={t('insights.strategy.description')}>
        <QueryView
          state={byStrategy.state}
          onRetry={byStrategy.reload}
          loadingLabel={t('insights.optimization.loading')}
          errorTitle={t('insights.optimization.error')}
          empty={{ title: t('insights.consumption.emptyTitle'), description: t('insights.consumption.emptyBody') }}
          isEmpty={(data) => data.rows.length === 0}
        >
          {(data) => <EfficiencyTable rows={data.rows} groupBy="strategy" caption={t('insights.strategy.table')} />}
        </QueryView>
      </Card>
    </Page>
  );
}

function useOptimization(report: ReturnType<typeof useReportParams>, groupBy: 'model' | 'strategy') {
  const query = reportQuery(report.window, report.filters, { group_by: groupBy });
  return useApiQuery(`reports/optimization?${JSON.stringify(query)}`, (client, signal) =>
    unwrap(
      client.GET('/v1/reports/optimization', {
        params: { query: query as { from: string; to: string; group_by: typeof groupBy } },
        signal,
      }),
    ),
  );
}

function ModelMix({ data }: { data: OptimizationReport }) {
  const served = data.rows;
  return (
    <div className="space-y-6">
      <ReportMeta metadata={data.metadata} />
      <ChartFigure
        title={t('insights.models.chart')}
        description={t('insights.models.chart.description')}
        table={{
          caption: t('insights.models.chart.caption'),
          columns: [{ label: t('insights.col.model') }, { label: t('insights.col.servedRequests'), numeric: true }],
          rows: served.map((row) => ({ key: row.group || '(none)', cells: [dimensionLabel('model_id', row.group, row.label), formatCount(row.request_count)] })),
        }}
      >
        <BarList
          data={served.map((row) => ({
            key: row.group || '(none)',
            label: dimensionLabel('model_id', row.group, row.label),
            value: row.request_count,
            display: formatCount(row.request_count),
            detail: row.latency.average_attempt_ms != null ? t('insights.models.latency', { latency: formatDurationMs(row.latency.average_attempt_ms) }) : undefined,
          }))}
        />
      </ChartFigure>
      <EfficiencyTable rows={data.rows} groupBy="model_id" caption={t('insights.models.table')} />
    </div>
  );
}

function EfficiencyTable({ rows, groupBy, caption }: { rows: readonly OptimizationRow[]; groupBy: string; caption: string }) {
  // Packing is measured as passages kept. The prompt-to-sent token change counts
  // retrieval as growth, so a public visitor does not see it (Story mode).
  const visitor = usePublicVisitor();
  return (
    <DataTable
      caption={caption}
      minWidth="80rem"
      columns={[
        { label: t('insights.col.group') },
        { label: t('insights.col.requests'), numeric: true },
        { label: t('insights.col.cimInvoked'), numeric: true },
        { label: t('insights.col.cimBypassed'), numeric: true },
        { label: t('insights.col.localFast'), numeric: true },
        { label: t('insights.col.cacheHits'), numeric: true },
        { label: t('insights.col.passagesKept'), numeric: true },
        ...(visitor ? [] : [{ label: t('insights.col.contextTokens'), numeric: true }]),
        { label: t('insights.col.promptCache'), numeric: true },
        { label: t('insights.col.cimOverhead'), numeric: true },
        { label: t('insights.col.retries'), numeric: true },
        { label: t('insights.col.latency'), numeric: true },
      ]}
      rows={rows.map((row) => ({
        key: row.group || '(none)',
        cells: [
          <span key="g" className="flex flex-col">
            <bdi>{dimensionLabel(groupBy, row.group, row.label)}</bdi>
            {row.group && row.label !== row.group && <Ltr className="font-mono text-secondary text-ink-muted">{row.group}</Ltr>}
          </span>,
          formatCount(row.request_count),
          formatCount(row.cim_invoked),
          formatCount(row.cim_bypassed_by_policy),
          formatCount(row.local_fast),
          formatCount(row.cache_hits),
          <PassagesKept key="k" row={row} />,
          ...(visitor
            ? []
            : [
                <span key="c" className="flex flex-col items-end">
                  <span>
                    {formatCount(row.context_tokens_before)} → {formatCount(row.context_tokens_after)}
                  </span>
                  <span className="text-secondary text-ink-muted">{contextChange(row)}</span>
                </span>,
              ]),
          formatCount(row.cached_input_tokens),
          <span key="o" className="inline-flex flex-col items-end gap-1">
            <span>{t('insights.models.cimTokens', { tokens: formatCount(row.cim_overhead.input_tokens + row.cim_overhead.output_tokens) })}</span>
            <Ltr className="tabular-nums">{formatMicrosUsd(row.cim_overhead.api.value + row.cim_overhead.gpu_estimated.value)}</Ltr>
            <ProvenanceBadge
              provenance={weakestProvenance(provenanceOf(row.cim_overhead.api), provenanceOf(row.cim_overhead.gpu_estimated))}
            />
          </span>,
          tPlural('insights.models.retries', row.retries.attempts, { retries: formatCount(row.retries.retries) }),
          row.latency.average_attempt_ms != null ? formatDurationMs(row.latency.average_attempt_ms) : '—',
        ],
      }))}
    />
  );
}

/**
 * Of the passages the CIM was given on the requests that sent its package, how
 * many it kept, and the input tokens the answering model read there (the run
 * view's counting rule).
 */
function PassagesKept({ row }: { row: OptimizationRow }) {
  const packing = row.packing;
  if (packing.packed_runs === 0) return <span className="text-ink-muted">{t('insights.models.notPacked')}</span>;
  return (
    <span className="inline-flex flex-col items-end gap-1">
      <span>
        {packing.passages_retrieved
          ? t('insights.models.passagesKept', {
              kept: formatCount(packing.passages_kept),
              retrieved: formatCount(packing.passages_retrieved),
            })
          : t('insights.models.noPassages')}
      </span>
      <span className="text-secondary text-ink-muted">
        {tPlural('insights.models.packedRuns', packing.packed_runs, {
          tokens: formatCount(packing.model_input_tokens.total),
        })}
      </span>
      <ProvenanceBadge provenance={provenanceOf(packing.model_input_tokens)} />
    </span>
  );
}

/** “30% fewer” when context construction removed tokens, “12% more” when packaging added some. */
function contextChange(row: OptimizationRow): string {
  const ratio = row.context_reduction;
  if (ratio.value === null || ratio.value === undefined) return t('insights.models.noContext');
  const magnitude = formatRatio({ ...ratio, value: Math.abs(ratio.value) }) ?? '—';
  return ratio.value >= 0 ? t('insights.models.reduction', { ratio: magnitude }) : t('insights.models.increase', { ratio: magnitude });
}
