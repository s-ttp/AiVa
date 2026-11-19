import type { ReactNode } from 'react';

interface PageHeaderProps {
  title: string;
  /** Area the page belongs to, e.g. "AiVa Govern". */
  eyebrow?: string;
  description?: ReactNode;
}

export function PageHeader({ title, eyebrow, description }: PageHeaderProps) {
  return (
    <header className="mb-6 border-b border-line pb-6">
      {eyebrow && <p className="text-secondary font-semibold text-ink-muted">{eyebrow}</p>}
      <h1 className="text-title font-semibold text-brand-navy">{title}</h1>
      {description && <p className="mt-2 max-w-3xl text-body text-ink-muted">{description}</p>}
    </header>
  );
}
