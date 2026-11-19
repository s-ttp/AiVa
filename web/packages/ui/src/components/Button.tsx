import type { ButtonHTMLAttributes } from 'react';
import { cx } from '../cx';

export type ButtonVariant = 'primary' | 'secondary';

/** Button styling, also used for router links that look like buttons. */
export function buttonClasses(variant: ButtonVariant = 'primary'): string {
  return cx(
    'inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-body font-semibold',
    'transition-colors aria-disabled:cursor-not-allowed aria-disabled:opacity-60',
    variant === 'primary'
      ? 'bg-brand-navy text-on-brand hover:bg-brand-navy/90'
      : 'border border-line bg-card text-ink hover:bg-canvas',
  );
}

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
}

export function Button({ variant, className, type = 'button', ...props }: ButtonProps) {
  return <button type={type} className={cx(buttonClasses(variant), className)} {...props} />;
}
