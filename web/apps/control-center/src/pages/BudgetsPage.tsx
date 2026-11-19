import { Badge, Card, Ltr, ProvenanceBadge } from '@aiva/ui';
import { TriangleAlert } from 'lucide-react';
import { unwrap, type BudgetAlert, type BudgetList, type BudgetStatus } from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { DataTable } from '../components/DataTable';
import { QueryView } from '../components/QueryView';
import { formatList, formatMicrosUsd, formatPercent, formatUtc, isProvenance, percentOfCap } from '../format';
import { t, tRich } from '../i18n';
import { Page } from '../layout/Page';

/** AiVa Govern › Budgets & Quotas (LLD §12, §22.6): caps, spend, reservations, forecast and alerts. */
export function BudgetsPage() {
  const budgets = useApiQuery('admin/budgets', (client, signal) =>
    unwrap(client.GET('/v1/admin/budgets', { signal })),
  );
  const alerts = useApiQuery('admin/budgets/alerts', (client, signal) =>
    unwrap(client.GET('/v1/admin/budgets/alerts', { signal })),
  );
  return (
    <Page
      title={t('view.budgets-quotas.title')}
      eyebrow={t('area.govern')}
      description={t('view.budgets-quotas.description')}
    >
      <Card title={t('budgets.title')} description={t('budgets.description')}>
        <QueryView
          state={budgets.state}
          onRetry={budgets.reload}
          loadingLabel={t('budgets.loading')}
          errorTitle={t('budgets.error')}
          empty={{ title: t('budgets.empty.title'), description: t('budgets.empty.body') }}
          isEmpty={(list) => list.budgets.length === 0}
        >
          {(list) => <BudgetTable list={list} />}
        </QueryView>
      </Card>
      <Card title={t('budgetAlerts.title')} description={t('budgetAlerts.description')}>
        <QueryView
          state={alerts.state}
          onRetry={alerts.reload}
          loadingLabel={t('budgetAlerts.loading')}
          errorTitle={t('budgetAlerts.error')}
          empty={{ title: t('budgetAlerts.empty.title'), description: t('budgetAlerts.empty.body') }}
          isEmpty={(list) => list.alerts.length === 0}
        >
          {(list) => <AlertTable alerts={list.alerts} />}
        </QueryView>
      </Card>
    </Page>
  );
}

function scopeOf(budget: BudgetStatus) {
  return (
    <span className="flex flex-col">
      <span>{t(`budgets.scope.${budget.scope_type}`)}</span>
      <Ltr className="font-mono text-secondary text-ink-muted">{budget.scope_id}</Ltr>
    </span>
  );
}

function Utilization({ budget }: { budget: BudgetStatus }) {
  const atCap = (
    <span className="text-secondary text-ink-muted">
      {t('budgets.atCap', { action: t(`budgets.exhaustion.${budget.on_exhaustion ?? 'block'}`) })}
    </span>
  );
  if (budget.period_key == null) {
    return (
      <div className="flex flex-col gap-1.5">
        <span className="text-ink-muted">{t('budgets.template')}</span>
        {atCap}
      </div>
    );
  }
  const percent = percentOfCap(budget.spent_micros, budget.cap_micros);
  if (percent === null) return <span className="text-ink-muted">{t('budgets.noCap')}</span>;
  const crossed = budget.thresholds_crossed ?? [];
  const thresholds = budget.alert_thresholds ?? [];
  return (
    <div className="flex min-w-36 flex-col gap-1.5">
      <span className="tabular-nums">{t('budgets.percentOfCap', { percent })}</span>
      <div aria-hidden="true" className="relative h-2 rounded-full bg-line">
        <div
          className={`h-2 rounded-full ${crossed.length ? 'bg-warning' : 'bg-brand-teal'}`}
          style={{ inlineSize: `${Math.min(100, Math.max(percent, percent > 0 ? 1 : 0))}%` }}
        />
        {thresholds.map((threshold) => (
          <span
            key={threshold}
            className="absolute -top-0.5 h-3 w-0.5 bg-ink"
            style={{ insetInlineStart: `${Math.min(100, threshold * 100)}%` }}
          />
        ))}
      </div>
      <span className="text-secondary text-ink-muted">
        {t('budgets.alertsAt', { thresholds: formatList(thresholds.map(formatPercent)) || '—' })}
      </span>
      {crossed.length > 0 && (
        <Badge icon={TriangleAlert} tone="warning">
          {t('budgets.crossed', { thresholds: formatList(crossed.map(formatPercent)) })}
        </Badge>
      )}
      {atCap}
    </div>
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

function BudgetTable({ list }: { list: BudgetList }) {
  const reconciliation = list.last_reconciliation;
  return (
    <div className="space-y-4">
      <DataTable
        caption={t('budgets.caption')}
        minWidth="56rem"
        columns={[
          { label: t('budgets.col.scope') },
          { label: t('budgets.col.period') },
          { label: t('budgets.col.cap'), numeric: true },
          { label: t('budgets.col.spent'), numeric: true },
          { label: t('budgets.col.reserved'), numeric: true },
          { label: t('budgets.col.remaining'), numeric: true },
          { label: t('budgets.col.forecast'), numeric: true },
          { label: t('budgets.col.utilization') },
        ]}
        rows={list.budgets.map((budget) => {
          return {
            key: budget.id,
            cells: [
              scopeOf(budget),
              <span key="p" className="flex flex-col">
                <span>{t(`budgets.period.${budget.period}`)}</span>
                {budget.period_key && <Ltr className="font-mono text-secondary text-ink-muted">{budget.period_key}</Ltr>}
              </span>,
              <Money key="c" micros={budget.cap_micros} />,
              <Money key="s" micros={budget.spent_micros} provenance={budget.provenance} />,
              <Money key="r" micros={budget.reserved_micros} />,
              <Money key="m" micros={budget.remaining_micros} provenance={budget.provenance} />,
              <Money key="f" micros={budget.forecast_micros} provenance={budget.forecast_provenance} />,
              <Utilization key="u" budget={budget} />,
            ],
          };
        })}
      />
      <p className="text-secondary text-ink-muted">
        {reconciliation
          ? t('budgets.reconciliation', {
              status: t(`budgets.reconciliation.${reconciliation.status}`),
              time: formatUtc(reconciliation.completed_at ?? reconciliation.started_at),
            })
          : t('budgets.reconciliation.none')}
      </p>
      <p className="text-secondary text-ink-muted">{t('budgets.readOnly')}</p>
    </div>
  );
}

function AlertTable({ alerts }: { alerts: readonly BudgetAlert[] }) {
  return (
    <DataTable
      caption={t('budgetAlerts.caption')}
      minWidth="56rem"
      columns={[
        { label: t('budgetAlerts.col.time') },
        { label: t('budgetAlerts.col.kind') },
        { label: t('budgetAlerts.col.message') },
        { label: t('budgetAlerts.col.budget') },
        { label: t('budgetAlerts.col.spent'), numeric: true },
      ]}
      rows={alerts.map((alert) => ({
        key: alert.alert_id,
        cells: [
          <span key="t" className="whitespace-nowrap">
            {formatUtc(alert.created_at)}
          </span>,
          t(`budgetAlerts.kind.${alert.kind}`),
          alert.message,
          alert.budget_id ? <Ltr key="b" className="font-mono text-secondary">{alert.budget_id}</Ltr> : '—',
          alert.spent_micros != null && alert.cap_micros != null ? (
            <span key="s" className="inline-flex flex-col items-end gap-1">
              <span className="tabular-nums">
                {tRich('budgetAlerts.spentOfCap', {
                  spent: <Ltr>{formatMicrosUsd(alert.spent_micros)}</Ltr>,
                  cap: <Ltr>{formatMicrosUsd(alert.cap_micros)}</Ltr>,
                })}
              </span>
              {isProvenance(alert.provenance) && <ProvenanceBadge provenance={alert.provenance} />}
            </span>
          ) : (
            '—'
          ),
        ],
      }))}
    />
  );
}
