import { cx, mirrorInRtl } from '@aiva/ui';
import { LogOut } from 'lucide-react';
import { useNavigate } from 'react-router';
import { useProfile, useSession } from '../auth/session';
import { formatList } from '../format';
import { t } from '../i18n';
import { roleLabel } from '../labels';

/** Who is signed in, with which roles, and how to sign out. */
export function AccountPanel() {
  const profile = useProfile();
  const { signOut } = useSession();
  const navigate = useNavigate();
  const name = profile.display_name ?? profile.user_id ?? profile.application_id ?? '';
  return (
    <div className="space-y-2 text-secondary">
      <p className="text-on-brand/75">{t('account.signedInAs')}</p>
      <p className="font-semibold text-on-brand">{name}</p>
      <p className="text-on-brand/75">{formatList((profile.roles ?? []).map(roleLabel), 'and')}</p>
      <button
        type="button"
        onClick={() => {
          signOut();
          navigate('/', { replace: true });
        }}
        className="inline-flex items-center gap-2 rounded-lg border border-on-brand/30 px-3 py-1.5 font-semibold text-on-brand hover:bg-on-brand/10"
      >
        <LogOut className={cx('size-4', mirrorInRtl)} />
        {t('account.signOut')}
      </button>
    </div>
  );
}
