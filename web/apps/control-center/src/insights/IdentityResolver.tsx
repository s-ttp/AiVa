import { Button, Card, Ltr, TextAreaField } from '@aiva/ui';
import { ShieldAlert } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { unwrap } from '../api/client';
import { useApiAction } from '../api/useApiAction';
import { describeError } from '../api/describeError';
import { DataTable } from '../components/DataTable';
import { t } from '../i18n';
import type { ResolvedIdentity } from './types';

/**
 * Authorized identity resolution (LLD §21.8). Only for callers holding
 * `reports:identity`; each request states a reason and is written to the
 * audit log by the gateway. Results stay on this panel and never enter
 * reports or exports.
 */
export function IdentityResolver({ pseudonyms }: { pseudonyms: readonly string[] }) {
  const [selected, setSelected] = useState<string[]>([]);
  const [reason, setReason] = useState('');
  const action = useApiAction(async (client, body: { pseudonyms: string[]; reason: string }) =>
    unwrap(client.POST('/v1/reports/identities/resolve', { body })),
  );
  const candidates = pseudonyms.filter((p) => p.startsWith('usr_'));

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void action.run({ pseudonyms: selected, reason: reason.trim() });
  }

  function toggle(pseudonym: string) {
    setSelected((current) => (current.includes(pseudonym) ? current.filter((p) => p !== pseudonym) : [...current, pseudonym]));
  }

  return (
    <Card title={t('insights.identity.title')} description={t('insights.identity.description')}>
      <form onSubmit={onSubmit} className="space-y-4">
        <fieldset>
          <legend className="text-secondary font-semibold text-ink">{t('insights.identity.pick')}</legend>
          {candidates.length === 0 ? (
            <p className="text-secondary text-ink-muted">{t('insights.identity.noCandidates')}</p>
          ) : (
            <ul className="mt-2 flex flex-wrap gap-x-6 gap-y-2">
              {candidates.map((pseudonym) => (
                <li key={pseudonym}>
                  <label className="inline-flex items-center gap-2 text-secondary">
                    <input
                      type="checkbox"
                      className="size-4 accent-brand-navy"
                      checked={selected.includes(pseudonym)}
                      onChange={() => toggle(pseudonym)}
                    />
                    <Ltr className="font-mono">{pseudonym}</Ltr>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </fieldset>
        <TextAreaField
          label={t('insights.identity.reason')}
          hint={t('insights.identity.reason.hint')}
          value={reason}
          minLength={10}
          maxLength={500}
          required
          onChange={(event) => setReason(event.target.value)}
        />
        <Button type="submit" disabled={selected.length === 0 || reason.trim().length < 10 || action.state.status === 'pending'}>
          <ShieldAlert className="size-4" aria-hidden="true" />
          {t('insights.identity.submit')}
        </Button>
      </form>
      <div aria-live="polite" className="mt-4">
        {action.state.status === 'error' && (
          <p role="alert" className="text-secondary text-danger">
            {describeError(action.state.error)}
          </p>
        )}
        {action.state.status === 'success' && <Resolved identities={action.state.data.identities} />}
      </div>
    </Card>
  );
}

function Resolved({ identities }: { identities: readonly ResolvedIdentity[] }) {
  return (
    <div className="space-y-2">
      <p className="text-secondary text-ink">{t('insights.identity.audited')}</p>
      <DataTable
        caption={t('insights.identity.caption')}
        showCaption
        columns={[
          { label: t('insights.col.user') },
          { label: t('insights.identity.kind') },
          { label: t('insights.identity.userId') },
          { label: t('insights.identity.name') },
        ]}
        rows={identities.map((identity) => ({
          key: identity.pseudonym,
          cells: [
            <Ltr key="p" className="font-mono">{identity.pseudonym}</Ltr>,
            t(`insights.identity.kind.${identity.kind}`),
            identity.user_id ? <Ltr key="u" className="font-mono">{identity.user_id}</Ltr> : '—',
            identity.display_name ?? '—',
          ],
        }))}
      />
    </div>
  );
}
