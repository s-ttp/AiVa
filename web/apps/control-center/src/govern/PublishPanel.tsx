import { Badge, Button, ErrorState, TextAreaField } from '@aiva/ui';
import { History, ShieldAlert, Upload, UserRoundX } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import {
  detailIds,
  unwrap,
  type ApiError,
  type PolicyPublishResponse,
  type PolicyVersionDetail,
} from '../api/client';
import { describeError } from '../api/describeError';
import { useApiAction } from '../api/useApiAction';
import { useSession } from '../auth/session';
import { IdList, Mono, Note } from '../components/inline';
import { t, tRich } from '../i18n';
import { ChangesTable } from './ChangesTable';

/**
 * Publishes a draft or rolls back to an archived version
 * (POST /v1/admin/policies/publish). The gateway refuses a high-impact draft
 * published by its own author (separation of duties); `authored_by_viewer`
 * lets the panel say so before anyone tries, and a refusal is still shown if
 * the gateway returns one.
 */
export function PublishPanel({
  detail,
  onPublished,
}: {
  detail: PolicyVersionDetail;
  onPublished: (result: PolicyPublishResponse) => void;
}) {
  const { can } = useSession();
  const [note, setNote] = useState('');
  const publish = useApiAction((client, approvalNote: string) =>
    unwrap(client.POST('/v1/admin/policies/publish', { body: { version: detail.version, approval_note: approvalNote } })),
  );

  if (detail.status === 'published') {
    return <Note>{t('publish.isPublished', { version: detail.version })}</Note>;
  }
  if (!can('policies:publish')) return <Note>{t('publish.readOnly')}</Note>;

  const rollback = detail.status === 'archived';
  const highImpact = (detail.changes_from_published ?? []).filter((change) => change.high_impact);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const result = await publish.run(note.trim());
    if (result) onPublished(result);
  }

  if (!rollback && highImpact.length > 0 && detail.authored_by_viewer) {
    return (
      <div className="space-y-2 rounded-card border border-warning/40 bg-warning/10 p-4">
        <p className="flex items-center gap-2 font-semibold">
          <UserRoundX className="size-5 shrink-0 text-warning" />
          {t('publish.sod.ownDraft.title')}
        </p>
        <p className="text-secondary">{t('publish.sod.ownDraft.body', { version: detail.version })}</p>
        <p className="text-secondary font-semibold">{t('publish.sod.changes')}</p>
        <IdList ids={highImpact.map((change) => change.target_id)} empty={t('common.none')} />
      </div>
    );
  }

  const pending = publish.state.status === 'pending';
  return (
    <div className="space-y-4">
      <p className="text-secondary text-ink-muted">
        {t(rollback ? 'publish.rollback.body' : 'publish.publish.body', {
          version: detail.version,
          published: detail.published_version ?? '—',
        })}
      </p>
      {highImpact.length > 0 && (
        <div className="space-y-2 rounded-card border border-warning/40 bg-warning/10 p-4">
          <Badge icon={ShieldAlert} tone="warning">
            {t('publish.highImpact.badge')}
          </Badge>
          <p className="text-secondary">
            {t(rollback ? 'publish.highImpact.rollback' : 'publish.highImpact.publish', {
              author: detail.created_by ?? '—',
            })}
          </p>
          <IdList ids={highImpact.map((change) => change.target_id)} empty={t('common.none')} />
        </div>
      )}
      <form onSubmit={onSubmit} className="space-y-4">
        <TextAreaField
          label={t('publish.note')}
          hint={t('publish.note.hint')}
          value={note}
          required
          minLength={3}
          maxLength={2000}
          onChange={(event) => setNote(event.target.value)}
        />
        {publish.state.status === 'error' && <PublishError error={publish.state.error} rollback={rollback} />}
        <Button type="submit" disabled={pending} aria-busy={pending}>
          {rollback ? <History className="size-4" /> : <Upload className="size-4" />}
          {pending
            ? t(rollback ? 'publish.rollback.pending' : 'publish.publish.pending')
            : t(rollback ? 'publish.rollback.submit' : 'publish.publish.submit', { version: detail.version })}
        </Button>
      </form>
    </div>
  );
}

function PublishError({ error, rollback }: { error: ApiError; rollback: boolean }) {
  if (error.code === 'SEPARATION_OF_DUTIES') {
    return (
      <div role="alert" className="space-y-2 rounded-card border border-danger/40 bg-card p-4">
        <p className="flex items-center gap-2 font-semibold">
          <UserRoundX className="size-5 shrink-0 text-danger" />
          {t('publish.sod.title')}
        </p>
        <p className="text-secondary">{error.message}</p>
        <p className="text-secondary font-semibold">{t('publish.sod.changes')}</p>
        <IdList ids={detailIds(error, 'high_impact')} empty={t('common.none')} />
        <p className="text-secondary text-ink-muted">{error.remediation ?? t('publish.sod.remediation')}</p>
      </div>
    );
  }
  return (
    <ErrorState
      title={t(rollback ? 'publish.rollback.error' : 'publish.publish.error')}
      description={
        error.code === 'ALREADY_PUBLISHED' ? `${describeError(error)} ${t('publish.reload')}` : describeError(error)
      }
    />
  );
}

/** The result of a publish or rollback: which bundle is now enforced and what changed. */
export function PublishSummary({ result }: { result: PolicyPublishResponse }) {
  const note = typeof result.details?.approval_note === 'string' ? result.details.approval_note : null;
  return (
    <div className="space-y-3">
      <p className="text-body">
        {t(result.kind === 'rollback' ? 'publish.done.rollback' : 'publish.done.publish', {
          version: result.version,
          previous: result.previous_version ?? '—',
        })}
      </p>
      <p className="text-secondary text-ink-muted wrap-anywhere">
        {tRich('publish.done.bundle', { hash: <Mono>{result.bundle_hash}</Mono> })}
      </p>
      {note && <p className="text-secondary text-ink-muted">{t('publish.done.note', { note })}</p>}
      <ChangesTable
        changes={result.changes ?? []}
        caption={t('publish.done.changes', { previous: result.previous_version ?? '—' })}
        empty={t('changes.none')}
      />
    </div>
  );
}
