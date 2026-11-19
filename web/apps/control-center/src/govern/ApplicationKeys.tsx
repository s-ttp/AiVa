import { Badge, Button, ErrorState, formatUtc, TextField } from '@aiva/ui';
import { CircleCheck, CircleOff, KeyRound, Timer } from 'lucide-react';
import { useEffect, useId, useRef, useState, type FormEvent, type RefObject } from 'react';
import { unwrap, type ApplicationOut, type CredentialList, type CredentialOut } from '../api/client';
import { describeError } from '../api/describeError';
import { useApiAction } from '../api/useApiAction';
import { useApiQuery } from '../api/useApiQuery';
import { useSession } from '../auth/session';
import { DataTable } from '../components/DataTable';
import { Mono } from '../components/inline';
import { QueryView } from '../components/QueryView';
import { Section } from '../components/Section';
import { t } from '../i18n';
import { KeyDialog, type IssuedKey } from './KeyDialog';
import { UseTheApi } from './UseTheApi';

/** Keys issued from the admin console expire within this many days (`CredentialCreate`). */
const DAYS = { min: 1, max: 90, initial: 30 } as const;

/**
 * An application's keys (ADR-0027): the list for anyone who may read them, issuing and revoking for an
 * administrator. What the caller may do comes from the gateway (`viewer_can_issue`, `viewer_can_revoke`). A new
 * key's secret is shown once in `KeyDialog` and dropped when it closes.
 */
export function ApplicationKeys({
  application,
  headingRef,
  onCount,
}: {
  application: ApplicationOut;
  headingRef: RefObject<HTMLHeadingElement | null>;
  /** The application's active keys, each time the list is read: the registry's row shows the same count. */
  onCount: (active: number) => void;
}) {
  const headingId = useId();
  const { state, reload } = useApiQuery(`admin/applications/${application.id}/credentials`, (client, signal) =>
    unwrap(
      client.GET('/v1/admin/applications/{application_id}/credentials', {
        params: { path: { application_id: application.id } },
        signal,
      }),
    ),
  );
  const [issued, setIssued] = useState<IssuedKey | null>(null);
  const [notice, setNotice] = useState('');
  const changed = (message = '') => {
    setNotice(message);
    reload();
  };
  const active = state?.status === 'success' ? state.data.credentials.filter((key) => key.active).length : null;
  useEffect(() => {
    if (active !== null) onCount(active);
  }, [active, onCount]);

  return (
    <section aria-labelledby={headingId} className="space-y-4">
      <h2 id={headingId} ref={headingRef} tabIndex={-1} className="text-heading font-semibold text-brand-navy">
        {t('keys.title', { application: application.name })}
      </h2>
      <p className="text-secondary text-ink-muted">
        {t('keys.intro')} {t(application.internet_callers ? 'keys.internet' : 'keys.networksOnly')}
        {application.internet_callers && <> {t('keys.internetNoWake')}</>}
      </p>
      <p role="status" className="text-secondary">
        {notice}
      </p>
      <QueryView state={state} onRetry={reload} loadingLabel={t('keys.loading')} errorTitle={t('keys.error')}>
        {(list) => (
          <>
            <KeyTable list={list} application={application} onRevoked={(key) => changed(t('keys.revoked', { label: key.label }))} />
            <IssueForm
              list={list}
              application={application}
              onIssued={(key) => {
                setIssued(key);
                changed();
              }}
            />
          </>
        )}
      </QueryView>
      <UseTheApi application={application} />
      {issued && <KeyDialog issued={issued} onClose={() => setIssued(null)} />}
    </section>
  );
}

function KeyTable({
  list,
  application,
  onRevoked,
}: {
  list: CredentialList;
  application: ApplicationOut;
  onRevoked: (key: CredentialOut) => void;
}) {
  const active = list.credentials.filter((key) => key.active).length;
  const revocable = list.viewer_can_revoke;
  return (
    <div className="space-y-2">
      <p className="text-secondary">{t('keys.count', { active, limit: list.active_limit })}</p>
      {list.credentials.length === 0 ? (
        <p className="text-secondary text-ink-muted">{t('keys.empty')}</p>
      ) : (
        <DataTable
          caption={t('keys.caption')}
          minWidth="44rem"
          columns={[
            { label: t('keys.col.key') },
            { label: t('keys.col.created') },
            { label: t('keys.col.expires') },
            { label: t('keys.col.lastUsed') },
            { label: t('keys.col.status') },
            ...(revocable ? [{ label: t('keys.col.action') }] : []),
          ]}
          rows={list.credentials.map((key) => ({
            key: key.key_id,
            cells: [
              <span key="k" className="flex flex-col">
                <span>{key.label}</span>
                <span className="text-secondary text-ink-muted">
                  <Mono>{key.key_id}</Mono>
                </span>
              </span>,
              <span key="c" className="flex flex-col">
                <span>{formatUtc(key.created_at)}</span>
                <span className="text-secondary text-ink-muted">{t('keys.createdBy', { who: key.created_by })}</span>
              </span>,
              key.expires_at ? formatUtc(key.expires_at) : t('keys.neverExpires'),
              key.last_used_at ? formatUtc(key.last_used_at) : t('keys.neverUsed'),
              <KeyStatus key="s" credential={key} />,
              ...(revocable
                ? [key.active ? <RevokeControl key="r" application={application} credential={key} onRevoked={onRevoked} /> : '']
                : []),
            ],
          }))}
        />
      )}
    </div>
  );
}

function KeyStatus({ credential }: { credential: CredentialOut }) {
  if (credential.revoked_at) {
    return (
      <span className="flex flex-col items-start gap-1">
        <Badge icon={CircleOff} tone="neutral">
          {t('keys.status.revoked')}
        </Badge>
        <span className="text-secondary text-ink-muted">
          {t('keys.revokedBy', { time: formatUtc(credential.revoked_at), who: credential.revoked_by ?? '—' })}
        </span>
      </span>
    );
  }
  return credential.active ? (
    <Badge icon={CircleCheck} tone="success">
      {t('keys.status.active')}
    </Badge>
  ) : (
    <Badge icon={Timer} tone="neutral">
      {t('keys.status.expired')}
    </Badge>
  );
}

/** Revoke, then confirm: the key stops working at once and cannot be restored. */
function RevokeControl({
  application,
  credential,
  onRevoked,
}: {
  application: ApplicationOut;
  credential: CredentialOut;
  onRevoked: (key: CredentialOut) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const group = useRef<HTMLDivElement>(null);
  const action = useApiAction((client) =>
    unwrap(
      client.POST('/v1/admin/applications/{application_id}/credentials/{key_id}/revoke', {
        params: { path: { application_id: application.id, key_id: credential.key_id } },
      }),
    ),
  );
  // The question replaces the Revoke button, so focus moves to its safe answer.
  useEffect(() => {
    if (confirming) group.current?.querySelector<HTMLButtonElement>('[data-cancel]')?.focus();
  }, [confirming]);

  if (!confirming) {
    return (
      <Button
        variant="secondary"
        className="min-h-11"
        aria-label={t('keys.revokeFor', { label: credential.label })}
        onClick={() => setConfirming(true)}
      >
        {t('keys.revoke')}
      </Button>
    );
  }
  const pending = action.state.status === 'pending';
  return (
    <div ref={group} role="group" aria-label={t('keys.revokeFor', { label: credential.label })} className="space-y-2">
      <p className="text-secondary">{t('keys.revoke.confirm', { label: credential.label })}</p>
      <div className="flex flex-wrap gap-2">
        <Button
          className="min-h-11"
          disabled={pending}
          aria-busy={pending}
          onClick={async () => {
            const revoked = await action.run();
            if (revoked) onRevoked(revoked);
          }}
        >
          {pending ? t('keys.revoke.pending') : t('keys.revoke.yes')}
        </Button>
        <Button data-cancel="" variant="secondary" className="min-h-11" disabled={pending} onClick={() => setConfirming(false)}>
          {t('keys.revoke.no')}
        </Button>
      </div>
      {action.state.status === 'error' && <ErrorState title={t('keys.revoke.failed')} description={describeError(action.state.error)} />}
    </div>
  );
}

function IssueForm({
  list,
  application,
  onIssued,
}: {
  list: CredentialList;
  application: ApplicationOut;
  onIssued: (key: IssuedKey) => void;
}) {
  const { can } = useSession();
  const [label, setLabel] = useState('');
  const [days, setDays] = useState(String(DAYS.initial));
  const action = useApiAction((client, body: { label: string; expires_in_days: number }) =>
    unwrap(
      client.POST('/v1/admin/applications/{application_id}/credentials', {
        params: { path: { application_id: application.id } },
        body,
      }),
    ),
  );

  if (!can('credentials:manage')) {
    return <p className="text-secondary text-ink-muted">{t('keys.readOnly')}</p>;
  }
  const active = list.credentials.filter((key) => key.active).length;
  const blocked = !list.viewer_can_issue;

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const result = await action.run({ label: label.trim(), expires_in_days: Number(days) });
    if (!result) return;
    // The secret goes to the dialog alone: the action's own state lets go of it at once.
    action.reset();
    setLabel('');
    onIssued({
      credential: result.credential,
      label: result.metadata.label,
      application: application.name,
      expiresAt: result.metadata.expires_at ?? null,
    });
  }

  const pending = action.state.status === 'pending';
  return (
    <Section title={t('keys.issue.title')}>
      {blocked && (
        <p className="text-secondary text-ink-muted">
          {active >= list.active_limit ? t('keys.atLimit', { limit: list.active_limit }) : t('keys.cannotIssue')}
        </p>
      )}
      <form onSubmit={onSubmit} className="grid gap-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <TextField
          label={t('keys.issue.label')}
          hint={t('keys.issue.labelHint')}
          value={label}
          required
          maxLength={100}
          disabled={blocked}
          onChange={(event) => setLabel(event.target.value)}
        />
        <TextField
          label={t('keys.issue.days')}
          hint={t('keys.issue.daysHint')}
          type="number"
          inputMode="numeric"
          value={days}
          required
          min={DAYS.min}
          max={DAYS.max}
          step={1}
          disabled={blocked}
          onChange={(event) => setDays(event.target.value)}
        />
        <div className="sm:col-span-2">
          <Button type="submit" disabled={blocked || pending} aria-busy={pending}>
            <KeyRound aria-hidden="true" className="size-4" />
            {pending ? t('keys.issue.pending') : t('keys.issue.submit')}
          </Button>
        </div>
      </form>
      {action.state.status === 'error' && <ErrorState title={t('keys.issue.failed')} description={describeError(action.state.error)} />}
    </Section>
  );
}
