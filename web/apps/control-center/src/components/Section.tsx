import { useId, type ReactNode } from 'react';

interface SectionProps {
  title: string;
  description?: ReactNode;
  /** Optional element beside the heading, e.g. a provenance badge. */
  aside?: ReactNode;
  children: ReactNode;
}

/** A titled region inside a card or tab panel (heading level 3). */
export function Section({ title, description, aside, children }: SectionProps) {
  const titleId = useId();
  return (
    <section aria-labelledby={titleId} className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id={titleId} className="text-body font-semibold text-brand-navy">
          {title}
        </h3>
        {aside}
      </div>
      {description && <p className="text-secondary text-ink-muted">{description}</p>}
      {children}
    </section>
  );
}
