import { EmptyState, ErrorState, LoadingState } from '@aiva/ui';
import type { ReactNode } from 'react';
import { isGatewayDown, isNotImplemented } from '../api/client';
import { describeError } from '../api/describeError';
import type { QueryState } from '../api/useApiQuery';
import { GatewayDown } from './GatewayDown';
import { NotImplementedState } from './NotImplementedState';

interface QueryViewProps<T> {
  state: QueryState<T> | null;
  onRetry: () => void;
  loadingLabel: string;
  errorTitle: string;
  /** What the view offers, named in the "not available yet" state when the gateway answers 501. */
  feature?: string;
  /** Explicit empty state; shown when `isEmpty(data)` holds. */
  empty?: { title: string; description: string };
  isEmpty?: (data: T) => boolean;
  children: (data: T) => ReactNode;
}

/**
 * Renders loading, error, empty and loaded states explicitly; never substitutes sample data.
 * A gateway that is still starting behind the public demo's proxy is a waiting state, not an error.
 */
export function QueryView<T>({
  state,
  onRetry,
  loadingLabel,
  errorTitle,
  feature,
  empty,
  isEmpty,
  children,
}: QueryViewProps<T>) {
  if (state === null) return null;
  if (state.status === 'loading') return <LoadingState label={loadingLabel} />;
  if (state.status === 'error') {
    if (isGatewayDown(state.error)) return <GatewayDown error={state.error} onRetry={onRetry} />;
    if (feature && isNotImplemented(state.error)) return <NotImplementedState error={state.error} feature={feature} />;
    return <ErrorState title={errorTitle} description={describeError(state.error)} onRetry={onRetry} />;
  }
  if (empty && isEmpty?.(state.data)) {
    return <EmptyState headingLevel={3} title={empty.title} description={empty.description} />;
  }
  return children(state.data);
}
