import test from 'node:test';
import assert from 'node:assert/strict';
import {
  BulkAction, bulkAvailability, eligibleIds, isSelectable, pruneSelection, removeFromSelection,
  selectAllVisible, selectedEntries, summarizeBulkResult, toggleSelection,
} from '../src/lib/sessionSelection.js';

const marker = { userId: 'u1', role: 'marker' };
const admin = { userId: 'a1', role: 'admin' };
const row = (id, status, owner = 'u1') => ({ id, status, createdBy: owner ? { userId: owner } : null });

test('toggle adds, removes, and never mutates its input', () => {
  const start = new Set(['a']);
  const added = toggleSelection(start, 'b');
  assert.deepEqual([...added], ['a', 'b']);
  assert.deepEqual([...start], ['a']);
  assert.deepEqual([...toggleSelection(added, 'a')], ['b']);
});

test('select all takes the visible rows except a session uploading in this tab', () => {
  const rows = [row('a', 'completed'), row('b', 'uploading'), { status: 'failed' }];
  const picked = selectAllVisible(rows, (id) => id === 'b');
  assert.deepEqual([...picked], ['a']);
  assert.equal(isSelectable(rows[2]), false);
});

test('pruning keeps what is still visible and hands back the same set when nothing left', () => {
  const selection = new Set(['a', 'b']);
  assert.equal(pruneSelection(selection, [row('a', 'failed'), row('b', 'failed')]), selection);
  assert.deepEqual([...pruneSelection(selection, [row('b', 'failed')])], ['b']);
  assert.equal(pruneSelection(selection, []).size, 0);
  const empty = new Set();
  assert.equal(pruneSelection(empty, [row('a', 'failed')]), empty);
});

test('removing handled ids leaves the failed ones selected', () => {
  assert.deepEqual([...removeFromSelection(new Set(['a', 'b', 'c']), ['a', 'c'])], ['b']);
  const same = new Set(['a']);
  assert.equal(removeFromSelection(same, ['zzz']), same);
  assert.deepEqual(selectedEntries(new Set(['b']), [row('a', 'failed'), row('b', 'failed')]).map((e) => e.id), ['b']);
});

test('availability: re-run only failed/cancelled the viewer may change', () => {
  const entries = [row('f1', 'failed'), row('c1', 'cancelled'), row('ok', 'completed'), row('theirs', 'failed', 'other'), row('legacy', 'failed', null)];
  const a = bulkAvailability(entries, marker);
  assert.equal(a[BulkAction.RERUN].count, 3);
  assert.equal(a[BulkAction.RERUN].allowed, true);
  assert.match(a[BulkAction.RERUN].reason, /2 skipped/);
  assert.deepEqual(eligibleIds(BulkAction.RERUN, entries, marker), ['f1', 'c1', 'legacy']);
  assert.equal(bulkAvailability(entries, admin)[BulkAction.RERUN].count, 4);
});

test('availability: re-score needs a finished session with a transcript the viewer may change', () => {
  const long = { ...row('long', 'completed'), hasVideoClips: true };
  const entries = [
    row('ok', 'completed'), row('failed-no-steps', 'failed'), row('busy', 'processing'), long, row('theirs', 'completed', 'other'),
  ];
  const a = bulkAvailability(entries, marker)[BulkAction.RESCORE];
  assert.deepEqual(eligibleIds(BulkAction.RESCORE, entries, marker), ['ok']);
  assert.equal(a.count, 1);
  assert.match(a.reason, /4 skipped: only finished sessions with a transcript/);
  assert.equal(bulkAvailability(entries, admin)[BulkAction.RESCORE].count, 2);
});

test('availability: delete covers any non-uploading session the viewer may change', () => {
  const entries = [row('a', 'completed'), row('b', 'processing'), row('u', 'uploading'), row('t', 'completed', 'other')];
  const d = bulkAvailability(entries, marker)[BulkAction.DELETE];
  assert.equal(d.count, 2);
  assert.equal(bulkAvailability(entries, admin)[BulkAction.DELETE].count, 3);
});

test('availability: export is open to every marker but needs results', () => {
  const entries = [row('a', 'completed', 'other'), row('b', 'cropped', 'other'), row('c', 'failed')];
  const e = bulkAvailability(entries, marker)[BulkAction.EXPORT];
  assert.equal(e.count, 2);
  assert.match(e.reason, /no results/);
});

test('nothing eligible: not allowed, with a reason; an empty selection has none', () => {
  const r = bulkAvailability([row('a', 'completed')], marker)[BulkAction.RERUN];
  assert.deepEqual({ allowed: r.allowed, count: r.count }, { allowed: false, count: 0 });
  assert.ok(r.reason);
  const none = bulkAvailability([], marker)[BulkAction.DELETE];
  assert.deepEqual(none, { allowed: false, count: 0, reason: '' });
});

test('summary wording: singular, plural, partial, all failed', () => {
  const ok = (id) => ({ sessionId: id, ok: true });
  const bad = (id, error) => ({ sessionId: id, ok: false, error, code: 'conflict' });
  assert.equal(summarizeBulkResult({ results: [ok('a')] }, 'delete').message, '1 session deleted.');
  assert.equal(summarizeBulkResult({ results: [ok('a'), ok('b')] }, 'rerun').message, '2 sessions queued for re-run.');
  const partial = summarizeBulkResult({ results: [ok('a'), bad('b', 'In flight')] }, 'delete');
  assert.equal(partial.message, '1 session deleted; 1 failed.');
  assert.equal(partial.tone, 'warning');
  assert.deepEqual(partial.succeededIds, ['a']);
  assert.deepEqual(partial.failures, [{ sessionId: 'b', error: 'In flight', code: 'conflict' }]);
  const all = summarizeBulkResult({ results: [bad('a'), bad('b')] }, 'rerun');
  assert.equal(all.message, 'Could not re-run any of the 2 sessions.');
  assert.equal(all.tone, 'danger');
  assert.equal(all.failures[0].error, 'The request failed.');
  assert.equal(summarizeBulkResult({ results: [bad('a')] }, 'delete').message, 'Could not delete the session.');
  assert.equal(summarizeBulkResult({ results: [ok('a'), ok('b')] }, 'rescore').message, '2 sessions queued for re-score.');
  const refused = summarizeBulkResult({ results: [ok('a'), { sessionId: 'b', ok: false, error: 'No transcript.', code: 'no_transcript' }] }, 'rescore');
  assert.equal(refused.message, '1 session queued for re-score; 1 failed.');
  assert.deepEqual(refused.failures, [{ sessionId: 'b', error: 'No transcript.', code: 'no_transcript' }]);
});

test('a malformed response is reported, never counted as success', () => {
  for (const response of [null, {}, { results: 'x' }]) {
    const s = summarizeBulkResult(response, 'delete');
    assert.equal(s.ok, 0);
    assert.equal(s.tone, 'danger');
    assert.deepEqual(s.succeededIds, []);
  }
});

import { MAX_EXPORT_SESSIONS } from '../src/lib/sessionSelection.js';

test('export is capped so its query string stays a sane length', () => {
  const many = Array.from({ length: MAX_EXPORT_SESSIONS + 1 }, (_, i) => row(`s${i}`, 'completed'));
  const e = bulkAvailability(many, marker)[BulkAction.EXPORT];
  assert.equal(e.allowed, false);
  assert.match(e.reason, /at most 100/);
  assert.equal(bulkAvailability(many.slice(1), marker)[BulkAction.EXPORT].allowed, true);
});
