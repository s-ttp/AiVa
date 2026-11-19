import { Badge, Card, KpiTile, Ltr, ProvenanceBadge } from '@aiva/ui';
import { Info } from 'lucide-react';
import { unwrap } from '../../api/client';
import { useApiQuery } from '../../api/useApiQuery';
import { DataTable } from '../../components/DataTable';
import { QueryView } from '../../components/QueryView';
import { t } from '../../i18n';
import { BarList, ChartFigure } from '../../insights/charts';
import { FilterBar } from '../../insights/FilterBar';
import { ReportMeta } from '../../insights/figures';
import { dimensionLabel } from '../../insights/labels';
import { bucketLabel, countProvenance, formatCount, formatMicrosUsd, formatRatio, provenanceOf } from '../../insights/numbers';
import { reportQuery, useReportParams } from '../../insights/params';
import type { ClassificationReport } from '../../insights/types';
import { Page } from '../../layout/Page';

const DIMENSIONS = ['business_function', 'task_category', 'intent', 'complexity', 'sensitivity', 'language'] as const;
type Dimension = (typeof DIMENSIONS)[number];

function dimensionOf(value: string | null): Dimension {
  return (DIMENSIONS as readonly string[]).includes(value ?? '') ? (value as Dimension) : 'task_category';
}

/**
 * AiVa Insights › Prompt Categories: classification intelligence (LLD §21.3,
 * §21.7). Workload mix, top intents, complexity shifts, language mix, the
 * low-confidence (unclassified) share and model suitability by category, with
 * the taxonomy and classifier versions that produced the labels.
 */
export function PromptCategoriesPage() {
  const report = useReportParams();
  const dimension = dimensionOf(report.params.get('dimension'));
  const query = reportQuery(report.window, report.filters, { group_by: dimension });
  const { state, reload } = useApiQuery(`reports/classification?${JSON.stringify(query)}`, (client, signal) =>
    unwrap(
      client.GET('/v1/reports/classification', {
        params: { query: query as { from: string; to: string; group_by: Dimension } },
        signal,
      }),
    ),
  );
  return (
    <Page
      title={t('view.prompt-categories.title')}
      eyebrow={t('area.insights')}
      description={t('view.prompt-categories.description')}
    >
      <FilterBar report={report} />
      <Card title={t('insights.classification.title')} description={t('insights.classification.description')}>
        <div className="space-y-6">
          <div role="group" aria-label={t('insights.classification.dimension')} className="flex flex-wrap gap-2">
            {DIMENSIONS.map((item) => (
              <button
                key={item}
                type="button"
                aria-pressed={item === dimension}
                onClick={() => report.setParams({ dimension: item })}
                className={
                  item === dimension
                    ? 'rounded-full border border-brand-navy bg-brand-navy px-3 py-1 text-secondary font-semibold text-on-brand'
                    : 'rounded-full border border-line bg-card px-3 py-1 text-secondary text-ink hover:border-ink-muted'
                }
              >
                {t(`insights.dimension.${item}`)}
              </button>
            ))}
          </div>
          {dimension === 'language' && (
            <Badge icon={Info} tone="info">
              {t('insights.classification.englishOnly')}
            </Badge>
          )}
          <QueryView
            state={state}
            onRetry={reload}
            loadingLabel={t('insights.classification.loading')}
            errorTitle={t('insights.classification.error')}
            empty={{ title: t('insights.consumption.emptyTitle'), description: t('insights.consumption.emptyBody') }}
            isEmpty={(data) => data.rows.length === 0}
          >
            {(data) => <Classification data={data} dimension={dimension} />}
          </QueryView>
        </div>
      </Card>
    </Page>
  );
}

function Classification({ data, dimension }: { data: ClassificationReport; dimension: Dimension }) {
  const label = (group: string) => dimensionLabel(dimension, group);
  const buckets = [...new Set(data.trend.map((point) => point.bucket))].sort();
  const groups = data.rows.map((row) => row.group);
  const counts = new Map(data.trend.map((point) => [`${point.bucket}|${point.group}`, point.request_count]));
  const windowText = t('insights.meta.windowValue', { from: data.metadata.window.from, to: data.metadata.window.to });
  return (
    <div className="space-y-6">
      <ReportMeta metadata={data.metadata} />
      <div className="grid gap-4 sm:grid-cols-2">
        <KpiTile
          label={t('insights.classification.unclassified')}
          value={formatRatio(data.unclassified)}
          provenance={countProvenance(data.metadata.data_designation)}
          detail={{
            source: t('insights.classification.unclassified.source'),
            calculation: t('overview.kpi.ratio', {
              numerator: formatCount(data.unclassified.numerator),
              denominator: formatCount(data.unclassified.denominator),
            }),
            window: windowText,
            limitations: t('insights.classification.unclassified.limitations', {
              threshold: data.taxonomy.review_confidence_threshold,
            }),
          }}
        />
        <div className="flex flex-col gap-2 rounded-card border border-line bg-card p-4 text-secondary">
          <p className="font-medium text-ink-muted">{t('insights.classification.taxonomy')}</p>
          <p className="text-heading font-semibold text-brand-navy">
            <Ltr>{data.taxonomy.version}</Ltr>
          </p>
          <p className="text-ink-muted">
            {t('insights.classification.threshold', { threshold: data.taxonomy.review_confidence_threshold })}
          </p>
        </div>
      </div>
      <ChartFigure
        title={t('insights.classification.chart', { dimension: t(`insights.dimension.${dimension}`) })}
        description={t('insights.classification.chart.description')}
        table={{
          caption: t('insights.classification.table'),
          columns: [
            { label: t('insights.col.group') },
            { label: t('insights.col.requests'), numeric: true },
            { label: t('insights.col.share'), numeric: true },
            { label: t('insights.col.tokens'), numeric: true },
            { label: t('insights.col.apiSpend'), numeric: true },
          ],
          rows: data.rows.map((row) => ({
            key: row.group,
            cells: [
              row.low_confidence ? t('insights.classification.lowConfidence', { label: label(row.group) }) : label(row.group),
              formatCount(row.request_count),
              formatRatio(row.share) ?? '—',
              formatCount(row.tokens),
              <span key="s" className="inline-flex items-center gap-2">
                <Ltr className="tabular-nums">{formatMicrosUsd(row.api.value)}</Ltr>
                <ProvenanceBadge provenance={provenanceOf(row.api)} />
              </span>,
            ],
          })),
        }}
      >
        <BarList
          emphasis={undefined}
          data={data.rows.map((row) => ({
            key: row.group,
            label: row.low_confidence ? t('insights.classification.lowConfidence', { label: label(row.group) }) : label(row.group),
            value: row.request_count,
            display: t('insights.classification.countShare', {
              count: formatCount(row.request_count),
              share: formatRatio(row.share) ?? '—',
            }),
          }))}
        />
      </ChartFigure>
      <DataTable
        caption={t('insights.classification.trend', { granularity: t(`insights.granularity.${data.metadata.granularity}`) })}
        showCaption
        minWidth="40rem"
        columns={[{ label: t('insights.col.bucket') }, ...groups.map((group) => ({ label: label(group), numeric: true }))]}
        rows={buckets.map((bucket) => ({
          key: bucket,
          cells: [bucketLabel(bucket, data.metadata.granularity), ...groups.map((group) => formatCount(counts.get(`${bucket}|${group}`) ?? 0))],
        }))}
        empty={t('insights.classification.trend.empty')}
      />
      <DataTable
        caption={t('insights.classification.models')}
        showCaption
        minWidth="40rem"
        columns={[
          { label: t('insights.col.group') },
          { label: t('insights.col.model') },
          { label: t('insights.col.servedRequests'), numeric: true },
          { label: t('insights.col.qualityPass'), numeric: true },
        ]}
        rows={data.rows.flatMap((row) =>
          row.models.map((model) => ({
            key: `${row.group}|${model.model_id}`,
            cells: [
              label(row.group),
              <Ltr key="m" className="font-mono text-secondary">{model.model_id}</Ltr>,
              formatCount(model.request_count),
              model.quality_checked
                ? t('insights.classification.quality', {
                    passed: formatCount(model.quality_passed),
                    checked: formatCount(model.quality_checked),
                  })
                : '—',
            ],
          })),
        )}
        empty={t('insights.classification.models.empty')}
      />
    </div>
  );
}
