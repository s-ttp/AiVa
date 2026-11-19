import { Card, KpiTile } from '@aiva/ui';
import { Link } from 'react-router';
import { unwrap } from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { QueryView } from '../components/QueryView';
import { formatMicrosUsd } from '../format';
import { t } from '../i18n';
import { BarList, ChartFigure, StackedColumns, type Series } from '../insights/charts';
import { FilterBar } from '../insights/FilterBar';
import { FrontierComparison } from '../insights/frontier';
import { ReportMeta, UsageTiles } from '../insights/figures';
import { outcomeLabel } from '../insights/labels';
import { bucketLabel, formatCompact, formatCount, formatRatio, provenanceOf, rateCards } from '../insights/numbers';
import { reportLink, reportQuery, useReportParams, type ReportWindow } from '../insights/params';
import { BaselineCard, SavingsChart } from '../insights/savings';
import type { OverviewReport, TrendPoint } from '../insights/types';
import { usePublicVisitor } from '../insights/visitor';
import { Page } from '../layout/Page';

const TOKEN_SERIES: readonly Series[] = [
  { key: 'upstream', label: t('insights.tokens.upstream'), slot: 1 },
  { key: 'cim', label: t('insights.tokens.cim'), slot: 2 },
  { key: 'retries', label: t('insights.tokens.retries'), slot: 3 },
  { key: 'comparisons', label: t('insights.tokens.comparisons'), slot: 4 },
];

function tokenValues(point: TrendPoint): Record<string, number> {
  const cim = point.cim_tokens;
  const retries = point.retry_tokens;
  // Calls that measured runs rather than served them: baseline comparisons and answer-quality checks.
  const comparisons = point.baseline_tokens + (point.quality_comparison_tokens ?? 0);
  return {
    upstream: Math.max(0, point.input_tokens + point.output_tokens - cim - retries - comparisons),
    cim,
    retries,
    comparisons,
  };
}

/**
 * AiVa Admin › Overview: the executive view of LLD §21.7. Totals for requests,
 * tokens, provider API spend and estimated GPU spend; cost trend; cache hit
 * rate; estimated savings; and quality outcomes, each with its provenance.
 */
export function OverviewPage() {
  const report = useReportParams();
  const { window, filters } = report;
  const query = reportQuery(window, filters);
  const { state, reload } = useApiQuery(`reports/overview?${JSON.stringify(query)}`, (client, signal) =>
    unwrap(client.GET('/v1/reports/overview', { params: { query: query as { from: string; to: string } }, signal })),
  );
  return (
    <Page title={t('view.overview.title')} eyebrow={t('area.admin')} description={t('view.overview.description')}>
      <FilterBar report={report} keys={['department_id', 'application_id', 'execution_path', 'environment']} />
      <QueryView
        state={state}
        onRetry={reload}
        loadingLabel={t('overview.loading')}
        errorTitle={t('overview.error')}
        empty={{ title: t('overview.activity.empty.title'), description: t('overview.activity.empty.body') }}
        isEmpty={(data) => data.usage.request_count === 0}
      >
        {(data) => <Overview data={data} window={window} />}
      </QueryView>
    </Page>
  );
}

function Overview({ data, window }: { data: OverviewReport; window: ReportWindow }) {
  const meta = data.metadata;
  const windowText = t('insights.meta.windowValue', { from: meta.window.from, to: meta.window.to });
  const outcomes = Object.entries(data.outcomes)
    .filter(([, count]) => (count ?? 0) > 0)
    .sort(([, a], [, b]) => (b ?? 0) - (a ?? 0));
  const net = data.savings.net_savings_micros;
  const baseline = data.baseline;
  // A public visitor sees one savings figure, the frontier comparison; the
  // modelled waterfall is for administrators and finance (Story mode).
  const visitor = usePublicVisitor();
  return (
    <>
      <Card title={t('overview.kpis.title')}>
        <div className="space-y-4">
          <ReportMeta metadata={meta} />
          <UsageTiles usage={data.usage} metadata={meta} />
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <KpiTile
              label={t('overview.kpi.cacheRate.label')}
              value={formatRatio(data.cache_hit_rate)}
              provenance={provenanceOf(data.usage)}
              detail={{
                source: t('overview.kpi.cacheRate.source'),
                calculation: t('overview.kpi.ratio', {
                  numerator: formatCount(data.cache_hit_rate.numerator),
                  denominator: formatCount(data.cache_hit_rate.denominator),
                }),
                window: windowText,
                cacheAccounting: t('overview.kpi.cacheAccounting'),
              }}
            />
            <KpiTile
              label={t('overview.kpi.quality.label')}
              value={formatRatio(data.quality_pass_rate)}
              provenance={provenanceOf(data.usage)}
              detail={{
                source: t('overview.kpi.quality.source'),
                calculation: t('overview.kpi.ratio', {
                  numerator: formatCount(data.quality_pass_rate.numerator),
                  denominator: formatCount(data.quality_pass_rate.denominator),
                }),
                window: windowText,
                limitations: t('overview.kpi.quality.limitations'),
              }}
            />
            {visitor ? (
              <KpiTile
                label={t('insights.frontier.kpi.label')}
                value={baseline.runs ? formatMicrosUsd(baseline.savings.value) : null}
                provenance={provenanceOf(baseline.savings)}
                detail={{
                  source: t('insights.frontier.kpi.source', {
                    model: baseline.frontier_model_name,
                    runs: formatCount(baseline.runs),
                  }),
                  calculation: t('insights.frontier.kpi.calculation', {
                    frontier: formatMicrosUsd(baseline.baseline.value),
                    aiva: formatMicrosUsd(baseline.actual.value),
                  }),
                  window: windowText,
                  pricingVersion: rateCards(meta),
                  limitations: t('insights.frontier.kpi.limitations'),
                }}
              />
            ) : (
              <KpiTile
                label={t('overview.kpi.savings.label')}
                value={formatMicrosUsd(net)}
                provenance={provenanceOf(data.savings)}
                detail={{
                  source: t('overview.kpi.savings.source', { model: data.savings.frontier_model_id }),
                  calculation: t('overview.kpi.savings.calculation'),
                  window: windowText,
                  pricingVersion: data.savings.pricing_snapshot,
                  cacheAccounting: t('overview.kpi.avoided.cacheAccounting'),
                  limitations: t('overview.kpi.savings.limitations'),
                }}
              />
            )}
            <KpiTile
              label={t('overview.kpi.variance.label')}
              value={formatMicrosUsd(data.cost_variance.variance_micros)}
              provenance={provenanceOf(data.cost_variance)}
              detail={{
                source: t('overview.kpi.variance.source'),
                calculation: t('overview.kpi.variance.calculation', {
                  reserved: formatMicrosUsd(data.cost_variance.reserved_micros),
                  committed: formatMicrosUsd(data.cost_variance.committed_micros),
                }),
                window: windowText,
                pricingVersion: meta.pricing_snapshot,
              }}
            />
          </div>
          <p className="text-secondary">
            <Link
              to={reportLink('/admin/quality', window, {})}
              className="font-semibold text-brand-navy underline underline-offset-2"
            >
              {t('quality.overviewLink')}
            </Link>
          </p>
        </div>
      </Card>

      <Card title={t('overview.activity.title')} description={t('overview.activity.description')}>
        <div className="grid gap-8 xl:grid-cols-2">
          <ChartFigure
            title={t('overview.trend.tokens.title')}
            description={t('overview.trend.tokens.description')}
            legend={TOKEN_SERIES}
            table={{
              caption: t('overview.trend.tokens.caption'),
              columns: [
                { label: t('insights.col.bucket') },
                ...TOKEN_SERIES.map((s) => ({ label: s.label, numeric: true })),
              ],
              rows: data.trend.map((point) => {
                const values = tokenValues(point);
                return {
                  key: point.bucket,
                  cells: [bucketLabel(point.bucket, meta.granularity), ...TOKEN_SERIES.map((s) => formatCount(values[s.key] ?? 0))],
                };
              }),
            }}
          >
            <StackedColumns
              series={TOKEN_SERIES}
              format={formatCompact}
              data={data.trend.map((point) => ({
                key: point.bucket,
                label: bucketLabel(point.bucket, meta.granularity),
                values: tokenValues(point),
              }))}
            />
          </ChartFigure>
          <ChartFigure
            title={t('overview.trend.spend.title')}
            description={t('overview.trend.spend.description')}
            table={{
              caption: t('overview.trend.spend.caption'),
              columns: [
                { label: t('insights.col.bucket') },
                { label: t('insights.col.requests'), numeric: true },
                { label: t('insights.col.apiSpend'), numeric: true },
                { label: t('insights.col.gpuEstimate'), numeric: true },
                { label: t('insights.col.avoided'), numeric: true },
              ],
              rows: data.trend.map((point) => ({
                key: point.bucket,
                cells: [
                  bucketLabel(point.bucket, meta.granularity),
                  formatCount(point.request_count),
                  formatMicrosUsd(point.api_micros),
                  formatMicrosUsd(point.gpu_estimated_micros),
                  formatMicrosUsd(point.avoided_micros),
                ],
              })),
            }}
          >
            <StackedColumns
              series={[{ key: 'api', label: t('insights.col.apiSpend'), slot: 1 }]}
              format={(micros) => formatMicrosUsd(micros)}
              data={data.trend.map((point) => ({
                key: point.bucket,
                label: bucketLabel(point.bucket, meta.granularity),
                values: { api: point.api_micros },
              }))}
            />
          </ChartFigure>
        </div>
        <div className="mt-8">
          <ChartFigure
            title={t('overview.outcomes.title')}
            description={t('overview.outcomes.description')}
            table={{
              caption: t('overview.outcomes.caption'),
              columns: [{ label: t('insights.col.outcome') }, { label: t('insights.col.requests'), numeric: true }],
              rows: outcomes.map(([outcome, count]) => ({ key: outcome, cells: [outcomeLabel(outcome), formatCount(count ?? 0)] })),
            }}
          >
            <BarList
              data={outcomes.map(([outcome, count]) => ({
                key: outcome,
                label: outcomeLabel(outcome),
                value: count ?? 0,
                display: formatCount(count ?? 0),
              }))}
            />
          </ChartFigure>
        </div>
      </Card>

      {visitor ? (
        <Card title={t('insights.frontier.title')}>
          <FrontierComparison baseline={baseline} metadata={meta} waterfallHidden />
        </Card>
      ) : (
        <Card title={t('overview.savings.title')} description={t('overview.savings.description')}>
          <div className="grid gap-8 xl:grid-cols-[3fr_2fr]">
            <SavingsChart savings={data.savings} />
            <div className="space-y-3">
              <h3 className="text-body font-semibold text-brand-navy">{t('insights.baseline.title')}</h3>
              <BaselineCard baseline={data.baseline} />
            </div>
          </div>
          <p className="mt-4 text-secondary">
            <Link className="font-medium text-brand-navy underline underline-offset-2" to={reportLink('/admin/insights/savings', window, {})}>
              {t('overview.savings.link')}
            </Link>
          </p>
        </Card>
      )}
    </>
  );
}
