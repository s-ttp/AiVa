import type { Classification, ExecutionPath } from './api/client';
import { t } from './i18n';

/** User-facing labels for gateway vocabularies. Unknown values are shown as sent, never hidden. */
const ROLES = [
  'platform_admin',
  'tenant_admin',
  'finance_analyst',
  'department_manager',
  'security_auditor',
  'application_owner',
  'tester',
  'public_visitor',
] as const;

export function roleLabel(role: string): string {
  return (ROLES as readonly string[]).includes(role) ? t(`role.${role as (typeof ROLES)[number]}`) : role;
}

/**
 * A run's matched network. `internet` is no trusted network but a documented value: a call made with an
 * application's key from any source, which `internet_callers` allows (ADR-0027). It has no entry to link to.
 */
export const isInternetCaller = (networkId: string | null | undefined) => networkId === 'internet';

export function classificationLabel(value: Classification): string {
  return t(`classification.${value}`);
}

const EXECUTION_PATHS: readonly ExecutionPath[] = ['cache-return', 'sovereign-direct', 'local-fast', 'cim-assisted', 'none'];

export function pathLabel(value: ExecutionPath | string): string {
  return (EXECUTION_PATHS as readonly string[]).includes(value) ? t(`path.${value as ExecutionPath}`) : value;
}

const IDENTITY_MODES = ['authenticated', 'simulated'] as const;
const SOURCE_MODES = ['observed', 'simulated'] as const;

export function identityModeLabel(value: string): string {
  return (IDENTITY_MODES as readonly string[]).includes(value)
    ? t(`identityMode.${value as (typeof IDENTITY_MODES)[number]}`)
    : value;
}

export function sourceModeLabel(value: string): string {
  return (SOURCE_MODES as readonly string[]).includes(value)
    ? t(`sourceMode.${value as (typeof SOURCE_MODES)[number]}`)
    : value;
}

export function designationLabel(value: string): string {
  return value === 'synthetic' || value === 'actual' ? t(`designation.${value}`) : value;
}

const CACHE_TYPES = ['exact', 'semantic', 'exact+semantic'] as const;

/** The gateway reports the cache that answered a hit, or the caches it checked on a miss. */
export function cacheTypeLabel(value: string): string {
  return (CACHE_TYPES as readonly string[]).includes(value)
    ? t(`cacheType.${value as (typeof CACHE_TYPES)[number]}`)
    : value;
}
