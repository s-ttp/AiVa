import type { PolicyEnforce, PolicyMatch } from '../api/client';
import { formatList } from '../format';
import { t } from '../i18n';
import { classificationLabel, pathLabel } from '../labels';

const LIST_CONDITIONS = [
  'classification_any',
  'application_any',
  'task_any',
  'tags_any',
  'dlp_findings_any',
  'source_cidr_any',
  'environment_any',
  'tool_any',
  'tool_access_any',
  'tool_egress_any',
] as const;

/** Plain-language conditions of a policy match; an empty match applies to every request. */
export function matchConditions(match: PolicyMatch): string[] {
  const conditions: string[] = [];
  for (const key of LIST_CONDITIONS) {
    const values = match[key];
    if (!values || values.length === 0) continue;
    const shown =
      key === 'classification_any'
        ? (match.classification_any ?? []).map(classificationLabel)
        : (values as readonly string[]);
    conditions.push(t(`policyMatch.${key}`, { values: formatList(shown) }));
  }
  if (match.injection_detected != null) {
    conditions.push(t(match.injection_detected ? 'policyMatch.injection.yes' : 'policyMatch.injection.no'));
  }
  if (match.evidence_injection_detected != null) {
    conditions.push(
      t(match.evidence_injection_detected ? 'policyMatch.evidenceInjection.yes' : 'policyMatch.evidenceInjection.no'),
    );
  }
  return conditions.length ? conditions : [t('policyMatch.always')];
}

/** Plain-language restrictions a policy enforces when it matches. */
export function enforcement(enforce: PolicyEnforce): string[] {
  const effects: string[] = [];
  if (enforce.action === 'deny') effects.push(t('policyEnforce.deny'));
  if (enforce.action === 'approval_required') effects.push(t('policyEnforce.approval'));
  if (enforce.execution_path) effects.push(t('policyEnforce.path', { path: pathLabel(enforce.execution_path) }));
  if (enforce.allowed_models) {
    effects.push(
      enforce.allowed_models.length
        ? t('policyEnforce.models', { models: formatList(enforce.allowed_models) })
        : t('policyEnforce.models.none'),
    );
  }
  if (enforce.cim === 'bypass') effects.push(t('policyEnforce.cimBypass'));
  if (enforce.external_providers === 'deny') effects.push(t('policyEnforce.externalDeny'));
  if (enforce.output_dlp === 'required') effects.push(t('policyEnforce.outputDlp'));
  if (enforce.audit_level) effects.push(t(`policyEnforce.audit.${enforce.audit_level}`));
  return effects.length ? effects : [t('policyEnforce.none')];
}
