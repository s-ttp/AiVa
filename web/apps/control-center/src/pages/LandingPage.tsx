import { Button, buttonClasses, LoadingState, Wordmark } from '@aiva/ui';
import { BadgeCheck, CircleAlert, Info, LockKeyhole, PiggyBank, TriangleAlert, type LucideIcon } from 'lucide-react';
import { useId } from 'react';
import { Link, Navigate, useSearchParams } from 'react-router';
import { isGatewayDown } from '../api/client';
import { permissionChecker, safeNextPath } from '../auth/permissions';
import { useSession } from '../auth/session';
import { GatewayDown } from '../components/GatewayDown';
import { formatList } from '../format';
import { t } from '../i18n';
import { roleLabel } from '../labels';
import { homePath } from '../navigation';
import { SignInMethods } from './SignInMethods';

/** What the solution is for, in plain words and without figures (owner, 2026-10-22). */
const PILLARS: ReadonlyArray<{ id: 'cost' | 'sovereign' | 'proof'; icon: LucideIcon }> = [
  { id: 'cost', icon: PiggyBank },
  { id: 'sovereign', icon: LockKeyhole },
  { id: 'proof', icon: BadgeCheck },
];

/**
 * The front door, at "/" and "/sign-in" alike: the value proposition and the sign-in.
 */
export function LandingPage() {
  const signInId = useId();
  return (
    <>
      <title>{t('app.name')}</title>
      <main id="main" className="grid grow lg:grid-cols-[3fr_2fr]">
        <p
          role="note"
          className="flex items-start gap-2 border-b border-warning/40 bg-warning/10 px-6 py-3 font-semibold sm:px-12 lg:col-span-2 lg:px-16"
        >
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" />
          {t('landing.publicNotice')}
        </p>
        <section className="flex flex-col justify-center gap-8 bg-brand-navy px-6 py-12 text-on-brand [--aiva-focus-ring:var(--aiva-brand-teal-light)] sm:px-12 lg:px-16">
          <div className="space-y-3">
            <p className="text-secondary font-semibold text-brand-teal-light">{t('landing.category')}</p>
            <h1>
              <Wordmark size="lg" tone="onDark" />
            </h1>
            <p className="text-heading font-medium text-on-brand/90">{t('landing.meaning')}</p>
          </div>
          <p className="max-w-2xl text-kpi font-semibold">{t('landing.tagline')}</p>
          <div className="space-y-4">
            <h2 className="text-body font-semibold text-brand-teal-light">{t('landing.pillars')}</h2>
            <ul className="grid gap-4 md:grid-cols-3">
              {PILLARS.map(({ id, icon: Icon }) => (
                <li key={id} className="rounded-card border border-on-brand/15 bg-on-brand/5 p-4">
                  <p className="flex items-center gap-2 font-semibold">
                    <Icon className="size-5 text-brand-teal-light" />
                    {t(`landing.pillar.${id}.name`)}
                  </p>
                  <p className="mt-2 text-secondary text-on-brand/80">{t(`landing.pillar.${id}.text`)}</p>
                </li>
              ))}
            </ul>
          </div>
        </section>
        <section aria-labelledby={signInId} className="flex items-center justify-center px-6 py-12">
          <div className="w-full max-w-sm space-y-5 rounded-panel border border-line bg-card p-8">
            <h2 id={signInId} className="text-heading font-semibold text-brand-navy">
              {t('landing.signIn.title')}
            </h2>
            <SignInPanel />
          </div>
        </section>
      </main>
      <footer className="border-t border-line px-6 py-4 text-secondary text-ink-muted">
        {t('landing.footer')}
      </footer>
    </>
  );
}

function SignInPanel() {
  const { state, signOut, retry } = useSession();
  const [params] = useSearchParams();
  const next = safeNextPath(params.get('next'));

  if (state.status === 'restoring') return <LoadingState label={t('session.checking')} />;
  // A stored session waits for a waking demo instead of offering a new sign-in.
  if (state.status === 'unverified' && isGatewayDown(state.error)) return <GatewayDown error={state.error} onRetry={retry} />;
  if (state.status === 'signedIn') {
    if (next) return <Navigate to={next} replace />;
    const { profile } = state;
    return (
      <div className="space-y-4">
        <p className="text-body">
          {t('landing.signedIn', { name: profile.display_name ?? profile.user_id ?? '' })}
          <span className="block text-secondary text-ink-muted">{formatList((profile.roles ?? []).map(roleLabel), 'and')}</span>
        </p>
        <div className="flex flex-col gap-2">
          <Link to={homePath(permissionChecker(profile.permissions ?? []))} className={buttonClasses('primary')}>
            {t('landing.continue')}
          </Link>
          <Button variant="secondary" onClick={signOut}>
            {t('account.signOut')}
          </Button>
        </div>
      </div>
    );
  }
  return (
    <>
      {state.status === 'signedOut' && state.reason !== 'none' && (
        <p role="status" className="flex items-start gap-2 rounded-lg border border-info/40 bg-info/10 p-3 text-secondary">
          <Info className="mt-0.5 size-4 shrink-0 text-info" />
          {t(state.reason === 'expired' ? 'landing.notice.expired' : 'landing.notice.signedOut')}
        </p>
      )}
      {state.status === 'unverified' && (
        <p role="status" className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 p-3 text-secondary">
          <CircleAlert className="mt-0.5 size-4 shrink-0 text-warning" />
          {state.error.message}
        </p>
      )}
      <SignInMethods next={next} />
      <SingleSignOn />
    </>
  );
}

function SingleSignOn() {
  const noticeId = useId();
  return (
    <div className="space-y-3 border-t border-line pt-5">
      <p id={noticeId} className="text-secondary text-ink-muted">
        {t('landing.sso.notice')}
      </p>
      <Button variant="secondary" aria-disabled="true" aria-describedby={noticeId} className="w-full">
        {t('landing.sso.button')}
      </Button>
    </div>
  );
}
