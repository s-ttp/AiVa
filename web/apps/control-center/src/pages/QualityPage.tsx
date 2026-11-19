import { Card, KpiTile, Ltr, ProvenanceBadge, SelectField, type Provenance } from '@aiva/ui';
import type { ReactNode } from 'react';
import { unwrap } from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { DataTable } from '../components/DataTable';
import { QueryView } from '../components/QueryView';
import { Section } from '../components/Section';
import { formatPercent } from '../format';
import { t } from '../i18n';
import { ChartFigure, StackedColumns, type Series } from '../insights/charts';
import { FilterBar } from '../insights/FilterBar';
import { ReportMeta } from '../insights/figures';
import { dimensionLabel } from '../insights/labels';
import {
  bucketLabel,
  countProvenance,
  formatCompact,
  formatCount,
  formatRatio,
  provenanceOf,
} from '../insights/numbers';
import { reportQuery, useReportParams, type ReportParams } from '../insights/params';
import type { GuardFailureKind, QualityBlocks, QualityReport, QualityRow, Ratio } from '../insights/types';
import { pathLabel } from '../labels';
import { Page } from '../layout/Page';

const GROUPINGS = ['application', 'context_profile', 'execution_path'] as const;
type Grouping = (typeof GROUPINGS)[number];

function grouping(value: string | null): Grouping {
  return (GROUPINGS as readonly string[]).includes(value ?? '') ? (value as Grouping) : 'application';
}

/** A ratio as a percentage, or "Not available" for 0 / 0: never 0% or 100% for nothing. */
function ratioText(ratio: Ratio | null | undefined): string {
  return (ratio && formatRatio(ratio)) ?? t('quality.notAvailable');
}

function ratioDetail(ratio: Ratio): string {
  return t('quality.ratioOf', {
    numerator: formatCount(ratio.numerator),
    denominator: formatCount(ratio.denominator),
  });
}

/** A ratio cell: the percentage, its numerator and denominator, and its provenance when it is a number. */
function RatioCell({ ratio, provenance }: { ratio: Ratio; provenance: Provenance }) {
  const known = ratio.value !== null && ratio.value !== undefined;
  return (
    <span className="inline-flex flex-col items-end gap-1">
      <span className="tabular-nums">{ratioText(ratio)}</span>
      <span className="text-secondary text-ink-muted">{ratioDetail(ratio)}</span>
      {known && <ProvenanceBadge provenance={provenance} />}
    </span>
  );
}

/**
 * Admin › Quality & Assurance (LLD §15, §21.7): assurance checks, output DLP,
 * context sufficiency, Context Guard and evidence coverage, from
 * GET /v1/reports/quality. The report is computed at request time from the
 * run records in your scope; every rate carries the provenance of its runs,
 * and a rate with nothing to measure reads "Not available".
 */
export function QualityPage() {
  const report = useReportParams();
  const group = grouping(report.params.get('group'));
  const query = reportQuery(report.window, report.filters, { group_by: group });
  const { state, reload } = useApiQuery(`reports/quality?${JSON.stringify(query)}`, (client, signal) =>
    unwrap(
      client.GET('/v1/reports/quality', {
        params: {
          query: query as { from: string; to: string; group_by: Grouping },
        },
        signal,
      }),
    ),
  );
  return (
    <Page
      title={t('view.quality-assurance.title')}
      eyebrow={t('area.admin')}
      description={t('view.quality-assurance.description')}
    >
      <FilterBar report={report} />
      <QueryView
        state={state}
        onRetry={reload}
        loadingLabel={t('quality.loading')}
        errorTitle={t('quality.error')}
        empty={{
          title: t('quality.empty.title'),
          description: t('quality.empty.body'),
        }}
        isEmpty={(data) => data.totals.request_count === 0}
      >
        {(data) => <QualityReportView data={data} report={report} group={group} />}
      </QueryView>
    </Page>
  );
}

function QualityReportView({ data, report, group }: { data: QualityReport; report: ReportParams; group: Grouping }) {
  const totals = data.totals;
  return (
    <>
      <Card title={t('quality.summary.title')}>
        <div className="space-y-4">
          <ReportMeta metadata={data.metadata} computedAt={data.computed_at} />
          <SummaryTiles totals={totals} designation={data.metadata.data_designation} window={windowText(data)} />
        </div>
      </Card>
      <Card
        title={t('quality.groups.title', {
          group: t(`quality.groups.${group}`).toLowerCase(),
        })}
        description={t('quality.groups.description')}
      >
        <div className="space-y-4">
          <SelectField
            label={t('quality.groups.select')}
            className="max-w-xs"
            value={group}
            onChange={(event) => report.setParams({ group: event.target.value })}
          >
            {GROUPINGS.map((value) => (
              <option key={value} value={value}>
                {t(`quality.groups.${value}`)}
              </option>
            ))}
          </SelectField>
          <GroupTable rows={data.rows} group={group} />
        </div>
      </Card>
      <Card title={t('quality.trend.title')} description={t('quality.trend.description')}>
        <Trend data={data} />
      </Card>
      <Card title={t('quality.assurance.title')} description={t('quality.assurance.description')}>
        <AssuranceDetail totals={totals} />
      </Card>
      <Card title={t('quality.sufficiency.title')} description={t('quality.sufficiency.description')}>
        <SufficiencyDetail totals={totals} />
      </Card>
      <Card title={t('quality.guard.title')} description={t('quality.guard.description')}>
        <GuardDetail totals={totals} />
      </Card>
      <Card title={t('quality.coverage.title')} description={t('quality.coverage.description')}>
        <CoverageDetail totals={totals} />
      </Card>
    </>
  );
}

function windowText(data: QualityReport): string {
  return t('insights.meta.windowValue', {
    from: data.metadata.window.from,
    to: data.metadata.window.to,
  });
}

function SummaryTiles({ totals, designation, window }: { totals: QualityBlocks; designation: string; window: string }) {
  const { assurance, sufficiency, guard, evidence_coverage: coverage } = totals;
  const measured = coverage.status === 'measured' && coverage.mean !== null && coverage.mean !== undefined;
  const ratioTile = (ratio: Ratio, provenance: Provenance) => ({
    value: ratioText(ratio),
    provenance: ratio.value === null || ratio.value === undefined ? null : provenance,
  });
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
      <KpiTile
        label={t('quality.kpi.requests')}
        value={formatCompact(totals.request_count)}
        provenance={countProvenance(designation)}
        detail={{
          source: t('quality.kpi.requests.source'),
          calculation: t('quality.kpi.requests.calculation'),
          window,
        }}
      />
      <KpiTile
        label={t('quality.kpi.assurance')}
        {...ratioTile(assurance.passed, provenanceOf(assurance))}
        detail={{
          source: t('quality.kpi.assurance.source'),
          calculation: t('quality.kpi.assurance.calculation', {
            ratio: ratioDetail(assurance.passed),
          }),
          window,
          limitations: t('quality.kpi.assurance.limitations'),
        }}
      />
      <KpiTile
        label={t('quality.kpi.sufficiency')}
        {...ratioTile(sufficiency.verified, provenanceOf(sufficiency))}
        detail={{
          source: t('quality.kpi.sufficiency.source'),
          calculation: t('quality.kpi.sufficiency.calculation', {
            ratio: ratioDetail(sufficiency.verified),
          }),
          window,
          limitations: t('quality.kpi.sufficiency.limitations'),
        }}
      />
      <KpiTile
        label={t('quality.kpi.guard')}
        {...ratioTile(guard.passed, provenanceOf(guard))}
        detail={{
          source: t('quality.kpi.guard.source'),
          calculation: t('quality.kpi.guard.calculation', {
            ratio: ratioDetail(guard.passed),
          }),
          window,
        }}
      />
      <KpiTile
        label={t('quality.kpi.coverage')}
        value={measured ? formatPercent(coverage.mean ?? 0) : t('quality.kpi.coverage.notMeasured')}
        provenance={measured ? provenanceOf(coverage) : null}
        detail={{
          source: t('quality.kpi.coverage.source'),
          calculation: measured
            ? t('quality.kpi.coverage.calculation', {
                runs: formatCount(coverage.measured_runs),
              })
            : t('quality.kpi.coverage.notMeasuredDetail'),
          window,
        }}
      />
    </div>
  );
}

function groupLabel(group: Grouping, row: QualityRow): ReactNode {
  if (group === 'execution_path') return pathLabel(row.group);
  const name =
    group === 'application' ? dimensionLabel('application_id', row.group, row.label) : row.label || row.group;
  return (
    <span className="flex flex-col">
      <bdi>{name}</bdi>
      {row.group && name !== row.group && <Ltr className="font-mono text-secondary text-ink-muted">{row.group}</Ltr>}
    </span>
  );
}

function coverageText(blocks: QualityBlocks): ReactNode {
  const coverage = blocks.evidence_coverage;
  if (coverage.status !== 'measured' || coverage.mean === null || coverage.mean === undefined) {
    return <span className="text-ink-muted">{t('quality.kpi.coverage.notMeasured')}</span>;
  }
  return (
    <span className="inline-flex flex-col items-end gap-1">
      <span className="tabular-nums">{formatPercent(coverage.mean)}</span>
      <ProvenanceBadge provenance={provenanceOf(coverage)} />
    </span>
  );
}

function GroupTable({ rows, group }: { rows: readonly QualityRow[]; group: Grouping }) {
  return (
    <DataTable
      caption={t('quality.groups.table', {
        group: t(`quality.groups.${group}`).toLowerCase(),
      })}
      minWidth="56rem"
      empty={t('quality.none')}
      columns={[
        { label: t('quality.col.group') },
        { label: t('quality.col.requests'), numeric: true },
        { label: t('quality.col.assurance'), numeric: true },
        { label: t('quality.col.sufficiency'), numeric: true },
        { label: t('quality.col.guard'), numeric: true },
        { label: t('quality.col.coverage'), numeric: true },
      ]}
      rows={rows.map((row) => ({
        key: row.group || '(none)',
        cells: [
          groupLabel(group, row),
          formatCount(row.request_count),
          <RatioCell key="a" ratio={row.assurance.passed} provenance={provenanceOf(row.assurance)} />,
          <RatioCell key="s" ratio={row.sufficiency.verified} provenance={provenanceOf(row.sufficiency)} />,
          <RatioCell key="g" ratio={row.guard.passed} provenance={provenanceOf(row.guard)} />,
          coverageText(row),
        ],
      }))}
    />
  );
}

const TREND_SERIES: readonly Series[] = [
  { key: 'passed', label: t('quality.trend.passed'), slot: 1 },
  { key: 'notPassed', label: t('quality.trend.notPassed'), slot: 3 },
];

function Trend({ data }: { data: QualityReport }) {
  const granularity = data.metadata.granularity;
  const points = data.trend.map((point) => ({
    key: point.bucket,
    label: bucketLabel(point.bucket, granularity),
    values: {
      passed: point.passed_runs,
      notPassed: Math.max(0, point.checked_runs - point.passed_runs),
    },
    point,
  }));
  return (
    <ChartFigure
      title={t('quality.trend.caption')}
      legend={TREND_SERIES}
      table={{
        caption: t('quality.trend.caption'),
        columns: [
          { label: t('quality.trend.period') },
          { label: t('quality.trend.requests'), numeric: true },
          { label: t('quality.trend.passed'), numeric: true },
          { label: t('quality.trend.notPassed'), numeric: true },
          { label: t('quality.trend.cimInvoked'), numeric: true },
          { label: t('quality.trend.verified'), numeric: true },
        ],
        rows: points.map(({ key, label, values, point }) => ({
          key,
          cells: [
            label,
            formatCount(point.request_count),
            formatCount(values.passed),
            formatCount(values.notPassed),
            formatCount(point.cim_invoked_runs),
            formatCount(point.verified_runs),
          ],
        })),
      }}
    >
      <StackedColumns data={points} series={TREND_SERIES} format={formatCompact} />
    </ChartFigure>
  );
}

function CountTable({ caption, rows }: { caption: string; rows: ReadonlyArray<{ name: string; count: number }> }) {
  return (
    <DataTable
      caption={caption}
      showCaption
      minWidth="14rem"
      empty={t('quality.none')}
      columns={[{ label: t('quality.col.code') }, { label: t('quality.col.count'), numeric: true }]}
      rows={rows.map((row) => ({
        key: row.name,
        cells: [
          <Ltr key="c" className="font-mono">
            {row.name}
          </Ltr>,
          formatCount(row.count),
        ],
      }))}
    />
  );
}

function AssuranceDetail({ totals }: { totals: QualityBlocks }) {
  const { assurance } = totals;
  const dlp = assurance.output_dlp;
  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <DataTable
        caption={t('quality.dlp.title')}
        showCaption
        minWidth="14rem"
        columns={[{ label: t('quality.col.outcome') }, { label: t('quality.col.count'), numeric: true }]}
        rows={(['clean', 'redacted', 'blocked', 'not_run'] as const).map((outcome) => ({
          key: outcome,
          cells: [t(`quality.dlp.${outcome}`), formatCount(dlp[outcome])],
        }))}
      />
      <CountTable caption={t('quality.escalations.title')} rows={assurance.escalations} />
      <CountTable caption={t('quality.checks.title')} rows={assurance.checks} />
    </div>
  );
}

function SufficiencyDetail({ totals }: { totals: QualityBlocks }) {
  const { sufficiency } = totals;
  const provenance = provenanceOf(sufficiency);
  const ofCim = t('quality.sufficiency.ofCim', {
    count: formatCount(sufficiency.cim_invoked_runs),
  });
  const ofAll = t('quality.sufficiency.ofAll', {
    count: formatCount(sufficiency.runs),
  });
  const rows: Array<[string, string, Ratio, string]> = [
    ['verified', t('quality.sufficiency.verified'), sufficiency.verified, ofCim],
    ['cim-selected', t('quality.sufficiency.cimSelected'), sufficiency.cim_selected, ofCim],
    ['fallback', t('quality.sufficiency.fallback'), sufficiency.fallback, ofCim],
    ['bypassed', t('quality.sufficiency.bypassed'), sufficiency.bypassed_by_policy, ofAll],
    ['skipped', t('quality.sufficiency.skipped'), sufficiency.skipped_sufficient, ofAll],
  ];
  return (
    <DataTable
      caption={t('quality.sufficiency.table')}
      minWidth="36rem"
      columns={[
        { label: t('quality.col.outcome') },
        { label: t('quality.col.share'), numeric: true },
        { label: t('quality.col.base') },
      ]}
      rows={rows.map(([key, label, ratio, base]) => ({
        key,
        cells: [label, <RatioCell key="r" ratio={ratio} provenance={provenance} />, base],
      }))}
    />
  );
}

function GuardDetail({ totals }: { totals: QualityBlocks }) {
  const { guard } = totals;
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <div className="space-y-2">
        <dl className="space-y-2 text-secondary">
          {[
            [t('quality.guard.packages'), formatCount(guard.packages)],
            [t('quality.kpi.guard'), ratioText(guard.passed)],
            [t('quality.guard.unverified'), formatCount(guard.relevance_unverified_passages)],
            [t('quality.guard.recallFloor'), formatCount(guard.recall_floor_added_runs)],
          ].map(([label, value]) => (
            <div key={label} className="flex justify-between gap-4 border-b border-line pb-2">
              <dt className="text-ink-muted">{label}</dt>
              <dd className="font-semibold tabular-nums text-ink">
                <bdi>{value}</bdi>
              </dd>
            </div>
          ))}
        </dl>
        <div className="flex justify-end">
          <ProvenanceBadge provenance={provenanceOf(guard)} />
        </div>
      </div>
      <DataTable
        caption={t('quality.guard.failures')}
        showCaption
        minWidth="20rem"
        empty={t('quality.none')}
        columns={[{ label: t('quality.col.kind') }, { label: t('quality.col.count'), numeric: true }]}
        rows={guard.failures.map((failure) => ({
          key: failure.kind,
          cells: [guardKindLabel(failure.kind), formatCount(failure.count)],
        }))}
      />
    </div>
  );
}

function guardKindLabel(kind: GuardFailureKind): string {
  return t(`quality.guardKind.${kind}`);
}

function CoverageDetail({ totals }: { totals: QualityBlocks }) {
  const coverage = totals.evidence_coverage;
  if (coverage.status !== 'measured') {
    return (
      <Section title={t('quality.kpi.coverage.notMeasured')}>
        <p className="text-secondary text-ink-muted">{t('quality.kpi.coverage.notMeasuredDetail')}</p>
      </Section>
    );
  }
  return (
    <dl className="grid gap-2 text-secondary sm:grid-cols-3">
      {[
        [t('quality.coverage.measuredRuns'), formatCount(coverage.measured_runs)],
        [t('quality.coverage.mean'), coverageText(totals)],
        [
          t('quality.coverage.withUnresolved'),
          coverage.with_unresolved ? ratioText(coverage.with_unresolved) : t('quality.notAvailable'),
        ],
      ].map(([label, value]) => (
        <div key={String(label)} className="flex flex-col gap-1 rounded-card border border-line p-3">
          <dt className="text-ink-muted">{label}</dt>
          <dd className="font-semibold text-ink">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
