import { screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import approvalQueue from './test/fixtures/govern/approvalQueue.json';
import policyDraft from './test/fixtures/govern/policyDraft.json';
import policyVersions from './test/fixtures/govern/policyVersions.json';
import scenarios from './test/fixtures/scenarios.json';
import { devSignIn, mockGateway, signedInAs } from './test/gateway';
import { renderRoute } from './test/renderRoute';

/** Control Center and shared UI sources (copy and tests excluded), as raw text keyed by path. */
const SOURCES = import.meta.glob<string>(
  [
    './**/*.{ts,tsx,css}',
    '../../../packages/ui/src/**/*.{ts,tsx,css}',
    '!./**/*.test.{ts,tsx}',
    '!../../../packages/ui/src/**/*.test.{ts,tsx}',
    '!./**/i18n.ts',
  ],
  { query: '?raw', import: 'default', eager: true },
);

/** Tailwind utilities tied to left/right; use the logical (start/end, inline) forms instead. */
const PHYSICAL_UTILITY =
  /^-?(?:m[lr]|p[lr]|left|right|border-[lr]|rounded-(?:[lr]|[tb][lr])|scroll-[mp][lr]|origin-(?:(?:top|bottom)-)?(?:left|right)|bg-(?:linear|gradient)-to-[lr]|float-(?:left|right)|clear-(?:left|right)|text-(?:left|right))(?:-|$)/;
/** Physical properties in CSS and in inline style objects. */
const PHYSICAL_CSS =
  /(?:margin|padding|border)-(?:left|right)|(?:^|[\s;{])(?:left|right)\s*:|text-align\s*:\s*(?:left|right)|float\s*:\s*(?:left|right)/;
const PHYSICAL_STYLE =
  /\b(?:margin|padding|border)(?:Left|Right)\w*\s*:|\b(?:left|right)\s*:\s*[`'"\d-]|textAlign\s*:\s*['"](?:left|right)['"]|float\s*:\s*['"](?:left|right)['"]/;

function physicalProperties([path, source]: [string, string]): string[] {
  const lineOf = (index: number) => source.slice(0, index).split('\n').length;
  const found: string[] = [];
  if (path.endsWith('.css')) {
    source.split('\n').forEach((line, index) => {
      if (PHYSICAL_CSS.test(line)) found.push(`${path}:${index + 1}: ${line.trim()}`);
    });
    return found;
  }
  for (const match of source.matchAll(/(['"`])((?:\\.|(?!\1)[^\\])*)\1/g)) {
    for (const token of (match[2] ?? '').split(/\s+/)) {
      const utility = token.split(':').at(-1) ?? '';
      if (PHYSICAL_UTILITY.test(utility)) found.push(`${path}:${lineOf(match.index)}: ${token}`);
    }
  }
  source.split('\n').forEach((line, index) => {
    if (PHYSICAL_STYLE.test(line)) found.push(`${path}:${index + 1}: ${line.trim()}`);
  });
  return found;
}

describe('right-to-left readiness', () => {
  it('uses only logical CSS properties (inline-start/end, not left/right)', () => {
    const files = Object.entries(SOURCES);
    expect(Object.keys(SOURCES)).toEqual(
      expect.arrayContaining(['./layout/ControlCenterShell.tsx']),
    );
    expect(files.length).toBeGreaterThan(50);
    expect(files.some(([path]) => path.includes('packages/ui/src'))).toBe(true);
    expect(files.flatMap(physicalProperties)).toEqual([]);
  });

  it('flags physical properties when they appear', () => {
    expect(['ml-2', 'hover:pr-4', 'text-left', 'rounded-tl-lg', '-left-1', 'border-r'].every((token) =>
      PHYSICAL_UTILITY.test(token.split(':').at(-1) ?? ''),
    )).toBe(true);
    expect(['ms-2', 'pe-4', 'text-start', 'rounded-lg', 'border-line', 'place-items-center', 'prose'].some((token) =>
      PHYSICAL_UTILITY.test(token),
    )).toBe(false);
    expect(PHYSICAL_CSS.test('  margin-left: 4px;')).toBe(true);
    expect(PHYSICAL_STYLE.test("style={{ marginLeft: 4 }}")).toBe(true);
    expect(PHYSICAL_STYLE.test("style={{ insetInlineStart: '10%' }}")).toBe(false);
  });

  describe('pages rendered right-to-left', () => {
    beforeEach(() => {
      document.documentElement.dir = 'rtl';
    });
    afterEach(() => {
      document.documentElement.dir = 'ltr';
    });

    it('renders the sign-in page', async () => {
      mockGateway(devSignIn);
      renderRoute('/');
      expect(await screen.findByRole('heading', { level: 1 })).toBeVisible();
    });

    it('renders the Approval Queue with identifiers isolated left-to-right and the sign-out icon mirrored', async () => {
      signedInAs('u-platform-admin', { 'GET /v1/admin/approvals': approvalQueue });
      renderRoute('/admin/govern/approval-queue');

      expect(await screen.findByRole('table', { name: 'Pending approval requests' })).toBeVisible();
      const ids = [...document.querySelectorAll('bdi[dir="ltr"]')].map((node) => node.textContent);
      expect(ids).toContain(approvalQueue.approvals[0]?.application_id);
      const signOut = screen.getByRole('button', { name: 'Sign out' });
      expect(signOut.querySelector('svg')).toHaveClass('rtl:-scale-x-100');
    });

    it('renders the document copilot', async () => {
      signedInAs('u-tester', {
        'GET /v1/catalog/scenarios': scenarios,
        'GET /v1/artifacts': { items: [], page: { next_cursor: null, limit: 50 } },
      });
      renderRoute('/console/artifacts');

      expect(await screen.findByRole('heading', { level: 1, name: 'Document copilot' })).toBeVisible();
      expect(await screen.findByRole('textbox', { name: 'Source 1 ID' })).toHaveAttribute('dir', 'ltr');
    });

    it('renders a policy version with its simulator', async () => {
      signedInAs('u-platform-admin', {
        'GET /v1/admin/policies': policyVersions,
        'GET /v1/admin/policies/*': policyDraft,
        'GET /v1/catalog/scenarios': scenarios,
      });
      renderRoute('/admin/govern/policy-rules?version=3');

      expect(await screen.findByRole('heading', { level: 2, name: 'Simulate version 3' })).toBeVisible();
      expect(screen.getByRole('heading', { level: 1, name: 'Policy Rules' })).toBeVisible();
    });
  });
});
