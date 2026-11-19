import { CircleAlert, CirclePause, Hourglass, Inbox, LoaderCircle, type LucideIcon } from 'lucide-react';
import { useEffect, type ReactNode } from 'react';
import { t } from '../strings';
import { Button } from './Button';

interface EmptyStateProps {
  title: string;
  description: ReactNode;
  icon?: LucideIcon;
  headingLevel?: 2 | 3;
  /** Optional follow-up actions. */
  children?: ReactNode;
}

/** Explicit "nothing here yet" state; never substitute illustrative data. */
export function EmptyState({
  title,
  description,
  icon: Icon = Inbox,
  headingLevel = 2,
  children,
}: EmptyStateProps) {
  const Heading = headingLevel === 2 ? 'h2' : 'h3';
  return (
    <div className="flex flex-col items-center gap-3 rounded-card border border-dashed border-line bg-card px-6 py-10 text-center">
      <Icon className="size-8 text-ink-muted" />
      <Heading className="text-body font-semibold text-ink">{title}</Heading>
      <p className="max-w-prose text-secondary text-ink-muted">{description}</p>
      {children}
    </div>
  );
}

export function LoadingState({ label = t('state.loading') }: { label?: string }) {
  return (
    <div role="status" className="flex items-center gap-2 py-6 text-ink-muted">
      <LoaderCircle className="size-4 motion-safe:animate-spin" />
      <span>{label}</span>
    </div>
  );
}

interface ErrorStateProps {
  title: string;
  description: ReactNode;
  onRetry?: () => void;
}

export function ErrorState({ title, description, onRetry }: ErrorStateProps) {
  return (
    <div
      role="alert"
      className="flex flex-col items-start gap-3 rounded-card border border-danger/40 bg-card p-6"
    >
      <h2 className="flex items-center gap-2 text-body font-semibold text-ink">
        <CircleAlert className="size-5 shrink-0 text-danger" />
        {title}
      </h2>
      <p className="text-secondary text-ink-muted">{description}</p>
      {onRetry && (
        <Button variant="secondary" onClick={onRetry}>
          {t('state.retry')}
        </Button>
      )}
    </div>
  );
}

/** How often a waking gateway is asked again; the platform proxy answers with Retry-After: 30. */
const GATEWAY_STARTING_RETRY_MS = 30_000;

/**
 * The public demo's gateway Space is asleep or starting, and its platform proxy
 * answered 503 GATEWAY_STARTING (ADR-0019). Not an error: the view tries again
 * on its own every 30 seconds and offers to try at once.
 */
export function GatewayStartingState({ onRetry }: { onRetry: () => void }) {
  useEffect(() => {
    const timer = window.setTimeout(onRetry, GATEWAY_STARTING_RETRY_MS);
    return () => window.clearTimeout(timer);
  }, [onRetry]);
  return (
    <div role="status" className="flex items-start gap-3 rounded-card border border-info/40 bg-info/10 p-4">
      <Hourglass className="mt-0.5 size-5 shrink-0 text-info" />
      <div className="flex flex-col items-start gap-2">
        <p className="font-semibold text-ink">{t('state.gatewayStarting.title')}</p>
        <p className="text-secondary">{t('state.gatewayStarting.body')}</p>
        <Button variant="secondary" onClick={onRetry}>
          {t('state.gatewayStarting.retry')}
        </Button>
      </div>
    </div>
  );
}

/**
 * The public demo's gateway Space is paused on Hugging Face (ADR-0025). Unlike a sleeping one it does not wake
 * on its own, so the view does not retry by itself: it says so and offers to try again.
 */
export function GatewayPausedState({ onRetry }: { onRetry: () => void }) {
  return (
    <div role="status" className="flex items-start gap-3 rounded-card border border-line bg-card p-4">
      <CirclePause className="mt-0.5 size-5 shrink-0 text-ink-muted" />
      <div className="flex flex-col items-start gap-2">
        <p className="font-semibold text-ink">{t('state.gatewayPaused.title')}</p>
        <p className="text-secondary">{t('state.gatewayPaused.body')}</p>
        <Button variant="secondary" onClick={onRetry}>
          {t('state.retry')}
        </Button>
      </div>
    </div>
  );
}
