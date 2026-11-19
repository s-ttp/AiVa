import { Badge, Button, buttonClasses, cx, ErrorState, mirrorInRtl } from '@aiva/ui';
import { CircleCheck, CircleX, Clock, Hourglass, PackageCheck, RefreshCw, Send, type LucideIcon } from 'lucide-react';
import { Link } from 'react-router';
import { unwrap, type ApprovalRequest, type ChatResponse } from '../api/client';
import { describeError } from '../api/describeError';
import { useApiAction } from '../api/useApiAction';
import { useSession } from '../auth/session';
import { FactList } from '../components/FactList';
import { IdList, Mono } from '../components/inline';
import { Section } from '../components/Section';
import { formatUtc } from '../format';
import { t } from '../i18n';

const STATUS_ICONS: Record<ApprovalRequest['status'], LucideIcon> = {
  pending: Hourglass,
  approved: CircleCheck,
  denied: CircleX,
  expired: Clock,
  consumed: PackageCheck,
};

/**
 * Shown when a run paused for approval: the approval request, its current
 * status (GET /v1/approvals/{id}) and, once approved, resubmission of the
 * identical request with the approval ID. Policy is evaluated again on resume;
 * an approval never overrides a deny.
 */
export function ApprovalPanel({ run, onResubmit }: { run: ChatResponse; onResubmit: (approvalId: string) => void }) {
  const { can } = useSession();
  const approvalId = run.approval?.approval_id ?? null;
  const check = useApiAction((client, id: string) =>
    unwrap(client.GET('/v1/approvals/{approval_id}', { params: { path: { approval_id: id } } })),
  );
  if (approvalId === null) return null;
  const current = check.state.status === 'success' ? check.state.data : null;
  return (
    <Section title={t('runApproval.title')} description={t('runApproval.description')}>
      <FactList
        facts={[
          { label: t('runApproval.id'), value: <Mono>{approvalId}</Mono> },
          { label: t('runApproval.rules'), value: <IdList ids={run.approval?.rule_ids ?? []} empty={t('common.none')} /> },
          {
            label: t('runApproval.expires'),
            value: run.approval?.expires_at ? formatUtc(run.approval.expires_at) : '—',
          },
          {
            label: t('runApproval.status'),
            value: current ? (
              <span className="flex flex-col items-start gap-1">
                <Badge icon={STATUS_ICONS[current.status]} tone={current.status === 'approved' ? 'success' : 'neutral'}>
                  {t(`approvals.status.${current.status}`)}
                </Badge>
                {current.decision_note && (
                  <span className="text-secondary">{t('runApproval.note', { note: current.decision_note })}</span>
                )}
              </span>
            ) : (
              t('runApproval.status.unchecked')
            ),
          },
        ]}
      />
      {check.state.status === 'error' && (
        <ErrorState title={t('runApproval.checkFailed')} description={describeError(check.state.error)} />
      )}
      {current && current.status !== 'pending' && current.status !== 'approved' && (
        <p role="status" className="text-secondary">
          {t(`runApproval.closed.${current.status}`)}
        </p>
      )}
      <div className="flex flex-wrap gap-3">
        <Button
          variant="secondary"
          onClick={() => void check.run(approvalId)}
          disabled={check.state.status === 'pending'}
          aria-busy={check.state.status === 'pending'}
        >
          <RefreshCw className="size-4" />
          {t('runApproval.check')}
        </Button>
        {current?.status === 'approved' && (
          <Button onClick={() => onResubmit(approvalId)}>
            <Send className={cx('size-4', mirrorInRtl)} />
            {t('runApproval.resubmit')}
          </Button>
        )}
        {can('approvals:read') && (
          <Link
            to={`/admin/govern/approval-queue?approval=${encodeURIComponent(approvalId)}`}
            className={buttonClasses('secondary')}
          >
            {t('runApproval.openQueue')}
          </Link>
        )}
      </div>
    </Section>
  );
}
