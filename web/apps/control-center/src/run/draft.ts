import type { ChatRequest, Classification, Scenario } from '../api/client';
import { withExecutionMode, type ExecutionMode } from './executionMode';

export const CLASSIFICATIONS: readonly Classification[] = ['public', 'internal', 'confidential', 'restricted', 'sovereign-only'];
export const ROUTING_MODES = ['hybrid', 'local-only', 'pinned'] as const;
export const QUALITY_TIERS = ['standard', 'high', 'critical'] as const;

/**
 * Default simulated source: an RFC 5737 documentation address inside the
 * "AiVa Test Console (simulated ingress)" network of config/tenant.yaml.
 */
export const DEFAULT_SIMULATED_IP = '192.0.2.10';

/** Caller-supplied evidence; the gateway treats it as untrusted data, never as instructions (LLD §17). */
export type Evidence = NonNullable<ChatRequest['evidence']>;

export interface RunDraft {
  scenarioId: string;
  prompt: string;
  applicationId: string;
  simulatedSourceIp: string;
  classification: Classification | '';
  tags: string;
  routingMode: (typeof ROUTING_MODES)[number];
  qualityTier: (typeof QUALITY_TIERS)[number];
  maxOutputTokens: number;
  baselineComparison: boolean;
  /** Replay or live (ADR-0019); null leaves the choice to the gateway. */
  executionMode: ExecutionMode | null;
  /** The scenario's synthetic evidence from the catalog, sent as is so its recorded replay matches. */
  evidence: Evidence;
}

/**
 * The scenario's default request, as the catalog defines it: its prompt, application, classification, tags,
 * quality tier, output budget and evidence. The tester's own settings (source address, routing mode, baseline
 * toggle, execution mode) carry over from `previous` when the scenario changes.
 */
export function draftFor(scenario: Scenario | undefined, previous?: RunDraft): RunDraft {
  return {
    scenarioId: scenario?.id ?? '',
    prompt: scenario?.sample_prompt ?? '',
    applicationId: scenario?.application_id ?? previous?.applicationId ?? '',
    simulatedSourceIp: previous?.simulatedSourceIp ?? DEFAULT_SIMULATED_IP,
    classification: scenario?.declared_classification ?? '',
    tags: scenario?.routing_tags.join(', ') ?? '',
    routingMode: previous?.routingMode ?? 'hybrid',
    qualityTier: scenario?.quality_tier ?? previous?.qualityTier ?? 'standard',
    maxOutputTokens: scenario?.max_output_tokens ?? previous?.maxOutputTokens ?? 1024,
    baselineComparison: previous?.baselineComparison ?? false,
    executionMode: previous?.executionMode ?? null,
    evidence: (scenario?.sample_evidence ?? []).map(({ id, text }) => ({ id, text })),
  };
}

/**
 * The POST /v1/ai/chat body for a draft: always traced, always as a simulated Test Console identity.
 * `evidence` is added only when the scenario defines it, after `test_profile`, exactly as the replay
 * check builds it (`tests/e2e/scenario_requests.py::chat_body`), so every other body is unchanged.
 */
export function toChatRequest(draft: RunDraft): ChatRequest {
  const tags = draft.tags
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean);
  return withExecutionMode({
    messages: [{ role: 'user', content: draft.prompt }],
    routing: { mode: draft.routingMode, tags },
    data_classification: draft.classification || null,
    quality_tier: draft.qualityTier,
    max_output_tokens: draft.maxOutputTokens,
    scenario_id: draft.scenarioId || null,
    baseline_comparison: draft.baselineComparison,
    trace: true,
    test_profile: {
      application_id: draft.applicationId,
      simulated_source_ip: draft.simulatedSourceIp.trim() || null,
    },
    ...(draft.evidence.length > 0 ? { evidence: draft.evidence } : {}),
  }, draft.executionMode);
}
