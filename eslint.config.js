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
    },
  },
  {
    files: ['src/lib/sessionWorkspace.js', 'src/lib/resumableUpload.js'],
    rules: { 'no-unused-vars': ['error', { argsIgnorePattern: '^_' }] },
  },
];
