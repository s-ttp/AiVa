import { Badge, Button, Card, Ltr, SelectField } from '@aiva/ui';
import { Download, FileDown, Info } from 'lucide-react';
import { useEffect, useState, type FormEvent } from 'react';
import { isPlatformRateLimited, unwrap, type GatewayClient } from '../../api/client';
import { describeError } from '../../api/describeError';
import { useHfPause } from '../../api/hfBackoff';
import { useApiAction } from '../../api/useApiAction';
import { useApiQuery } from '../../api/useApiQuery';
import { DataTable } from '../../components/DataTable';
import { InlineIds } from '../../components/inline';
import { QueryView } from '../../components/QueryView';
import { formatList, formatUtc } from '../../format';
import { t } from '../../i18n';
import { FilterBar } from '../../insights/FilterBar';
import { dimensionLabel, filterLabel } from '../../insights/labels';
import { formatCount } from '../../insights/numbers';
import { useReportParams, type FilterKey } from '../../insights/params';
import type { ExportJob, ExportRequest } from '../../insights/types';
import { Page } from '../../layout/Page';

const REPORTS = {
  consumption: ['application', 'department', 'user', 'category', 'model', 'provider', 'path'],
  classification: ['business_function', 'task_category', 'intent', 'complexity', 'sensitivity', 'language'],
  optimization: ['application', 'strategy', 'model'],
  governance: ['policy', 'application', 'source_policy'],
} as const;
type ReportName = keyof typeof REPORTS;
type GroupName = (typeof REPORTS)[ReportName][number];
const GROUPS = new Set<string>(Object.values(REPORTS).flat());

function groupLabel(group: string): string {
  return GROUPS.has(group) ? t(`insights.groupBy.${group as GroupName}`) : group;
}

const POLL_MS = 1500;

/**
 * AiVa Insights › Export Center (LLD §21.6, §22.6): request a filtered CSV of
 * a report, then download it before it expires. Exports run in the gateway,
 * carry their filters, period, versions and data designation in their header
 * lines, list users by pseudonym only, and are audited.
 */
export function ExportCenterPage() {
  const report = useReportParams();
  const [name, setName] = useState<ReportName>('consumption');
  const [groupBy, setGroupBy] = useState<string>(REPORTS.consumption[0]);
  const create = useApiAction(async (client, body: ExportRequest) => unwrap(client.POST('/v1/reports/exports', { body })));
  const exports = useApiQuery('reports/exports', (client, signal) => unwrap(client.GET('/v1/reports/exports', { signal })));
  const pending = exports.state?.status === 'success' && exports.state.data.exports.some((job) => job.status === 'queued' || job.status === 'running');
  const { reload } = exports;
  const hfPaused = useHfPause() !== null;
  const hfLimited = exports.state?.status === 'error' && isPlatformRateLimited(exports.state.error);

  // While Hugging Face rate-limits the demo, wait for the shared back-off; then ask again, also after its 429.
  useEffect(() => {
    if (hfPaused || !(pending || hfLimited)) return;
    const timer = setTimeout(reload, hfLimited ? 0 : POLL_MS);
    return () => clearTimeout(timer);
  }, [pending, hfLimited, hfPaused, reload, exports.state]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const job = await create.run({
      report: name,
      group_by: groupBy,
      window: { from: report.window.from, to: report.window.to, timezone: 'UTC' },
      filters: report.filters,
    });
    if (job) reload();
  }

  const active = Object.entries(report.filters) as [FilterKey, string][];
  return (
    <Page title={t('view.export-center.title')} eyebrow={t('area.insights')} description={t('view.export-center.description')}>
      <FilterBar report={report} />
      <Card title={t('insights.export.new')} description={t('insights.export.new.description')}>
        <form className="space-y-4" onSubmit={onSubmit}>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <SelectField
              label={t('insights.export.report')}
              value={name}
              onChange={(event) => {
                const next = event.target.value as ReportName;
                setName(next);
                setGroupBy(REPORTS[next][0]);
              }}
            >
              {(Object.keys(REPORTS) as ReportName[]).map((key) => (
                <option key={key} value={key}>
                  {t(`insights.export.report.${key}`)}
                </option>
              ))}
            </SelectField>
            <SelectField label={t('insights.export.groupBy')} value={groupBy} onChange={(event) => setGroupBy(event.target.value)}>
              {REPORTS[name].map((group) => (
                <option key={group} value={group}>
                  {t(`insights.groupBy.${group}`)}
                </option>
              ))}
            </SelectField>
          </div>
          <p className="text-secondary text-ink">
            {t('insights.export.summary', {
              from: report.window.from,
              to: report.window.to,
              filters: active.length
                ? formatList(
                    active.map(([key, value]) =>
                      t('insights.filters.chip', { filter: filterLabel(key), value: dimensionLabel(key, value) }),
                    ),
                  )
                : t('insights.filters.allData'),
            })}
          </p>
          <Badge icon={Info}>{t('insights.export.privacy')}</Badge>
          <div>
            <Button type="submit" disabled={create.state.status === 'pending'}>
              <FileDown className="size-4" aria-hidden="true" />
              {t('insights.export.submit')}
            </Button>
          </div>
          <div aria-live="polite">
            {create.state.status === 'error' && (
              <p role="alert" className="text-secondary text-danger">
                {describeError(create.state.error)}
              </p>
            )}
            {create.state.status === 'success' && <p className="text-secondary text-ink">{t('insights.export.queued')}</p>}
          </div>
        </form>
      </Card>
      <Card title={t('insights.export.list')} description={t('insights.export.list.description')}>
        <QueryView
          state={exports.state}
          onRetry={reload}
          loadingLabel={t('insights.export.loading')}
          errorTitle={t('insights.export.error')}
          empty={{ title: t('insights.export.empty.title'), description: t('insights.export.empty.body') }}
          isEmpty={(list) => list.exports.length === 0}
        >
          {(list) => <ExportTable jobs={list.exports} />}
        </QueryView>
      </Card>
    </Page>
  );
}

function ExportTable({ jobs }: { jobs: readonly ExportJob[] }) {
  const download = useApiAction(async (client: GatewayClient, job: ExportJob) => {
    const blob = await unwrap(
      client.GET('/v1/reports/exports/{export_id}/download', {
        params: { path: { export_id: job.export_id } },
        parseAs: 'blob',
      }),
    );
    saveBlob(blob as Blob, `aiva-${job.report}-${job.group_by}-${job.window.from}-${job.window.to}.csv`);
    return job.export_id;
  });
  return (
    <div className="space-y-3">
      <div aria-live="polite">
        {download.state.status === 'error' && (
          <p role="alert" className="text-secondary text-danger">
            {describeError(download.state.error)}
          </p>
        )}
        {download.state.status === 'success' && <p className="text-secondary text-ink">{t('insights.export.downloaded')}</p>}
      </div>
      <DataTable
        caption={t('insights.export.caption')}
        minWidth="64rem"
        columns={[
          { label: t('insights.col.requested') },
          { label: t('insights.export.report') },
          { label: t('insights.col.period') },
          { label: t('insights.col.filters') },
          { label: t('insights.col.status') },
          { label: t('insights.col.rows'), numeric: true },
          { label: t('insights.col.expires') },
          { label: t('insights.col.file') },
        ]}
        rows={jobs.map((job) => ({
          key: job.export_id,
          cells: [
            <span key="r" className="flex flex-col">
              <span className="whitespace-nowrap">{formatUtc(job.created_at)}</span>
              <Ltr className="font-mono text-secondary text-ink-muted">{job.export_id}</Ltr>
            </span>,
            t('insights.export.reportBy', { report: t(`insights.export.report.${job.report}`), group: groupLabel(job.group_by) }),
            t('insights.window.range', { from: job.window.from, to: job.window.to }),
            <InlineIds
              key="f"
              ids={Object.entries(job.filters ?? {}).map(([key, value]) => `${key}=${value}`)}
              empty={t('insights.none')}
            />,
            t(`insights.export.status.${job.status}`),
            job.row_count != null ? formatCount(job.row_count) : '—',
            <span key="e" className="whitespace-nowrap">{formatUtc(job.expires_at)}</span>,
            job.status === 'completed' ? (
              <Button
                key="d"
                variant="secondary"
                onClick={() => void download.run(job)}
                aria-label={t('insights.export.downloadOne', { id: job.export_id })}
              >
                <Download className="size-4" aria-hidden="true" />
                {t('insights.export.download')}
              </Button>
            ) : (
              '—'
            ),
          ],
        }))}
      />
    </div>
  );
}

function saveBlob(blob: Blob, filename: string): void {
  if (typeof URL.createObjectURL !== 'function') return;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}
