import { Card } from '@aiva/ui';
import { Link } from 'react-router';
import { unwrap } from '../../api/client';
import { useApiQuery } from '../../api/useApiQuery';
import { useProfile } from '../../auth/session';
import { QueryView } from '../../components/QueryView';
import { t } from '../../i18n';
import { BarList, ChartFigure } from '../../insights/charts';
import { ConsumptionTable } from '../../insights/ConsumptionTable';
import { FilterBar } from '../../insights/FilterBar';
import { ReportMeta } from '../../insights/figures';
import { IdentityResolver } from '../../insights/IdentityResolver';
import { dimensionLabel } from '../../insights/labels';
import { formatCount } from '../../insights/numbers';
import { reportLink, reportQuery, useReportParams } from '../../insights/params';
import { Page } from '../../layout/Page';

/**
 * AiVa Insights › Users: consumption by pseudonymous user or service principal
 * (LLD §21.8). The gateway limits rows to the caller's scope; resolving a
 * pseudonym needs `reports:identity` and is audited.
 */
export function InsightsUsersPage() {
  const report = useReportParams();
  const { window, filters } = report;
  const profile = useProfile();
  const canResolve = (profile.permissions ?? []).includes('reports:identity');
  const query = reportQuery(window, filters, { group_by: 'user', limit: 100 });
  const { state, reload } = useApiQuery(`reports/consumption?${JSON.stringify(query)}`, (client, signal) =>
    unwrap(
      client.GET('/v1/reports/consumption', {
        params: { query: query as { from: string; to: string; group_by: 'user' } },
        signal,
      }),
    ),
  );
  const pseudonyms = state?.status === 'success' ? state.data.rows.map((row) => row.group) : [];
  return (
    <Page title={t('view.users.title')} eyebrow={t('area.insights')} description={t('view.users.description')}>
      <FilterBar report={report} />
      <Card title={t('insights.users.title')} description={t('insights.users.description')}>
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
                title={t('insights.users.chart')}
                description={t('insights.users.chart.description')}
                table={{
                  caption: t('insights.users.chart.caption'),
                  columns: [{ label: t('insights.col.user') }, { label: t('insights.col.tokens'), numeric: true }],
                  rows: data.rows.slice(0, 15).map((row) => ({
                    key: row.group,
                    cells: [dimensionLabel('user', row.group, row.label), formatCount(row.usage.tokens.input_tokens.total + row.usage.tokens.output_tokens.total)],
                  })),
                }}
              >
                <BarList
                  data={data.rows.slice(0, 15).map((row) => {
                    const tokens = row.usage.tokens.input_tokens.total + row.usage.tokens.output_tokens.total;
                    return {
                      key: row.group,
                      label: dimensionLabel('user', row.group, row.label),
                      value: tokens,
                      display: formatCount(tokens),
                    };
                  })}
                />
              </ChartFigure>
              <ConsumptionTable
                rows={data.rows}
                groupBy="user"
                caption={t('insights.users.table')}
                groupHeader={t('insights.col.user')}
                link={(row, label) => (
                  <Link
                    className="font-mono font-semibold text-brand-navy underline underline-offset-2"
                    to={reportLink('/admin/insights', window, { ...filters, user: row.group }, { level: 'category' })}
                    aria-label={t('insights.users.explore', { label })}
                  >
                    <bdi>{label}</bdi>
                  </Link>
                )}
              />
            </div>
          )}
        </QueryView>
      </Card>
      {canResolve && <IdentityResolver pseudonyms={pseudonyms} />}
    </Page>
  );
}
