import { GatewayPausedState, GatewayStartingState } from '@aiva/ui';
import { isGatewayPaused, type ApiError } from '../api/client';

/** For an error `isGatewayDown` accepts: a paused Space waits for the presenter, a waking one retries on its own. */
export function GatewayDown({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  return isGatewayPaused(error) ? <GatewayPausedState onRetry={onRetry} /> : <GatewayStartingState onRetry={onRetry} />;
}
