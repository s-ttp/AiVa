import { PageHeader } from '@aiva/ui';
import type { ReactNode } from 'react';
import { t } from '../i18n';

interface PageProps {
  title: string;
  eyebrow?: string;
  description?: string;
  children: ReactNode;
}

/** Page frame: document title, page header and vertically spaced content. */
export function Page({ title, eyebrow, description, children }: PageProps) {
  return (
    <>
      <title>{t('app.documentTitle', { page: title })}</title>
      <PageHeader title={title} eyebrow={eyebrow} description={description} />
      <div className="space-y-6">{children}</div>
    </>
  );
}
