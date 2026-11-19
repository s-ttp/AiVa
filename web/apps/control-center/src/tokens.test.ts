import { describe, expect, it } from 'vitest';
import tokens from '../../../packages/ui/src/styles/tokens.css?raw';

/** Control Center and shared UI sources (tests and tokens.css excluded), as raw text keyed by path. */
const SOURCES = import.meta.glob<string>(
  [
    './**/*.{ts,tsx,css}',
    '../../../packages/ui/src/**/*.{ts,tsx,css}',
    '!./**/*.test.{ts,tsx}',
    '!../../../packages/ui/src/**/*.test.{ts,tsx}',
    '!../../../packages/ui/src/styles/tokens.css',
  ],
  { query: '?raw', import: 'default', eager: true },
);

/** A colour written as a literal: hex, or an rgb/hsl/hwb/lab/lch/oklab/oklch function. */
const COLOUR_LITERAL = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\(/;

const SERIES_SLOTS = ['--aiva-viz-1', '--aiva-viz-2', '--aiva-viz-3', '--aiva-viz-4'];

/** The declarations inside the first block whose selector matches. */
function block(css: string, selector: RegExp): string {
  const start = css.search(selector);
  if (start < 0) return '';
  const open = css.indexOf('{', start);
  return css.slice(open + 1, css.indexOf('}', open));
}

describe('design tokens', () => {
  it('defines every colour in @aiva/ui tokens.css, nowhere else', () => {
    expect(Object.keys(SOURCES)).toEqual(
      expect.arrayContaining(['./insights/charts.css', '../../../packages/ui/src/styles/theme.css']),
    );
    const found = Object.entries(SOURCES).flatMap(([path, source]) =>
      source
        .split('\n')
        .map((line, index) => ({ line: line.trim(), number: index + 1 }))
        .filter(({ line }) => COLOUR_LITERAL.test(line))
        .map(({ line, number }) => `${path}:${number}: ${line}`),
    );
    expect(found).toEqual([]);
  });

  it('gives every chart series slot a light value and a dark step', () => {
    const light = block(tokens, /:root\s*\{/);
    const dark = block(tokens, /:root\[data-theme="dark"\]\s*\{/);
    for (const slot of SERIES_SLOTS) {
      expect(light, `${slot} (light)`).toMatch(new RegExp(`${slot}:\\s*#[0-9a-f]{6};`));
      expect(dark, `${slot} (dark)`).toMatch(new RegExp(`${slot}:\\s*#[0-9a-f]{6};`));
    }
  });
});
