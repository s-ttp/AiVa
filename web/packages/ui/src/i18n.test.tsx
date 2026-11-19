import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { applyDocumentLocale, createRichTranslator, createTranslator, pluralCategory } from './i18n';

describe('i18n', () => {
  it('looks up keys and fills placeholders', () => {
    const t = createTranslator({ en: { greeting: '{page} · AiVa', plain: 'AiVa' } });
    expect(t('greeting', { page: 'Audit' })).toBe('Audit · AiVa');
    expect(t('plain')).toBe('AiVa');
  });

  it('isolates substituted values for right-to-left copy', () => {
    const t = createTranslator({ en: { spent: 'Spent {amount} of {cap}' } }, 'en', { isolateValues: true });
    expect(t('spent', { amount: '$1.25', cap: '$10.00' })).toBe('Spent \u2068$1.25\u2069 of \u2068$10.00\u2069');
    expect(createTranslator({ en: { spent: 'Spent {amount}' } })('spent', { amount: 1 })).toBe('Spent 1');
  });

  it('substitutes React nodes into a translated message', () => {
    const t = createTranslator({ en: { enforced: 'Enforced by {version} with hash {hash}.' } });
    const tRich = createRichTranslator(t);
    const { container } = render(<p>{tRich('enforced', { version: <a href="/v">version 2</a>, hash: <code>28a8</code> })}</p>);
    expect(container.innerHTML).toBe('<p>Enforced by <a href="/v">version 2</a> with hash <code>28a8</code>.</p>');
  });

  it('selects CLDR plural categories', () => {
    expect([0, 1, 2].map((count) => pluralCategory(count))).toEqual(['other', 'one', 'other']);
  });

  it('sets lang and dir on the document root', () => {
    const root = document.createElement('html');
    applyDocumentLocale('en', root);
    expect(root).toHaveAttribute('lang', 'en');
    expect(root).toHaveAttribute('dir', 'ltr');
  });
});
