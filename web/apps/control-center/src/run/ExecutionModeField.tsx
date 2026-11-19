import { useId } from 'react';
import { t } from '../i18n';
import type { ExecutionMode, ModeChoice } from './executionMode';
import { LiveBudget } from './LiveBudget';

interface ExecutionModeFieldProps {
  choice: Exclude<ModeChoice, 'none'>;
  value: ExecutionMode | null;
  runCount: number;
  onChange: (mode: ExecutionMode | null) => void;
}

const OPTIONS: Record<Exclude<ModeChoice, 'none'>, ReadonlyArray<ExecutionMode | null>> = {
  visitor: ['replay', 'live'],
  member: [null, 'replay', 'live'],
};

/**
 * Replay or live for one run (ADR-0019), for roles with `chat:live`. A visitor
 * starts on the recorded replay and sees the public live budget; other roles
 * may also leave the choice to the gateway's configured mode.
 */
export function ExecutionModeField({ choice, value, runCount, onChange }: ExecutionModeFieldProps) {
  const id = useId();
  const budgetId = `${id}-budget`;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <fieldset aria-describedby={`${id}-hint`} className="space-y-2">
        <legend className="text-secondary font-semibold text-ink">{t('runForm.mode')}</legend>
        <p id={`${id}-hint`} className="text-secondary text-ink-muted">
          {t(`runForm.mode.hint.${choice}`)}
        </p>
        {OPTIONS[choice].map((mode) => {
          const key = mode ?? 'default';
          const inputId = `${id}-${key}`;
          return (
            <div
              key={key}
              className="flex items-start gap-3 rounded-lg border border-transparent px-2 py-1.5 has-checked:border-brand-navy has-checked:bg-canvas"
            >
              <input
                id={inputId}
                type="radio"
                name={`${id}-mode`}
                checked={value === mode}
                onChange={() => onChange(mode)}
                aria-describedby={mode === 'live' && choice === 'visitor' ? budgetId : undefined}
                className="mt-1 size-4 shrink-0 accent-brand-navy"
              />
              <label htmlFor={inputId} className="flex grow cursor-pointer flex-col text-body text-ink">
                {t(`runForm.mode.${key}`)}
                <span className="text-secondary text-ink-muted">{t(`runForm.mode.${key}.text`)}</span>
              </label>
            </div>
          );
        })}
      </fieldset>
      {choice === 'visitor' && <LiveBudget id={budgetId} refresh={runCount} />}
    </div>
  );
}
