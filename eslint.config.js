import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';

export default [
  {
    files: ['src/**/*.{js,jsx}'],
    plugins: { 'react-hooks': reactHooks },
    linterOptions: { reportUnusedDisableDirectives: false },
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: globals.browser,
    },
    rules: {
      'no-undef': 'error',
      'no-dupe-args': 'error',
      'no-dupe-keys': 'error',
      'no-unreachable': 'error',
      'no-unsafe-finally': 'error',
      'no-constant-binary-expression': 'error',
      'react-hooks/rules-of-hooks': 'error',
      // Dead imports and helpers accumulate silently after a refactor (the
      // workspace split left an unused icon pair and a commented-out card
      // behind). `React` stays importable for JSX-only files, and a caught
      // error that is only logged may be left unnamed.
      'no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^(_|React$)', caughtErrors: 'none' },
      ],
    },
  },
];
