import { Badge, Card, Ltr, ProvenanceBadge } from '@aiva/ui';
import { ShieldCheck, TriangleAlert } from 'lucide-react';
import { unwrap } from '../../api/client';
import { useApiQuery } from '../../api/useApiQuery';
import { DataTable } from '../../components/DataTable';
import { QueryView } from '../../components/QueryView';
import { formatList, formatPercent, formatUtc, isProvenance, percentOfCap } from '../../format';
import { t, tPlural } from '../../i18n';
import { Meter } from '../../insights/charts';
import { FilterBar } from '../../insights/FilterBar';
import { ReportMeta } from '../../insights/figures';
import { formatCount, formatMicrosUsd } from '../../insights/numbers';
import { reportQuery, useReportParams } from '../../insights/params';
import type { GovernanceReport, GovernanceRow } from '../../insights/types';
import { Page } from '../../layout/Page';

const GROUPS = ['application', 'policy', 'source_policy'] as const;
type Group = (typeof GROUPS)[number];
const ANOMALIES = new Set(['token_spike', 'expensive_model_share', 'repeated_retries', 'high_output_ratio', 'unusual_user_consumption']);

/**
 * AiVa Insights › Quotas: FinOps and governance (LLD §21.7, §21.9). Budget
 * consumed, reserved and remaining with forecast and thresholds; anomaly and
 * quota alerts; hard-limit denials; source-policy matches; local-only
 * enforcement for confidential traffic; and policy version changes.
 */
export function QuotasPage() {
  const report = useReportParams();
  const groupParam = report.params.get('group');
  const group: Group = (GROUPS as readonly string[]).includes(groupParam ?? '') ? (groupParam as Group) : 'application';
  const budgets = useApiQuery('admin/budgets', (client, signal) => unwrap(client.GET('/v1/admin/budgets', { signal })));
  const alerts = useApiQuery('admin/budgets/alerts', (client, signal) =>
    unwrap(client.GET('/v1/admin/budgets/alerts', { params: { query: { limit: 200 } }, signal })),
  );
  const query = reportQuery(report.window, report.filters, { group_by: group });
  const governance = useApiQuery(`reports/governance?${JSON.stringify(query)}`, (client, signal) =>
    unwrap(
      client.GET('/v1/reports/governance', { params: { query: query as { from: string; to: string; group_by: Group } }, signal }),
    ),
  );
  return (
    <Page title={t('view.quotas.title')} eyebrow={t('area.insights')} description={t('view.quotas.description')}>
      <Card title={t('insights.quotas.budgets')} description={t('insights.quotas.budgets.description')}>
        <QueryView
          state={budgets.state}
          onRetry={budgets.reload}
          loadingLabel={t('insights.quotas.loading')}
          errorTitle={t('insights.quotas.error')}
          empty={{ title: t('insights.quotas.empty.title'), description: t('insights.quotas.empty.body') }}
          isEmpty={(list) => list.budgets.length === 0}
        >
          {(list) => (
            <DataTable
              caption={t('insights.quotas.budgets.caption')}
              minWidth="56rem"
              columns={[
                { label: t('insights.col.scope') },
                { label: t('insights.col.utilization') },
                { label: t('insights.col.spent'), numeric: true },
                { label: t('insights.col.reserved'), numeric: true },
                { label: t('insights.col.remaining'), numeric: true },
                { label: t('insights.col.forecast'), numeric: true },
              ]}
              rows={list.budgets
                .filter((budget) => budget.period_key)
                .map((budget) => {
                  const percent = percentOfCap(budget.spent_micros, budget.cap_micros);
                  const crossed = budget.thresholds_crossed ?? [];
                  return {
                    key: budget.id,
                    cells: [
                      <span key="s" className="flex flex-col">
                        <span>{t('insights.quotas.scope', { type: budget.scope_type, period: budget.period })}</span>
                        <Ltr className="font-mono text-secondary text-ink-muted">{budget.scope_id}</Ltr>
                      </span>,
                      <div key="u" className="flex min-w-40 flex-col gap-1.5">
                        <span className="tabular-nums">
                          {percent === null ? '—' : t('insights.quotas.percent', {
                                percent: formatPercent(percent / 100),
                                cap: formatMicrosUsd(budget.cap_micros),
                              })}
                        </span>
                        <Meter
                          fraction={percent === null ? 0 : percent / 100}
                          thresholds={budget.alert_thresholds ?? []}
                          warn={crossed.length > 0}
                        />
                        {crossed.length > 0 && (
                          <Badge icon={TriangleAlert} tone="warning">
                            {t('insights.quotas.crossed', { thresholds: formatList(crossed.map(formatPercent)) })}
                          </Badge>
                        )}
                      </div>,
                      <Money key="sp" micros={budget.spent_micros} provenance={budget.provenance} />,
                      <Money key="r" micros={budget.reserved_micros} />,
                      <Money key="m" micros={budget.remaining_micros} />,
                      <Money key="f" micros={budget.forecast_micros} provenance={budget.forecast_provenance} />,
                    ],
                  };
                })}
            />
          )}
        </QueryView>
      </Card>

      <Card title={t('insights.quotas.alerts')} description={t('insights.quotas.alerts.description')}>
        <QueryView
          state={alerts.state}
          onRetry={alerts.reload}
          loadingLabel={t('insights.quotas.loading')}
          errorTitle={t('insights.quotas.error')}
          empty={{ title: t('insights.quotas.alerts.empty'), description: t('insights.quotas.alerts.emptyBody') }}
          isEmpty={(list) => list.alerts.length === 0}
        >
          {(list) => (
            <DataTable
              caption={t('insights.quotas.alerts.caption')}
              minWidth="56rem"
              columns={[
                { label: t('insights.col.time') },
                { label: t('insights.col.kind') },
                { label: t('insights.col.scope') },
                { label: t('insights.col.message') },
              ]}
              rows={list.alerts.map((alert) => ({
                key: alert.alert_id,
                cells: [
                  <span key="t" className="whitespace-nowrap">{formatUtc(alert.created_at)}</span>,
                  <span key="k" className="flex flex-col items-start gap-1">
                    <span>{alertLabel(alert.kind)}</span>
                    {ANOMALIES.has(alert.kind) && <span className="text-secondary text-ink-muted">{t('insights.quotas.anomaly')}</span>}
                    {isProvenance(alert.provenance) && <ProvenanceBadge provenance={alert.provenance} />}
                  </span>,
                  alert.scope_id ? <Ltr key="s" className="font-mono text-secondary">{alert.scope_id}</Ltr> : '—',
                  alert.message,
                ],
              }))}
            />
          )}
        </QueryView>
      </Card>

      <FilterBar report={report} keys={['department_id', 'application_id', 'sensitivity', 'source_policy', 'environment']} />
      <Card title={t('insights.governance.title')} description={t('insights.governance.description')}>
        <div className="space-y-4">
          <div role="group" aria-label={t('insights.governance.groupBy')} className="flex flex-wrap gap-2">
            {GROUPS.map((item) => (
              <button
                key={item}
                type="button"
                aria-pressed={item === group}
                onClick={() => report.setParams({ group: item })}
                className={
                  item === group
                    ? 'rounded-full border border-brand-navy bg-brand-navy px-3 py-1 text-secondary font-semibold text-on-brand'
                    : 'rounded-full border border-line bg-card px-3 py-1 text-secondary text-ink hover:border-ink-muted'
                }
              >
                {t(`insights.governance.by.${item}`)}
              </button>
            ))}
          </div>
          <QueryView
            state={governance.state}
            onRetry={governance.reload}
            loadingLabel={t('insights.governance.loading')}
            errorTitle={t('insights.governance.error')}
          >
            {(data) => <Governance data={data} group={group} />}
          </QueryView>
        </div>
      </Card>
    </Page>
  );
}

function Money({ micros, provenance }: { micros: number | null | undefined; provenance?: string | null }) {
  if (micros == null) return <span className="text-ink-muted">—</span>;
  return (
    <span className="inline-flex flex-col items-end gap-1">
      <Ltr className="tabular-nums">{formatMicrosUsd(micros)}</Ltr>
      {isProvenance(provenance) && <ProvenanceBadge provenance={provenance} />}
    </span>
  );
}

function Governance({ data, group }: { data: GovernanceReport; group: Group }) {
  const totals = data.totals;
  const leak = totals.confidential_external_attempts + totals.confidential_cim_attempts;
  // Answer-quality checks under ADR-0023's scenario 11 exception: reported apart, never hidden.
  const comparisons = totals.confidential_comparison_attempts ?? 0;
  return (
    <div className="space-y-6">
      <ReportMeta metadata={data.metadata} />
      <div className="flex flex-wrap gap-2">
        <Badge icon={leak === 0 ? ShieldCheck : TriangleAlert} tone={leak === 0 ? 'success' : 'danger'}>
          {leak > 0
            ? tPlural('insights.governance.leak', leak)
            : comparisons > 0
              ? tPlural('insights.governance.servedSovereign', totals.confidential_requests)
              : tPlural('insights.governance.localOnly', totals.confidential_requests)}
        </Badge>
        {comparisons > 0 && (
          <Badge icon={TriangleAlert} tone="warning">
            {tPlural('insights.governance.comparisons', comparisons)}
          </Badge>
        )}
        {data.alerts.map((alert) => (
          <Badge key={alert.kind} icon={TriangleAlert} tone="warning">
            {t('insights.governance.alertCount', { kind: alertLabel(alert.kind), count: formatCount(alert.count) })}
          </Badge>
        ))}
      </div>
      <DataTable
        caption={t('insights.governance.table', { group: t(`insights.governance.by.${group}`) })}
        minWidth="72rem"
        columns={[
          { label: t('insights.col.group') },
          { label: t('insights.col.requests'), numeric: true },
          { label: t('insights.col.allowed'), numeric: true },
          { label: t('insights.col.policyDenied'), numeric: true },
          { label: t('insights.col.approval'), numeric: true },
          { label: t('insights.col.quotaDenied'), numeric: true },
          { label: t('insights.col.confidential'), numeric: true },
          { label: t('insights.col.sovereign'), numeric: true },
          { label: t('insights.col.exceptions'), numeric: true },
          { label: t('insights.col.denialReasons') },
        ]}
        rows={[...data.rows, totals].map((row: GovernanceRow) => ({
          key: row === totals ? '__all' : row.group,
          cells: [
            row === totals ? t('insights.governance.all') : row.label === 'none' ? t('insights.none') : row.label,
            formatCount(row.request_count),
            formatCount(row.allowed),
            formatCount(row.policy_denied),
            formatCount(row.approval_required),
            formatCount(row.quota_denied),
            formatCount(row.confidential_requests),
            formatCount(row.sovereign_direct),
            formatCount(row.approval_exceptions),
            row.denial_reasons.length ? (
              <span key="d" className="font-mono text-secondary">
                {formatList(
                  row.denial_reasons.map((reason) =>
                    t('insights.governance.denialReason', { rule: reason.rule_id, count: formatCount(reason.count) }),
                  ),
                )}
              </span>
            ) : (
              '—'
            ),
          ],
        }))}
      />
      <DataTable
        caption={t('insights.governance.versions')}
        showCaption
        columns={[{ label: t('insights.col.version') }, { label: t('insights.col.published') }]}
        rows={data.policy_versions.map((version) => ({
          key: `${version.version}-${version.published_at}`,
          cells: [<Ltr key="v" className="font-mono">{version.version}</Ltr>, formatUtc(version.published_at)],
        }))}
        empty={t('insights.governance.versions.empty')}
      />
    </div>
  );
}

const ALERT_KINDS = [
  'threshold',
  'hard_cap',
  'reservation_overrun',
  'reservation_expired',
  'reconciliation_adjustment',
  'counter_corrected',
  'approved_exception',
  'token_spike',
  'expensive_model_share',
  'repeated_retries',
  'high_output_ratio',
  'unusual_user_consumption',
] as const;

/** The Govern label of an alert kind; a kind this build does not know is shown as sent. */
function alertLabel(kind: string): string {
  return (ALERT_KINDS as readonly string[]).includes(kind)
    ? t(`budgetAlerts.kind.${kind as (typeof ALERT_KINDS)[number]}`)
    : kind;
}
