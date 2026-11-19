import { cx } from '../cx';
import { t } from '../strings';

const SIZES = { sm: 'text-[1.25rem]', md: 'text-[1.75rem]', lg: 'text-[3.5rem]' } as const;

interface WordmarkProps {
  size?: keyof typeof SIZES;
  tone?: 'onLight' | 'onDark';
  /** Optional teal dot after the name (LLD §22.4). */
  dot?: boolean;
}

/**
 * The "AiVa" wordmark. A working brand pending legal clearance, so it never
 * carries a ™ or ® mark.
 */
export function Wordmark({ size = 'md', tone = 'onLight', dot = true }: WordmarkProps) {
  return (
    <span
      dir="ltr"
      className={cx(
        'inline-flex items-baseline font-bold leading-none tracking-tight',
        SIZES[size],
        tone === 'onDark' ? 'text-on-brand' : 'text-brand-navy',
      )}
    >
      {t('brand.name')}
      {dot && (
        <span aria-hidden="true" className="ms-[0.06em] size-[0.2em] rounded-full bg-brand-teal" />
      )}
    </span>
  );
}
