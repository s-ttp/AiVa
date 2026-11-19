import { Badge, KpiTile, Ltr, StatusBadge, buttonClasses } from '@aiva/ui';
import {
  CircleDot,
  CircleSlash,
  FlaskConical,
  History,
  Info,
  ShieldAlert,
  ShieldX,
  WalletCards,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import { Link } from 'react-router';
import type { ChatResponse } from '../api/client';
import { useSession } from '../auth/session';
import { FactList } from '../components/FactList';
import { IdList } from '../components/inline';
import { formatCount, formatDurationMs, formatList, formatMicrosUsd } from '../format';
import { t, tRich } from '../i18n';
import { cacheTypeLabel, classificationLabel, isInternetCaller, pathLabel } from '../labels';
import { executionModeOf, liveRefusal, type RanMode } from './executionMode';
import { rootSpan } from './trace';

const MODE_ICONS: Record<RanMode, LucideIcon> = { replay: History, live: Zap, record: CircleDot, simulated: FlaskConical };

/** The policy outcome badge, with the LLD §22.8 copy, and the execution mode that actually ran (ADR-0019). */
export function RunStatusBadges({ run }: { run: ChatResponse }) {
  const mode = executionModeOf(run);
  // A refused live run is neither a policy block, a budget cap nor an outage: it says what it is.
  const liveRefused = liveRefusal(run.error) !== null;
  return (
    <div className="flex flex-wrap gap-2">
      {mode && (
        <Badge icon={MODE_ICONS[mode]} tone="info">
          {t(`run.mode.${mode}`)}
        </Badge>
      )}
      {liveRefused && (
        <Badge icon={CircleSlash} tone="warning">
          {t('run.status.liveUnavailable')}
        </Badge>
      )}
      {run.status === 'completed' && <StatusBadge status="allowed" />}
      {!liveRefused && run.status === 'blocked' && run.outcome === 'quota_denied' && (
        <Badge icon={WalletCards} tone="danger">
          {t('run.status.quotaDenied')}
        </Badge>
      )}
      {!liveRefused && run.status === 'blocked' && run.outcome !== 'quota_denied' && <StatusBadge status="blocked" />}
      {run.status === 'approval_required' && <StatusBadge status="approvalRequired" />}
      {!liveRefused && run.status === 'failed' && (
        <Badge icon={ShieldAlert} tone="danger">
          {t('run.status.failed')}
        </Badge>
      )}
      {run.execution_path === 'sovereign-direct' && <StatusBadge status="sovereignDirect" />}
      {run.cim.sufficiency === 'verified' && <StatusBadge status="cimActive" />}
      {run.cim.sufficiency === 'failed-fallback-applied' && <StatusBadge status="cimFailed" />}
    </div>
  );
}

/** Denials carry the rule IDs and remediation the gateway chose to disclose (LLD §22.6). */
export function RunErrorNotice({ run }: { run: ChatResponse }) {
  const error = run.error;
  if (!error) return null;
  const ruleIds = error.rule_ids ?? [];
  return (
    <div className="flex items-start gap-3 rounded-card border border-danger/40 bg-danger/5 p-4">
      <ShieldX className="mt-0.5 size-5 shrink-0 text-danger" />
      <div className="space-y-1">
        <p className="font-semibold text-ink">{error.message}</p>
        {ruleIds.length > 0 && (
          <p className="text-secondary">
            {tRich('run.error.rules', { ids: <Ltr className="font-mono">{formatList(ruleIds)}</Ltr> })}
          </p>
        )}
        {error.remediation && <p className="text-secondary">{t('run.error.remediation', { text: error.remediation })}</p>}
        <p className="text-secondary text-ink-muted">
          {tRich('run.error.code', { code: <Ltr className="font-mono">{error.code}</Ltr> })}
        </p>
      </div>
    </div>
  );
}

type AnswerCitations = NonNullable<ChatResponse['assurance']['answer_citations']>;

/**
 * The answer-citation check (ADR-0009): whether the answer cites only what its
 * context package held. Informational: it never blocks a run.
 */
function CitationsText({ citations }: { citations: AnswerCitations }) {
  if (citations.status !== 'unresolved') return <>{t(`run.citations.${citations.status}`)}</>;
  const refs = citations.unresolved_refs ?? [];
  return (
    <span className="flex flex-col gap-1">
      {t('run.citations.unresolved', {
        unresolved: formatCount(citations.unresolved),
        cited: formatCount(citations.cited),
      })}
      {refs.length > 0 && <IdList ids={refs} empty="" />}
    </span>
  );
}

function cacheText(run: ChatResponse): string {
  if (!run.cache.checked) return t('run.cache.notChecked');
  if (run.cache.hit) return t('run.cache.hit', { type: cacheTypeLabel(run.cache.cache_type ?? 'exact') });
  return t('run.cache.miss');
}

export function RunSummary({ run }: { run: ChatResponse }) {
  const { can } = useSession();
  const total = rootSpan(run.trace ?? []);
  const upstreamCall = run.cache.upstream_call;
  const routeCost = run.cost.upstream_micros + run.cost.cim_micros;
  const window = t('kpi.window.run');
  // Null when no call ran on a priced GPU endpoint: no GPU estimate applies (never shown as $0).
  const gpuProvenance = run.cost.gpu_estimated_provenance ?? null;
  // A run that stopped before AI policy (e.g. a refused live run) records no policy version.
  const policyEvaluated = Boolean(run.policy.policy_version);
  return (
    <div className="space-y-6">
      <RunStatusBadges run={run} />
      {executionModeOf(run) === 'replay' && run.usage.provenance === 'simulated' && (
        <p className="flex items-start gap-2 text-secondary">
          <Info className="mt-0.5 size-4 shrink-0 text-info" />
          {t('run.mode.replayMiss')}
        </p>
      )}
      <RunErrorNotice run={run} />
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <KpiTile
          label={t('kpi.inputTokens')}
          value={upstreamCall ? formatCount(run.usage.upstream_submitted_input_tokens) : t('kpi.noUpstreamCall')}
          provenance={upstreamCall ? run.usage.provenance : null}
          detail={{
            source: t('kpi.tokens.source'),
            calculation: t('kpi.inputTokens.calculation'),
            window,
            cacheAccounting: t('kpi.tokens.cache'),
          }}
        />
        <KpiTile
          label={t('kpi.outputTokens')}
          value={upstreamCall ? formatCount(run.usage.upstream_output_tokens) : t('kpi.noUpstreamCall')}
          provenance={upstreamCall ? run.usage.provenance : null}
          detail={{
            source: t('kpi.tokens.source'),
            calculation: t('kpi.outputTokens.calculation'),
            window,
            cacheAccounting: t('kpi.tokens.cache'),
          }}
        />
        <KpiTile
          label={t('kpi.routeCost')}
          value={formatMicrosUsd(routeCost)}
          provenance={run.cost.provenance}
          detail={{
            source: t('kpi.cost.source'),
            calculation: t('kpi.routeCost.calculation'),
            window,
            pricingVersion: run.cost.rate_card_version,
            limitations: t('kpi.routeCost.limitations'),
          }}
        />
        <KpiTile
          label={t('kpi.gpuCost')}
          value={gpuProvenance ? formatMicrosUsd(run.cost.gpu_estimated_micros) : t('kpi.notApplicable')}
          provenance={gpuProvenance}
          detail={{
            source: t('kpi.gpuCost.source'),
            calculation: t('kpi.gpuCost.calculation'),
            window,
            pricingVersion: run.cost.rate_card_version,
            limitations: gpuProvenance ? t('kpi.gpuCost.limitations') : t('kpi.gpuCost.notApplicable'),
          }}
        />
        <KpiTile
          label={t('kpi.reserved')}
          value={run.budget.reservation_id ? formatMicrosUsd(run.budget.reserved_micros) : t('kpi.noReservation')}
          provenance={run.budget.reservation_id ? run.budget.provenance : null}
          detail={{
            source: t('kpi.reserved.source'),
            calculation: t('kpi.reserved.calculation', {
              committed: formatMicrosUsd(run.budget.committed_micros),
              released: formatMicrosUsd(run.budget.released_micros),
            }),
            window,
          }}
        />
        <KpiTile
          label={t('kpi.gatewayTime')}
          value={total ? formatDurationMs(total.duration_ms) : null}
          provenance={run.usage.provenance}
          detail={{
            source: t('kpi.gatewayTime.source'),
            calculation: t('kpi.gatewayTime.calculation'),
            window,
            limitations: t('kpi.gatewayTime.limitations'),
          }}
        />
      </div>
      <FactList
        facts={[
          {
            label: t('run.fact.policy'),
            value: policyEvaluated
              ? t('run.fact.policyValue', {
                  action: t(`policyAction.${run.policy.action}`),
                  version: run.policy.policy_version,
                })
              : t('run.fact.policyNotEvaluated'),
          },
          ...(run.approval?.satisfied_by
            ? [{ label: t('run.fact.approval'), value: <Ltr className="font-mono">{run.approval.satisfied_by}</Ltr> }]
            : []),
          {
            label: t('run.fact.classification'),
            value: run.policy.declared_classification
              ? t('run.fact.classificationDeclared', {
                  effective: classificationLabel(run.policy.effective_classification),
                  declared: classificationLabel(run.policy.declared_classification),
                })
              : t('run.fact.classificationUndeclared', {
                  effective: classificationLabel(run.policy.effective_classification),
                }),
          },
          {
            label: t('run.fact.identity'),
            value: tRich('run.fact.identityValue', {
              application: <Ltr className="font-mono">{run.identity.application_id}</Ltr>,
              identity: t(`identityMode.${run.identity.identity_mode}`),
              source: t(`sourceMode.${run.identity.source_mode}`),
              network: isInternetCaller(run.identity.matched_network_id) ? (
                t('run.network.internet')
              ) : run.identity.matched_network_id ? (
                <Ltr className="font-mono">{run.identity.matched_network_id}</Ltr>
              ) : policyEvaluated ? (
                t('run.fact.noNetwork')
              ) : (
                t('run.fact.networkNotEvaluated')
              ),
            }),
          },
          { label: t('run.fact.path'), value: pathLabel(run.execution_path) },
          {
            label: t('run.fact.model'),
            value: !run.selected_model ? (
              t('run.fact.noModel')
            ) : run.cache.upstream_call || run.cache.hit ? (
              <Ltr className="font-mono">{run.selected_model}</Ltr>
            ) : (
              t('run.fact.modelNotCalled', { model: run.selected_model })
            ),
          },
          {
            label: t('run.fact.rlcd'),
            value: run.routing
              ? t('run.fact.rlcdValue', {
                  version: run.routing.rlcd_version,
                  task: run.routing.task_category ?? '—',
                  complexity: run.routing.complexity ?? '—',
                })
              : t('run.fact.notReached'),
          },
          { label: t('run.fact.cim'), value: run.cim.message },
          { label: t('run.fact.sufficiency'), value: t(`sufficiency.${run.cim.sufficiency}`) },
          { label: t('run.fact.cache'), value: cacheText(run) },
          {
            label: t('run.fact.assurance'),
            value:
              (run.assurance.checks ?? []).length === 0 && run.assurance.output_dlp === 'not-run'
                ? t('run.assurance.notRun')
                : t('run.fact.assuranceValue', {
                    result: run.assurance.passed ? t('run.assurance.passed') : t('run.assurance.notPassed'),
                    dlp: t(`outputDlp.${run.assurance.output_dlp}`),
                  }),
          },
          ...(run.assurance.answer_citations
            ? [{ label: t('run.fact.citations'), value: <CitationsText citations={run.assurance.answer_citations} /> }]
            : []),
          {
            label: t('run.fact.designation'),
            value: t(`designation.${run.data_designation}`),
          },
          { label: t('run.fact.runId'), value: <Ltr className="font-mono">{run.run_id}</Ltr> },
          { label: t('run.fact.traceId'), value: <Ltr className="font-mono">{run.trace_id}</Ltr> },
        ]}
      />
      {can('runs:read') && (
        <Link to={`/admin/audit?run=${encodeURIComponent(run.run_id)}`} className={buttonClasses('secondary')}>
          {t('run.openInExplorer')}
        </Link>
      )}
    </div>
  );
}
