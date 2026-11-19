import { FlaskConical, Gauge } from 'lucide-react';
import { cx } from '../cx';
import { t } from '../strings';

export type DataMode = 'synthetic' | 'measured';

/** Global notice separating synthetic illustration from measured execution (LLD §22.8). */
export function SyntheticDataBanner({ mode = 'synthetic' }: { mode?: DataMode }) {
  const Icon = mode === 'synthetic' ? FlaskConical : Gauge;
  const title = t(`banner.${mode}.title`);
  return (
    <div
      role="note"
      aria-label={title}
      className={cx(
        'border-b px-4 py-2 text-secondary',
        mode === 'synthetic' ? 'border-info/30 bg-info/10' : 'border-success/30 bg-success/10',
      )}
    >
      <p className="mx-auto flex max-w-(--aiva-content-max-width) flex-wrap items-center gap-x-2 gap-y-1">
        <Icon className="size-4 shrink-0" />
        <strong className="font-semibold">{title}</strong>
        <span className="text-ink">{t(`banner.${mode}.body`)}</span>
      </p>
    </div>
  );
}
