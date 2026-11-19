import { Link } from 'react-router';
import { Mono } from '../components/inline';
import { t, tRich } from '../i18n';

/** Which published bundle an administrative read reflects, linking to that version's review. */
export function EnforcedBy({ version, bundleHash }: { version: string; bundleHash: string }) {
  return (
    <p className="text-secondary text-ink-muted wrap-anywhere">
      {tRich('enforcedBy.text', {
        version: (
          <Link
            to={`/admin/govern/policy-rules?version=${encodeURIComponent(version)}`}
            className="font-semibold text-brand-navy underline underline-offset-2"
          >
            {t('enforcedBy.version', { version })}
          </Link>
        ),
        hash: <Mono>{bundleHash}</Mono>,
      })}
    </p>
  );
}
