// The app's palette in both themes.
//
// The design system is written in Tailwind's own palette names — slate on
// white, cyan for the primary, violet for the long-recording workflow,
// emerald / amber / rose for outcomes — and every one of those names is used
// as a *role*, not a hue: `text-slate-500` is "muted text", `border-slate-200`
// is "a border", `bg-white` is "the surface", `bg-rose-50 text-rose-700` is
// "a warning box". Dark mode keeps the roles and changes the colours, so the
// palette is tonal: each step is a CSS variable, `:root` holds Tailwind's
// values, and `.dark` re-maps them (the model Radix Colors uses). No call site
// changes, and a component written tomorrow in the same vocabulary is themed
// without knowing it.
//
// What re-maps and what does not, stated once:
//
//   * The neutral scale (slate) re-maps at every step in every utility.
//     `bg-slate-800` is a light fill under a dark theme — pair it with a tonal
//     text (`text-slate-50`), never `text-white`.
//   * An accent hue re-maps in its tints (50–200, any utility) and as text
//     (600+), so `bg-rose-50 border-rose-200 text-rose-700` stays legible both
//     ways. Its solids stay literal — `bg-rose-600`, `ring-cyan-500`,
//     `from-cyan-600 to-blue-700` — because those carry `text-white`, which is
//     also literal. A solid is the same colour in both themes.
//   * `white` is the surface wherever it *fills* (`bg-`, gradient stops) and
//     literally white wherever it *draws over* a fill (`text-`, `border-`,
//     `ring-`). Something that must be dark in both themes says so: `bg-black`.
//   * A subtree that keeps the base palette whatever the document's theme —
//     the pre-login hero, dark by design — carries the `theme-fixed` class.
//
// Imported by tailwind.config.js (which turns it into the theme and the
// `:root` / `.dark` declarations) and by test/theme.test.mjs (which checks the
// dark ramp against the design system's own contrast floor). Runtime theme
// switching lives in src/lib/theme.js and has nothing to do with this file.
// The extension matters: tailwindcss v3 publishes no `exports` map, and node's
// own ESM resolver (the test runner) needs it where Tailwind's loader does not.
import colors from 'tailwindcss/colors.js';

export const NEUTRAL = 'slate';
export const ACCENTS = Object.freeze(['rose', 'amber', 'emerald', 'violet', 'cyan', 'blue']);
export const STEPS = Object.freeze([50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950]);
/** Accent steps that are a tinted ground in any utility. */
export const TINT_STEPS = Object.freeze([50, 100, 200]);
/** Accent steps that are text on such a ground, or on the surface. */
export const TEXT_STEPS = Object.freeze([600, 700, 800, 900, 950]);

/** The class a subtree carries to keep the base palette under any theme. */
export const FIXED_SCOPE_CLASS = 'theme-fixed';
/** The class on <html> that selects the dark ramp (Tailwind's `selector` strategy). */
export const DARK_SCOPE_CLASS = 'dark';

// The dark ramp, in Tailwind's own stops so it reads as a statement — "in the
// dark theme, slate-50 is slate-950" — rather than a wall of hex. The page
// ground is the darkest step, the surface (cards, the header) one step up
// (elevated surfaces are lighter in a dark theme), borders and chips above
// that. Text compresses toward the top: dark-theme hierarchy always does.
export const DARK = Object.freeze({
  surface: colors.slate[900],
  slate: Object.freeze({
    50: colors.slate[950], // page ground; a recessed panel on a card
    100: colors.slate[800], // chip, tab list, muted ground
    200: colors.slate[700], // border, progress track, skeleton bone
    300: colors.slate[600], // input border, disabled text
    400: colors.slate[500], // decorative icon, placeholder
    500: colors.slate[400], // muted text — the floor, 4.5:1 on the surface
    600: colors.slate[300],
    700: colors.slate[200], // body text
    800: colors.slate[100],
    900: colors.slate[50], // heading
    950: colors.white,
  }),
  // Per accent hue: tint step -> the stop that plays it in the dark.
  tint: Object.freeze({ 50: 950, 100: 900, 200: 800 }),
  // Per accent hue: text step -> the stop that plays it in the dark.
  text: Object.freeze({ 600: 400, 700: 300, 800: 200, 900: 100, 950: 50 }),
});

/** '#rrggbb' (or '#rgb') -> 'r g b', the channel form `rgb(var(--x) / <alpha-value>)` needs. */
export function channels(hex) {
  const match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(hex).trim());
  if (!match) throw new Error(`Expected a hex colour, got ${JSON.stringify(hex)}`);
  const digits = match[1].length === 3 ? [...match[1]].map((d) => d + d).join('') : match[1];
  const value = parseInt(digits, 16);
  return `${(value >> 16) & 255} ${(value >> 8) & 255} ${value & 255}`;
}

const tintVariable = (hue, step) => `--${hue}-${step}`;
const textVariable = (hue, step) => `--${hue}-text-${step}`;
const neutralVariable = (step) => `--${NEUTRAL}-${step}`;
const SURFACE_VARIABLE = '--surface';

/**
 * Every custom property the theme references, with its value in each theme.
 * The two maps have identical keys by construction; the test pins it anyway.
 */
export function cssVariables() {
  const light = { [SURFACE_VARIABLE]: channels(colors.white) };
  const dark = { [SURFACE_VARIABLE]: channels(DARK.surface) };
  for (const step of STEPS) {
    light[neutralVariable(step)] = channels(colors[NEUTRAL][step]);
    dark[neutralVariable(step)] = channels(DARK.slate[step]);
  }
  for (const hue of ACCENTS) {
    for (const step of TINT_STEPS) {
      light[tintVariable(hue, step)] = channels(colors[hue][step]);
      dark[tintVariable(hue, step)] = channels(colors[hue][DARK.tint[step]]);
    }
    for (const step of TEXT_STEPS) {
      light[textVariable(hue, step)] = channels(colors[hue][step]);
      dark[textVariable(hue, step)] = channels(colors[hue][DARK.text[step]]);
    }
  }
  return { light, dark };
}

const reference = (name) => `rgb(var(${name}) / <alpha-value>)`;

/** What tailwind.config.js spreads into `theme.extend`. */
export function themeExtension() {
  const slate = Object.fromEntries(STEPS.map((step) => [step, reference(neutralVariable(step))]));
  const tints = Object.fromEntries(
    ACCENTS.map((hue) => [hue, Object.fromEntries(TINT_STEPS.map((step) => [step, reference(tintVariable(hue, step))]))]),
  );
  const text = Object.fromEntries(
    ACCENTS.map((hue) => [hue, Object.fromEntries(TEXT_STEPS.map((step) => [step, reference(textVariable(hue, step))]))]),
  );
  return {
    // `colors` feeds every colour utility, so the neutral scale and the
    // accent tints re-map everywhere. Accent solids are left to Tailwind.
    colors: { [NEUTRAL]: slate, ...tints },
    // Accent text re-maps; the same step as a fill or a gradient stop does not.
    textColor: text,
    // `white` fills are the surface; `text-white` / `border-white` stay white.
    backgroundColor: { white: reference(SURFACE_VARIABLE) },
    gradientColorStops: { white: reference(SURFACE_VARIABLE) },
    // The halo `ring-offset-2` paints between a control and its focus ring is
    // white by default; on a dark surface that would be a white box.
    ringOffsetColor: { DEFAULT: `rgb(var(${SURFACE_VARIABLE}))` },
  };
}

/** The Tailwind plugin that emits the `:root` / `.theme-fixed` / `.dark` declarations. */
export function themePlugin({ addBase }) {
  const { light, dark } = cssVariables();
  addBase({
    [`:root, .${FIXED_SCOPE_CLASS}`]: light,
    [`.${DARK_SCOPE_CLASS}`]: dark,
  });
}

/**
 * The colour a class paints in a theme — the rules above as one function, so
 * a test can ask "what is `text-rose-700` on `bg-rose-50` in the dark?"
 * without re-deriving the mapping. `utility` is `text`, `bg`, `border`,
 * `ring`, `gradient` or `fill`; `token` is `white`, `black` or `<hue>-<step>`.
 */
export function paint(theme, utility, token) {
  if (theme !== 'light' && theme !== 'dark') throw new Error(`Unknown theme ${JSON.stringify(theme)}`);
  const dark = theme === 'dark';
  if (token === 'black') return colors.black;
  if (token === 'white') {
    const fills = utility === 'bg' || utility === 'gradient';
    return dark && fills ? DARK.surface : colors.white;
  }
  const match = /^([a-z]+)-(\d{2,3})$/.exec(token);
  if (!match) throw new Error(`Unknown colour token ${JSON.stringify(token)}`);
  const hue = match[1];
  const step = Number(match[2]);
  const literal = colors[hue]?.[step];
  if (!literal) throw new Error(`Unknown colour token ${JSON.stringify(token)}`);
  if (!dark) return literal;
  if (hue === NEUTRAL) return DARK.slate[step];
  if (!ACCENTS.includes(hue)) return literal;
  if (TINT_STEPS.includes(step)) return colors[hue][DARK.tint[step]];
  if (utility === 'text' && TEXT_STEPS.includes(step)) return colors[hue][DARK.text[step]];
  return literal;
}
