import { cx } from '../cx';
import type { ReactNode } from 'react';

export interface Fact {
  label: string;
  value: ReactNode;
}

/**
 * Label/value pairs as a description list, two columns from small screens up.
 * Plain-text values are isolated with <bdi> so they keep their own direction.
 */
export function FactList({ facts, className }: { facts: readonly Fact[]; className?: string }) {
  return (
    <dl className={cx('grid gap-x-6 gap-y-3 sm:grid-cols-[minmax(0,13rem)_1fr]', className)}>
      {facts.map((fact) => (
        <div key={fact.label} className="contents">
          <dt className="text-secondary font-semibold text-ink-muted">{fact.label}</dt>
          <dd className="min-w-0 wrap-anywhere">
            {typeof fact.value === 'string' || typeof fact.value === 'number' ? <bdi>{fact.value}</bdi> : fact.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}
