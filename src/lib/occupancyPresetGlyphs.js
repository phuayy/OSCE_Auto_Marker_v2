// Figure layout for the small per-preset diagram in the occupancy-rule
// picker (see components/OccupancyPresetGlyph.jsx). Kept as data, not JSX, so
// the scenario each preset draws is a plain, testable fact rather than
// something only visible by rendering the component.
//
// Each figure is `{ left, size, ghost? }`: `left` is a percentage of the
// frame's width, `size` a pixel icon size, and `ghost: true` marks the
// "visible but not counted" figure a rule's height gate would reject at the
// frame edge — present for every preset whose `minBoxHeightRatio` is above
// zero (pair_strict, solo; see fastapi_backend/app/pipeline/person_presets.py),
// absent for `pair`, which has no gate at all.
const OCCUPANCY_PRESET_SCENARIOS = {
  pair: [
    { left: 32, size: 16 },
    { left: 68, size: 16 },
  ],
  pair_strict: [
    { left: 28, size: 16 },
    { left: 58, size: 16 },
    { left: 97, size: 16, ghost: true },
  ],
  solo: [
    { left: 42, size: 20 },
    { left: 97, size: 16, ghost: true },
  ],
};

/**
 * The figures to draw for a preset id, or `undefined` for one with no fixed
 * scenario ("custom" — the operator sets the zone by hand — and anything
 * this build does not recognise).
 */
export function occupancyPresetGlyphFigures(presetId) {
  return OCCUPANCY_PRESET_SCENARIOS[presetId];
}
