/// <reference types="vite/client" />

interface Window {
  /** Assigned by /config.js; see deploy/spaces/control-center. */
  readonly __AIVA_CONFIG__?: { readonly gatewayUrl?: string };
}
