import { t } from '../i18n';
import type { ApiError } from './client';

/** Message plus remediation and status, without leaking more than the gateway chose to say. */
export function describeError(error: ApiError): string {
  const parts = [error.message];
  if (error.remediation) parts.push(error.remediation);
  if (error.status !== null) parts.push(t('error.status', { status: error.status, code: error.code ?? '—' }));
  return parts.join(' ');
}
