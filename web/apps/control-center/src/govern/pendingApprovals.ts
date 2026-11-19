import { createContext, useContext } from 'react';
import type { ApiError, ApprovalRequest } from '../api/client';
import { formatCount, formatUtc } from '../format';
import { t } from '../i18n';

/** Page size of the shared pending query; a full page means there may be more. */
export const PENDING_LIMIT = 200;
/** Poll interval while the page is visible and the last poll succeeded. */
export const POLL_INTERVAL_MS = 30_000;
/** Longest wait between polls after repeated failures. */
export const MAX_BACKOFF_MS = 300_000;

export interface PendingApprovals {
  /** Pending requests from the last successful poll; null until one succeeds. */
  list: readonly ApprovalRequest[] | null;
  /** When `list` was fetched (epoch ms). */
  fetchedAt: number | null;
  /** Set when the latest poll failed: `list` is then from an earlier poll (stale). */
  error: ApiError | null;
  /** The first poll is in flight. */
  loading: boolean;
  /** Polls now, e.g. after a decision. */
  refresh: () => void;
}

export const PendingApprovalsContext = createContext<PendingApprovals | null>(null);

/** The shared pending-approvals query, or null when the caller cannot read approvals. */
export function usePendingApprovals(): PendingApprovals | null {
  return useContext(PendingApprovalsContext);
}

/**
 * Requests awaiting the viewer's decision. Only `viewer_can_decide` rows
 * count: a requester's own request or one outside their roles is not action
 * for them. `capped` means the page was full, so the count is a lower bound.
 */
export function awaitingDecision(list: readonly ApprovalRequest[]): { count: number; capped: boolean } {
  return {
    count: list.filter((approval) => approval.viewer_can_decide).length,
    capped: list.length >= PENDING_LIMIT,
  };
}

/** Delay before the next poll: the interval, doubled per consecutive failure up to the maximum. */
export function nextPollDelay(failures: number): number {
  return failures === 0 ? POLL_INTERVAL_MS : Math.min(POLL_INTERVAL_MS * 2 ** failures, MAX_BACKOFF_MS);
}

export interface ApprovalBadge {
  /** Visible text, e.g. "3" or "200+". */
  text: string;
  /** The count comes from an earlier poll because the latest one failed. */
  stale: boolean;
  /** Accessible name of the Approval Queue link with the badge. */
  label: string;
}

/**
 * The navigation badge for the Approval Queue, or null when nothing awaits the
 * viewer's decision or no poll has succeeded yet: a count is never guessed,
 * and 0 is shown as no badge.
 */
export function approvalBadge(pending: PendingApprovals | null): ApprovalBadge | null {
  if (!pending?.list) return null;
  const { count, capped } = awaitingDecision(pending.list);
  if (count === 0) return null;
  const text = capped ? t('approvals.badge.countCapped', { count: formatCount(count) }) : formatCount(count);
  const view = t('view.approval-queue.title');
  const stale = pending.error !== null;
  const label = stale
    ? t('approvals.badge.labelStale', { view, count: text, time: formatUtc(new Date(pending.fetchedAt ?? 0).toISOString()) })
    : t('approvals.badge.label', { view, count: text });
  return { text, stale, label };
}
