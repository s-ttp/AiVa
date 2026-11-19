import { ApiError, toApiError, type ChatRequest, type ChatResponse, type GatewayClient } from '../api/client';
import { t } from '../i18n';

function isChatResponse(value: unknown): value is ChatResponse {
  return typeof value === 'object' && value !== null && 'run_id' in value && 'policy' in value;
}

/**
 * Executes one governed request. A run that failed closed (e.g. 503) still
 * returns a run record, which is shown like any other run.
 */
export async function executeRun(client: GatewayClient, body: ChatRequest, signal?: AbortSignal): Promise<ChatResponse> {
  const headers: Record<string, string> = {};
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) headers['Idempotency-Key'] = crypto.randomUUID();
  let result;
  try {
    result = await client.POST('/v1/ai/chat', { body, headers, signal });
  } catch {
    throw new ApiError({ kind: 'network', message: t('run.networkError') });
  }
  if (result.data) return result.data;
  if (isChatResponse(result.error)) return result.error;
  throw toApiError(result.response.status, result.error, undefined, result.response.headers);
}
