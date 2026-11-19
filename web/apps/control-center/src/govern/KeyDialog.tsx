import { Button, formatUtc } from '@aiva/ui';
import { Copy } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { t } from '../i18n';

/** A key just issued: the only moment its secret exists in the browser (ADR-0027). */
export interface IssuedKey {
  credential: string;
  label: string;
  application: string;
  expiresAt: string | null;
}

/**
 * Shows a newly issued key once, in a modal dialog that says it will not be shown again; opening it focuses the key,
 * selected for copying. The secret lives only in the caller's state for as long as this dialog is open: closing it
 * (Done or Escape) calls `onClose`, which must drop the key. It never reaches a query cache, the URL or storage.
 */
export function KeyDialog({ issued, onClose }: { issued: IssuedKey; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const warningId = useId();
  const keyId = useId();
  const [copy, setCopy] = useState<'idle' | 'copied' | 'failed'>('idle');

  // No cleanup: closing here would fire `close` and drop the key (React runs effects twice in development), and a
  // removed dialog leaves the top layer by itself.
  useEffect(() => {
    const element = dialog.current;
    if (element && !element.open) element.showModal();
  }, []);

  async function copyKey() {
    try {
      await navigator.clipboard.writeText(issued.credential);
      setCopy('copied');
    } catch {
      setCopy('failed');
    }
  }

  return (
    <dialog
      ref={dialog}
      aria-labelledby={titleId}
      aria-describedby={warningId}
      onClose={onClose}
      className="m-auto w-[min(36rem,calc(100vw-2rem))] rounded-card border border-line bg-card p-5 text-ink shadow-lg backdrop:bg-ink/40"
    >
      <div className="space-y-4">
        <h2 id={titleId} className="text-heading font-semibold text-brand-navy">
          {t('keys.dialog.title', { application: issued.application })}
        </h2>
        <p id={warningId} className="rounded-card border border-warning/40 bg-warning/10 p-3 text-secondary">
          {t('keys.dialog.warning')}
        </p>
        <div className="space-y-1">
          <label htmlFor={keyId} className="block text-secondary font-semibold">
            {t('keys.dialog.key', { label: issued.label })}
          </label>
          <input
            id={keyId}
            readOnly
            value={issued.credential}
            dir="ltr"
            spellCheck={false}
            autoComplete="off"
            onFocus={(event) => event.currentTarget.select()}
            className="w-full rounded-lg border border-ink-muted bg-canvas px-3 py-2 text-start font-mono text-secondary text-ink"
          />
          {issued.expiresAt && (
            <p className="text-secondary text-ink-muted">{t('keys.dialog.expires', { time: formatUtc(issued.expiresAt) })}</p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={() => void copyKey()}>
            <Copy aria-hidden="true" className="size-4" />
            {t('keys.dialog.copy')}
          </Button>
          <Button variant="secondary" onClick={() => dialog.current?.close()}>
            {t('keys.dialog.done')}
          </Button>
          <p role="status" className="text-secondary">
            {copy === 'copied' ? t('keys.dialog.copied') : copy === 'failed' ? t('keys.dialog.copyFailed') : ''}
          </p>
        </div>
      </div>
    </dialog>
  );
}
