import { Button, ErrorState, LoadingState, SelectField, TextField } from '@aiva/ui';
import { CircleAlert } from 'lucide-react';
import { useId, useState, type FormEvent, type ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { ApiError, isGatewayDown, unwrap, type CallerProfile, type DemoUser } from '../api/client';
import { useApiQuery } from '../api/useApiQuery';
import { permissionChecker } from '../auth/permissions';
import { useSession } from '../auth/session';
import { signInErrorText } from '../auth/signInErrors';
import { GatewayDown } from '../components/GatewayDown';
import { formatList } from '../format';
import { t } from '../i18n';
import { requestMainFocus } from '../layout/arrival';
import { roleLabel } from '../labels';
import { homePath } from '../navigation';

/**
 * The sign-ins this gateway offers (GET /v1/auth/sign-in-options, ADR-0019):
 * a public visitor session, the passcode sign-in for demo users on deployed
 * gateways, and the password-less dev sign-in on local gateways only.
 */
export function SignInMethods({ next }: { next: string | null }) {
  const { gatewayUrl } = useSession();
  const { state, reload } = useApiQuery(gatewayUrl ? 'sign-in-options' : null, (client, signal) =>
    unwrap(client.GET('/v1/auth/sign-in-options', { signal })),
  );

  if (!gatewayUrl || state === null) {
    return <ErrorState title={t('landing.demo.noGateway.title')} description={t('landing.demo.noGateway.body')} />;
  }
  if (state.status === 'loading') return <LoadingState label={t('signIn.options.loading')} />;
  if (state.status === 'error') {
    if (isGatewayDown(state.error)) return <GatewayDown error={state.error} onRetry={reload} />;
    return <ErrorState title={t('signIn.options.error')} description={state.error.message} onRetry={reload} />;
  }
  const { public: visitor, passcode, dev } = state.data;
  if (!visitor && !passcode && !dev) {
    return <p className="text-secondary text-ink-muted">{t('signIn.none')}</p>;
  }
  return (
    <div className="space-y-5 divide-y divide-line [&>*:not(:first-child)]:pt-5">
      {visitor && <VisitorSignIn next={next} />}
      {passcode && <PasscodeSignIn next={next} />}
      {dev && <DevSignIn next={next} />}
    </div>
  );
}

/** Runs a sign-in, then lands on `next` or the role's home with focus on the main content. */
function useSignIn(next: string | null) {
  const navigate = useNavigate();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  async function run(signIn: () => Promise<CallerProfile>) {
    setPending(true);
    setError(null);
    try {
      const profile = await signIn();
      requestMainFocus();
      navigate(next ?? homePath(permissionChecker(profile.permissions ?? [])), { replace: true });
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause
          : new ApiError({ kind: 'network', message: t('landing.demo.error.title') }),
      );
      setPending(false);
    }
  }
  return { pending, error, run };
}

function SignInError({ id, error }: { id: string; error: ApiError | null }) {
  if (!error) return null;
  return (
    <p id={id} role="alert" className="flex items-start gap-2 text-secondary text-danger">
      <CircleAlert className="mt-0.5 size-4 shrink-0" />
      {signInErrorText(error)}
    </p>
  );
}

function Method({ title, children }: { title: string; children: ReactNode }) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <h3 id={headingId} className="text-body font-semibold text-ink">
        {title}
      </h3>
      {children}
    </section>
  );
}

function VisitorSignIn({ next }: { next: string | null }) {
  const { startVisitorSession } = useSession();
  const { pending, error, run } = useSignIn(next);
  const hintId = useId();
  const errorId = useId();
  return (
    <Method title={t('signIn.visitor.title')}>
      <p id={hintId} className="text-secondary text-ink-muted">
        {t('signIn.visitor.text')}
      </p>
      <SignInError id={errorId} error={error} />
      <Button
        className="w-full"
        onClick={() => void run(startVisitorSession)}
        disabled={pending}
        aria-busy={pending}
        aria-describedby={error ? `${hintId} ${errorId}` : hintId}
      >
        {pending ? t('signIn.visitor.starting') : t('signIn.visitor.submit')}
      </Button>
    </Method>
  );
}

function PasscodeSignIn({ next }: { next: string | null }) {
  const { state, reload } = useApiQuery('demo-users', (client, signal) =>
    unwrap(client.GET('/v1/auth/demo/users', { signal })),
  );
  return (
    <Method title={t('signIn.passcode.title')}>
      {state?.status === 'success' ? (
        state.data.length === 0 ? (
          <p className="text-secondary text-ink-muted">{t('landing.demo.empty')}</p>
        ) : (
          <PasscodeForm users={state.data} next={next} />
        )
      ) : state?.status === 'error' ? (
        isGatewayDown(state.error) ? (
          <GatewayDown error={state.error} onRetry={reload} />
        ) : (
          <ErrorState title={t('signIn.passcode.error')} description={state.error.message} onRetry={reload} />
        )
      ) : (
        <LoadingState label={t('landing.demo.loading')} />
      )}
    </Method>
  );
}

function defaultUser(users: readonly DemoUser[]): string {
  return users.find((user) => user.roles.includes('tester'))?.user_id ?? users[0]?.user_id ?? '';
}

function PasscodeForm({ users, next }: { users: readonly DemoUser[]; next: string | null }) {
  const { signInWithPasscode } = useSession();
  const { pending, error, run } = useSignIn(next);
  const errorId = useId();
  const [selected, setSelected] = useState(() => defaultUser(users));
  const [passcode, setPasscode] = useState('');

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void run(() => signInWithPasscode(selected, passcode));
  }

  return (
    <form onSubmit={onSubmit} className="space-y-3">
      <p className="text-secondary text-ink-muted">{t('signIn.passcode.text')}</p>
      <SelectField label={t('landing.demo.legend')} value={selected} onChange={(event) => setSelected(event.target.value)}>
        {users.map((user) => (
          <option key={user.user_id} value={user.user_id}>
            {t('signIn.passcode.option', {
              name: user.display_name,
              roles: formatList(user.roles.map(roleLabel), 'and'),
            })}
          </option>
        ))}
      </SelectField>
      <TextField
        label={t('signIn.passcode.label')}
        type="password"
        required
        maxLength={256}
        autoComplete="current-password"
        value={passcode}
        onChange={(event) => setPasscode(event.target.value)}
        aria-describedby={error ? errorId : undefined}
        aria-invalid={error?.code === 'DEMO_SIGN_IN_FAILED' || undefined}
      />
      <SignInError id={errorId} error={error} />
      <Button type="submit" className="w-full" disabled={pending || !selected} aria-busy={pending}>
        {pending ? t('landing.demo.signingIn') : t('signIn.passcode.submit')}
      </Button>
    </form>
  );
}

function DevSignIn({ next }: { next: string | null }) {
  const { state, reload } = useApiQuery('dev-users', (client, signal) =>
    unwrap(client.GET('/v1/auth/dev/users', { signal })),
  );
  let body: ReactNode;
  if (state === null || state.status === 'loading') body = <LoadingState label={t('landing.demo.loading')} />;
  else if (state.status === 'error') {
    body =
      state.error.code === 'DEV_SIGN_IN_DISABLED' ? (
        <div className="space-y-1 text-secondary">
          <p className="font-semibold text-ink">{t('landing.demo.disabled.title')}</p>
          <p className="text-ink-muted">{state.error.remediation ?? state.error.message}</p>
        </div>
      ) : (
        <ErrorState title={t('landing.demo.error.title')} description={state.error.message} onRetry={reload} />
      );
  } else if (state.data.length === 0) body = <p className="text-secondary text-ink-muted">{t('landing.demo.empty')}</p>;
  else body = <DevUserForm users={state.data} next={next} />;
  return <Method title={t('signIn.dev.title')}>{body}</Method>;
}

function DevUserForm({ users, next }: { users: readonly DemoUser[]; next: string | null }) {
  const { signInAsDemoUser } = useSession();
  const { pending, error, run } = useSignIn(next);
  const hintId = useId();
  const errorId = useId();
  const [selected, setSelected] = useState(() => defaultUser(users));

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void run(() => signInAsDemoUser(selected));
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4" aria-describedby={error ? errorId : undefined}>
      <fieldset aria-describedby={hintId} className="space-y-2">
        <legend className="text-body font-semibold text-ink">{t('landing.demo.legend')}</legend>
        <p id={hintId} className="text-secondary text-ink-muted">
          {t('landing.demo.hint')}
        </p>
        <div className="max-h-80 space-y-1 overflow-y-auto">
          {users.map((user) => {
            const inputId = `${hintId}-${user.user_id}`;
            return (
              <div
                key={user.user_id}
                className="flex items-start gap-3 rounded-lg border border-transparent px-2 py-1.5 has-checked:border-brand-navy has-checked:bg-canvas"
              >
                <input
                  id={inputId}
                  type="radio"
                  name="demo-user"
                  value={user.user_id}
                  checked={selected === user.user_id}
                  onChange={() => setSelected(user.user_id)}
                  className="mt-1 size-4 shrink-0 accent-brand-navy"
                />
                <label htmlFor={inputId} className="flex grow cursor-pointer flex-col text-body text-ink">
                  {user.display_name}
                  <span className="text-secondary text-ink-muted">{formatList(user.roles.map(roleLabel), 'and')}</span>
                </label>
              </div>
            );
          })}
        </div>
      </fieldset>
      <SignInError id={errorId} error={error} />
      <Button type="submit" className="w-full" disabled={pending || !selected} aria-busy={pending}>
        {pending ? t('landing.demo.signingIn') : t('landing.demo.submit')}
      </Button>
    </form>
  );
}
