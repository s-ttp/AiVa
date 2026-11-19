import { describe, expect, it } from 'vitest';
import type { ApprovalRequest } from '../api/client';
import approvalQueue from '../test/fixtures/govern/approvalQueue.json';
import { required } from '../test/required';
import { approvalBadge, awaitingDecision, nextPollDelay, PENDING_LIMIT, type PendingApprovals } from './pendingApprovals';

const base = required(approvalQueue.approvals[0]) as ApprovalRequest;
const request = (id: string, viewerCanDecide: boolean): ApprovalRequest => ({
  ...base,
  approval_id: id,
  viewer_can_decide: viewerCanDecide,
});
const state = (list: readonly ApprovalRequest[] | null, error = false): PendingApprovals => ({
  list,
  fetchedAt: Date.parse('2026-10-14T13:20:00Z'),
  error: error ? ({ message: 'down' } as PendingApprovals['error']) : null,
  loading: false,
  refresh: () => undefined,
});

describe('requests awaiting the viewer’s decision', () => {
  it('counts only requests the viewer can decide', () => {
    expect(awaitingDecision([request('a', true), request('b', false), request('c', true)])).toEqual({
      count: 2,
      capped: false,
    });
  });

  it('marks a full page as a lower bound', () => {
    const page = Array.from({ length: PENDING_LIMIT }, (_, index) => request(`r${index}`, true));
    expect(awaitingDecision(page)).toEqual({ count: PENDING_LIMIT, capped: true });
  });

  it('backs off after failures, up to five minutes', () => {
    expect([0, 1, 2, 3, 4, 10].map(nextPollDelay)).toEqual([30_000, 60_000, 120_000, 240_000, 300_000, 300_000]);
  });
});

describe('Approval Queue badge', () => {
  it('names the count in the link’s accessible name', () => {
    expect(approvalBadge(state([request('a', true), request('b', false)]))).toEqual({
      text: '1',
      stale: false,
      label: 'Approval Queue, 1 awaiting your decision',
    });
  });

  it('is hidden when nothing awaits the viewer', () => {
    expect(approvalBadge(state([request('a', false)]))).toBeNull();
    expect(approvalBadge(state([]))).toBeNull();
  });

  it('is never guessed before a poll succeeds', () => {
    expect(approvalBadge(state(null))).toBeNull();
    expect(approvalBadge(state(null, true))).toBeNull();
    expect(approvalBadge(null)).toBeNull();
  });

  it('shows a full page as “200+”', () => {
    const page = Array.from({ length: PENDING_LIMIT }, (_, index) => request(`r${index}`, true));
    expect(approvalBadge(state(page))).toMatchObject({ text: '200+', label: 'Approval Queue, 200+ awaiting your decision' });
  });

  it('keeps the last count after a failed poll and marks it stale', () => {
    expect(approvalBadge(state([request('a', true)], true))).toEqual({
      text: '1',
      stale: true,
      label: 'Approval Queue, 1 awaiting your decision as of 14 Oct 2026, 13:20:00 UTC; the latest check failed',
    });
  });
});
