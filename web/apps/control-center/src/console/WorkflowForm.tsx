import { Button, SelectField, TextAreaField, TextField } from '@aiva/ui';
import { Play } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import type { Scenario } from '../api/client';
import { t } from '../i18n';
import { classificationLabel } from '../labels';
import { CLASSIFICATIONS, QUALITY_TIERS } from '../run/draft';
import { workflowDraftFor, type WorkflowDraft } from './requests';

interface WorkflowFormProps {
  scenario: Scenario | undefined;
  applications: readonly string[];
  pending: boolean;
  onSubmit: (draft: WorkflowDraft) => void;
}

/** Starts the service-incident agent (LLD §15 #6) as a simulated Test Console identity. */
export function WorkflowForm({ scenario, applications, pending, onSubmit }: WorkflowFormProps) {
  const [draft, setDraft] = useState<WorkflowDraft>(() => workflowDraftFor(scenario));
  const update = (patch: Partial<WorkflowDraft>) => setDraft((current) => ({ ...current, ...patch }));

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit(draft);
  }

  return (
    <form onSubmit={submit} className="space-y-5" aria-busy={pending}>
      <div className="grid gap-5 lg:grid-cols-2">
        <TextField
          label={t('agents.form.incident')}
          hint={t('agents.form.incident.hint')}
          value={draft.incidentId}
          required
          pattern="INC-[0-9]{3,8}"
          autoComplete="off"
          spellCheck={false}
          dir="ltr"
          onChange={(event) => update({ incidentId: event.target.value })}
        />
        <SelectField
          label={t('runForm.application')}
          hint={t('runForm.application.hint')}
          value={draft.applicationId}
          required
          onChange={(event) => update({ applicationId: event.target.value })}
        >
          {draft.applicationId === '' && <option value="">{t('runForm.application.choose')}</option>}
          {applications.map((id) => (
            <option key={id} value={id}>
              {id}
            </option>
          ))}
        </SelectField>
        <TextAreaField
          className="lg:col-span-2"
          label={t('agents.form.instructions')}
          hint={t('agents.form.instructions.hint')}
          value={draft.instructions}
          maxLength={4000}
          onChange={(event) => update({ instructions: event.target.value })}
        />
        <SelectField
          label={t('runForm.classification')}
          hint={t('runForm.classification.hint')}
          value={draft.classification}
          onChange={(event) => update({ classification: event.target.value as WorkflowDraft['classification'] })}
        >
          <option value="">{t('runForm.classification.none')}</option>
          {CLASSIFICATIONS.map((value) => (
            <option key={value} value={value}>
              {classificationLabel(value)}
            </option>
          ))}
        </SelectField>
        <TextField
          label={t('runForm.sourceIp')}
          hint={t('runForm.sourceIp.hint')}
          value={draft.simulatedSourceIp}
          autoComplete="off"
          spellCheck={false}
          dir="ltr"
          onChange={(event) => update({ simulatedSourceIp: event.target.value })}
        />
        <SelectField
          label={t('runForm.qualityTier')}
          value={draft.qualityTier}
          onChange={(event) => update({ qualityTier: event.target.value as WorkflowDraft['qualityTier'] })}
        >
          {QUALITY_TIERS.map((tier) => (
            <option key={tier} value={tier}>
              {t(`runForm.qualityTier.${tier}`)}
            </option>
          ))}
        </SelectField>
        <TextField
          label={t('agents.form.maxOutputTokens')}
          type="number"
          min={64}
          max={4096}
          required
          value={draft.maxOutputTokens}
          onChange={(event) => update({ maxOutputTokens: Number(event.target.value) })}
        />
      </div>
      <div className="flex flex-wrap items-center gap-4">
        <Button type="submit" disabled={pending}>
          <Play className="size-4" />
          {pending ? t('agents.form.running') : t('agents.form.submit')}
        </Button>
        <p className="text-secondary text-ink-muted">{t('agents.form.note')}</p>
      </div>
    </form>
  );
}
