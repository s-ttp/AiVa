import { Card } from '@aiva/ui';
import { Link } from 'react-router';
import { unwrap } from '../../api/client';
import { useApiQuery } from '../../api/useApiQuery';
import { useSession } from '../../auth/session';
import { QueryView } from '../../components/QueryView';
import { t, tPlural } from '../../i18n';
import { BarList, ChartFigure } from '../../insights/charts';
import { ConsumptionTable } from '../../insights/ConsumptionTable';
import { FilterBar } from '../../insights/FilterBar';
import { ReportMeta } from '../../insights/figures';
import { formatCount, formatMicrosUsd } from '../../insights/numbers';
import { reportLink, reportQuery, useReportParams } from '../../insights/params';
import { Page } from '../../layout/Page';

/** AiVa Insights › Applications: consumption and spend ranked by application, with drilldown (LLD §21.7). */
export function InsightsApplicationsPage() {
  const report = useReportParams();
  const { window, filters } = report;
  // Drill into users, or past them when the caller does not see the user dimension (ADR-0019).
  const { usersVisible } = useSession();
  const query = reportQuery(window, filters, { group_by: 'application', limit: 100 });
  const { state, reload } = useApiQuery(`reports/consumption?${JSON.stringify(query)}`, (client, signal) =>
    unwrap(
      client.GET('/v1/reports/consumption', {
        params: { query: query as { from: string; to: string; group_by: 'application' } },
        signal,
      }),
    ),
  );
  return (
    <Page title={t('view.applications.title')} eyebrow={t('area.insights')} description={t('view.applications.description')}>
      <FilterBar report={report} />
      <Card title={t('insights.applications.title')} description={t('insights.applications.description')}>
        <QueryView
          state={state}
          onRetry={reload}
          loadingLabel={t('insights.consumption.loading')}
          errorTitle={t('insights.consumption.error')}
          empty={{ title: t('insights.consumption.emptyTitle'), description: t('insights.consumption.emptyBody') }}
          isEmpty={(data) => data.rows.length === 0}
        >
          {(data) => (
            <div className="space-y-6">
              <ReportMeta metadata={data.metadata} />
              <ChartFigure
                title={t('insights.applications.chart')}
                description={t('insights.applications.chart.description')}
                table={{
                  caption: t('insights.applications.chart.caption'),
                  columns: [
                    { label: t('insights.col.application') },
                    { label: t('insights.col.apiSpend'), numeric: true },
                    { label: t('insights.col.requests'), numeric: true },
                  ],
                  rows: data.rows.map((row) => ({
                    key: row.group,
                    cells: [row.label, formatMicrosUsd(row.usage.spend.api.total), formatCount(row.usage.request_count)],
                  })),
                }}
              >
                <BarList
                  data={data.rows.map((row) => ({
                    key: row.group,
                    label: row.label,
                    value: row.usage.spend.api.total,
                    display: formatMicrosUsd(row.usage.spend.api.total),
                    detail: tPlural('insights.applications.detail', row.usage.request_count, {
                      gpu: formatMicrosUsd(row.usage.spend.gpu_estimated.value),
                    }),
                  }))}
                />
              </ChartFigure>
              <ConsumptionTable
                rows={data.rows}
                groupBy="application_id"
                caption={t('insights.applications.table')}
                groupHeader={t('insights.col.application')}
                link={(row, label) => (
                  <Link
                    className="font-semibold text-brand-navy underline underline-offset-2"
                    to={reportLink('/admin/insights', window, { ...filters, application_id: row.group }, { level: usersVisible === true ? 'user' : 'category' })}
                    aria-label={t(usersVisible === true ? 'insights.applications.explore' : 'insights.applications.exploreCategories', { label })}
                  >
                    <bdi>{label}</bdi>
                  </Link>
                )}
              />
            </div>
          )}
        </QueryView>
      </Card>
    </Page>
  );
}
