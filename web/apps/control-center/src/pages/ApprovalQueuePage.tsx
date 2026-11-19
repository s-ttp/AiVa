import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  LoadingState,
  Ltr,
  ProvenanceBadge,
  SelectField,
  TextAreaField,
  type BadgeTone,
} from '@aiva/ui';
import {
  CircleCheck,
  CircleSlash,
  CircleX,
  Clock,
  Hourglass,
  PackageCheck,
  ShieldX,
  type LucideIcon,
} from 'lucide-react';
import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router';
import { unwrap, type ApprovalDecisionResponse, type ApprovalRequest } from '../api/client';
import { describeError } from '../api/describeError';
import { useApiAction } from '../api/useApiAction';
import { useApiQuery } from '../api/useApiQuery';
import { useSession } from '../auth/session';
import { DataTable } from '../components/DataTable';
import { FactList, type Fact } from '../components/FactList';
import { IdList, Mono, Note } from '../components/inline';
import { QueryView } from '../components/QueryView';
import { Section } from '../components/Section';
import { formatMicrosUsd, formatUtc } from '../format';
import { usePendingApprovals, type PendingApprovals } from '../govern/pendingApprovals';
import { PolicyDecisionView } from '../govern/PolicyDecisionView';
import { t, tRich } from '../i18n';
import { classificationLabel, identityModeLabel, isInternetCaller, sourceModeLabel } from '../labels';
import { Page } from '../layout/Page';

const STATUSES = ['pending', 'approved', 'denied', 'expired', 'consumed'] as const;
type ApprovalStatus = (typeof STATUSES)[number];

const STATUS_BADGES: Record<ApprovalStatus, { icon: LucideIcon; tone: BadgeTone }> = {
  pending: { icon: Hourglass, tone: 'warning' },
  approved: { icon: CircleCheck, tone: 'success' },
  denied: { icon: CircleX, tone: 'danger' },
  expired: { icon: Clock, tone: 'neutral' },
  consumed: { icon: PackageCheck, tone: 'neutral' },
};

function isStatus(value: string | null): value is ApprovalStatus {
  return (STATUSES as readonly (string | null)[]).includes(value);
}

function ApprovalStatusBadge({ status }: { status: ApprovalStatus }) {
  const { icon, tone } = STATUS_BADGES[status];
  return (
    <Badge icon={icon} tone={tone}>
      {t(`approvals.status.${status}`)}
    </Badge>
  );
}

/**
 * AiVa Govern › Approval Queue (LLD §22.5, ADR-0012). Lists the approval
 * requests the gateway lets the caller see; approvers decide them here.
 * Approving re-evaluates the stored request against the current policy, so a
 * request the current policy denies is closed instead of approved.
 */
export function ApprovalQueuePage() {
  const [params, setParams] = useSearchParams();
  const requested = params.get('status');
  const status: ApprovalStatus = isStatus(requested) ? requested : 'pending';
  const approvalId = params.get('approval')?.trim() || null;
  const pending = usePendingApprovals();
  // Pending requests come from the shared, auto-refreshing query (also behind the
  // navigation badge); other statuses are loaded on demand.
  const other = useApiQuery(status === 'pending' ? null : `admin/approvals?status=${status}`, (client, signal) =>
    unwrap(client.GET('/v1/admin/approvals', { params: { query: { status } }, signal })),
  );
  const [decided, setDecided] = useState<ReadonlySet<string>>(new Set());
  const livePending = pending?.list && !pending.error ? pending.list.some((a) => a.approval_id === approvalId) : null;

  function update(next: { status?: ApprovalStatus; approval?: string | null }) {
    const merged = new URLSearchParams(params);
    if (next.status) merged.set('status', next.status);
    if (next.approval === null) merged.delete('approval');
    else if (next.approval) merged.set('approval', next.approval);
    setParams(merged);
  }

  return (
    <Page
      title={t('view.approval-queue.title')}
      eyebrow={t('area.govern')}
      description={t('view.approval-queue.description')}
    >
      <Card title={t('approvals.list.title')} description={t('approvals.list.description')}>
        <div className="space-y-4">
          <SelectField
            className="max-w-xs"
            label={t('approvals.filter.label')}
            value={status}
            onChange={(event) => update({ status: event.target.value as ApprovalStatus })}
          >
            {STATUSES.map((value) => (
              <option key={value} value={value}>
                {t(`approvals.status.${value}`)}
              </option>
            ))}
          </SelectField>
          {status === 'pending' && pending ? (
            <PendingList
              pending={pending}
              selected={approvalId}
              // Hold the rows still while the viewer is deciding the open request.
              holding={approvalId !== null && !decided.has(approvalId)}
            />
          ) : (
            <QueryView
              state={other.state}
              onRetry={other.reload}
              loadingLabel={t('approvals.loading')}
              errorTitle={t('approvals.error')}
              empty={{ title: t(`approvals.empty.${status}.title`), description: t('approvals.empty.body') }}
              isEmpty={(data) => data.approvals.length === 0}
            >
              {(data) => <ApprovalTable approvals={data.approvals} status={status} selected={approvalId} />}
            </QueryView>
          )}
        </div>
      </Card>
      {approvalId === null ? (
        <Card>
          <p className="text-body text-ink-muted">{t('approvals.detail.idle')}</p>
        </Card>
      ) : (
        <ApprovalDetail
          key={approvalId}
          approvalId={approvalId}
          livePending={livePending}
          onDecided={() => {
            setDecided((previous) => new Set(previous).add(approvalId));
            pending?.refresh();
            other.reload();
          }}
        />
      )}
    </Page>
  );
}

function sameRequests(a: readonly ApprovalRequest[], b: readonly ApprovalRequest[]): boolean {
  return a.length === b.length && a.every((approval, index) => approval.approval_id === b[index]?.approval_id);
}

/**
 * The pending list from the shared query. While `holding`, rows stay as they
 * were when the request was opened and a notice offers the latest list, so
 * rows never move under a viewer mid-decision.
 */
function PendingList({
  pending,
  selected,
  holding,
}: {
  pending: PendingApprovals;
  selected: string | null;
  holding: boolean;
}) {
  const [held, setHeld] = useState<readonly ApprovalRequest[] | null>(null);
  const live = pending.list;
  if (holding && held === null && live !== null) setHeld(live);
  if (!holding && held !== null) setHeld(null);
  const shown = holding && held !== null ? held : live;
  const changed = holding && held !== null && live !== null && !sameRequests(held, live);

  if (shown === null) {
    return pending.error ? (
      <ErrorState
        title={t('approvals.error')}
        description={describeError(pending.error)}
        onRetry={pending.refresh}
      />
    ) : (
      <LoadingState label={t('approvals.loading')} />
    );
  }
  return (
    <div className="space-y-4">
      {pending.error && (
        <div role="status" className="space-y-2 rounded-card border border-warning/40 bg-warning/10 p-4">
          <p className="text-secondary">
            {t('approvals.live.stale', { time: formatUtc(new Date(pending.fetchedAt ?? 0).toISOString()) })}{' '}
            {describeError(pending.error)}
          </p>
          <Button variant="secondary" onClick={pending.refresh}>
            {t('approvals.live.retry')}
          </Button>
        </div>
      )}
      {changed && (
        <div role="status" className="flex flex-wrap items-center gap-3 rounded-card border border-info/40 bg-info/10 p-4">
          <p className="text-secondary">{t('approvals.live.changed')}</p>
          <Button variant="secondary" onClick={() => setHeld(live)}>
            {t('approvals.live.showLatest')}
          </Button>
        </div>
      )}
      {shown.length === 0 ? (
        <EmptyState headingLevel={3} title={t('approvals.empty.pending.title')} description={t('approvals.empty.body')} />
      ) : (
        <ApprovalTable approvals={shown} status="pending" selected={selected} />
      )}
      {!pending.error && pending.fetchedAt !== null && (
        <p className="text-secondary text-ink-muted">
          {t('approvals.live.updated', { time: formatUtc(new Date(pending.fetchedAt).toISOString()) })}
        </p>
      )}
    </div>
  );
}

function ApprovalTable({
  approvals,
  status,
  selected,
}: {
  approvals: readonly ApprovalRequest[];
  status: ApprovalStatus;
  selected: string | null;
}) {
  return (
    <DataTable
      caption={t('approvals.caption', { status: t(`approvals.status.${status}`) })}
      minWidth="60rem"
      columns={[
        { label: t('approvals.col.requested') },
        { label: t('approvals.col.kind') },
        { label: t('approvals.col.application') },
        { label: t('approvals.col.rules') },
        { label: t('approvals.col.requester') },
        { label: t('approvals.col.expires') },
        { label: t('approvals.col.status') },
        { label: t('approvals.col.open') },
      ]}
      rows={approvals.map((approval) => ({
        key: approval.approval_id,
        cells: [
          <span key="r" className="whitespace-nowrap">
            {formatUtc(approval.requested_at)}
          </span>,
          t(`approvals.kind.${approval.kind}`),
          <Mono key="a">{approval.application_id}</Mono>,
          <IdList key="rules" ids={approval.rule_ids} empty={t('common.none')} />,
          <Requester key="u" approval={approval} />,
          <span key="e" className="whitespace-nowrap">
            {formatUtc(approval.expires_at)}
          </span>,
          <ApprovalStatusBadge key="s" status={approval.status} />,
          <Link
            key="o"
            to={{ search: `?status=${status}&approval=${encodeURIComponent(approval.approval_id)}` }}
            aria-current={selected === approval.approval_id ? 'true' : undefined}
            className="font-semibold text-brand-navy underline underline-offset-2"
          >
            {t('approvals.open', { id: approval.approval_id })}
          </Link>,
        ],
      }))}
    />
  );
}

function ApprovalDetail({
  approvalId,
  livePending,
  onDecided,
}: {
  approvalId: string;
  /** Whether the shared pending list still contains this request; null when unknown. */
  livePending: boolean | null;
  onDecided: () => void;
}) {
  const { state, reload } = useApiQuery(`approvals/${approvalId}`, (client, signal) =>
    unwrap(client.GET('/v1/approvals/{approval_id}', { params: { path: { approval_id: approvalId } }, signal })),
  );
  const [decision, setDecision] = useState<ApprovalDecisionResponse | null>(null);
  // A pending request that leaves the pending list was decided elsewhere or expired.
  const [closedElsewhere, setClosedElsewhere] = useState(false);
  const loaded = state?.status === 'success' ? state.data : null;
  if (!closedElsewhere && !decision && loaded?.status === 'pending' && livePending === false) setClosedElsewhere(true);
  useEffect(() => {
    if (closedElsewhere) reload();
  }, [closedElsewhere, reload]);

  return (
    <Card title={t('approvals.detail.title')} description={<Mono>{approvalId}</Mono>}>
      <QueryView
        state={state}
        onRetry={reload}
        loadingLabel={t('approvals.detail.loading')}
        errorTitle={t('approvals.detail.error')}
      >
        {(approval) => {
          const current = decision?.approval.approval_id === approval.approval_id ? decision.approval : approval;
          return (
            <div className="space-y-8">
              {closedElsewhere && current.status !== 'pending' && (
                <p role="status" className="rounded-card border border-info/40 bg-info/10 p-4 text-secondary">
                  {t('approvals.detail.closedElsewhere', { status: t(`approvals.status.${current.status}`) })}
                </p>
              )}
              <ApprovalFacts approval={current} />
              {decision ? (
                <DecisionOutcome result={decision} />
              ) : (
                current.status === 'pending' && (
                  <DecideForm
                    approval={current}
                    onDecided={(result) => {
                      setDecision(result);
                      onDecided();
                    }}
                  />
                )
              )}
            </div>
          );
        }}
      </QueryView>
    </Card>
  );
}

function ApprovalFacts({ approval }: { approval: ApprovalRequest }) {
  const request: Fact[] = [
    { label: t('approvals.fact.status'), value: <ApprovalStatusBadge status={approval.status} /> },
    { label: t('approvals.fact.kind'), value: t(`approvals.kind.${approval.kind}`) },
    {
      label: t('approvals.fact.run'),
      value: (
        <Link
          to={`/admin/audit?run=${encodeURIComponent(approval.run_id)}`}
          className="font-semibold text-brand-navy underline underline-offset-2"
        >
          <Mono>{approval.run_id}</Mono>
        </Link>
      ),
    },
    { label: t('approvals.fact.application'), value: <Mono>{approval.application_id}</Mono> },
    ...(approval.tool_id ? [{ label: t('approvals.fact.tool'), value: <Mono>{approval.tool_id}</Mono> }] : []),
    {
      label: t('approvals.fact.classification'),
      value: approval.effective_classification ? classificationLabel(approval.effective_classification) : '—',
    },
    { label: t('approvals.fact.task'), value: approval.task ?? '—' },
    { label: t('approvals.fact.scenario'), value: approval.scenario_id ? <Mono>{approval.scenario_id}</Mono> : '—' },
    {
      label: t('approvals.fact.ingress'),
      value: t('approvals.fact.ingressValue', {
        identity: approval.identity_mode ? identityModeLabel(approval.identity_mode) : '—',
        source: approval.source_mode ? sourceModeLabel(approval.source_mode) : '—',
        network: isInternetCaller(approval.matched_network_id)
          ? t('run.network.internet')
          : (approval.matched_network_id ?? t('run.fact.noNetwork')),
      }),
    },
    {
      label: t('approvals.fact.requester'),
      value: <Requester approval={approval} />,
    },
    { label: t('approvals.fact.requestedAt'), value: formatUtc(approval.requested_at) },
    { label: t('approvals.fact.expiresAt'), value: formatUtc(approval.expires_at) },
    { label: t('approvals.fact.versionAtRequest'), value: approval.policy_version_at_request },
    {
      label: t('approvals.fact.carried'),
      value: <IdList ids={(approval.carried_grants ?? []).map(grantLabel)} empty={t('common.none')} />,
    },
  ];
  if (approval.approved_amount_micros != null) {
    request.push({
      label: t('approvals.fact.amount'),
      value: (
        <span className="inline-flex flex-wrap items-center gap-2">
          <Ltr className="tabular-nums">{formatMicrosUsd(approval.approved_amount_micros)}</Ltr>
          <ProvenanceBadge provenance="estimated" />
        </span>
      ),
    });
  }
  const decided = approval.decided_at != null;
  return (
    <div className="space-y-8">
      <Section title={t('approvals.section.request')}>
        <FactList facts={request} />
      </Section>
      <Section title={t('approvals.section.reasons')}>
        <DataTable
          caption={t('approvals.reasons.caption')}
          columns={[
            { label: t('routing.reasons.rule') },
            { label: t('routing.reasons.message') },
            { label: t('routing.reasons.remediation') },
          ]}
          rows={approval.reasons.map((reason, index) => ({
            key: `${reason.rule_id}-${index}`,
            cells: [<Mono key="r">{reason.rule_id}</Mono>, reason.message, reason.remediation ?? '—'],
          }))}
          empty={t('routing.reasons.empty')}
        />
      </Section>
      <Section title={t('approvals.section.decision')}>
        {decided ? (
          <FactList
            facts={[
              {
                label: t('approvals.fact.decidedBy'),
                value: approval.decided_by ? <Mono>{approval.decided_by}</Mono> : '—',
              },
              { label: t('approvals.fact.decidedAt'), value: formatUtc(approval.decided_at ?? '') },
              { label: t('approvals.fact.note'), value: approval.decision_note ?? '—' },
              { label: t('approvals.fact.versionAtDecision'), value: approval.policy_version_at_decision ?? '—' },
              {
                label: t('approvals.fact.grants'),
                value: <IdList ids={(approval.approved_grants ?? []).map(grantLabel)} empty={t('common.none')} />,
              },
              {
                label: t('approvals.fact.consumedBy'),
                value: approval.consumed_by_run_id ? (
                  <Link
                    to={`/admin/audit?run=${encodeURIComponent(approval.consumed_by_run_id)}`}
                    className="font-semibold text-brand-navy underline underline-offset-2"
                  >
                    <Mono>{approval.consumed_by_run_id}</Mono>
                  </Link>
                ) : (
                  t('approvals.notConsumed')
                ),
              },
            ]}
          />
        ) : (
          <Note>{t(approval.status === 'expired' ? 'approvals.expiredUndecided' : 'approvals.undecided')}</Note>
        )}
      </Section>
    </div>
  );
}

function Requester({ approval }: { approval: ApprovalRequest }) {
  if (!approval.requested_by) return <>{t('approvals.requester.application')}</>;
  if (!approval.requested_by_viewer) return <Mono>{approval.requested_by}</Mono>;
  return <span>{tRich('approvals.requester.you', { pseudonym: <Mono>{approval.requested_by}</Mono> })}</span>;
}

function grantLabel(grant: { rule_id: string; tool_id?: string | null }): string {
  return grant.tool_id ? t('approvals.grant.withTool', { rule: grant.rule_id, tool: grant.tool_id }) : grant.rule_id;
}

function DecideForm({
  approval,
  onDecided,
}: {
  approval: ApprovalRequest;
  onDecided: (result: ApprovalDecisionResponse) => void;
}) {
  const { can } = useSession();
  const headingId = useId();
  const [note, setNote] = useState('');
  const [choice, setChoice] = useState<'approve' | 'deny' | null>(null);
  // Set by the button that submitted the form, read by onSubmit.
  const verdict = useRef<'approve' | 'deny'>('approve');
  const action = useApiAction((client, verdict: 'approve' | 'deny', text: string) => {
    const init = { params: { path: { approval_id: approval.approval_id } }, body: { note: text } };
    return unwrap(
      verdict === 'approve'
        ? client.POST('/v1/admin/approvals/{approval_id}/approve', init)
        : client.POST('/v1/admin/approvals/{approval_id}/deny', init),
    );
  });

  if (!can('approvals:decide') || !approval.viewer_can_decide) {
    return (
      <Section title={t('approvals.decide.title')}>
        <Note>{t(whyNotDecidable(approval, can('approvals:decide')))}</Note>
      </Section>
    );
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setChoice(verdict.current);
    const result = await action.run(verdict.current, note.trim());
    if (result) onDecided(result);
  }

  const pending = action.state.status === 'pending';
  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <h3 id={headingId} className="text-body font-semibold text-brand-navy">
        {t('approvals.decide.title')}
      </h3>
      <p className="text-secondary text-ink-muted">{t('approvals.decide.reEvaluation')}</p>
      <form onSubmit={onSubmit} className="space-y-4">
        <TextAreaField
          label={t('approvals.decide.note')}
          hint={t('approvals.decide.note.hint')}
          value={note}
          required
          minLength={3}
          maxLength={2000}
          onChange={(event) => setNote(event.target.value)}
        />
        {action.state.status === 'error' && (
          <ErrorState
            title={t(choice === 'deny' ? 'approvals.decide.denyFailed' : 'approvals.decide.approveFailed')}
            description={describeDecideError(action.state.error)}
          />
        )}
        <div className="flex flex-wrap gap-3">
          <Button
            type="submit"
            disabled={pending}
            aria-busy={pending && choice === 'approve'}
            onClick={() => (verdict.current = 'approve')}
          >
            <CircleCheck className="size-4" />
            {pending && choice === 'approve' ? t('approvals.decide.approving') : t('approvals.decide.approve')}
          </Button>
          <Button
            type="submit"
            variant="secondary"
            disabled={pending}
            aria-busy={pending && choice === 'deny'}
            onClick={() => (verdict.current = 'deny')}
          >
            <CircleSlash className="size-4" />
            {pending && choice === 'deny' ? t('approvals.decide.denying') : t('approvals.decide.deny')}
          </Button>
        </div>
      </form>
    </section>
  );
}

/**
 * Why the caller cannot decide a pending request. The gateway computes
 * `viewer_can_decide` with the same checks it enforces on approve and deny.
 */
function whyNotDecidable(approval: ApprovalRequest, mayDecide: boolean) {
  if (!mayDecide) return 'approvals.decide.readOnly' as const;
  if (approval.requested_by_viewer) return 'approvals.decide.ownRequest' as const;
  if (Date.parse(approval.expires_at) <= Date.now()) return 'approvals.decide.expired' as const;
  return 'approvals.decide.notYours' as const;
}

function describeDecideError(error: Parameters<typeof describeError>[0]): string {
  if (error.code === 'APPROVAL_ALREADY_DECIDED') return `${describeError(error)} ${t('approvals.decide.reload')}`;
  return describeError(error);
}

function DecisionOutcome({ result }: { result: ApprovalDecisionResponse }) {
  const headingId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  // Move focus to the outcome so keyboard and screen-reader users land on it.
  useEffect(() => headingRef.current?.focus(), []);
  const outcome = {
    approved: { icon: CircleCheck, tone: 'success' },
    denied: { icon: CircleX, tone: 'danger' },
    closed_by_policy: { icon: ShieldX, tone: 'danger' },
  } as const satisfies Record<ApprovalDecisionResponse['outcome'], { icon: LucideIcon; tone: BadgeTone }>;
  const { icon, tone } = outcome[result.outcome];
  return (
    <section aria-labelledby={headingId} className="space-y-4">
      <h3 id={headingId} ref={headingRef} tabIndex={-1} className="text-body font-semibold text-brand-navy outline-none">
        {t('approvals.outcome.title')}
      </h3>
      <Badge icon={icon} tone={tone}>
        {t(`approvals.outcome.${result.outcome}`)}
      </Badge>
      <p className="text-secondary">{t(`approvals.outcome.${result.outcome}.body`)}</p>
      {result.re_evaluation ? (
        <Section
          title={t('approvals.reEvaluation.title', { version: result.re_evaluation.policy_version || '—' })}
          description={t('approvals.reEvaluation.description')}
        >
          <PolicyDecisionView decision={result.re_evaluation} caption={t('approvals.reEvaluation.context')} />
        </Section>
      ) : (
        <Note>{t('approvals.reEvaluation.none')}</Note>
      )}
    </section>
  );
}
