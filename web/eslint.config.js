import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    ignores: [
      'dist/**',
      'node_modules/**',
      'playwright-report/**',
      'test-results/**',
      '**/*.d.ts',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    plugins: {
      'react-hooks': reactHooks,
    },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      // Chinese editorial copy intentionally uses full-width spaces in JSX.
      'no-irregular-whitespace': ['error', { skipJSXText: true }],
      // CSS custom properties occasionally need a narrow `as any` cast in
      // React style objects; TypeScript still checks every other assignment.
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
);
