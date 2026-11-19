import { Card, Ltr, ProvenanceBadge } from '@aiva/ui';
import { unwrap } from '../../api/client';
import { useApiQuery } from '../../api/useApiQuery';
import { DataTable } from '../../components/DataTable';
import { QueryView } from '../../components/QueryView';
import { t } from '../../i18n';
import { FilterBar } from '../../insights/FilterBar';
import { FrontierComparison } from '../../insights/frontier';
import { ReportMeta } from '../../insights/figures';
import { formatMicrosUsd, provenanceOf, weakestProvenance } from '../../insights/numbers';
import { reportQuery, useReportParams } from '../../insights/params';
import { BaselineCard, SavingsChart } from '../../insights/savings';
import type { OptimizationReport } from '../../insights/types';
import { usePublicVisitor } from '../../insights/visitor';
import { Page } from '../../layout/Page';

/**
 * AiVa Insights › Savings: estimated savings versus the pinned frontier
 * baseline, separated into cache avoidance, context reduction, model-rate
 * differential and provider prompt cache, against the measured baseline
 * comparison with its quality pass/fail (LLD §21.4, §22.7). A public visitor
 * sees only the frontier comparison (Story mode); the full waterfall is for
 * administrators and finance.
 */
export function SavingsPage() {
  const report = useReportParams();
  const query = reportQuery(report.window, report.filters, { group_by: 'application' });
  const { state, reload } = useApiQuery(`reports/optimization?${JSON.stringify(query)}`, (client, signal) =>
    unwrap(
      client.GET('/v1/reports/optimization', {
        params: { query: query as { from: string; to: string; group_by: 'application' } },
        signal,
      }),
    ),
  );
  return (
    <Page title={t('view.savings.title')} eyebrow={t('area.insights')} description={t('view.savings.description')}>
      <FilterBar report={report} />
      <QueryView
        state={state}
        onRetry={reload}
        loadingLabel={t('insights.optimization.loading')}
        errorTitle={t('insights.optimization.error')}
        empty={{ title: t('insights.consumption.emptyTitle'), description: t('insights.consumption.emptyBody') }}
        isEmpty={(data) => data.rows.length === 0}
      >
        {(data) => <Savings data={data} />}
      </QueryView>
    </Page>
  );
}

function Savings({ data }: { data: OptimizationReport }) {
  const visitor = usePublicVisitor();
  if (visitor) {
    return (
      <Card title={t('insights.frontier.title')}>
        <div className="space-y-6">
          <ReportMeta metadata={data.metadata} />
          <FrontierComparison baseline={data.baseline} metadata={data.metadata} waterfallHidden />
        </div>
      </Card>
    );
  }
  return (
    <>
      <Card title={t('insights.savings.title')} description={t('insights.savings.description')}>
        <div className="space-y-6">
          <ReportMeta metadata={data.metadata} />
          <SavingsChart savings={data.savings} />
        </div>
      </Card>
      <Card title={t('insights.baseline.title')} description={t('insights.baseline.description')}>
        <BaselineCard baseline={data.baseline} />
      </Card>
      <Card title={t('insights.savings.byApplication')} description={t('insights.savings.byApplication.description')}>
        <SavingsByApplication rows={data.rows} />
      </Card>
    </>
  );
}

/**
 * Every waterfall step per application, in waterfall order, so each row visibly
 * sums to its net cost (baseline, signed reductions, then overheads). A row's
 * amounts carry its provenance: the least trustworthy of its steps.
 */
function SavingsByApplication({ rows }: { rows: OptimizationReport['rows'] }) {
  const steps = rows[0]?.savings.steps.map((step) => step.step) ?? [];
  return (
    <DataTable
      caption={t('insights.savings.byApplication.caption')}
      minWidth="96rem"
      columns={[
        { label: t('insights.col.application') },
        { label: t('insights.col.provenance') },
        ...steps.map((step) => ({ label: t(`insights.savings.step.${step}`), numeric: true })),
        { label: t('insights.col.netSavings'), numeric: true },
        { label: t('insights.col.measurementCost'), numeric: true },
      ]}
      rows={rows.map((row) => {
        const amounts = new Map(row.savings.steps.map((step) => [step.step, step.amount_micros]));
        return {
          key: row.group,
          cells: [
            row.label,
            <ProvenanceBadge
              key="p"
              provenance={weakestProvenance(provenanceOf(row.savings), ...row.savings.steps.map(provenanceOf))}
            />,
            ...steps.map((step) => formatMicrosUsd(amounts.get(step) ?? 0)),
            <Ltr key="n" className="tabular-nums font-semibold">
              {formatMicrosUsd(row.savings.net_savings_micros)}
            </Ltr>,
            formatMicrosUsd(row.savings.measurement_cost.value),
          ],
        };
      })}
    />
  );
}
