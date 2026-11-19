import { isGatewayPaused, isGatewayStarting, retryAfterMinutes, type ApiError } from '../api/client';
import { t, tPlural } from '../i18n';

/**
 * What a failed sign-in tells the person (ADR-0019): a wrong passcode, a
 * lockout or a visitor-session limit with the time until they may try again,
 * and a demo that is still waking up. Other errors keep the gateway's message.
 */
export function signInErrorText(error: ApiError): string {
  const minutes = retryAfterMinutes(error);
  switch (error.code) {
    case 'DEMO_SIGN_IN_FAILED':
      return t('signIn.error.passcode');
    case 'DEMO_SIGN_IN_LOCKED':
      return minutes === null ? error.message : tPlural('signIn.error.locked', minutes);
    case 'PUBLIC_SESSION_LIMITED':
      return minutes === null ? error.message : tPlural('signIn.error.visitorLimited', minutes);
    case 'PUBLIC_SESSION_UNAVAILABLE':
      return t('signIn.error.visitorUnavailable');
    default:
      if (isGatewayPaused(error)) return t('signIn.error.paused');
      return isGatewayStarting(error) ? t('signIn.error.starting') : error.message;
  }
}
