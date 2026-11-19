import { Button, Card, Ltr, ProvenanceBadge, TextField } from '@aiva/ui';
import { Search } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { useSearchParams } from 'react-router';
import { unwrap, type RunDetail, type RunTrace } from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { DataTable } from '../components/DataTable';
import { FactList } from '../components/FactList';
import { QueryView } from '../components/QueryView';
import { formatCount, formatDurationMs, formatList, formatUtc, isProvenance, weakestProvenance } from '../format';
import { t } from '../i18n';
import { designationLabel, identityModeLabel, pathLabel, sourceModeLabel } from '../labels';
import { Page } from '../layout/Page';
import { ExecutionGraph } from '../run/ExecutionGraph';

/**
 * AiVa Admin › Audit: the run and trace explorer. Runs open by ID (from the
 * Test Console or a report); the gateway scopes which runs a caller may read
 * and answers 404 for any other.
 */
export function TraceExplorerPage() {
  const [params, setParams] = useSearchParams();
  const runId = params.get('run')?.trim() || null;
  const [draft, setDraft] = useState(runId ?? '');
  const { state, reload } = useApiQuery(runId && `runs/${runId}`, async (client, signal) => {
    const path = { params: { path: { run_id: runId ?? '' } }, signal };
    const [detail, trace] = await Promise.all([
      unwrap(client.GET('/v1/runs/{run_id}', path)),
      unwrap(client.GET('/v1/runs/{run_id}/trace', path)),
    ]);
    return { detail, trace };
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = draft.trim();
    setParams(value ? { run: value } : {});
  }

  return (
    <Page title={t('view.audit.title')} eyebrow={t('area.admin')} description={t('view.audit.description')}>
      <Card title={t('explorer.find.title')} description={t('explorer.find.description')}>
        <form onSubmit={onSubmit} className="flex flex-wrap items-end gap-4" role="search" aria-label={t('explorer.find.title')}>
          <TextField
            className="min-w-64 grow"
            label={t('explorer.runId')}
            hint={t('explorer.runId.hint')}
            value={draft}
            required
            autoComplete="off"
            spellCheck={false}
            dir="ltr"
            onChange={(event) => setDraft(event.target.value)}
          />
          <Button type="submit" className="mb-7">
            <Search className="size-4" />
            {t('explorer.open')}
          </Button>
        </form>
      </Card>
      {runId === null ? (
        <Card>
          <p className="text-body text-ink-muted">{t('explorer.idle')}</p>
        </Card>
      ) : (
        <QueryView
          state={state}
          onRetry={reload}
          loadingLabel={t('explorer.loading')}
          errorTitle={t('explorer.error')}
        >
          {({ detail, trace }) => <RunRecord detail={detail} trace={trace} />}
        </QueryView>
      )}
    </Page>
  );
}

function RunRecord({ detail, trace }: { detail: RunDetail; trace: RunTrace }) {
  const attemptProvenance = detail.attempts.map((attempt) => attempt.provenance).filter(isProvenance);
  const runProvenance = weakestProvenance(...attemptProvenance);
  return (
    <>
      <Card title={t('explorer.run.title')}>
        <FactList
          facts={[
            { label: t('run.fact.runId'), value: <Ltr className="font-mono">{detail.run_id}</Ltr> },
            { label: t('run.fact.traceId'), value: <Ltr className="font-mono">{detail.trace_id}</Ltr> },
            { label: t('explorer.status'), value: t('explorer.statusValue', { status: t(`runStatus.${detail.status}`), outcome: t(`outcome.${detail.outcome}`) }) },
            { label: t('explorer.scenario'), value: detail.scenario_id ? <Ltr className="font-mono">{detail.scenario_id}</Ltr> : '—' },
            { label: t('routing.identity.application'), value: <Ltr className="font-mono">{detail.application_id}</Ltr> },
            {
              label: t('explorer.identity'),
              value: `${identityModeLabel(detail.identity_mode)} · ${sourceModeLabel(detail.source_mode)}`,
            },
            { label: t('run.fact.path'), value: pathLabel(detail.execution_path) },
            {
              label: t('run.fact.model'),
              value: detail.selected_model ? <Ltr className="font-mono">{detail.selected_model}</Ltr> : t('run.fact.noModel'),
            },
            { label: t('explorer.cimState'), value: t(`cimState.${detail.cim_state}`) },
            { label: t('explorer.cacheHit'), value: detail.cache_hit ? t('cache.hit') : t('explorer.noCacheHit') },
            { label: t('routing.policy.version'), value: detail.policy_version ?? '—' },
            { label: t('run.fact.designation'), value: designationLabel(detail.data_designation) },
            { label: t('explorer.created'), value: formatUtc(detail.created_at) },
            { label: t('explorer.completed'), value: detail.completed_at ? formatUtc(detail.completed_at) : '—' },
            {
              label: t('explorer.latency'),
              value:
                detail.latency_ms == null ? (
                  '—'
                ) : (
                  <span className="inline-flex flex-wrap items-center gap-2">
                    <Ltr>{formatDurationMs(detail.latency_ms)}</Ltr>
                    <ProvenanceBadge provenance={runProvenance} />
                  </span>
                ),
            },
          ]}
        />
      </Card>
      <Card title={t('explorer.attempts.title')} description={t('explorer.attempts.description')}>
        <DataTable
          caption={t('explorer.attempts.title')}
          minWidth="48rem"
          empty={t('explorer.attempts.empty')}
          columns={[
            { label: t('explorer.col.stage') },
            { label: t('explorer.col.model') },
            { label: t('explorer.col.status') },
            { label: t('explorer.col.input'), numeric: true },
            { label: t('explorer.col.output'), numeric: true },
            { label: t('explorer.col.cached'), numeric: true },
            { label: t('explorer.col.latency'), numeric: true },
            { label: t('explorer.col.provenance') },
          ]}
          rows={detail.attempts.map((attempt) => ({
            key: attempt.attempt_id,
            cells: [
              attempt.stage,
              <Ltr key="m" className="font-mono">
                {attempt.model_id} ({attempt.provider})
              </Ltr>,
              attempt.status,
              formatCount(attempt.input_tokens),
              formatCount(attempt.output_tokens),
              formatCount(attempt.cached_input_tokens),
              attempt.latency_ms == null ? '—' : formatDurationMs(attempt.latency_ms),
              isProvenance(attempt.provenance) ? <ProvenanceBadge key="p" provenance={attempt.provenance} /> : attempt.provenance,
            ],
          }))}
        />
      </Card>
      <Card title={t('explorer.events.title')} description={t('explorer.events.description')}>
        <DataTable
          caption={t('explorer.events.title')}
          minWidth="48rem"
          empty={t('explorer.events.empty')}
          columns={[
            { label: t('explorer.col.stage') },
            { label: t('explorer.col.action') },
            { label: t('explorer.col.rules') },
            { label: t('explorer.col.classification') },
            { label: t('explorer.col.version') },
          ]}
          rows={detail.policy_events.map((event, index) => ({
            key: `${event.stage}-${index}`,
            cells: [
              event.stage,
              event.action,
              <Ltr key="r" className="font-mono">
                {formatList(event.matched_rule_ids) || '—'}
              </Ltr>,
              event.effective_classification,
              <Ltr key="v" className="font-mono">
                {event.policy_version} · {event.bundle_hash.slice(0, 12)}
              </Ltr>,
            ],
          }))}
        />
      </Card>
      <Card title={t('run.graph.title')} description={t('run.graph.description')}>
        <ExecutionGraph spans={trace.spans} executionPath={detail.execution_path} />
      </Card>
    </>
  );
}
