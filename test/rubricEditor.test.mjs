import test from 'node:test';
import assert from 'node:assert/strict';
import * as R from '../src/lib/rubricEditor.js';

function deepFreeze(o) {
  if (o && typeof o === 'object' && !Object.isFrozen(o)) {
    Object.freeze(o);
    Object.values(o).forEach(deepFreeze);
  }
  return o;
}

const LABELS = ['Empathy', 'Listening', 'Clarity', 'Structure', 'Rapport', 'Closing', 'Pace'];

function serverRubric(overrides = {}) {
  const criteria = LABELS.map((label, i) => ({
    id: i + 1,
    section: i < 3 ? 'Verbal' : i < 5 ? null : 'Wrap up',
    label,
    indicators: [`${label} one`, `${label} two`],
  }));
  return {
    schema: 'communication-rubric-v1',
    title: 'Communication',
    notes: 'Some notes',
    scoring_scale: { All: 3, Most: 2, Some: 1, None: 0 },
    pass_threshold: 11,
    criteria,
    ...overrides,
  };
}

function editable(overrides = {}) {
  return { ...R.toEditableRubric(serverRubric()), ...overrides };
}

test('round trip keeps the rubric, drops keys and ids', () => {
  const out = R.fromEditableRubric(R.toEditableRubric(serverRubric()));
  assert.equal(out.title, 'Communication');
  assert.equal(out.criteria.length, 7);
  assert.deepEqual(out.criteria[0], { section: 'Verbal', label: 'Empathy', indicators: ['Empathy one', 'Empathy two'] });
  assert.equal(out.criteria[3].section, null);
  assert.equal(out.pass_threshold, null);
  assert.deepEqual(out.scoring_scale, { All: 3, Most: 2, Some: 1, None: 0 });
  assert.ok(!JSON.stringify(out).includes('"key"'));
  assert.ok(!JSON.stringify(out).includes('"id"'));
});

test('criteria are sorted by numeric id and keys are unique', () => {
  const r = serverRubric();
  r.criteria = [r.criteria[2], r.criteria[0], r.criteria[1], ...r.criteria.slice(3)];
  const e = R.toEditableRubric(r);
  assert.deepEqual(e.criteria.slice(0, 3).map((c) => c.label), ['Empathy', 'Listening', 'Clarity']);
  const keys = e.criteria.flatMap((c) => [c.key, ...c.indicators.map((i) => i.key)]);
  assert.equal(new Set(keys).size, keys.length);
});

test('blank indicators are dropped and text trimmed on the way out', () => {
  let e = editable();
  const k = e.criteria[0].key;
  e = R.addIndicator(e, k);
  e = R.updateCriterion(e, k, { label: '  Empathy  ', section: '   ' });
  const out = R.fromEditableRubric(e);
  assert.deepEqual(out.criteria[0].indicators, ['Empathy one', 'Empathy two']);
  assert.equal(out.criteria[0].label, 'Empathy');
  assert.equal(out.criteria[0].section, null);
});

test('passThreshold: default stored is automatic, other stored is explicit', () => {
  assert.equal(R.toEditableRubric(serverRubric({ pass_threshold: 11 })).passThreshold, null);
  assert.equal(R.toEditableRubric(serverRubric({ pass_threshold: 12 })).passThreshold, 12);
  const five = serverRubric({
    scoring_scale: { All: 4, Most: 2, Some: 1, None: 0 },
    pass_threshold: 10,
  });
  five.criteria = five.criteria.slice(0, 5);
  const e = R.toEditableRubric(five);
  assert.equal(e.passThreshold, null);
  assert.equal(e.scoringScale.All, 4);
});

test('defaultPassThreshold', () => {
  assert.equal(R.defaultPassThreshold(7, 21), 11);
  assert.equal(R.defaultPassThreshold(5, 15), 8);
  assert.equal(R.defaultPassThreshold(6, 18), 9);
});

test('deriveRubricTotals: automatic follows count, explicit stays', () => {
  let e = editable();
  let t = R.deriveRubricTotals(e);
  assert.deepEqual(t, { criteriaCount: 7, maxScore: 21, defaultPassThreshold: 11, passThreshold: 11, isAutomatic: true });
  e = R.addCriterion(e);
  t = R.deriveRubricTotals(e);
  assert.equal(t.criteriaCount, 8);
  assert.equal(t.maxScore, 24);
  assert.equal(t.passThreshold, 12);
  const explicit = R.addCriterion(R.setPassThreshold(editable(), '15'), null);
  t = R.deriveRubricTotals(explicit);
  assert.equal(t.passThreshold, 15);
  assert.equal(t.isAutomatic, false);
  assert.equal(R.deriveRubricTotals(R.setScalePoints(editable(), 'All', '')).maxScore, 0);
});

test('a valid rubric passes with no issues', () => {
  const v = R.validateEditableRubric(editable());
  assert.equal(v.valid, true);
  assert.deepEqual(v.issues, []);
  assert.deepEqual(v.fieldErrors.criteria, {});
});

test('title rules', () => {
  assert.equal(R.validateEditableRubric(editable({ title: '  ' })).valid, false);
  assert.ok(R.validateEditableRubric(editable({ title: '  ' })).fieldErrors.title);
  assert.equal(R.validateEditableRubric(editable({ title: 'x'.repeat(R.MAX_TITLE_CHARS) })).valid, true);
  assert.ok(R.validateEditableRubric(editable({ title: 'x'.repeat(R.MAX_TITLE_CHARS + 1) })).fieldErrors.title);
});

test('notes length limit', () => {
  assert.equal(R.validateEditableRubric(editable({ notes: 'x'.repeat(R.MAX_NOTES_CHARS) })).valid, true);
  assert.ok(R.validateEditableRubric(editable({ notes: 'x'.repeat(R.MAX_NOTES_CHARS + 1) })).fieldErrors.notes);
});

test('criteria count bounds', () => {
  let e = editable();
  e = { ...e, criteria: e.criteria.slice(0, 1) };
  assert.match(R.validateEditableRubric(e).issues.join(' '), /at least 2/);
  e = { ...e, criteria: e.criteria.concat(editable().criteria.slice(0, 1)) };
  e.criteria[1] = { ...e.criteria[1], label: 'Other' };
  assert.equal(R.validateEditableRubric(e).valid, true);
  const many = Array.from({ length: R.MAX_CRITERIA + 1 }, (_, i) => ({ key: `k${i}`, section: '', label: `L${i}`, indicators: [] }));
  const v = R.validateEditableRubric({ ...editable(), criteria: many });
  assert.match(v.issues.join(' '), /at most 60/);
  assert.equal(R.validateEditableRubric({ ...editable(), criteria: many.slice(0, 60) }).valid, true);
});

test('label rules: required, length, duplicates case-insensitive', () => {
  const base = editable();
  let e = base;
  const [a, b] = e.criteria;
  e = R.updateCriterion(e, a.key, { label: '  ' });
  let v = R.validateEditableRubric(e);
  assert.ok(v.issues.includes('Criterion 1 needs a label.'));
  assert.ok(v.fieldErrors.criteria[a.key].label);

  e = R.updateCriterion(base, a.key, { label: 'x'.repeat(R.MAX_LABEL_CHARS + 1) });
  assert.ok(R.validateEditableRubric(e).fieldErrors.criteria[a.key].label);
  e = R.updateCriterion(base, a.key, { label: 'x'.repeat(R.MAX_LABEL_CHARS) });
  assert.equal(R.validateEditableRubric(e).valid, true);

  e = R.updateCriterion(base, a.key, { label: 'Empathy' });
  e = R.updateCriterion(e, b.key, { label: ' EMPATHY ' });
  v = R.validateEditableRubric(e);
  assert.ok(v.issues.includes('Two criteria are both called "Empathy".'));
  assert.ok(v.fieldErrors.criteria[a.key].label);
  assert.ok(v.fieldErrors.criteria[b.key].label);
});

test('section and indicator limits; blank indicator is not an error', () => {
  const base = editable();
  let e = base;
  const c = e.criteria[0];
  e = R.updateCriterion(e, c.key, { section: 'x'.repeat(R.MAX_SECTION_CHARS + 1) });
  assert.ok(R.validateEditableRubric(e).fieldErrors.criteria[c.key].section);

  e = R.addIndicator(base, c.key);
  assert.equal(R.validateEditableRubric(e).valid, true);

  const longKey = e.criteria[0].indicators[0].key;
  e = R.updateIndicator(base, c.key, c.indicators[0].key, 'x'.repeat(R.MAX_INDICATOR_CHARS + 1));
  assert.ok(R.validateEditableRubric(e).fieldErrors.criteria[c.key].indicators[c.indicators[0].key]);
  assert.ok(longKey);

  let many = base;
  for (let i = 0; i < R.MAX_INDICATORS_PER_CRITERION; i += 1) {
    many = R.addIndicator(many, c.key);
    const list = many.criteria[0].indicators;
    many = R.updateIndicator(many, c.key, list[list.length - 1].key, `t${i}`);
  }
  const v = R.validateEditableRubric(many);
  assert.equal(v.valid, false);
  assert.match(v.issues.join(' '), /at most 30 indicators/);
});

test('scale rules, each', () => {
  const bad = (label, value) => R.validateEditableRubric(R.setScalePoints(editable(), label, value));
  for (const [label, value] of [['All', ''], ['Most', '1.5'], ['Some', -1], ['None', 101], ['All', 'abc']]) {
    const v = bad(label, value);
    assert.equal(v.valid, false, `${label}=${value}`);
    assert.ok(v.fieldErrors.scale);
  }
  assert.equal(bad('All', ' 3 ').valid, true);
  assert.equal(bad('All', 100).valid, true);
  let e = R.setScalePoints(editable(), 'Most', 5);
  assert.match(R.validateEditableRubric(e).issues.join(' '), /must not increase/);
  e = { ...editable(), scoringScale: { All: 2, Most: 2, Some: 2, None: 2 } };
  assert.match(R.validateEditableRubric(e).issues.join(' '), /"All" must score more than "None"/);
  e = { ...editable(), scoringScale: { All: 3, Most: 3, Some: 1, None: 1 } };
  assert.equal(R.validateEditableRubric(e).valid, true);
});

test('pass threshold rules', () => {
  const check = (v) => R.validateEditableRubric(R.setPassThreshold(editable(), v));
  assert.equal(check(null).valid, true);
  assert.equal(check(0).valid, true);
  assert.equal(check(21).valid, true);
  assert.equal(check(' 12 ').valid, true);
  assert.ok(check(22).fieldErrors.passThreshold);
  assert.ok(check('').fieldErrors.passThreshold);
  assert.ok(check('1.5').fieldErrors.passThreshold);
  assert.ok(check(-1).fieldErrors.passThreshold);
});

test('several problems are reported at once', () => {
  let e = editable({ title: '' });
  e = R.setScalePoints(e, 'All', '');
  e = R.updateCriterion(e, e.criteria[1].key, { label: '' });
  const v = R.validateEditableRubric(e);
  assert.equal(v.valid, false);
  assert.ok(v.issues.length >= 3);
  assert.ok(v.fieldErrors.title && v.fieldErrors.scale);
});

test('edit ops never mutate and unknown keys are no-ops', () => {
  const e = deepFreeze(editable());
  const k = e.criteria[1].key;
  const ik = e.criteria[1].indicators[0].key;
  const results = [
    R.setRubricField(e, 'title', 'T'),
    R.setScalePoints(e, 'All', 5),
    R.setPassThreshold(e, 9),
    R.addCriterion(e, k),
    R.removeCriterion(e, k),
    R.moveCriterion(e, k, 2),
    R.updateCriterion(e, k, { label: 'Z' }),
    R.addIndicator(e, k),
    R.updateIndicator(e, k, ik, 'new'),
    R.removeIndicator(e, k, ik),
  ];
  for (const r of results) assert.notEqual(r, e);
  assert.equal(R.setRubricField(e, 'bogus', 1), e);
  assert.equal(R.setScalePoints(e, 'Bogus', 1), e);
  assert.equal(R.addCriterion(e, 'nope'), e);
  assert.equal(R.removeCriterion(e, 'nope'), e);
  assert.equal(R.moveCriterion(e, 'nope', 1), e);
  assert.equal(R.updateCriterion(e, 'nope', { label: 'x' }), e);
  assert.equal(R.addIndicator(e, 'nope'), e);
  assert.equal(R.updateIndicator(e, k, 'nope', 'x'), e);
  assert.equal(R.removeIndicator(e, k, 'nope'), e);
});

test('field, scale, threshold, criterion and indicator ops change the right thing', () => {
  let e = editable();
  assert.equal(R.setRubricField(e, 'notes', 'N').notes, 'N');
  assert.equal(R.setScalePoints(e, 'None', '0').scoringScale.None, '0');
  assert.equal(R.setPassThreshold(e, null).passThreshold, null);
  const c = e.criteria[2];
  e = R.updateCriterion(e, c.key, { label: 'New' });
  assert.equal(e.criteria[2].label, 'New');
  assert.equal(e.criteria[2].section, c.section);
  e = R.addIndicator(e, c.key);
  assert.equal(e.criteria[2].indicators.length, 3);
  const added = e.criteria[2].indicators[2];
  assert.equal(added.text, '');
  e = R.updateIndicator(e, c.key, added.key, 'hello');
  assert.equal(e.criteria[2].indicators[2].text, 'hello');
  e = R.removeIndicator(e, c.key, added.key);
  assert.equal(e.criteria[2].indicators.length, 2);
  e = R.removeCriterion(e, c.key);
  assert.equal(e.criteria.length, 6);
});

test('addCriterion position and section inheritance', () => {
  const e = editable();
  const after = R.addCriterion(e, e.criteria[1].key);
  assert.equal(after.criteria[2].label, '');
  assert.equal(after.criteria[2].section, 'Verbal');
  assert.deepEqual(after.criteria[2].indicators, []);
  assert.equal(after.criteria.length, 8);
  const appended = R.addCriterion(e);
  assert.equal(appended.criteria[7].section, 'Wrap up');
  const empty = R.addCriterion({ ...e, criteria: [] });
  assert.equal(empty.criteria[0].section, '');
  const keys = after.criteria.map((c) => c.key);
  assert.equal(new Set(keys).size, keys.length);
});

test('moveCriterion clamps at the ends', () => {
  const e = editable();
  const labels = (x) => x.criteria.map((c) => c.label);
  assert.equal(labels(R.moveCriterion(e, e.criteria[2].key, -1))[1], 'Clarity');
  assert.equal(labels(R.moveCriterion(e, e.criteria[2].key, -10))[0], 'Clarity');
  assert.equal(labels(R.moveCriterion(e, e.criteria[2].key, 99))[6], 'Clarity');
  assert.equal(R.moveCriterion(e, e.criteria[0].key, -1), e);
});

test('isRubricDirty: clean, edited, reverted', () => {
  const original = editable();
  assert.equal(R.isRubricDirty(original, R.toEditableRubric(R.fromEditableRubric(original))), false);
  const edited = R.setRubricField(original, 'title', 'Other');
  assert.equal(R.isRubricDirty(original, edited), true);
  const reverted = R.setRubricField(edited, 'title', 'Communication');
  assert.equal(R.isRubricDirty(original, reverted), false);
  const added = R.addIndicator(original, original.criteria[0].key);
  assert.equal(R.isRubricDirty(original, added), false);
});

test('sectionSuggestions: first-appearance order, deduped, blanks skipped', () => {
  assert.deepEqual(R.sectionSuggestions(editable()), ['Verbal', 'Wrap up']);
  assert.deepEqual(R.sectionSuggestions({ criteria: [] }), []);
});

test('garbage input gives an empty editable', () => {
  for (const input of [null, undefined, 'x', 3, [], {}]) {
    const e = R.toEditableRubric(input);
    assert.deepEqual(e, { title: '', notes: '', scoringScale: { All: 3, Most: 2, Some: 1, None: 0 }, passThreshold: null, criteria: [] });
  }
  const e = R.toEditableRubric({ scoring_scale: { All: 1 }, criteria: 'nope' });
  assert.deepEqual(e.scoringScale, { All: 3, Most: 2, Some: 1, None: 0 });
  assert.deepEqual(e.criteria, []);
  assert.doesNotThrow(() => R.fromEditableRubric(R.setScalePoints(editable(), 'All', 'abc')));
});
