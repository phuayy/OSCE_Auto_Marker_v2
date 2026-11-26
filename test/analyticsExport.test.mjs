// Unit tests for the Analytics page's CSV exports.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  CSV_BOM,
  RESULTS_CSV_HEADER,
  criteriaExportParams,
  criteriaFileName,
  describeExportBlocker,
  requestCriteriaExport,
  resultRowsToCsv,
  resultsFileName,
} from '../src/lib/analyticsExport.js';
import { emptyFilter } from '../src/lib/analyticsFilters.js';

function row(overrides) {
  return {
    sessionId: 'c1',
    sessionName: 'Constipation - Ann',
    studentId: 'stu-Ann',
    studentName: 'Ann',
    resultType: 'content',
    scoreTotal: 2,
    scoreMax: 3,
    passFail: 'Pass',
    rootSessionId: 'rec1',
    rootSessionName: 'Constipation',
    rootSessionCreatedAt: '2026-01-10T08:00:00',
    createdAt: '2026-01-11T09:30:00',
    ...overrides,
  };
}

const lines = (csv) => csv.slice(CSV_BOM.length).split('\r\n');

test('the CSV starts with one byte-order mark then the header', () => {
  const csv = resultRowsToCsv([row({})]);
  assert.ok(csv.startsWith(`${CSV_BOM}Recording,`));
  assert.equal(csv.split(CSV_BOM).length, 2);
  assert.equal(lines(csv)[0], RESULTS_CSV_HEADER.join(','));
});

test('a row becomes recording, scored session, student, type, numbers, percent and dates', () => {
  assert.equal(
    lines(resultRowsToCsv([row({})]))[1],
    'Constipation,2026-01-10,Constipation - Ann,Ann,Content,2,3,66.7,Pass,2026-01-11',
  );
});

test('no rows is a header-only file', () => {
  assert.deepEqual(lines(resultRowsToCsv([])), [RESULTS_CSV_HEADER.join(',')]);
});

test('missing score, maximum and result are empty cells, and percent needs a positive maximum', () => {
  const [, nulls, zeroMax] = lines(
    resultRowsToCsv([
      row({ scoreTotal: null, scoreMax: null, passFail: null, createdAt: null, rootSessionCreatedAt: null }),
      row({ scoreTotal: 1, scoreMax: 0 }),
    ]),
  );
  assert.equal(nulls, 'Constipation,,Constipation - Ann,Ann,Content,,,,,');
  assert.equal(zeroMax.split(',').slice(5, 8).join(','), '1,0,');
});

test('a zero score is 0, not empty', () => {
  assert.equal(lines(resultRowsToCsv([row({ scoreTotal: 0 })]))[1].split(',').slice(5, 8).join(','), '0,3,0');
});

test('formula-leading names are neutralised and commas, quotes and unicode survive', () => {
  const line = lines(
    resultRowsToCsv([row({ studentName: '=cmd', rootSessionName: 'A, "B" é', resultType: 'audio_professionalism' })]),
  )[1];
  assert.ok(line.startsWith('"A, ""B"" é",'));
  assert.ok(line.includes(",'=cmd,Audio professionalism,"));
});

test('a deleted recording falls back to its id, as the table does', () => {
  const line = lines(resultRowsToCsv([row({ rootSessionName: null, rootSessionCreatedAt: null })]))[1];
  assert.ok(line.startsWith('rec1,,'));
});

test('file names carry the UTC day', () => {
  const now = new Date('2026-03-04T23:30:00Z');
  assert.equal(resultsFileName(now), 'osce-results-2026-03-04.csv');
  assert.equal(criteriaFileName(now), 'osce-criteria-2026-03-04.csv');
});

test('the blocker explains an empty selection and is blank otherwise', () => {
  assert.match(describeExportBlocker(0), /No rows/);
  assert.equal(describeExportBlocker(3), '');
});

const NOW = new Date('2026-03-10T12:00:00Z');
const paramsOf = (filter, layout) => Object.fromEntries([...criteriaExportParams(filter, { layout, now: NOW })].map(([k, v]) => [k, v]));

test('no filter sends only the layout, defaulting to long', () => {
  assert.equal(criteriaExportParams(emptyFilter(), { now: NOW }).toString(), 'format=long');
  assert.equal(criteriaExportParams(emptyFilter(), { layout: 'wide', now: NOW }).toString(), 'format=wide');
});

test('recordings repeat and the student is one studentIds value', () => {
  const params = criteriaExportParams({ ...emptyFilter(), sessionIds: ['r1', 'r 2'], studentId: 'stu-Ann' }, { now: NOW });
  assert.deepEqual(params.getAll('rootSessionIds'), ['r1', 'r 2']);
  assert.deepEqual(params.getAll('studentIds'), ['stu-Ann']);
  assert.equal(params.has('from') || params.has('to'), false);
});

test('a preset window sends the UTC day it starts on', () => {
  assert.equal(paramsOf({ ...emptyFilter(), preset: '7d' }).from, '2026-03-03');
  assert.equal(paramsOf({ ...emptyFilter(), preset: '30d' }).from, '2026-02-08');
  assert.equal(paramsOf({ ...emptyFilter(), preset: '7d' }).to, undefined);
});

test('a custom range sends either end as typed and drops a malformed one', () => {
  const custom = { ...emptyFilter(), preset: 'custom' };
  assert.deepEqual(
    { ...paramsOf({ ...custom, dateFrom: '2026-01-01', dateTo: '2026-01-31' }) },
    { format: 'long', from: '2026-01-01', to: '2026-01-31' },
  );
  assert.deepEqual(paramsOf({ ...custom, dateTo: '2026-01-31' }), { format: 'long', to: '2026-01-31' });
  assert.deepEqual(paramsOf({ ...custom, dateFrom: '01/02/2026', dateTo: '' }), { format: 'long' });
});

test('every filter shape combined', () => {
  const params = criteriaExportParams(
    { preset: 'custom', dateFrom: '2026-01-01', dateTo: '2026-01-02', sessionIds: ['r1'], studentId: 's1' },
    { layout: 'wide', now: NOW },
  );
  assert.equal(params.toString(), 'format=wide&rootSessionIds=r1&studentIds=s1&from=2026-01-01&to=2026-01-02');
});

function fakeFetch(response, seen) {
  return async (url, init) => {
    seen.push({ url, init });
    return response;
  };
}

test('requestCriteriaExport returns the blob and the dated file name', async () => {
  const seen = [];
  const blob = new Blob(['x']);
  const { blob: got, fileName } = await requestCriteriaExport(
    { ...emptyFilter(), sessionIds: ['r1'] },
    { layout: 'wide', now: NOW, fetchImpl: fakeFetch({ ok: true, status: 200, blob: async () => blob }, seen) },
  );
  assert.equal(got, blob);
  assert.equal(fileName, 'osce-criteria-2026-03-10.csv');
  assert.equal(seen[0].url, '/api/analytics/export?format=wide&rootSessionIds=r1');
});

test('a 422 surfaces the server message as an ApiError', async () => {
  const response = { ok: false, status: 422, json: async () => ({ error: 'Narrow the filter (a date range).' }) };
  await assert.rejects(
    requestCriteriaExport(emptyFilter(), { now: NOW, fetchImpl: fakeFetch(response, []) }),
    (error) => error.message === 'Narrow the filter (a date range).' && error.status === 422,
  );
});

test('an unreadable failure body falls back, and a gateway error says the server is unreachable', async () => {
  const broken = { ok: false, status: 400, json: async () => { throw new Error('not json'); } };
  await assert.rejects(requestCriteriaExport(emptyFilter(), { fetchImpl: fakeFetch(broken, []) }), /Failed to download the marks/);
  const gateway = { ok: false, status: 502, json: async () => ({ error: 'Proxy error' }) };
  await assert.rejects(
    requestCriteriaExport(emptyFilter(), { fetchImpl: fakeFetch(gateway, []) }),
    /Cannot reach the server/,
  );
});
