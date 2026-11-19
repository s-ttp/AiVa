import type { ApiError, ChatRequest, ChatResponse } from '../api/client';
import { t } from '../i18n';

/**
 * The per-run execution mode of the public demo (ADR-0019): `replay` answers
 * from recorded provider exchanges, `live` calls the providers. A request may
 * ask for one; the response says which mode actually ran.
 */
export type ExecutionMode = NonNullable<ChatRequest['execution_mode']>;
/** What ran: also `record` (a live run that records its exchanges) and `simulated` (the configured mode). */
export type RanMode = NonNullable<ChatResponse['execution_mode']>;

/** Who chooses the mode: a visitor (replay by default), another live-capable role, or nobody. */
export type ModeChoice = 'visitor' | 'member' | 'none';

export const LIVE_DEMO_REASONS = ['budget', 'visitor_limit', 'endpoint_cold'] as const;
export type LiveDemoReason = (typeof LIVE_DEMO_REASONS)[number];
/** Why the UI offers the replay instead: a public-demo limit, or this role or gateway cannot run live. */
export type LiveRefusalReason = LiveDemoReason | 'not_permitted' | 'gateway_not_live';

export interface LiveRefusal {
  /** Null for a reason this UI does not know yet; the gateway's message is shown instead. */
  reason: LiveRefusalReason | null;
  /** When a live run can next be allowed; null when unknown (e.g. a sleeping endpoint). */
  resetsAt: string | null;
}

/** The chat request with the chosen mode; no field at all leaves the choice to the gateway. */
export function withExecutionMode(body: ChatRequest, mode: ExecutionMode | null): ChatRequest {
  return mode === null ? body : { ...body, execution_mode: mode };
}

/** The mode that actually ran, as the gateway reported it; null when the response does not say. */
export function executionModeOf(run: ChatResponse): RanMode | null {
  return run.execution_mode ?? null;
}

function isLiveDemoReason(value: unknown): value is LiveDemoReason {
  return typeof value === 'string' && (LIVE_DEMO_REASONS as readonly string[]).includes(value);
}

/**
 * A refused live run and why. The public demo refuses with a blocked run
 * (HTTP 200, `LIVE_DEMO_UNAVAILABLE`); a role without `chat:live`
 * (`LIVE_NOT_PERMITTED`, 403) or a gateway that is not live-capable
 * (`EXECUTION_MODE_UNAVAILABLE`, 409) refuses the request itself.
 */
export function liveRefusal(error: ApiError | ChatResponse['error'] | null | undefined): LiveRefusal | null {
  switch (error?.code) {
    case 'LIVE_DEMO_UNAVAILABLE': {
      const details: Record<string, unknown> = error.details ?? {};
      return {
        reason: isLiveDemoReason(details.reason) ? details.reason : null,
        resetsAt: typeof details.resets_at === 'string' ? details.resets_at : null,
      };
    }
    case 'LIVE_NOT_PERMITTED':
      return { reason: 'not_permitted', resetsAt: null };
    case 'EXECUTION_MODE_UNAVAILABLE':
      return { reason: 'gateway_not_live', resetsAt: null };
    default:
      return null;
  }
}

/** A refusal that holds for every later live request here, so the choice is hidden from then on. */
export function refusesEveryLiveRun(refusal: LiveRefusal | null): boolean {
  return refusal?.reason === 'not_permitted' || refusal?.reason === 'gateway_not_live';
}

/** Why a live run is refused, in the person's terms; one sentence. */
export function liveRefusalText(reason: LiveRefusalReason): string {
  return t(`liveDemo.reason.${reason}`);
}
