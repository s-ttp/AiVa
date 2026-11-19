import { t } from '../i18n';
import { classificationLabel, designationLabel, pathLabel } from '../labels';
import type { Classification } from '../api/client';
import type { FilterKey } from './params';

/** Display labels for report dimension values; unknown values are shown as sent, never hidden. */
const OUTCOMES = ['success', 'quality_fail', 'policy_denied', 'quota_denied', 'approval_required', 'timeout', 'error'] as const;
const SENSITIVITIES = ['public', 'internal', 'confidential', 'restricted', 'sovereign-only'] as const;
const CIM_STATES = ['invoked', 'bypassed-policy', 'skipped-sufficient', 'skipped-cache', 'not-reached'] as const;

export function outcomeLabel(value: string): string {
  return (OUTCOMES as readonly string[]).includes(value) ? t(`outcome.${value as (typeof OUTCOMES)[number]}`) : value;
}

export function cimStateLabel(value: string): string {
  return (CIM_STATES as readonly string[]).includes(value)
    ? t(`cimState.${value as (typeof CIM_STATES)[number]}`)
    : value;
}

export function sensitivityLabel(value: string): string {
  return (SENSITIVITIES as readonly string[]).includes(value) ? classificationLabel(value as Classification) : value;
}

/** Taxonomy IDs are stable kebab/snake case; the unclassified bucket is named explicitly (LLD §21.3). */
export function categoryLabel(value: string): string {
  if (value === 'unclassified') return t('insights.unclassified');
  if (value === 'none') return t('insights.none');
  if (value === 'unknown') return t('insights.unknown');
  const words = value.replace(/[-_]/g, ' ');
  return words.length <= 3 ? words.toUpperCase() : words.charAt(0).toUpperCase() + words.slice(1);
}

/** The label of a value of a filter dimension, preferring the gateway's label when it sent one. */
export function dimensionLabel(key: FilterKey | string, value: string, gatewayLabel?: string): string {
  switch (key) {
    case 'execution_path':
    case 'path':
      return pathLabel(value);
    case 'sensitivity':
      return sensitivityLabel(value);
    case 'outcome':
      return outcomeLabel(value);
    case 'data_designation':
      return designationLabel(value);
    case 'task_category':
    case 'category':
    case 'business_function':
    case 'intent':
    case 'complexity':
    case 'language':
    case 'strategy':
      return categoryLabel(value);
    case 'source_policy':
    case 'policy':
      return value === 'none' ? t('insights.none') : value;
    default:
      return gatewayLabel && gatewayLabel !== value ? gatewayLabel : value || t('insights.none');
  }
}

/** Filter names as shown in the filter bar and on active-filter chips. */
export function filterLabel(key: FilterKey): string {
  return t(`insights.filter.${key}`);
}
