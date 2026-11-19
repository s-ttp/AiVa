import { Card, Ltr, ProvenanceBadge, StatusBadge } from '@aiva/ui';
import { Link } from 'react-router';
import { unwrap } from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { useSession } from '../auth/session';
import { DataTable } from '../components/DataTable';
import { FactList } from '../components/FactList';
import { InlineIds, Mono } from '../components/inline';
import { QueryView } from '../components/QueryView';
import { Section } from '../components/Section';
import { formatDurationMs, formatUtc, isProvenance } from '../format';
import { t, tRich } from '../i18n';
import { designationLabel, identityModeLabel, pathLabel, sourceModeLabel } from '../labels';
import { cimStateLabel, categoryLabel, outcomeLabel, sensitivityLabel } from './labels';
import { formatCount, formatMicrosUsd, formatScore } from './numbers';
import type { ReportRunDetail } from './types';

/**
 * The last drilldown level (LLD §21.7): one request's attempt-level usage,
 * ledger charges, classification, policy decisions and validation outcomes,
 * with its span timeline. No prompt or response text is ever shown here.
 */
export function RunDetailPanel({ traceId }: { traceId: string }) {
  const { state, reload } = useApiQuery(`reports/runs/${traceId}`, (client, signal) =>
    unwrap(client.GET('/v1/reports/runs/{trace_id}', { params: { path: { trace_id: traceId } }, signal })),
  );
  return (
    <Card title={t('insights.trace.title')} description={t('insights.trace.description')}>
      <QueryView state={state} onRetry={reload} loadingLabel={t('insights.trace.loading')} errorTitle={t('insights.trace.error')}>
        {(detail) => <RunDetail detail={detail} />}
      </QueryView>
    </Card>
  );
}

const HIDDEN_PRINCIPAL = 'hidden';

function RunDetail({ detail }: { detail: ReportRunDetail }) {
  const { can } = useSession();
  const run = detail.run;
  const classification = detail.classification;
  const sovereign = run.execution_path === 'sovereign-direct';
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-2">
        {sovereign && <StatusBadge status="sovereignDirect" />}
        {run.outcome === 'policy_denied' && <StatusBadge status="blocked" />}
        {run.outcome === 'approval_required' && <StatusBadge status="approvalRequired" />}
        {run.outcome === 'success' && <StatusBadge status="allowed" />}
        <ProvenanceBadge provenance={isProvenance(run.provenance) ? run.provenance : 'simulated'} />
      </div>
      <FactList
        facts={[
          { label: t('insights.trace.traceId'), value: <Mono>{run.trace_id}</Mono> },
          { label: t('insights.trace.runId'), value: <Mono>{run.run_id}</Mono> },
          { label: t('insights.trace.time'), value: formatUtc(run.created_at) },
          { label: t('insights.trace.application'), value: <Mono>{run.application_id}</Mono> },
          {
            label: t('insights.trace.principal'),
            // `hidden` is what the gateway shows a scope without the user dimension (public demo, ADR-0019).
            value: run.principal === HIDDEN_PRINCIPAL ? t('insights.trace.principalHidden') : <Mono>{run.principal}</Mono>,
          },
          {
            label: t('insights.trace.identity'),
            value: t('insights.trace.identityValue', {
              identity: identityModeLabel(run.identity_mode),
              source: sourceModeLabel(run.source_mode),
            }),
          },
          { label: t('insights.trace.sourcePolicy'), value: run.source_policy ? <Mono>{run.source_policy}</Mono> : t('insights.none') },
          { label: t('insights.trace.path'), value: pathLabel(run.execution_path) },
          { label: t('insights.trace.cim'), value: cimStateLabel(run.cim_state) },
          { label: t('insights.trace.outcome'), value: outcomeLabel(run.outcome) },
          { label: t('insights.trace.classification'), value: sensitivityLabel(run.effective_classification) },
          { label: t('insights.trace.policyVersion'), value: run.policy_version ?? t('insights.none') },
          { label: t('insights.trace.designation'), value: designationLabel(run.data_designation) },
        ]}
      />
      {can('runs:read') && (
        <p className="text-secondary">
          <Link className="font-medium text-brand-navy underline underline-offset-2" to={`/admin/audit?run=${run.run_id}`}>
            {t('insights.trace.openExplorer')}
          </Link>
        </p>
      )}

      <Section title={t('insights.trace.attempts')} description={t('insights.trace.attempts.description')}>
        <DataTable
          caption={t('insights.trace.attempts.caption')}
          minWidth="56rem"
          empty={t('insights.trace.attempts.empty')}
          columns={[
            { label: t('insights.col.stage') },
            { label: t('insights.col.model') },
            { label: t('insights.col.status') },
            { label: t('insights.col.inputTokens'), numeric: true },
            { label: t('insights.col.outputTokens'), numeric: true },
            { label: t('insights.col.latency'), numeric: true },
            { label: t('insights.col.charges') },
          ]}
          rows={detail.attempts.map((attempt) => ({
            key: attempt.attempt_id,
            cells: [
              <span key="s" className="flex flex-col">
                <span>{t(`insights.stage.${stageKey(attempt.stage)}`)}</span>
                {attempt.retry_of && <span className="text-secondary text-ink-muted">{t('insights.trace.retry')}</span>}
              </span>,
              <Mono key="m">{attempt.model_id}</Mono>,
              attempt.error_code ? (
                <span key="st">{tRich('insights.trace.attemptError', { status: attempt.status, code: <Mono>{attempt.error_code}</Mono> })}</span>
              ) : (
                attempt.status
              ),
              formatCount(attempt.input_tokens),
              formatCount(attempt.output_tokens),
              attempt.latency_ms != null ? formatDurationMs(attempt.latency_ms) : '—',
              <ul key="c" className="space-y-1">
                {attempt.charges.length === 0 && <li className="text-ink-muted">{t('insights.trace.noCharges')}</li>}
                {attempt.charges.map((charge) => (
                  <li key={charge.ledger_id} className="flex flex-wrap items-center gap-2">
                    <span>{t(`insights.charge.${chargeKey(charge.charge_type)}`)}</span>
                    <Ltr className="tabular-nums">{formatMicrosUsd(charge.amount_micros)}</Ltr>
                    <ProvenanceBadge provenance={isProvenance(charge.provenance) ? charge.provenance : 'simulated'} />
                  </li>
                ))}
              </ul>,
            ],
          }))}
        />
        {detail.other_charges.length > 0 && (
          <DataTable
            caption={t('insights.trace.otherCharges')}
            showCaption
            columns={[
              { label: t('insights.col.charge') },
              { label: t('insights.col.amount'), numeric: true },
              { label: t('insights.col.time') },
            ]}
            rows={detail.other_charges.map((charge) => ({
              key: charge.ledger_id,
              cells: [
                t(`insights.charge.${chargeKey(charge.charge_type)}`),
                <Ltr key="a" className="tabular-nums">{formatMicrosUsd(charge.amount_micros)}</Ltr>,
                formatUtc(charge.created_at),
              ],
            }))}
          />
        )}
      </Section>

      <Section title={t('insights.trace.classificationTitle')}>
        {classification ? (
          <FactList
            facts={[
              { label: t('insights.trace.taxonomy'), value: <Mono>{classification.taxonomy_version}</Mono> },
              { label: t('insights.trace.classifier'), value: <Mono>{classification.classifier_version}</Mono> },
              {
                label: t('insights.trace.task'),
                value: t('insights.trace.reported', {
                  reported: categoryLabel(classification.reported_task_category),
                  predicted: classification.predicted_task_category ? categoryLabel(classification.predicted_task_category) : t('insights.none'),
                }),
              },
              { label: t('insights.trace.complexity'), value: categoryLabel(classification.reported_complexity) },
              { label: t('insights.trace.intent'), value: classification.intent ? categoryLabel(classification.intent) : t('insights.none') },
              { label: t('insights.trace.function'), value: classification.business_function ? categoryLabel(classification.business_function) : t('insights.none') },
              { label: t('insights.trace.language'), value: categoryLabel(classification.language) },
              {
                label: t('insights.trace.confidence'),
                value:
                  classification.confidence != null
                    ? formatScore(classification.confidence)
                    : t('insights.trace.noConfidence'),
              },
              { label: t('insights.trace.status'), value: t(`insights.classStatus.${classification.status}`) },
            ]}
          />
        ) : (
          <p className="text-ink-muted">{t('insights.trace.noClassification')}</p>
        )}
      </Section>

      <Section title={t('insights.trace.policy')}>
        <DataTable
          caption={t('insights.trace.policy.caption')}
          empty={t('insights.trace.policy.empty')}
          minWidth="40rem"
          columns={[
            { label: t('insights.col.stage') },
            { label: t('insights.col.action') },
            { label: t('insights.col.rules') },
            { label: t('insights.col.version') },
          ]}
          rows={detail.policy_events.map((event, index) => ({
            key: `${event.stage}-${index}`,
            cells: [
              event.egress_model_id || event.tool_id ? (
                <span key="st">
                  {tRich('insights.trace.egress', { stage: event.stage, target: <Mono>{event.egress_model_id ?? event.tool_id}</Mono> })}
                </span>
              ) : (
                event.stage
              ),
              t(`insights.action.${actionKey(event.action)}`),
              <InlineIds key="r" ids={event.matched_rule_ids} empty={t('insights.none')} />,
              <Mono key="v">{event.policy_version}</Mono>,
            ],
          }))}
        />
      </Section>

      <Section title={t('insights.trace.validation')}>
        <FactList
          facts={[
            {
              label: t('insights.trace.assurance'),
              value:
                detail.validation.assurance_passed == null
                  ? t('insights.trace.notRun')
                  : detail.validation.assurance_passed
                    ? t('insights.trace.passed')
                    : t('insights.trace.failed'),
            },
            { label: t('insights.trace.outputDlp'), value: detail.validation.output_dlp ?? t('insights.trace.notRun') },
            { label: t('insights.trace.context'), value: detail.validation.context_validation ?? t('insights.none') },
            {
              label: t('insights.trace.contextTokens'),
              value: detail.optimization
                ? t('insights.trace.contextTokensValue', {
                    before: formatCount(detail.optimization.context_tokens_before),
                    after: formatCount(detail.optimization.context_tokens_after),
                  })
                : t('insights.none'),
            },
            {
              label: t('insights.trace.reservation'),
              value: detail.reservation
                ? t('insights.trace.reservationValue', {
                    reserved: formatMicrosUsd(detail.reservation.estimate_micros),
                    actual: formatMicrosUsd(detail.reservation.actual_micros),
                    released: formatMicrosUsd(detail.reservation.released_micros),
                  })
                : t('insights.trace.noReservation'),
            },
          ]}
        />
      </Section>

      <Section title={t('insights.trace.spans')} description={t('insights.trace.spans.description')}>
        <ol className="space-y-1 text-secondary">
          {detail.spans.map((span) => (
            <li key={`${span.name}-${span.started_at}`} className="flex flex-wrap gap-x-3">
              <Mono>{span.name}</Mono>
              <span className="text-ink-muted">{span.status}</span>
              <span className="tabular-nums">{formatDurationMs(span.duration_ms)}</span>
              {span.skip_reason && <span className="text-ink-muted">{span.skip_reason}</span>}
            </li>
          ))}
        </ol>
      </Section>
    </div>
  );
}

const STAGES = ['upstream', 'cim', 'baseline', 'validation', 'quality_reference', 'quality_grading'] as const;
const CHARGES = [
  'upstream_inference',
  'cim',
  'retrieval',
  'gpu_estimate',
  'retry',
  'baseline_comparison',
  'quality_reference',
  'quality_grading',
  'tool_call',
  'adjustment',
] as const;
const ACTIONS = ['allow', 'deny', 'approval_required'] as const;

function stageKey(value: string): (typeof STAGES)[number] | 'other' {
  return (STAGES as readonly string[]).includes(value) ? (value as (typeof STAGES)[number]) : 'other';
}

function chargeKey(value: string): (typeof CHARGES)[number] | 'other' {
  return (CHARGES as readonly string[]).includes(value) ? (value as (typeof CHARGES)[number]) : 'other';
}

function actionKey(value: string): (typeof ACTIONS)[number] | 'other' {
  return (ACTIONS as readonly string[]).includes(value) ? (value as (typeof ACTIONS)[number]) : 'other';
}
