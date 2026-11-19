import { createElement, Fragment, type ReactNode } from 'react';

/**
 * Minimal i18n: typed per-locale dictionaries, `t(key, vars)` lookup and document
 * direction. English is the only locale in this phase; the Arabic phase adds
 * `ar: 'rtl'` below and an `ar` dictionary with the same keys (enforced by type).
 */
export type Locale = 'en';
type Direction = 'ltr' | 'rtl';

const LOCALE_DIRECTION: Readonly<Record<Locale, Direction>> = { en: 'ltr' };

const currentLocale: Locale = 'en';

export type Dictionary<Key extends string> = Readonly<Record<Key, string>>;
export type Translator<Key extends string> = (
  key: Key,
  vars?: Readonly<Record<string, string | number>>,
) => string;

/** Unicode first-strong isolate and pop directional isolate. */
const FSI = '\u2068';
const PDI = '\u2069';

interface TranslatorOptions {
  /**
   * Wrap each substituted value in Unicode directional isolates, so IDs,
   * amounts and times keep their own direction inside right-to-left copy.
   * Defaults to on for right-to-left locales.
   */
  isolateValues?: boolean;
}

export function createTranslator<Key extends string>(
  dictionaries: Readonly<Record<Locale, Dictionary<Key>>>,
  locale: Locale = currentLocale,
  { isolateValues = LOCALE_DIRECTION[locale] === 'rtl' }: TranslatorOptions = {},
): Translator<Key> {
  const dictionary = dictionaries[locale];
  return (key, vars) =>
    dictionary[key].replace(/\{(\w+)\}/g, (placeholder, name: string) => {
      const value = vars?.[name];
      if (value === undefined) return placeholder;
      return isolateValues ? `${FSI}${value}${PDI}` : String(value);
    });
}

/** Marks a placeholder for rich substitution; NUL never occurs in copy. */
const MARK = '\u0000';

/**
 * Translates a message whose placeholders are React nodes (links, formatted
 * IDs), so a translation can reorder them freely instead of the UI gluing
 * sentence fragments together.
 */
export function createRichTranslator<Key extends string>(t: Translator<Key>) {
  return (key: Key, nodes: Readonly<Record<string, ReactNode>>): ReactNode =>
    createElement(
      Fragment,
      null,
      ...t(key, Object.fromEntries(Object.keys(nodes).map((name) => [name, `${MARK}${name}${MARK}`])))
        .split(MARK)
        .map((part, index) => (index % 2 === 1 ? createElement(Fragment, { key: index }, nodes[part]) : part)),
    );
}

/** The CLDR plural category of a count in the locale, e.g. "one" or "other" in English. */
export function pluralCategory(count: number, locale: Locale = currentLocale): Intl.LDMLPluralRule {
  return new Intl.PluralRules(locale).select(count);
}

/** Sets `lang` and `dir` on <html> so CSS logical properties follow the locale. */
export function applyDocumentLocale(
  locale: Locale = currentLocale,
  root: HTMLElement = document.documentElement,
): void {
  root.lang = locale;
  root.dir = LOCALE_DIRECTION[locale];
}
