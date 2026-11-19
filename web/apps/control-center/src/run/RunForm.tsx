import { Badge, Button, CheckboxField, Ltr, SelectField, TextAreaField, TextField } from '@aiva/ui';
import { Play, Shapes } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import type { Classification, Scenario } from '../api/client';
import { t } from '../i18n';
import { classificationLabel } from '../labels';
import { CLASSIFICATIONS, QUALITY_TIERS, ROUTING_MODES, draftFor, type Evidence, type RunDraft } from './draft';
import { ExecutionModeField } from './ExecutionModeField';
import type { ModeChoice } from './executionMode';

interface RunFormProps {
  scenarios: readonly Scenario[];
  initialScenarioId: string | null;
  pending: boolean;
  /** Who may choose replay or live (ADR-0019); a visitor starts on the recorded replay. */
  modeChoice: ModeChoice;
  /** Changes after every run, so the visitor's live budget is read again. */
  runCount: number;
  onSubmit: (draft: RunDraft) => void;
}

/**
 * The scenario's synthetic evidence, read-only: it is sent exactly as the catalog defines it, so the
 * recorded replay of the scenario still matches.
 */
function EvidenceList({ evidence }: { evidence: Evidence }) {
  return (
    <details className="rounded-card border border-line p-4">
      <summary className="cursor-pointer font-semibold text-brand-navy">
        {t('runForm.evidence', { count: evidence.length })}
      </summary>
      <div className="mt-3 space-y-3">
        <Badge icon={Shapes}>{t('runForm.evidence.sample')}</Badge>
        <p className="text-secondary text-ink-muted">{t('runForm.evidence.hint')}</p>
        <ol className="space-y-2">
          {evidence.map((item) => (
            <li key={item.id} className="rounded-lg bg-canvas p-3">
              <Ltr className="block font-mono text-secondary font-semibold">{item.id}</Ltr>
              <p className="mt-1 whitespace-pre-wrap text-secondary [overflow-wrap:anywhere]">{item.text}</p>
            </li>
          ))}
        </ol>
      </div>
    </details>
  );
}

export function RunForm({ scenarios, initialScenarioId, pending, modeChoice, runCount, onSubmit }: RunFormProps) {
  const runnable = scenarios.filter((scenario) => !scenario.deferred);
  const applications = [...new Set(scenarios.map((scenario) => scenario.application_id))].sort();
  const [draft, setDraft] = useState<RunDraft>(() => ({
    ...draftFor(runnable.find((scenario) => scenario.id === initialScenarioId) ?? runnable[0]),
    executionMode: modeChoice === 'visitor' ? 'replay' : null,
  }));
  const update = (patch: Partial<RunDraft>) => setDraft((current) => ({ ...current, ...patch }));

  function onScenarioChange(scenarioId: string) {
    setDraft((current) => draftFor(scenarios.find((scenario) => scenario.id === scenarioId), current));
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit(draft);
  }

  return (
    <form onSubmit={submit} className="space-y-5" aria-busy={pending}>
      <div className="grid gap-5 lg:grid-cols-2">
        <SelectField
          label={t('runForm.scenario')}
          value={draft.scenarioId}
          onChange={(event) => onScenarioChange(event.target.value)}
        >
          {scenarios.map((scenario) => (
            <option key={scenario.id} value={scenario.id} disabled={Boolean(scenario.deferred)}>
              {scenario.deferred
                ? t('runForm.scenarioDeferred', { number: scenario.number, name: scenario.name })
                : t('runForm.scenarioOption', { number: scenario.number, name: scenario.name })}
            </option>
          ))}
          <option value="">{t('runForm.customPrompt')}</option>
        </SelectField>
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
      </div>
      <TextAreaField
        label={t('runForm.prompt')}
        hint={t('runForm.prompt.hint')}
        value={draft.prompt}
        required
        rows={4}
        onChange={(event) => update({ prompt: event.target.value })}
      />
      {draft.evidence.length > 0 && <EvidenceList evidence={draft.evidence} />}
      <div className="grid gap-5 lg:grid-cols-3">
        <TextField
          label={t('runForm.sourceIp')}
          hint={t('runForm.sourceIp.hint')}
          value={draft.simulatedSourceIp}
          inputMode="decimal"
          autoComplete="off"
          spellCheck={false}
          dir="ltr"
          onChange={(event) => update({ simulatedSourceIp: event.target.value })}
        />
        <SelectField
          label={t('runForm.classification')}
          hint={t('runForm.classification.hint')}
          value={draft.classification}
          onChange={(event) => update({ classification: event.target.value as Classification | '' })}
        >
          <option value="">{t('runForm.classification.none')}</option>
          {CLASSIFICATIONS.map((value) => (
            <option key={value} value={value}>
              {classificationLabel(value)}
            </option>
          ))}
        </SelectField>
        <TextField
          label={t('runForm.tags')}
          hint={t('runForm.tags.hint')}
          value={draft.tags}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => update({ tags: event.target.value })}
        />
      </div>
      {modeChoice !== 'none' && (
        <ExecutionModeField
          choice={modeChoice}
          value={draft.executionMode}
          runCount={runCount}
          onChange={(executionMode) => update({ executionMode })}
        />
      )}
      <details className="rounded-card border border-line p-4">
        <summary className="cursor-pointer font-semibold text-brand-navy">{t('runForm.advanced')}</summary>
        <div className="mt-4 grid gap-5 lg:grid-cols-3">
          <SelectField
            label={t('runForm.routingMode')}
            value={draft.routingMode}
            onChange={(event) => update({ routingMode: event.target.value as RunDraft['routingMode'] })}
          >
            {ROUTING_MODES.map((mode) => (
              <option key={mode} value={mode}>
                {t(`runForm.routingMode.${mode}`)}
              </option>
            ))}
          </SelectField>
          <SelectField
            label={t('runForm.qualityTier')}
            value={draft.qualityTier}
            onChange={(event) => update({ qualityTier: event.target.value as RunDraft['qualityTier'] })}
          >
            {QUALITY_TIERS.map((tier) => (
              <option key={tier} value={tier}>
                {t(`runForm.qualityTier.${tier}`)}
              </option>
            ))}
          </SelectField>
          <TextField
            label={t('runForm.maxOutputTokens')}
            type="number"
            min={1}
            max={32768}
            required
            value={draft.maxOutputTokens}
            onChange={(event) => update({ maxOutputTokens: Number(event.target.value) })}
          />
          <CheckboxField
            className="lg:col-span-3"
            label={t('runForm.baseline')}
            hint={t('runForm.baseline.hint')}
            checked={draft.baselineComparison}
            onChange={(event) => update({ baselineComparison: event.target.checked })}
          />
        </div>
      </details>
      <div className="flex flex-wrap items-center gap-4">
        <Button type="submit" disabled={pending}>
          <Play className="size-4" />
          {pending ? t('runForm.running') : t('runForm.submit')}
        </Button>
        <p className="text-secondary text-ink-muted">{t('runForm.traceNote')}</p>
      </div>
    </form>
  );
}
