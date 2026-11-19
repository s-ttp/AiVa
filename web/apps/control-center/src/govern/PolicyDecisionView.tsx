import { StatusBadge, type Status } from '@aiva/ui';
import type { PolicyDecision } from '../api/client';
import { DataTable } from '../components/DataTable';
import { FactList } from '../components/FactList';
import { IdList, Mono } from '../components/inline';
import { t } from '../i18n';
import { isInternetCaller, pathLabel } from '../labels';

const ACTION_STATUS: Record<PolicyDecision['action'], Status> = {
  allow: 'allowed',
  deny: 'blocked',
  approval_required: 'approvalRequired',
};

export function PolicyActionBadge({ action }: { action: PolicyDecision['action'] }) {
  return <StatusBadge status={ACTION_STATUS[action]} />;
}

/** An AI-PEL decision: action, the bundle that made it, constraints, reasons and denied models. */
export function PolicyDecisionView({ decision, caption }: { decision: PolicyDecision; caption: string }) {
  const effective = decision.effective;
  return (
    <div className="space-y-4">
      <PolicyActionBadge action={decision.action} />
      <FactList
        facts={[
          { label: t('decision.version'), value: decision.policy_version || '—' },
          { label: t('decision.bundle'), value: decision.bundle_hash ? <Mono>{decision.bundle_hash}</Mono> : '—' },
          { label: t('decision.rules'), value: <IdList ids={decision.matched_rule_ids ?? []} empty={t('common.none')} /> },
          {
            label: t('decision.network'),
            value: isInternetCaller(decision.matched_network_id) ? (
              t('run.network.internet')
            ) : decision.matched_network_id ? (
              <Mono>{decision.matched_network_id}</Mono>
            ) : (
              t('run.fact.noNetwork')
            ),
          },
          {
            label: t('decision.path'),
            value: effective?.execution_path ? pathLabel(effective.execution_path) : t('decision.path.rlcd'),
          },
          {
            label: t('decision.models'),
            value: <IdList ids={effective?.allowed_models ?? []} empty={t('decision.models.none')} />,
          },
          { label: t('decision.cim'), value: t(`decision.cim.${effective?.cim ?? 'allowed'}`) },
          {
            label: t('decision.external'),
            value: t(`decision.external.${effective?.external_providers ?? 'allow'}`),
          },
          { label: t('decision.outputDlp'), value: t(`decision.outputDlp.${effective?.output_dlp ?? 'optional'}`) },
          { label: t('decision.audit'), value: t(`decision.audit.${effective?.audit_level ?? 'standard'}`) },
          {
            label: t('decision.satisfied'),
            value: <IdList ids={decision.satisfied_approval_rule_ids ?? []} empty={t('common.none')} />,
          },
        ]}
      />
      <DataTable
        caption={t('decision.reasons.caption', { context: caption })}
        showCaption
        columns={[
          { label: t('routing.reasons.rule') },
          { label: t('routing.reasons.message') },
          { label: t('routing.reasons.remediation') },
        ]}
        rows={(decision.reasons ?? []).map((reason, index) => ({
          key: `${reason.rule_id}-${index}`,
          cells: [<Mono key="r">{reason.rule_id}</Mono>, reason.message, reason.remediation ?? '—'],
        }))}
        empty={t('routing.reasons.empty')}
      />
      {(decision.denied_candidates ?? []).length > 0 && (
        <DataTable
          caption={t('decision.denied.caption', { context: caption })}
          showCaption
          columns={[{ label: t('routing.denied.model') }, { label: t('routing.denied.reason') }]}
          rows={(decision.denied_candidates ?? []).map((candidate) => ({
            key: candidate.model_id,
            cells: [<Mono key="m">{candidate.model_id}</Mono>, candidate.reason],
          }))}
        />
      )}
    </div>
  );
}
