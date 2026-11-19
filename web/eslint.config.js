import js from '@eslint/js';
import { defineConfig, globalIgnores } from 'eslint/config';
import jsxA11y from 'eslint-plugin-jsx-a11y-x';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default defineConfig(
  globalIgnores(['**/dist', 'packages/api-client/src/schema.d.ts']),
  js.configs.recommended,
  tseslint.configs.strict,
  {
    files: ['apps/*/src/**', 'packages/*/src/**'],
    languageOptions: { globals: globals.browser },
  },
  {
    files: ['**/*.tsx'],
    extends: [reactHooks.configs.flat.recommended, jsxA11y.configs.strict],
    rules: {
      // WAI-ARIA tab panels and keyboard-scrollable regions are focusable by design.
      'jsx-a11y-x/no-noninteractive-tabindex': ['error', { roles: ['tabpanel', 'region'] }],
    },
  },
  {
    files: ['apps/*/src/**/*.tsx'],
    ignores: ['**/*.test.tsx', '**/test/**'],
    extends: [reactRefresh.configs.vite],
  },
  {
    files: ['**/*.config.{js,ts}', '**/scripts/**', 'apps/*/public/**'],
    languageOptions: { globals: { ...globals.node, ...globals.browser } },
  },
);
