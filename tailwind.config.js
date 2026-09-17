import { themeExtension, themePlugin } from './tailwind.palette.js';

/** @type {import('tailwindcss').Config} */
export default {
  content: [
    './index.html',
    './src/**/*.{js,jsx,ts,tsx}'
  ],
  // The dark theme is the `dark` class on <html>, set before first paint by
  // the inline script in index.html and afterwards by src/lib/theme.js. The
  // palette is tonal (see tailwind.palette.js), so no component needs a
  // `dark:` variant — test/theme.test.mjs keeps it that way.
  darkMode: 'selector',
  theme: {
    extend: themeExtension()
  },
  plugins: [themePlugin]
};
