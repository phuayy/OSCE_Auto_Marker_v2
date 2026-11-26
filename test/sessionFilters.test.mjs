import test from 'node:test';
import assert from 'node:assert/strict';
import {
  CHIP_STORAGE_KEY, FILTER_CHIPS, FilterChip, MAX_QUERY_LENGTH, couldHideInFlight, describeEmptyResult,
  describeResultCount, filterKey, isFiltered, normalizeFilter, readStoredChip, toQueryParams, writeStoredChip,
} from '../src/lib/sessionFilters.js';
import { IN_FLIGHT_STATUSES } from '../src/lib/enums.js';

const f = (chip, q = '') => ({ chip, q });

test('the all chip with no text adds no parameters', () => {
  assert.deepEqual(toQueryParams(f('all')), []);
  assert.deepEqual(toQueryParams(undefined), []);
  assert.equal(isFiltered(f('all', '   ')), false);
});

test('every chip maps to its parameters', () => {
  assert.deepEqual(toQueryParams(f(FilterChip.MINE)), [['createdBy', 'me']]);
  assert.deepEqual(toQueryParams(f(FilterChip.FAILED)), [['status', 'failed']]);
  assert.deepEqual(toQueryParams(f(FilterChip.COMPLETED)), [['status', 'completed']]);
  assert.deepEqual(toQueryParams(f(FilterChip.LONG)), [['workflow', 'long']]);
  const inProgress = toQueryParams(f(FilterChip.IN_PROGRESS)).map(([, v]) => v);
  for (const status of IN_FLIGHT_STATUSES) assert.ok(inProgress.includes(status), status);
  assert.ok(toQueryParams(f(FilterChip.IN_PROGRESS)).every(([k]) => k === 'status'));
});

test('text is trimmed, collapsed, bounded, and unicode survives the query string', () => {
  assert.deepEqual(toQueryParams(f('all', '  chest   pain ')), [['q', 'chest pain']]);
  assert.equal(normalizeFilter({ q: 'x'.repeat(500) }).q.length, MAX_QUERY_LENGTH);
  const query = new URLSearchParams(toQueryParams(f('failed', 'Zoë 患者 & co=1')));
  assert.equal(query.get('q'), 'Zoë 患者 & co=1');
  assert.equal(new URLSearchParams(query.toString()).get('q'), 'Zoë 患者 & co=1');
});

test('an unknown chip is the all chip', () => {
  assert.deepEqual(toQueryParams(f('bogus')), []);
  assert.equal(filterKey(f('bogus')), filterKey(f('all')));
});

test('empty results name the filter, and the unfiltered case keeps the original line', () => {
  assert.equal(describeEmptyResult(f('all')), 'No saved sessions yet. Upload a video to create one.');
  assert.equal(describeEmptyResult(f('failed')), 'No failed sessions.');
  assert.equal(describeEmptyResult(f('mine')), 'No sessions created by you.');
  assert.equal(describeEmptyResult(f('all', 'abc')), 'No sessions match “abc”.');
  assert.equal(describeEmptyResult(f('failed', 'abc')), 'No failed sessions match “abc”.');
  for (const chip of FILTER_CHIPS) assert.ok(describeEmptyResult(f(chip.id, 'z')).length > 0);
});

test('result count wording', () => {
  assert.equal(describeResultCount(1, f('all')), '1 session');
  assert.equal(describeResultCount(0, f('all')), '0 sessions');
  assert.equal(describeResultCount(1, f('failed')), '1 matching session');
  assert.equal(describeResultCount(50, f('failed'), true), '50+ matching sessions');
  assert.equal(describeResultCount(undefined, f('all')), '0 sessions');
});

test('only the bare In progress chip shows every running session', () => {
  assert.equal(couldHideInFlight(f('all')), false);
  assert.equal(couldHideInFlight(f(FilterChip.IN_PROGRESS)), false);
  assert.equal(couldHideInFlight(f(FilterChip.IN_PROGRESS, 'x')), true);
  assert.equal(couldHideInFlight(f(FilterChip.FAILED)), true);
  assert.equal(couldHideInFlight(f('all', 'x')), true);
});

function fakeStorage(initial = {}) {
  const data = { ...initial };
  return { getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); }, removeItem: (k) => { delete data[k]; }, data };
}

test('the chip is remembered; garbage and a throwing store mean all', () => {
  const storage = fakeStorage();
  writeStoredChip('failed', storage);
  assert.equal(readStoredChip(storage), 'failed');
  writeStoredChip('all', storage);
  assert.equal(CHIP_STORAGE_KEY in storage.data, false);
  assert.equal(readStoredChip(fakeStorage({ [CHIP_STORAGE_KEY]: '{not a chip' })), 'all');
  const throwing = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); }, removeItem() { throw new Error('denied'); } };
  assert.equal(readStoredChip(throwing), 'all');
  assert.doesNotThrow(() => writeStoredChip('failed', throwing));
  assert.equal(readStoredChip(null), 'all');
});
