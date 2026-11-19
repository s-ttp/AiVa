import { describe, expect, it } from 'vitest';
import type { TraceSpan } from '../api/client';
import chatBlocked from '../test/fixtures/chatBlocked.json';
import runTrace from '../test/fixtures/runTrace.json';
import { STAGES, rootSpan, stageNodes } from './trace';

const spans = runTrace.spans as TraceSpan[];

describe('stageNodes', () => {
  it('maps a sovereign-direct trace onto the 13 stages in order', () => {
    const nodes = stageNodes(spans);

    expect(nodes.map((node) => node.name)).toEqual([...STAGES]);
    expect(nodes.filter((node) => node.status === 'skipped').map((node) => node.name)).toEqual([
      'retrieval',
      'cim',
      'context_guard',
    ]);
    expect(nodes.find((node) => node.name === 'cim')?.reason).toBe('CIM bypassed by mandatory sovereignty policy');
  });

  it('marks where a blocked run ended', () => {
    const nodes = stageNodes(chatBlocked.trace as TraceSpan[]);

    expect(nodes.find((node) => node.name === 'policy')).toMatchObject({
      status: 'halted',
      reason: 'Blocked by AI policy',
    });
    expect(nodes.find((node) => node.name === 'reconciliation')?.status).toBe('executed');
  });

  it('reports stages absent from the trace as missing and errors by their code', () => {
    const withoutCache = spans.filter((span) => span.name !== 'cache');
    const failing = withoutCache.map((span) =>
      span.name === 'inference' ? { ...span, attributes: { ...span.attributes, 'aiva.error.code': 'UPSTREAM_TIMEOUT' } } : span,
    );
    const nodes = stageNodes(failing);

    expect(nodes.find((node) => node.name === 'cache')).toMatchObject({ status: 'missing', span: null });
    expect(nodes.find((node) => node.name === 'inference')).toMatchObject({ status: 'error', reason: 'UPSTREAM_TIMEOUT' });
  });

  it('finds the root span for the total gateway time', () => {
    expect(rootSpan(spans)?.name).toBe('aiva.chat');
  });
});
