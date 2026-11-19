import { Button, TextAreaField, TextField } from '@aiva/ui';
import { CircleAlert, FilePlus2 } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import {
  unwrap,
  validationMessages,
  type PolicyDocument,
  type PolicyVersionDetail,
} from '../api/client';
import { describeError } from '../api/describeError';
import { useApiAction } from '../api/useApiAction';
import { BulletList } from '../components/inline';
import { t } from '../i18n';

type Parsed = { ok: true; policies: PolicyDocument[] } | { ok: false; message: string };

/**
 * Structural check only: the gateway validates the documents and their
 * references to applications, models, networks and tools.
 */
function parsePolicies(text: string): Parsed {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (cause) {
    return { ok: false, message: t('draft.invalidJson', { detail: cause instanceof Error ? cause.message : '' }) };
  }
  if (!Array.isArray(value)) return { ok: false, message: t('draft.notArray') };
  return { ok: true, policies: value as PolicyDocument[] };
}

/**
 * Creates a draft from a copy of an existing version (POST /v1/admin/policies).
 * A draft is validated and stored, but not enforced until it is published.
 */
export function DraftEditor({
  base,
  onCreated,
}: {
  base: PolicyVersionDetail;
  onCreated: (draft: PolicyVersionDetail) => void;
}) {
  const [text, setText] = useState(() => JSON.stringify(base.policies, null, 2));
  const [notes, setNotes] = useState('');
  const [parseError, setParseError] = useState<string | null>(null);
  const save = useApiAction((client, policies: PolicyDocument[], draftNotes: string) =>
    unwrap(client.POST('/v1/admin/policies', { body: { policies, notes: draftNotes || null } })),
  );

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = parsePolicies(text);
    if (!parsed.ok) {
      setParseError(parsed.message);
      save.reset();
      return;
    }
    setParseError(null);
    const draft = await save.run(parsed.policies, notes.trim());
    if (draft) onCreated(draft);
  }

  const errors =
    parseError !== null
      ? [parseError]
      : save.state.status === 'error'
        ? validationMessages(save.state.error)
        : [];
  const errorTitle =
    parseError !== null
      ? t('draft.error.parse')
      : save.state.status === 'error'
        ? describeError(save.state.error)
        : null;
  const pending = save.state.status === 'pending';
  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <TextAreaField
        className="[&_textarea]:font-mono [&_textarea]:text-secondary"
        label={t('draft.policies')}
        hint={t('draft.policies.hint', { version: base.version })}
        value={text}
        rows={18}
        required
        spellCheck={false}
        autoComplete="off"
        dir="ltr"
        onChange={(event) => setText(event.target.value)}
      />
      <TextField
        label={t('draft.notes')}
        hint={t('draft.notes.hint')}
        value={notes}
        maxLength={2000}
        onChange={(event) => setNotes(event.target.value)}
      />
      {errorTitle !== null && (
        <div role="alert" className="space-y-2 rounded-card border border-danger/40 bg-card p-4">
          <p className="flex items-center gap-2 font-semibold">
            <CircleAlert className="size-5 shrink-0 text-danger" />
            {errorTitle}
          </p>
          {errors.length > 0 && <BulletList items={errors} empty="" />}
        </div>
      )}
      <Button type="submit" disabled={pending} aria-busy={pending}>
        <FilePlus2 className="size-4" />
        {pending ? t('draft.saving') : t('draft.save')}
      </Button>
    </form>
  );
}
