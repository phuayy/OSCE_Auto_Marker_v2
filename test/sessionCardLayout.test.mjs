import test from 'node:test';
import assert from 'node:assert/strict';

import { ERROR_CLAMP_CHARS, errorNeedsToggle, singleLineName } from '../src/lib/sessionCardLayout.js';

test('only a long failure message gets a Show more toggle', () => {
  assert.equal(errorNeedsToggle(''), false);
  assert.equal(errorNeedsToggle(null), false);
  assert.equal(errorNeedsToggle('x'.repeat(ERROR_CLAMP_CHARS)), false);
  assert.equal(errorNeedsToggle('x'.repeat(ERROR_CLAMP_CHARS + 1)), true);
});

test('a name never holds a line break', () => {
  assert.equal(singleLineName('Station 3\n morning'), 'Station 3 morning');
  assert.equal(singleLineName('a\r\n\r\nb'), 'a b');
  assert.equal(singleLineName(undefined), '');
  assert.equal(singleLineName('OSCE Station 3 — Abdominal pain history'), 'OSCE Station 3 — Abdominal pain history');
});
