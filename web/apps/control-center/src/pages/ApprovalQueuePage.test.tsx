import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import approvalDecision from '../test/fixtures/govern/approvalDecision.json';
import approvalDenied from '../test/fixtures/govern/approvalDenied.json';
import approvalQueue from '../test/fixtures/govern/approvalQueue.json';
import { errorEnvelope, json, signedInAs } from '../test/gateway';
import { renderRoute } from '../test/renderRoute';
import { required } from '../test/required';

const pending = required(approvalQueue.approvals[0]);
const id = pending.approval_id;

function queueRoutes(extra: Record<string, unknown> = {}) {
  return {
    'GET /v1/admin/approvals': approvalQueue,
    'GET /v1/approvals/*': pending,
    ...extra,
  };
}

/** The detail card, once the request has loaded. */
async function detailCard() {
  const heading = await screen.findByRole('heading', { level: 2, name: 'Approval request' });
  const card = within(required(heading.closest('section')));
  await card.findByRole('heading', { level: 3, name: 'Request' });
  return card;
}

describe('approval queue', () => {
  it('lists pending requests with their rules, requester pseudonym and expiry', async () => {
    const gateway = signedInAs('u-platform-admin', queueRoutes());
    renderRoute('/admin/govern/approval-queue');

    const table = await screen.findByRole('table', { name: 'Pending approval requests' });
    const row = within(table).getByRole('row', { name: /Oct 2026/ });
    expect(row).toHaveTextContent('Policy rule');
    expect(row).toHaveTextContent('gct-support-chatbot');
    expect(row).toHaveTextContent('GCT-E2E-APPROVAL');
    expect(row).toHaveTextContent(required(pending.requested_by));
    expect(within(row).getByText('Pending')).toBeVisible();
    const list = gateway.requests.find((r) => new URL(r.url).pathname === '/v1/admin/approvals');
    expect(new URL(required(list).url).searchParams.get('status')).toBe('pending');
    expect(screen.getByText(/Choose “Review” on a request/)).toBeVisible();
  });

  it('filters by status and shows an explicit empty state', async () => {
    const gateway = signedInAs('u-platform-admin', {
      'GET /v1/admin/approvals': (_: Request, url: URL) =>
        json(url.searchParams.get('status') === 'denied' ? { approvals: [] } : approvalQueue),
    });
    const { router } = renderRoute('/admin/govern/approval-queue');

    await screen.findByRole('table', { name: 'Pending approval requests' });
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Show requests' }), 'denied');

    expect(await screen.findByText('No denied requests')).toBeVisible();
    expect(router.state.location.search).toBe('?status=denied');
    expect(gateway.requests.map((r) => new URL(r.url).searchParams.get('status'))).toContain('denied');
  });

  it('shows the gateway’s refusal instead of an empty queue', async () => {
    signedInAs('u-platform-admin', {
      'GET /v1/admin/approvals': errorEnvelope(403, 'PERMISSION_DENIED', 'You do not have permission for this action.'),
    });
    renderRoute('/admin/govern/approval-queue');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Approval requests could not be loaded');
    expect(alert).toHaveTextContent('You do not have permission for this action.');
  });

  it('opens a request with its policy facts and the rules that require approval', async () => {
    signedInAs('u-platform-admin', queueRoutes());
    const { router } = renderRoute('/admin/govern/approval-queue');

    await userEvent.click(await screen.findByRole('link', { name: `Review ${id}` }));
    expect(router.state.location.search).toBe(`?status=pending&approval=${id}`);
    const card = await detailCard();
    expect(card.getByText('Internal')).toBeVisible();
    expect(card.getByRole('link', { name: pending.run_id })).toHaveAttribute(
      'href',
      `/admin/audit?run=${pending.run_id}`,
    );
    expect(card.getByRole('table', { name: 'Rules requiring approval' })).toHaveTextContent(
      'Requests tagged needs-approval need an approver.',
    );
    expect(card.getByText('Not decided yet.')).toBeVisible();
    expect(card.getByText(/re-evaluates the stored request against the current published policy/)).toBeVisible();
  });

  it('approves with a note and shows the re-evaluation against the current policy', async () => {
    const gateway = signedInAs('u-platform-admin', queueRoutes({ 'POST /v1/admin/approvals/*/approve': approvalDecision }));
    renderRoute(`/admin/govern/approval-queue?approval=${id}`);

    const card = await detailCard();
    await userEvent.type(card.getByRole('textbox', { name: 'Decision note' }), 'Reviewed: synthetic billing question.');
    await userEvent.click(card.getByRole('button', { name: 'Approve' }));

    const outcome = await screen.findByRole('heading', { name: 'Decision recorded' });
    expect(outcome).toHaveFocus();
    const section = within(required(outcome.closest('section')));
    expect(section.getByText('Approved', { selector: 'span' })).toBeVisible();
    expect(
      section.getByRole('heading', { name: 'Re-evaluation against current policy (version 3)' }),
    ).toBeVisible();
    expect(section.getByText('Approval required')).toBeVisible();
    expect(section.getByText(approvalDecision.re_evaluation.bundle_hash)).toBeVisible();
    expect(card.getByText('Reviewed: synthetic billing question.', { selector: 'dd bdi' })).toBeVisible();
    expect(card.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();

    const post = required(gateway.requests.find((r) => r.method === 'POST'));
    expect(new URL(post.url).pathname).toBe(`/v1/admin/approvals/${id}/approve`);
    expect(await post.json()).toEqual({ note: 'Reviewed: synthetic billing question.' });
    // The queue reloads after the decision.
    await waitFor(() =>
      expect(gateway.requests.filter((r) => new URL(r.url).pathname === '/v1/admin/approvals')).toHaveLength(2),
    );
  });

  it('denies with a note', async () => {
    const gateway = signedInAs(
      'u-platform-admin',
      queueRoutes({ 'POST /v1/admin/approvals/*/deny': approvalDenied }),
    );
    renderRoute(`/admin/govern/approval-queue?approval=${id}`);

    const card = await detailCard();
    await userEvent.type(card.getByRole('textbox', { name: 'Decision note' }), 'Not needed for the demo.');
    await userEvent.click(card.getByRole('button', { name: 'Deny' }));

    const section = within(required((await screen.findByRole('heading', { name: 'Decision recorded' })).closest('section')));
    expect(section.getByText('Denied', { selector: 'span' })).toBeVisible();
    expect(section.getByText('Denials are recorded without re-evaluating the request.')).toBeVisible();
    const post = required(gateway.requests.find((r) => r.method === 'POST'));
    expect(new URL(post.url).pathname).toBe(`/v1/admin/approvals/${id}/deny`);
  });

  it('reports a request the current policy now blocks as closed by policy', async () => {
    const closed = {
      ...approvalDecision,
      approval: { ...approvalDecision.approval, status: 'denied' },
      outcome: 'closed_by_policy',
      re_evaluation: { ...approvalDecision.re_evaluation, action: 'deny' },
    };
    signedInAs('u-platform-admin', queueRoutes({ 'POST /v1/admin/approvals/*/approve': closed }));
    renderRoute(`/admin/govern/approval-queue?approval=${id}`);

    const card = await detailCard();
    await userEvent.type(card.getByRole('textbox', { name: 'Decision note' }), 'Looks fine.');
    await userEvent.click(card.getByRole('button', { name: 'Approve' }));

    const section = within(required((await screen.findByRole('heading', { name: 'Decision recorded' })).closest('section')));
    expect(section.getByText('Closed by policy')).toBeVisible();
    expect(section.getByText('Blocked by AI policy')).toBeVisible();
  });

  it('still shows the gateway’s refusal when a decision it allowed is refused', async () => {
    signedInAs(
      'u-platform-admin',
      queueRoutes({
        'POST /v1/admin/approvals/*/approve': errorEnvelope(403, 'PERMISSION_DENIED', 'Approvers cannot decide their own requests.'),
      }),
    );
    renderRoute(`/admin/govern/approval-queue?approval=${id}`);

    const card = await detailCard();
    await userEvent.type(card.getByRole('textbox', { name: 'Decision note' }), 'Self-approval attempt.');
    await userEvent.click(card.getByRole('button', { name: 'Approve' }));

    const alert = await card.findByRole('alert');
    expect(alert).toHaveTextContent('The request was not approved');
    expect(alert).toHaveTextContent('Approvers cannot decide their own requests.');
    expect(card.getByRole('button', { name: 'Approve' })).toBeEnabled();
  });

  it('does not offer a decision on the viewer’s own request', async () => {
    signedInAs(
      'u-platform-admin',
      queueRoutes({ 'GET /v1/approvals/*': { ...pending, requested_by_viewer: true, viewer_can_decide: false } }),
    );
    renderRoute(`/admin/govern/approval-queue?approval=${id}`);

    const card = await detailCard();
    expect(card.getByText('You made this request, so you cannot decide it. Another approver must review it.')).toBeVisible();
    expect(card.getByText('You', { exact: false, selector: 'dd span' })).toHaveTextContent(required(pending.requested_by));
    expect(card.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    expect(card.queryByRole('textbox', { name: 'Decision note' })).not.toBeInTheDocument();
  });

  it('marks the viewer’s own requests in the queue', async () => {
    signedInAs('u-platform-admin', {
      'GET /v1/admin/approvals': { approvals: [{ ...pending, requested_by_viewer: true, viewer_can_decide: false }] },
    });
    renderRoute('/admin/govern/approval-queue');

    const table = await screen.findByRole('table', { name: 'Pending approval requests' });
    expect(within(table).getByRole('row', { name: /Oct 2026/ })).toHaveTextContent(`You (${required(pending.requested_by)})`);
  });

  it('explains when an approver’s roles do not cover the request', async () => {
    // The recorded fixture's expiry has passed; an unexpired request isolates the roles reason.
    signedInAs(
      'u-support-owner',
      queueRoutes({
        'GET /v1/approvals/*': { ...pending, viewer_can_decide: false, expires_at: '2099-01-01T00:00:00Z' },
      }),
    );
    renderRoute(`/admin/govern/approval-queue?approval=${id}`);

    const card = await detailCard();
    expect(card.getByText(/^Your roles cannot decide this request\./)).toBeVisible();
    expect(card.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
  });

  it('explains that an expired request can no longer be decided', async () => {
    signedInAs(
      'u-platform-admin',
      queueRoutes({
        'GET /v1/approvals/*': { ...pending, viewer_can_decide: false, expires_at: '2026-01-01T00:00:00Z' },
      }),
    );
    renderRoute(`/admin/govern/approval-queue?approval=${id}`);

    const card = await detailCard();
    expect(card.getByText('This request has expired and can no longer be decided.')).toBeVisible();
    expect(card.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
  });

  it('lets roles without approvals:decide read but not decide', async () => {
    signedInAs('u-auditor', queueRoutes());
    renderRoute(`/admin/govern/approval-queue?approval=${id}`);

    const card = await detailCard();
    expect(card.getByText('Your roles can view approval requests but not decide them.')).toBeVisible();
    expect(card.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
  });

  it('labels a budget approval’s amount as an estimate', async () => {
    signedInAs(
      'u-finance',
      queueRoutes({ 'GET /v1/approvals/*': { ...pending, kind: 'budget', approved_amount_micros: 1_250_000 } }),
    );
    renderRoute(`/admin/govern/approval-queue?approval=${id}`);

    const card = await detailCard();
    const amount = required(card.getByText('$1.25').closest('dd'));
    expect(amount).toHaveTextContent('Estimated');
  });

  it('shows who decided an approved request and the run that used it', async () => {
    signedInAs('u-auditor', queueRoutes({ 'GET /v1/approvals/*': { ...approvalDecision.approval, status: 'consumed', consumed_by_run_id: 'run_resumed' } }));
    renderRoute(`/admin/govern/approval-queue?approval=${id}`);

    const card = await detailCard();
    expect(card.getByText(required(approvalDecision.approval.decided_by))).toBeVisible();
    expect(card.getByRole('link', { name: 'run_resumed' })).toHaveAttribute('href', '/admin/audit?run=run_resumed');
    expect(card.queryByRole('textbox', { name: 'Decision note' })).not.toBeInTheDocument();
  });
});
