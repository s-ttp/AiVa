import { t } from '../strings';

/** First focusable element; slides into view on keyboard focus. */
export function SkipLink({ targetId = 'main' }: { targetId?: string }) {
  return (
    <a
      href={`#${targetId}`}
      className="fixed start-4 top-4 z-50 -translate-y-24 rounded-lg border border-line bg-card px-4 py-2 font-semibold text-ink focus:translate-y-0"
    >
      {t('shell.skipToContent')}
    </a>
  );
}
