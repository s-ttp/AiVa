import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ApprovalRequest } from '../api/client';
import approvalQueue from '../test/fixtures/govern/approvalQueue.json';
import { errorEnvelope, hfRateLimited, json, signedInAs, type Route } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';
import { required } from '../test/required';

const pending = required(approvalQueue.approvals[0]) as ApprovalRequest;
const other: ApprovalRequest = { ...pending, approval_id: 'apr_other', requested_at: '2026-10-14T12:00:00Z' };

/** Answers each poll of the pending queue with the next response; the last one repeats. */
function polls(...responses: Array<readonly ApprovalRequest[] | Response>): Route {
  let call = 0;
  return () => {
    const next = required(responses[Math.min(call++, responses.length - 1)]);
    return next instanceof Response ? next.clone() : json({ approvals: next });
  };
}

/** The page regains focus: the shared query refetches. */
async function refocus() {
  await act(async () => {
    window.dispatchEvent(new Event('focus'));
  });
}

const queueLink = () => screen.findByRole('link', { name: /^Approval Queue/ });

describe('Approval Queue badge in the navigation', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('counts requests awaiting the viewer’s decision', async () => {
    signedInAs('u-platform-admin', {
      'GET /v1/admin/approvals': polls([pending, { ...other, viewer_can_decide: false }]),
    });
    renderRoute('/admin/settings');

    await waitFor(async () => expect(await queueLink()).toHaveAccessibleName('Approval Queue, 1 awaiting your decision'));
  });

  it('is hidden when nothing awaits the viewer', async () => {
    const gateway = signedInAs('u-platform-admin', {
      'GET /v1/admin/approvals': polls([{ ...pending, viewer_can_decide: false }]),
    });
    renderRoute('/admin/settings');

    await waitFor(() => expect(gateway.requests.some((r) => r.url.includes('/v1/admin/approvals'))).toBe(true));
    expect(await queueLink()).toHaveAccessibleName('Approval Queue');
  });

  it('is not polled for callers without approvals:read', async () => {
    const gateway = signedInAs('u-tester', { 'GET /v1/catalog/scenarios': [] });
    renderRoute('/console');

    await screen.findByRole('navigation', { name: 'Primary' });
    expect(gateway.requests.some((r) => r.url.includes('/v1/admin/approvals'))).toBe(false);
  });

  it('waits out a Hugging Face rate limit, then polls again once it resets', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const gateway = signedInAs('u-platform-admin', {
      'GET /v1/admin/approvals': polls([pending], hfRateLimited(120), [pending, other]),
    });
    const reads = () => gateway.requests.filter((r) => r.url.includes('/v1/admin/approvals')).length;
    renderRoute('/admin/settings');
    await waitFor(async () => expect(await queueLink()).toHaveAccessibleName('Approval Queue, 1 awaiting your decision'));

    await refocus();
    await waitFor(() => expect(reads()).toBe(2));
    // Neither the 30 s poll nor a regained focus asks while the limit holds.
    await refocus();
    await act(() => vi.advanceTimersByTimeAsync(110_000));
    expect(reads()).toBe(2);

    await act(() => vi.advanceTimersByTimeAsync(11_000));
    await waitFor(async () => expect(await queueLink()).toHaveAccessibleName('Approval Queue, 2 awaiting your decision'));
    expect(reads()).toBe(3);
  });

  it('keeps the last count, marked stale, when a poll fails', async () => {
    signedInAs('u-platform-admin', {
      'GET /v1/admin/approvals': polls([pending], errorEnvelope(503, 'UNAVAILABLE', 'Try again later.')),
    });
    renderRoute('/admin/settings');
    await waitFor(async () => expect(await queueLink()).toHaveAccessibleName('Approval Queue, 1 awaiting your decision'));

    await refocus();
    await waitFor(async () =>
      expect((await queueLink()).getAttribute('aria-label')).toMatch(
        /^Approval Queue, 1 awaiting your decision as of .+ UTC; the latest check failed$/,
      ),
    );
  });

  it('announces a change once, politely', async () => {
    signedInAs('u-platform-admin', { 'GET /v1/admin/approvals': polls([pending], []) });
    renderRoute('/admin/settings');
    await waitFor(async () => expect(await queueLink()).toHaveAccessibleName('Approval Queue, 1 awaiting your decision'));
    const live = required(document.querySelector('[aria-live="polite"].sr-only'));
    expect(live).toBeEmptyDOMElement();

    await refocus();
    await waitFor(() => expect(live).toHaveTextContent('Approval requests awaiting your decision: 0.'));
    expect(await queueLink()).toHaveAccessibleName('Approval Queue');
  });
});

describe('Approval Queue auto-refresh', () => {
  it('updates the pending list from the shared query', async () => {
    signedInAs('u-platform-admin', { 'GET /v1/admin/approvals': polls([pending], [other, pending]) });
    renderRoute('/admin/govern/approval-queue');
    const table = await screen.findByRole('table', { name: 'Pending approval requests' });
    expect(within(table).getAllByRole('row')).toHaveLength(2);

    await refocus();
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(3));
    expect(screen.getByText(/Pending requests refresh every 30 seconds\. Last updated/)).toBeVisible();
  });

  it('holds the rows still while a request is being decided', async () => {
    signedInAs('u-platform-admin', {
      'GET /v1/admin/approvals': polls([pending], [other, pending]),
      'GET /v1/approvals/*': pending,
    });
    renderRoute(`/admin/govern/approval-queue?approval=${pending.approval_id}`);
    const table = await screen.findByRole('table', { name: 'Pending approval requests' });

    await refocus();
    const notice = await screen.findByText(/The queue has changed since you opened this request/);
    expect(within(table).getAllByRole('row')).toHaveLength(2);
    await userEvent.click(within(required(notice.parentElement)).getByRole('button', { name: 'Show the latest list' }));
    expect(within(table).getAllByRole('row')).toHaveLength(3);
  });

  it('says so when the open request is decided elsewhere', async () => {
    let detailCalls = 0;
    signedInAs('u-platform-admin', {
      'GET /v1/admin/approvals': polls([pending], []),
      'GET /v1/approvals/*': () =>
        json(detailCalls++ === 0 ? pending : { ...pending, status: 'approved', viewer_can_decide: false }),
    });
    renderRoute(`/admin/govern/approval-queue?approval=${pending.approval_id}`);
    await screen.findByRole('textbox', { name: 'Decision note' });

    await refocus();
    expect(
      await screen.findByText(
        'This request is no longer pending: it is now Approved. It was decided elsewhere or expired while you had it open.',
      ),
    ).toBeVisible();
    expect(screen.queryByRole('textbox', { name: 'Decision note' })).not.toBeInTheDocument();
  });

  it('keeps showing the list, marked stale, when a refresh fails', async () => {
    signedInAs('u-platform-admin', {
      'GET /v1/admin/approvals': polls([pending], errorEnvelope(503, 'UNAVAILABLE', 'Try again later.')),
    });
    renderRoute('/admin/govern/approval-queue');
    const table = await screen.findByRole('table', { name: 'Pending approval requests' });

    await refocus();
    expect(await screen.findByText(/The queue could not be refreshed; showing the list as of .+ UTC\./)).toBeVisible();
    expect(within(table).getAllByRole('row')).toHaveLength(2);
  });
});
