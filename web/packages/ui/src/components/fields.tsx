import {
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react';
import { cx } from '../cx';

/**
 * Form controls with a visible label and an optional hint wired to
 * aria-describedby. Control borders use the muted text colour so their
 * boundary meets the 3:1 non-text contrast of WCAG 2.2 (1.4.11).
 */
const CONTROL =
  'w-full rounded-lg border border-ink-muted bg-card px-3 py-2 text-body text-ink disabled:cursor-not-allowed disabled:opacity-60';

interface FieldFrameProps {
  label: string;
  hint?: ReactNode;
  className?: string;
  children: (ids: { id: string; describedBy: string | undefined }) => ReactNode;
}

function FieldFrame({ label, hint, className, children }: FieldFrameProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  return (
    <div className={cx('flex flex-col gap-1.5', className)}>
      <label htmlFor={id} className="text-secondary font-semibold text-ink">
        {label}
      </label>
      {children({ id, describedBy: hint ? hintId : undefined })}
      {hint && (
        <p id={hintId} className="text-secondary text-ink-muted">
          {hint}
        </p>
      )}
    </div>
  );
}

interface CommonProps {
  label: string;
  hint?: ReactNode;
  className?: string;
}

export function TextField({
  label,
  hint,
  className,
  ...props
}: CommonProps & Omit<InputHTMLAttributes<HTMLInputElement>, 'id' | 'className'>) {
  return (
    <FieldFrame label={label} hint={hint} className={className}>
      {({ id, describedBy }) => <input id={id} aria-describedby={describedBy} className={CONTROL} {...props} />}
    </FieldFrame>
  );
}

export function TextAreaField({
  label,
  hint,
  className,
  ...props
}: CommonProps & Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'id' | 'className'>) {
  return (
    <FieldFrame label={label} hint={hint} className={className}>
      {({ id, describedBy }) => (
        <textarea id={id} aria-describedby={describedBy} className={cx(CONTROL, 'min-h-28')} {...props} />
      )}
    </FieldFrame>
  );
}

export function SelectField({
  label,
  hint,
  className,
  children,
  ...props
}: CommonProps & Omit<SelectHTMLAttributes<HTMLSelectElement>, 'id' | 'className'>) {
  return (
    <FieldFrame label={label} hint={hint} className={className}>
      {({ id, describedBy }) => (
        <select id={id} aria-describedby={describedBy} className={CONTROL} {...props}>
          {children}
        </select>
      )}
    </FieldFrame>
  );
}

export function CheckboxField({
  label,
  hint,
  className,
  ...props
}: CommonProps & Omit<InputHTMLAttributes<HTMLInputElement>, 'id' | 'className' | 'type'>) {
  const id = useId();
  const hintId = `${id}-hint`;
  return (
    <div className={cx('flex items-start gap-3', className)}>
      <input
        id={id}
        type="checkbox"
        aria-describedby={hint ? hintId : undefined}
        className="mt-1 size-4 shrink-0 accent-brand-navy"
        {...props}
      />
      <div className="flex flex-col gap-0.5">
        <label htmlFor={id} className="text-body font-semibold text-ink">
          {label}
        </label>
        {hint && (
          <p id={hintId} className="text-secondary text-ink-muted">
            {hint}
          </p>
        )}
      </div>
    </div>
  );
}
