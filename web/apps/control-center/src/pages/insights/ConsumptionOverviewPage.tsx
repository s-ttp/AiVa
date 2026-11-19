import { Button, Card, LoadingState, Ltr, ProvenanceBadge } from '@aiva/ui';
import { ChevronRight } from 'lucide-react';
import { Link } from 'react-router';
import { unwrap } from '../../api/client';
import { useApiQuery } from '../../api/useApiQuery';
import { DataTable } from '../../components/DataTable';
import { QueryView } from '../../components/QueryView';
import { formatUtc, isProvenance } from '../../format';
import { t, tPlural } from '../../i18n';
import { pathLabel } from '../../labels';
import { Page } from '../../layout/Page';
import { BarList, ChartFigure } from '../../insights/charts';
import { ConsumptionTable } from '../../insights/ConsumptionTable';
import { FilterBar } from '../../insights/FilterBar';
import { ReportMeta } from '../../insights/figures';
import { categoryLabel, dimensionLabel, outcomeLabel } from '../../insights/labels';
import { formatCount, formatMicrosUsd } from '../../insights/numbers';
import { reportQuery, useReportParams, type FilterKey, type ReportParams } from '../../insights/params';
import { RunDetailPanel } from '../../insights/RunDetailPanel';
import { useSession } from '../../auth/session';
import { useFilterOptions } from '../../insights/useFilterOptions';
import type { ConsumptionReport, RunList } from '../../insights/types';

/** The drilldown of LLD §21.7 and §22.6: each level filters the next. */
const ALL_LEVELS = [
  { level: 'department', groupBy: 'department', filter: 'department_id' },
  { level: 'application', groupBy: 'application', filter: 'application_id' },
  { level: 'user', groupBy: 'user', filter: 'user' },
  { level: 'category', groupBy: 'category', filter: 'task_category' },
  { level: 'model', groupBy: 'model', filter: 'model_id' },
] as const satisfies ReadonlyArray<{ level: string; groupBy: string; filter: FilterKey }>;

type LevelSpec = (typeof ALL_LEVELS)[number];
type Level = LevelSpec['level'] | 'runs';

/**
 * The levels a caller may drill through: without the user dimension (public
 * demo visitors), no user level. Null until the reporting scope has been read.
 */
function useLevels(): readonly LevelSpec[] | null {
  const { usersVisible } = useSession();
  if (usersVisible === null) return null;
  return usersVisible ? ALL_LEVELS : ALL_LEVELS.filter((l) => l.level !== 'user');
}

function currentLevel(value: string | null, levels: readonly LevelSpec[]): Level {
  return value === 'runs' || levels.some((l) => l.level === value) ? (value as Level) : 'department';
}

/**
 * AiVa Insights › Consumption Overview: the consumption explorer. Enterprise →
 * department → application → pseudonymous user → prompt category → model →
 * trace, with the global filters. The gateway enforces row-level scope at every
 * level; this view only builds the links.
 */
export function ConsumptionOverviewPage() {
  const report = useReportParams();
  const levels = useLevels();
  const level = levels === null ? null : currentLevel(report.params.get('level'), levels);
  const trace = report.params.get('trace');
  return (
    <Page
      title={t('view.consumption-overview.title')}
      eyebrow={t('area.insights')}
      description={t('view.consumption-overview.description')}
    >
      <FilterBar report={report} />
      {levels === null || level === null ? (
        <LoadingState label={t('reportScope.loading')} />
      ) : (
        <>
          <Breadcrumbs report={report} level={level} levels={levels} />
          {level === 'runs' ? <RunsLevel report={report} /> : <GroupLevel report={report} level={level} levels={levels} />}
        </>
      )}
      {trace && <RunDetailPanel traceId={trace} />}
    </Page>
  );
}

function Breadcrumbs({ report, level, levels }: { report: ReportParams; level: Level; levels: readonly LevelSpec[] }) {
  const { label } = useFilterOptions(report.window);
  const index = level === 'runs' ? levels.length : levels.findIndex((l) => l.level === level);
  const crumbs = [
    { key: 'enterprise', label: t('insights.explorer.enterprise'), target: 0 },
    ...levels.slice(0, index)
      .map((l, i) => ({ key: l.filter, value: report.filters[l.filter], target: i + 1 }))
      .filter((c) => c.value)
      .map((c) => ({ key: c.key, label: label(c.key, c.value ?? ''), target: c.target })),
  ];
  function goTo(target: number) {
    const changes: Record<string, string | null> = { level: levels[target]?.level ?? 'department', cursor: null, trace: null };
    for (const l of levels.slice(target)) changes[l.filter] = null;
    report.setParams(changes);
  }
  return (
    <nav aria-label={t('insights.explorer.path')}>
      <ol className="flex flex-wrap items-center gap-1 text-secondary">
        {crumbs.map((crumb, i) => (
          <li key={crumb.key} className="inline-flex items-center gap-1">
            {i > 0 && <ChevronRight className="size-4 text-ink-muted" aria-hidden="true" />}
            {i === crumbs.length - 1 && level !== 'runs' ? (
              <span aria-current="location" className="font-semibold text-ink">
                {crumb.label}
              </span>
            ) : (
              <button type="button" className="font-medium text-brand-navy underline underline-offset-2" onClick={() => goTo(crumb.target)}>
                {crumb.label}
              </button>
            )}
          </li>
        ))}
        {level === 'runs' && (
          <li className="inline-flex items-center gap-1">
            <ChevronRight className="size-4 text-ink-muted" aria-hidden="true" />
            <span aria-current="location" className="font-semibold text-ink">
              {t('insights.explorer.traces')}
            </span>
          </li>
        )}
      </ol>
    </nav>
  );
}

function GroupLevel({
  report,
  level,
  levels,
}: {
  report: ReportParams;
  level: Exclude<Level, 'runs'>;
  levels: readonly LevelSpec[];
}) {
  const index = levels.findIndex((l) => l.level === level);
  const spec = levels[index] ?? ALL_LEVELS[0];
  const next = levels[index + 1];
  const cursor = report.params.get('cursor') ?? undefined;
  const query = reportQuery(report.window, report.filters, { group_by: spec.groupBy, cursor, limit: 25 });
  const { state, reload } = useApiQuery(`reports/consumption?${JSON.stringify(query)}`, (client, signal) =>
    unwrap(
      client.GET('/v1/reports/consumption', {
        params: { query: query as { from: string; to: string; group_by: typeof spec.groupBy } },
        signal,
      }),
    ),
  );
  const title = t(`insights.explorer.by.${spec.groupBy}`);
  return (
    <Card
      title={title}
      description={t(levels.some((l) => l.level === 'user') ? 'insights.explorer.description' : 'insights.explorer.descriptionNoUsers')}
    >
      <QueryView
        state={state}
        onRetry={reload}
        loadingLabel={t('insights.consumption.loading')}
        errorTitle={t('insights.consumption.error')}
        empty={{ title: t('insights.consumption.emptyTitle'), description: t('insights.consumption.emptyBody') }}
        isEmpty={(data) => data.rows.length === 0}
      >
        {(data) => (
          <GroupView
            data={data}
            title={title}
            groupBy={spec.groupBy}
            drill={(group) =>
              report.setParams({ [spec.filter]: group, level: next ? next.level : 'runs', cursor: null, trace: null })
            }
            onPage={(nextCursor) => report.setParams({ cursor: nextCursor })}
          />
        )}
      </QueryView>
    </Card>
  );
}

function GroupView({
  data,
  title,
  groupBy,
  drill,
  onPage,
}: {
  data: ConsumptionReport;
  title: string;
  groupBy: string;
  drill: (group: string) => void;
  onPage: (cursor: string | null) => void;
}) {
  return (
    <div className="space-y-6">
      <ReportMeta metadata={data.metadata} />
      <ChartFigure
        title={t('insights.explorer.chart')}
        description={t('insights.explorer.chart.description')}
        table={{
          caption: t('insights.explorer.chart.caption'),
          columns: [{ label: t('insights.col.group') }, { label: t('insights.col.apiSpend'), numeric: true }, { label: t('insights.col.tokens'), numeric: true }],
          rows: data.rows.map((row) => ({
            key: row.group || '(none)',
            cells: [
              dimensionLabel(groupBy, row.group, row.label),
              formatMicrosUsd(row.usage.spend.api.total),
              formatCount(row.usage.tokens.input_tokens.total + row.usage.tokens.output_tokens.total),
            ],
          })),
        }}
      >
        <BarList
          data={data.rows.map((row) => {
            const tokens = row.usage.tokens.input_tokens.total + row.usage.tokens.output_tokens.total;
            return {
              key: row.group || '(none)',
              label: dimensionLabel(groupBy, row.group, row.label),
              value: tokens,
              display: formatCount(tokens),
              detail: tPlural('insights.explorer.detail', row.usage.request_count, {
                spend: formatMicrosUsd(row.usage.spend.api.total),
              }),
            };
          })}
        />
      </ChartFigure>
      <ConsumptionTable
        rows={data.rows}
        groupBy={groupBy}
        caption={t('insights.explorer.table', { group: title })}
        groupHeader={t('insights.col.group')}
        link={(row, label) =>
          row.group === '' ? (
            <bdi>{label}</bdi>
          ) : (
          <button
            type="button"
            className="text-start font-semibold text-brand-navy underline underline-offset-2"
            onClick={() => drill(row.group)}
            aria-label={t('insights.explorer.drill', { label })}
          >
            <bdi>{label}</bdi>
          </button>
          )
        }
      />
      <Pager page={data.page} onPage={onPage} />
    </div>
  );
}

function Pager({ page, onPage }: { page: ConsumptionReport['page']; onPage: (cursor: string | null) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-3 text-secondary">
      <span className="text-ink-muted">{tPlural('insights.page.total', page.total_rows)}</span>
      <Button variant="secondary" onClick={() => onPage(null)}>
        {t('insights.page.first')}
      </Button>
      <Button variant="secondary" disabled={!page.next_cursor} onClick={() => onPage(page.next_cursor ?? null)}>
        {t('insights.page.next')}
      </Button>
    </div>
  );
}

function RunsLevel({ report }: { report: ReportParams }) {
  const cursor = report.params.get('cursor') ?? undefined;
  const query = reportQuery(report.window, report.filters, { cursor, limit: 25 });
  const { state, reload } = useApiQuery(`reports/runs?${JSON.stringify(query)}`, (client, signal) =>
    unwrap(client.GET('/v1/reports/runs', { params: { query: query as { from: string; to: string } }, signal })),
  );
  return (
    <Card title={t('insights.explorer.traces')} description={t('insights.runs.description')}>
      <QueryView
        state={state}
        onRetry={reload}
        loadingLabel={t('insights.runs.loading')}
        errorTitle={t('insights.runs.error')}
        empty={{ title: t('insights.runs.emptyTitle'), description: t('insights.runs.emptyBody') }}
        isEmpty={(data) => data.runs.length === 0}
      >
        {(data) => <RunsTable data={data} report={report} />}
      </QueryView>
    </Card>
  );
}

function RunsTable({ data, report }: { data: RunList; report: ReportParams }) {
  // Without the user dimension (public demo visitors, ADR-0019) the gateway hides every principal: no column.
  const usersVisible = data.metadata.scope.users_visible !== false;
  return (
    <div className="space-y-4">
      <ReportMeta metadata={data.metadata} />
      <DataTable
        caption={t('insights.runs.caption')}
        minWidth="72rem"
        columns={[
          { label: t('insights.col.trace') },
          { label: t('insights.col.time') },
          { label: t('insights.col.application') },
          ...(usersVisible ? [{ label: t('insights.col.user') }] : []),
          { label: t('insights.col.category') },
          { label: t('insights.col.path') },
          { label: t('insights.col.outcome') },
          { label: t('insights.col.model') },
          { label: t('insights.col.tokens'), numeric: true },
          { label: t('insights.col.apiSpend'), numeric: true },
        ]}
        rows={data.runs.map((run) => ({
          key: run.run_id,
          cells: [
            <Link
              key="t"
              className="font-mono font-semibold text-brand-navy underline underline-offset-2"
              to={`?${new URLSearchParams({ ...Object.fromEntries(report.params), trace: run.trace_id }).toString()}`}
              aria-label={t('insights.runs.open', { trace: run.trace_id })}
            >
              <Ltr>{run.trace_id.slice(0, 12)}…</Ltr>
            </Link>,
            formatUtc(run.created_at),
            <Ltr key="a" className="font-mono text-secondary">{run.application_id}</Ltr>,
            ...(usersVisible ? [<Ltr key="u" className="font-mono text-secondary">{run.principal}</Ltr>] : []),
            categoryLabel(run.task_category),
            pathLabel(run.execution_path),
            outcomeLabel(run.outcome),
            run.model_id ? <Ltr key="m" className="font-mono text-secondary">{run.model_id}</Ltr> : t('insights.none'),
            formatCount(run.input_tokens + run.output_tokens),
            <span key="s" className="inline-flex flex-col items-end gap-1">
              <Ltr className="tabular-nums">{formatMicrosUsd(run.api_micros)}</Ltr>
              <ProvenanceBadge provenance={isProvenance(run.provenance) ? run.provenance : 'simulated'} />
            </span>,
          ],
        }))}
      />
      <Pager page={data.page} onPage={(next) => report.setParams({ cursor: next })} />
    </div>
  );
}
