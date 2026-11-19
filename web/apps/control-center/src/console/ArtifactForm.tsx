import { Badge, Button, SelectField, TextAreaField, TextField } from '@aiva/ui';
import { FilePlus2, Plus, Shapes, Trash2 } from 'lucide-react';
import { useId, useState, type FormEvent } from 'react';
import type { Scenario } from '../api/client';
import { t } from '../i18n';
import { classificationLabel } from '../labels';
import { CLASSIFICATIONS } from '../run/draft';
import { artifactDraftFor, emptyEvidence, emptyFigure, type ArtifactDraft, type EvidenceDraft, type FigureDraft } from './requests';

interface ArtifactFormProps {
  scenario: Scenario | undefined;
  applications: readonly string[];
  pending: boolean;
  onSubmit: (draft: ArtifactDraft) => void;
}

/** Drafting request for the document copilot (LLD §15 #10): instructions, evidence and critical figures. */
export function ArtifactForm({ scenario, applications, pending, onSubmit }: ArtifactFormProps) {
  const [draft, setDraft] = useState<ArtifactDraft>(() => artifactDraftFor(scenario));
  const update = (patch: Partial<ArtifactDraft>) => setDraft((current) => ({ ...current, ...patch }));
  const evidenceId = useId();
  const figuresId = useId();
  const sample = scenario?.sample_artifact ? (
    <Badge icon={Shapes}>{t('artifacts.form.sample')}</Badge>
  ) : null;

  function setEvidence(index: number, patch: Partial<EvidenceDraft>) {
    update({ evidence: draft.evidence.map((item, i) => (i === index ? { ...item, ...patch } : item)) });
  }
  function setFigure(index: number, patch: Partial<FigureDraft>) {
    update({ figures: draft.figures.map((item, i) => (i === index ? { ...item, ...patch } : item)) });
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit(draft);
  }

  return (
    <form onSubmit={submit} className="space-y-6" aria-busy={pending}>
      <div className="grid gap-5 lg:grid-cols-2">
        <TextField
          label={t('artifacts.form.title')}
          value={draft.title}
          required
          maxLength={200}
          onChange={(event) => update({ title: event.target.value })}
        />
        <SelectField
          label={t('artifacts.form.type')}
          value={draft.documentType}
          onChange={(event) => update({ documentType: event.target.value as ArtifactDraft['documentType'] })}
        >
          <option value="pptx">{t('artifacts.type.pptx')}</option>
          <option value="docx">{t('artifacts.type.docx')}</option>
        </SelectField>
        <TextAreaField
          className="lg:col-span-2"
          label={t('artifacts.form.instructions')}
          hint={t('artifacts.form.instructions.hint')}
          value={draft.instructions}
          required
          maxLength={20_000}
          onChange={(event) => update({ instructions: event.target.value })}
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
        <SelectField
          label={t('runForm.classification')}
          hint={t('artifacts.form.classification.hint')}
          value={draft.classification}
          onChange={(event) => update({ classification: event.target.value as ArtifactDraft['classification'] })}
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
      </div>

      <fieldset aria-describedby={`${evidenceId}-hint`} className="space-y-3">
        <legend className="text-body font-semibold text-brand-navy">{t('artifacts.form.evidence')}</legend>
        {sample}
        <p id={`${evidenceId}-hint`} className="text-secondary text-ink-muted">
          {t('artifacts.form.evidence.hint')}
        </p>
        {draft.evidence.map((item, index) => (
          <div key={index} className="grid gap-3 rounded-card border border-line p-4 lg:grid-cols-[minmax(0,14rem)_1fr_auto]">
            <TextField
              label={t('artifacts.form.evidence.id', { number: index + 1 })}
              value={item.id}
              required
              maxLength={200}
              autoComplete="off"
              spellCheck={false}
              dir="ltr"
              onChange={(event) => setEvidence(index, { id: event.target.value })}
            />
            <TextAreaField
              label={t('artifacts.form.evidence.text', { number: index + 1 })}
              value={item.text}
              required
              maxLength={100_000}
              onChange={(event) => setEvidence(index, { text: event.target.value })}
            />
            <Button
              variant="secondary"
              className="self-end"
              disabled={draft.evidence.length === 1}
              onClick={() => update({ evidence: draft.evidence.filter((_, i) => i !== index) })}
            >
              <Trash2 className="size-4" />
              {t('artifacts.form.evidence.remove', { number: index + 1 })}
            </Button>
          </div>
        ))}
        <Button variant="secondary" onClick={() => update({ evidence: [...draft.evidence, emptyEvidence()] })}>
          <Plus className="size-4" />
          {t('artifacts.form.evidence.add')}
        </Button>
      </fieldset>

      <fieldset aria-describedby={`${figuresId}-hint`} className="space-y-3">
        <legend className="text-body font-semibold text-brand-navy">{t('artifacts.form.figures')}</legend>
        {draft.figures.length > 0 && sample}
        <p id={`${figuresId}-hint`} className="text-secondary text-ink-muted">
          {t('artifacts.form.figures.hint')}
        </p>
        {draft.figures.map((figure, index) => (
          <div key={index} className="grid gap-3 rounded-card border border-line p-4 md:grid-cols-2 xl:grid-cols-[repeat(5,minmax(0,1fr))_auto]">
            <TextField
              label={t('artifacts.form.figure.id', { number: index + 1 })}
              value={figure.id}
              required
              maxLength={64}
              pattern="[A-Za-z0-9._\-]+"
              dir="ltr"
              spellCheck={false}
              onChange={(event) => setFigure(index, { id: event.target.value })}
            />
            <TextField
              label={t('artifacts.form.figure.label', { number: index + 1 })}
              value={figure.label}
              required
              maxLength={200}
              onChange={(event) => setFigure(index, { label: event.target.value })}
            />
            <TextField
              label={t('artifacts.form.figure.value', { number: index + 1 })}
              hint={t('artifacts.form.figure.value.hint')}
              value={figure.value}
              required
              maxLength={64}
              dir="ltr"
              onChange={(event) => setFigure(index, { value: event.target.value })}
            />
            <TextField
              label={t('artifacts.form.figure.unit', { number: index + 1 })}
              value={figure.unit}
              maxLength={32}
              onChange={(event) => setFigure(index, { unit: event.target.value })}
            />
            <TextField
              label={t('artifacts.form.figure.source', { number: index + 1 })}
              value={figure.sourceId}
              required
              maxLength={200}
              dir="ltr"
              spellCheck={false}
              onChange={(event) => setFigure(index, { sourceId: event.target.value })}
            />
            <Button
              variant="secondary"
              className="self-end"
              onClick={() => update({ figures: draft.figures.filter((_, i) => i !== index) })}
            >
              <Trash2 className="size-4" />
              {t('artifacts.form.figure.remove', { number: index + 1 })}
            </Button>
          </div>
        ))}
        <Button variant="secondary" onClick={() => update({ figures: [...draft.figures, emptyFigure()] })}>
          <Plus className="size-4" />
          {t('artifacts.form.figure.add')}
        </Button>
      </fieldset>

      <div className="flex flex-wrap items-center gap-4">
        <Button type="submit" disabled={pending}>
          <FilePlus2 className="size-4" />
          {pending ? t('artifacts.form.drafting') : t('artifacts.form.submit')}
        </Button>
        <p className="text-secondary text-ink-muted">{t('artifacts.form.note')}</p>
      </div>
    </form>
  );
}
