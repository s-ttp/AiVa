import type { TraceSpan } from '../api/client';

/** The 13 pipeline stages every run emits, in order (ADR-0004, gateway `telemetry/tracing.py`). */
export const STAGES = [
  'ingress',
  'policy',
  'cache',
  'rlcd1',
  'retrieval',
  'cim',
  'context_guard',
  'rlcd2',
  'quota',
  'preflight',
  'inference',
  'assurance',
  'reconciliation',
] as const;

export type StageName = (typeof STAGES)[number];

export type StageStatus = 'executed' | 'skipped' | 'halted' | 'error' | 'missing';

export interface StageNode {
  name: StageName;
  span: TraceSpan | null;
  status: StageStatus;
  /** Why the stage was skipped or where the run stopped, as recorded by the gateway. */
  reason: string | null;
}

export type SpanAttributes = TraceSpan['attributes'];

export function stringAttribute(attributes: SpanAttributes, key: string): string | null {
  const value = attributes[key];
  return typeof value === 'string' ? value : null;
}

export function listAttribute(attributes: SpanAttributes, key: string): string[] {
  const value = attributes[key];
  return Array.isArray(value) ? value.map(String) : [];
}

/**
 * Maps the trace onto the 13 stages. A stage absent from the trace is
 * reported as `missing`, never filled in.
 */
export function stageNodes(spans: readonly TraceSpan[]): StageNode[] {
  return STAGES.map((name) => {
    const span = spans.find((candidate) => candidate.name === name) ?? null;
    if (!span) return { name, span, status: 'missing', reason: null };
    const attrs = span.attributes;
    const errorCode = stringAttribute(attrs, 'aiva.error.code');
    if (errorCode) return { name, span, status: 'error', reason: errorCode };
    const halted = stringAttribute(attrs, 'aiva.stage.halted');
    if (halted) return { name, span, status: 'halted', reason: halted };
    if (span.status === 'skipped') {
      return { name, span, status: 'skipped', reason: stringAttribute(attrs, 'aiva.stage.skip_reason') };
    }
    return { name, span, status: 'executed', reason: null };
  });
}

/** The root span (`aiva.chat`) carries the run's total gateway time. */
export function rootSpan(spans: readonly TraceSpan[]): TraceSpan | null {
  return spans.find((span) => span.parent_span_id === null && !(STAGES as readonly string[]).includes(span.name)) ?? null;
}

export function stageAttribute(spans: readonly TraceSpan[], stage: StageName, key: string): unknown {
  return spans.find((span) => span.name === stage)?.attributes[key];
}
