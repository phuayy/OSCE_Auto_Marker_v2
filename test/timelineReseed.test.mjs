// When a refreshed clip list may reseed the manual timeline editor (M10).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { clipListSignature, planTimelineReseed } from '../src/lib/timelineSeed.js';

const clip = (id, start, end, extra = {}) => ({ id, start, end, label: `Student ${id}`, ...extra });

test('signature ignores array identity and key order, sees content changes', () => {
  const a = [clip(1, 0, 100), clip(2, 100, 200)];
  const b = a.map((c) => ({ ...c }));
  assert.equal(clipListSignature(a), clipListSignature(b));
  assert.notEqual(clipListSignature(a), clipListSignature([clip(1, 0, 100), clip(2, 100, 201)]));
  assert.notEqual(clipListSignature(a), clipListSignature([clip(1, 0, 100), { ...clip(2, 100, 200), planId: 'p' }]));
  assert.notEqual(clipListSignature(a), clipListSignature([clip(1, 0, 100), { ...clip(2, 100, 200), label: 'X' }]));
});

const snapshot = { boundaries: [100], kinds: ['session', 'session'], labels: ['Student 1', 'Student 2'] };
const lastSeed = { sessionId: 's1', key: 'k1', snapshot };

test('first seed, and a different session, always seed', () => {
  assert.equal(planTimelineReseed({ lastSeed: null, sessionId: 's1', key: 'k1', current: snapshot }), true);
  const edited = { ...snapshot, boundaries: [150] };
  assert.equal(planTimelineReseed({ lastSeed, sessionId: 's2', key: 'k1', current: edited }), true);
});

test('an identical refresh does not reseed', () => {
  assert.equal(planTimelineReseed({ lastSeed, sessionId: 's1', key: 'k1', current: snapshot }), false);
});

test('a refresh that changes the clips reseeds a clean editor', () => {
  assert.equal(planTimelineReseed({ lastSeed, sessionId: 's1', key: 'k2', current: snapshot }), true);
});

test('a refresh never overwrites an edited draft', () => {
  for (const edited of [
    { ...snapshot, boundaries: [140] },
    { ...snapshot, boundaries: [100, 150], kinds: ['session', 'session', 'session'], labels: ['a', 'b', 'c'] },
    { ...snapshot, kinds: ['session', 'intermission'] },
    { ...snapshot, labels: ['Student 1', 'Dr Who'] },
  ]) {
    assert.equal(planTimelineReseed({ lastSeed, sessionId: 's1', key: 'k2', current: edited }), false);
  }
});

test('sub-pixel float noise on a boundary is not an edit', () => {
  assert.equal(
    planTimelineReseed({ lastSeed, sessionId: 's1', key: 'k2', current: { ...snapshot, boundaries: [100.00001] } }),
    true
  );
});
