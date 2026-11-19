import { ApiError, createAivaClient, isGatewayPaused, isGatewayStarting, type AivaClient, type components } from '@aiva/api-client';
import { observeHfRateLimit } from './hfBackoff';

export {
  ApiError,
  isGatewayPaused,
  isGatewayStarting,
  isPlatformRateLimited,
  retryAfterMinutes,
  toApiError,
  unwrap,
} from '@aiva/api-client';

type Schemas = components['schemas'];
export type CallerProfile = Schemas['CallerProfile'];
export type DemoUser = Schemas['DemoUser'];
export type DevToken = Schemas['DevToken'];
export type SignInOptions = Schemas['SignInOptions'];
export type LiveBudget = Schemas['LiveBudget'];
export type Scenario = Schemas['Scenario'];
export type ChatRequest = Schemas['ChatRequest'];
export type ChatResponse = Schemas['ChatResponse'];
export type TraceSpan = Schemas['TraceSpanOut'];
export type RunDetail = Schemas['RunDetail'];
export type RunTrace = Schemas['RunTrace'];
export type PolicyVersionSummary = Schemas['PolicyVersionSummary'];
export type BudgetList = Schemas['BudgetList'];
export type BudgetStatus = Schemas['BudgetStatus'];
export type BudgetAlert = Schemas['BudgetAlert'];
export type ApprovalRequest = Schemas['ApprovalRequestOut'];
export type ApprovalDecisionResponse = Schemas['ApprovalDecisionResponse'];
export type PolicyDecision = Schemas['PolicyDecision'];
export type PolicyVersionDetail = Schemas['PolicyVersionDetail'];
export type PolicyChange = Schemas['PolicyChange'];
export type PolicyDocument = Schemas['PolicyDocument'];
export type PolicyMatch = Schemas['PolicyMatch'];
export type PolicyEnforce = Schemas['PolicyEnforce'];
export type PolicyAction = Schemas['PolicyAction'];
export type PolicyTestResult = Schemas['PolicyTestResult'];
export type PolicyTestCase = Schemas['PolicyTestCase'];
export type PolicyTestResponse = Schemas['PolicyTestResponse'];
export type PolicyPublishResponse = Schemas['PolicyPublishResponse'];
export type ApplicationList = Schemas['ApplicationList'];
export type ApplicationOut = Schemas['ApplicationOut'];
export type CredentialList = Schemas['CredentialList'];
export type CredentialOut = Schemas['CredentialOut'];
export type CredentialIssued = Schemas['CredentialIssued'];
export type TrustedNetworkList = Schemas['TrustedNetworkList'];
export type ModelAllowlist = Schemas['ModelAllowlist'];
export type ModelOut = Schemas['ModelOut'];
export type ModelSwitchResponse = Schemas['ModelSwitchResponse'];
export type ToolList = Schemas['ToolList'];
export type ArtifactCreateRequest = Schemas['ArtifactCreateRequest'];
export type ArtifactCreateResponse = Schemas['ArtifactCreateResponse'];
export type ArtifactOut = Schemas['ArtifactOut'];
export type ArtifactCheck = Schemas['ArtifactCheck'];
export type ArtifactPreview = Schemas['ArtifactPreview'];
export type PreviewBlock = Schemas['PreviewBlock'];
export type AgentWorkflowRequest = Schemas['AgentWorkflowRequest'];
export type AgentWorkflowOut = Schemas['AgentWorkflowOut'];
export type AgentStepOut = Schemas['AgentStepOut'];
export type WorkflowBudgetOut = Schemas['WorkflowBudgetOut'];
export type Classification = Schemas['Classification'];
export type ExecutionPath = Schemas['ExecutionPath'];
export type ErrorBody = Schemas['ErrorBody'];
export type ModelUsage = Schemas['ModelUsage'];
export type SearchSource = Schemas['SearchSource'];
export type QualityCheckRequest = Schemas['QualityCheckRequest'];
export type QualityCheckResponse = Schemas['QualityCheckResponse'];

export type GatewayClient = AivaClient;

/** The demo's gateway Space is not running: asleep or waking (retried on its own), or paused (not retried). */
export function isGatewayDown(error: ApiError): boolean {
  return isGatewayStarting(error) || isGatewayPaused(error);
}

/** The endpoint exists in the contract but the gateway does not implement it yet (HTTP 501). */
export function isNotImplemented(error: ApiError): boolean {
  return error.status === 501;
}

/**
 * Validation messages from a 422 envelope: the gateway sends strings for
 * reference errors and `{ loc, msg }` objects for schema errors.
 */
export function validationMessages(error: ApiError): string[] {
  const errors = error.details.errors;
  if (!Array.isArray(errors)) return [];
  return errors.flatMap((entry: unknown): string[] => {
    if (typeof entry === 'string') return [entry];
    if (typeof entry === 'object' && entry !== null && 'msg' in entry && typeof entry.msg === 'string') {
      const loc = 'loc' in entry && Array.isArray(entry.loc) ? entry.loc.join('.') : '';
      return [loc ? `${loc}: ${entry.msg}` : entry.msg];
    }
    return [];
  });
}

/** String identifiers listed under a key of the error details (e.g. `high_impact`). */
export function detailIds(error: ApiError, key: string): string[] {
  const value = error.details[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

/** The gateway is a Hugging Face Space the browser calls directly (ADR-0025), which may be asleep or waking. */
export function isHfSpace(baseUrl: string): boolean {
  return URL.canParse(baseUrl) && new URL(baseUrl).hostname.endsWith('.hf.space');
}

/**
 * Typed gateway client. `onUnauthorized` runs when an authenticated request
 * is rejected with 401, so the session can end instead of failing silently.
 * Every response feeds the shared Hugging Face back-off (./hfBackoff). The
 * token goes only in `X-AiVa-Authorization`, never `Authorization`, so no
 * platform edge on the way ever sees it there (ADR-0025).
 */
export function createGatewayClient(
  baseUrl: string,
  getToken: () => string | null,
  onUnauthorized: () => void,
): GatewayClient {
  return createAivaClient(baseUrl, getToken, {
    onUnauthorized,
    onHfRateLimit: observeHfRateLimit,
    credentialHeader: 'X-AiVa-Authorization',
    unreachableMeansStarting: isHfSpace(baseUrl),
  });
}
