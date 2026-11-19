import { useId, type ReactNode } from 'react';
import { cx } from '../cx';

interface CardProps {
  title?: string;
  description?: ReactNode;
  className?: string;
  children: ReactNode;
}

export function Card({ title, description, className, children }: CardProps) {
  const titleId = useId();
  return (
    <section
      aria-labelledby={title ? titleId : undefined}
      className={cx('rounded-card border border-line bg-card p-6', className)}
    >
      {title && (
        <h2 id={titleId} className="text-heading font-semibold text-brand-navy">
          {title}
        </h2>
      )}
      {description && <p className="mt-1 text-secondary text-ink-muted">{description}</p>}
      <div className={title || description ? 'mt-4' : undefined}>{children}</div>
    </section>
  );
}
