import '@testing-library/jest-dom/vitest';
import { cleanup, configure } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';
import { resetHfBackoff } from '../api/hfBackoff';
import { TEST_GATEWAY_URL } from './gateway';

// Views are lazily loaded (routes.tsx): the first render of a page in a test file includes importing its module,
// which can take longer than the default second while the whole suite runs in parallel.
configure({ asyncUtilTimeout: 4000 });

// jsdom has no modal dialogs (`HTMLDialogElement.showModal`): open and close them as a browser does, `close` included.
if (typeof HTMLDialogElement !== 'undefined' && !HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement, returnValue?: string) {
    if (!this.open) return;
    this.open = false;
    if (returnValue !== undefined) this.returnValue = returnValue;
    this.dispatchEvent(new Event('close'));
  };
}

beforeEach(() => {
  Object.assign(window, { __AIVA_CONFIG__: { gatewayUrl: TEST_GATEWAY_URL } });
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  resetHfBackoff();
  vi.unstubAllGlobals();
});
