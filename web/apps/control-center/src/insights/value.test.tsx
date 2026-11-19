import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { formatCount } from '../format';
import week from '../test/fixtures/insights/valueOverview.json';
import type { PackingSummary, TokenUsage, TrafficFlow } from './types';
import { PassagesKept, TokensByStage, TrafficFlowChart } from './value';

// A week of local synthetic traffic, recorded from GET /v1/reports/overview (see record.sh).
const flow = week.flow as TrafficFlow;
const packing = week.packing as PackingSummary;
const tokens = week.usage.tokens as TokenUsage;

describe('Story mode: where the traffic went', () => {
  it('splits the requests into stopped and routed, then by route, with every count as text', () => {
    render(<TrafficFlowChart flow={flow} />);
    const stopped = flow.stopped.reduce((sum, bucket) => sum + bucket.count, 0);
    const figure = screen.getByRole('figure', { name: 'Where the traffic went' });
    expect(figure).toHaveTextContent(`${formatCount(flow.requests)} requests arrive`);
    expect(figure).toHaveTextContent(`${formatCount(flow.requests - stopped)} routed`);
    const legend = screen.getByRole('list', { name: 'Paths taken' });
    const items = within(legend).getAllByRole('listitem').map((item) => item.textContent);
    // Stopped buckets that never happened are left out; every route is listed, zeros included.
    expect(items).toEqual([
      expect.stringMatching(/^7 stopped by policy\./),
      expect.stringMatching(/^2 held for approval\./),
      expect.stringMatching(/^2 failed before routing\./),
      expect.stringMatching(/^44 answered from cache\./),
      expect.stringMatching(/^13 kept in-country\./),
      expect.stringMatching(/^32 on the local fast path\./),
      expect.stringMatching(/^39 packed by the CIM\./),
      expect.stringMatching(/^6 sent with full context\./),
    ]);
    const table = within(figure).getByRole('table', { name: 'Requests by path' });
    expect(within(table).getAllByRole('row')).toHaveLength(1 + flow.stopped.length + flow.routed.length);
    expect(screen.getByText('Synthetic')).toBeInTheDocument();
  });
});

describe('Story mode: context packing', () => {
  it('reports passages kept of those given, and the tokens the model read', () => {
    render(<PassagesKept packing={packing} />);
    const figure = screen.getByRole('figure', { name: 'Context packing' });
    expect(figure).toHaveTextContent(`${packing.passages_kept} of ${packing.passages_retrieved} passages kept`);
    expect(figure).toHaveTextContent(
      `On the ${packing.packed_runs} requests the CIM packed, the answering model read ${formatCount(packing.model_input_tokens.total)} input tokens.`,
    );
    expect(figure).not.toHaveTextContent('set aside');
    expect(within(figure).getByText('Simulated')).toBeInTheDocument();
  });

  it('says how many passages were set aside, and when nothing was packed', () => {
    const { unmount } = render(<PassagesKept packing={{ ...packing, passages_retrieved: 8, passages_kept: 1 }} />);
    expect(screen.getByText('1 of 8 passages kept')).toBeInTheDocument();
    expect(screen.getByText('7 passages set aside as not needed for the question.')).toBeInTheDocument();
    unmount();
    render(<PassagesKept packing={{ ...packing, packed_runs: 0 }} />);
    expect(screen.getByText('The CIM packed no request in this window.')).toBeInTheDocument();
  });
});

describe('Story mode: where the tokens went', () => {
  it('splits every token in and out by stage, the parts summing to the total', () => {
    const { container } = render(<TokensByStage tokens={tokens} />);
    const total = tokens.input_tokens.total + tokens.output_tokens.total;
    // axe's definition-list and dlitem rules: every group is a <div> of only <dt> and <dd>.
    const groups = Array.from(container.querySelector('dl')?.children ?? []);
    expect(groups.length).toBeGreaterThan(0);
    for (const group of groups) {
      expect(group.tagName).toBe('DIV');
      expect(Array.from(group.children).map((child) => child.tagName)).toEqual(['DT', 'DD']);
    }
    const parts = {
      'Answering models': tokens.upstream_input_tokens + tokens.upstream_output_tokens,
      'Context packer': tokens.cim_input_tokens + tokens.cim_output_tokens,
      Retries: tokens.retry_input_tokens + tokens.retry_output_tokens,
      'Frontier baselines': tokens.baseline_input_tokens + tokens.baseline_output_tokens,
    };
    expect(Object.values(parts).reduce((sum, value) => sum + value, 0)).toBe(total);
    expect(screen.getByRole('figure', { name: 'Where the tokens went' })).toHaveTextContent(
      `${formatCount(total)} tokens in and out, by what they were for`,
    );
    for (const [label, value] of Object.entries(parts)) {
      expect(screen.getByText(`${label} · ${formatCount(value)}`)).toBeInTheDocument();
    }
    expect(screen.queryByText(/^Other ·/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Answer-quality checks ·/)).not.toBeInTheDocument();
    expect(screen.getByText('Comparison answers only, never shown to users.')).toBeInTheDocument();
  });

  it('lists answer-quality checks as their own stage when a presenter ran one', () => {
    render(<TokensByStage tokens={{ ...tokens, quality_comparison_input_tokens: 9000, quality_comparison_output_tokens: 750 }} />);
    expect(screen.getByText(`Answer-quality checks · ${formatCount(9750)}`)).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'Tokens by stage' })).toHaveTextContent('Answer-quality checks');
  });
});
