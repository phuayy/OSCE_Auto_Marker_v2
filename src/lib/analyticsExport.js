// Getting the Analytics page's numbers into the school mark sheet. Pure apart
// from `requestCriteriaExport`, which takes its fetch as a parameter; the page
// only wires the two buttons to this. `test/analyticsExport.test.mjs` pins it.
//
// Two downloads, for two questions:
//
//   * "Download results (CSV)" is built here, in the browser, from the rows
//     the page is already showing — one line per scored result. Nothing is
//     requested, so it works on exactly what the filter keeps.
//   * "Download per-criterion marks" asks the server
//     (`GET /api/analytics/export`), because the per-criterion marks are not
//     in the rows the page loaded. The page's filter travels as parameters
//     (`criteriaExportParams`), so the file covers the same recordings,
//     student and dates the page is showing.
//
// Like the CSV behind the workspace's score sheet, the numbers are raw — a
// score is `total` and `max` and a percentage is derived — whatever the
// account's score-display preference: a spreadsheet wants numbers, not "82%".

import { apiBlob } from './apiFetch.js';
import { rootSessionOf, presetRange } from './analyticsFilters.js';
import { rowsToCsv, toSafeDownloadName } from './download.js';
import { percentOf } from './scoreDisplay.js';

// Excel reads a CSV without a byte-order mark as the machine's legacy code
// page, which garbles a student called Zoë. The server's export starts with
// the same mark.
export const CSV_BOM = '﻿';

export const RESULTS_CSV_HEADER = Object.freeze([
  'Recording',
  'Recording date',
  'Scored session',
  'Student',
  'Result type',
  'Score',
  'Max',
  'Percent',
  'Pass/fail',
  'Date',
]);

/** The per-criterion layouts `GET /api/analytics/export?format=` accepts. */
export const EXPORT_LAYOUTS = Object.freeze([
  { value: 'long', label: 'Long list' },
  { value: 'wide', label: 'Mark-sheet grid' },
]);

export const DEFAULT_EXPORT_LAYOUT = EXPORT_LAYOUTS[0].value;

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

/** `YYYY-MM-DD` of an ISO timestamp, '' when there is none. */
function dayOf(value) {
  return value ? String(value).slice(0, 10) : '';
}

/** The UTC calendar day of an instant — the server counts days in UTC. */
function utcDay(date) {
  return date.toISOString().slice(0, 10);
}

function resultTypeLabel(resultType) {
  const text = String(resultType || '').replace(/_/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// One decimal is what the page shows its tiles at; it also keeps 2/3 from
// arriving in the sheet as 66.66666666666667.
function roundedPercent(total, max) {
  const percent = percentOf(total, max);
  return percent === null ? null : Math.round(percent * 10) / 10;
}

/** One CSV line's cells for a result row, in `RESULTS_CSV_HEADER` order. */
function resultCells(row) {
  const recording = rootSessionOf(row);
  return [
    recording.name,
    dayOf(recording.createdAt),
    row.sessionName || row.sessionId,
    row.studentName,
    resultTypeLabel(row.resultType),
    row.scoreTotal,
    row.scoreMax,
    roundedPercent(row.scoreTotal, row.scoreMax),
    row.passFail,
    dayOf(row.createdAt),
  ];
}

/**
 * The CSV of the rows the Analytics page shows: the header, then one line per
 * result, behind a UTF-8 byte-order mark. A missing score, maximum or
 * percentage is an empty cell — never 0, which would read as a mark.
 */
export function resultRowsToCsv(rows) {
  return CSV_BOM + rowsToCsv([RESULTS_CSV_HEADER, ...rows.map(resultCells)]);
}

export function resultsFileName(now = new Date()) {
  return `${toSafeDownloadName(`osce-results-${utcDay(now)}`, 'osce-results')}.csv`;
}

export function criteriaFileName(now = new Date()) {
  return `${toSafeDownloadName(`osce-criteria-${utcDay(now)}`, 'osce-criteria')}.csv`;
}

/** What the data table's download is called when a person has nothing to download. */
export function describeExportBlocker(rowCount) {
  return rowCount > 0 ? '' : 'No rows match this filter, so there is nothing to download.';
}

/**
 * The page's filter as `GET /api/analytics/export` parameters.
 *
 * The page filters over recordings (`sessionIds` on the filter are recording
 * ids), one student and a date window, and the server expresses exactly
 * those: `rootSessionIds`, `studentIds`, `from`, `to`. A preset window
 * ("Last 7 days") is sent as the UTC day it starts on, so the file can include
 * up to a day more than the page's instant-precise window — the server only
 * counts whole days. A custom range sends its two dates as typed, either end
 * optional; a malformed one is left out rather than failing the download.
 */
export function criteriaExportParams(filter, { layout = DEFAULT_EXPORT_LAYOUT, now = new Date() } = {}) {
  const params = new URLSearchParams();
  params.set('format', layout);
  (filter.sessionIds || []).forEach((id) => params.append('rootSessionIds', id));
  if (filter.studentId) params.append('studentIds', filter.studentId);

  if (filter.preset === 'custom') {
    if (ISO_DAY.test(filter.dateFrom || '')) params.set('from', filter.dateFrom);
    if (ISO_DAY.test(filter.dateTo || '')) params.set('to', filter.dateTo);
  } else {
    const start = presetRange(filter.preset, now);
    if (start) params.set('from', utcDay(start));
  }
  return params;
}

/**
 * Ask the server for the per-criterion CSV and return `{ blob, fileName }`.
 * A refusal — most usefully the 422 "Narrow the filter" — is thrown as an
 * `ApiError` whose message is the server's own words.
 */
export async function requestCriteriaExport(filter, { layout, now = new Date(), fetchImpl } = {}) {
  const blob = await apiBlob(`/api/analytics/export?${criteriaExportParams(filter, { layout, now })}`, {
    fetchImpl,
    fallbackMessage: 'Failed to download the marks.',
  });
  return { blob, fileName: criteriaFileName(now) };
}
