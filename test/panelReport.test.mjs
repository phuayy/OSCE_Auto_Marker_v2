// Unit tests for reading a panel-marked sheet's `panel` block.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  describeAgreement,
  describeResolution,
  markerInitial,
  markerLabel,
  panelCsvColumns,
  panelReport,
} from '../src/lib/panelReport.js';

const PANEL_SHEET = {
  criteria: [{ label: 'a' }, { label: 'b' }, { label: 'c' }],
  panel: {
    schema: 'content-panel-v1',
    markers: [
      { key: 'nvidia__nemotron', provider_id: 'nvidia', model: 'nemotron', summary: { pass_fail: 'Pass', yes_count: 3 } },
      { key: 'gemini__pro', provider_id: 'gemini', model: 'pro', summary: { pass_fail: 'Fail', yes_count: 2 } },
    ],
    adjudicator: { provider_id: 'deepseek', model: 'chat', called: true, ok: true, feedback_source: 'merged' },
    agreement: { total: 3, agreed: 2, disputed: 1, percent: 0.6667, cohen_kappa: 0.4, pass_fail_agreed: false },
    criteria: [
      { index: 0, votes: ['Yes', 'Yes'], reasons: ['r1', 'r2'], timestamps: ['00:00:01', '00:00:02'], resolution: 'agreed' },
      { index: 1, votes: ['Yes', 'No'], reasons: ['r1', 'r2'], timestamps: ['00:00:01', '00:00:00'], resolution: 'adjudicated', sided_with: 1, confidence: 0.8, reason: 'judged' },
      { index: 2, votes: ['No', 'Yes'], reasons: ['r1', 'r2'], timestamps: ['', ''], resolution: 'tie_break:lenient' },
    ],
    tie_break: 'lenient',
    degraded: null,
    warnings: ['one note'],
  },
};

test('a sheet without a panel block yields no report', () => {
  assert.equal(panelReport(null), null);
  assert.equal(panelReport({ criteria: [] }), null);
  assert.equal(panelReport({ panel: 'nope' }), null);
});

test('markers, adjudicator and agreement are read for display', () => {
  const report = panelReport(PANEL_SHEET);
  assert.deepEqual(
    report.markers.map((marker) => [marker.initial, marker.label, marker.passFail, marker.yesCount]),
    [['A', 'nvidia:nemotron', 'Pass', 3], ['B', 'gemini:pro', 'Fail', 2]],
  );
  assert.deepEqual(report.adjudicator, { label: 'deepseek:chat', called: true, ok: true, feedbackSource: 'merged' });
  assert.equal(report.agreementLabel, '2/3 agreed · κ 0.40 · markers split on pass/fail');
  assert.equal(report.tieBreak, 'lenient');
  assert.equal(report.degraded, null);
  assert.deepEqual(report.warnings, ['one note']);
  assert.equal(report.disputedCount, 2);
});

test('each criterion is joined by index with its votes and resolution', () => {
  const report = panelReport(PANEL_SHEET);
  const agreed = report.criteria.get(0);
  assert.equal(agreed.disputed, false);
  assert.equal(agreed.label, 'Markers agreed');
  assert.deepEqual(agreed.votes, ['Yes', 'Yes']);

  const adjudicated = report.criteria.get(1);
  assert.equal(adjudicated.disputed, true);
  assert.equal(adjudicated.label, 'Adjudicated');
  assert.equal(adjudicated.tone, 'attention');
  assert.equal(adjudicated.sidedWith, 1);
  assert.equal(adjudicated.confidence, 0.8);
  assert.equal(adjudicated.reason, 'judged');

  const tieBroken = report.criteria.get(2);
  assert.equal(tieBroken.label, 'Tie-break (lenient)');
  assert.equal(tieBroken.tone, 'warning');
  assert.equal(tieBroken.sidedWith, null);
  assert.equal(report.criteria.get(7), undefined);
});

test('a degraded panel is surfaced with its reason', () => {
  const report = panelReport({
    panel: {
      markers: [{ key: 'gemini__pro', provider_id: 'gemini', model: 'pro' }],
      adjudicator: { called: false },
      agreement: null,
      criteria: [{ index: 0, votes: ['Yes'], resolution: 'sole_marker' }],
      degraded: { reason: 'nvidia died', effective_mode: 'single', marker: 'gemini__pro' },
      warnings: ['Marker nvidia__nemotron failed and was left out: boom'],
    },
  });
  assert.deepEqual(report.degraded, { reason: 'nvidia died', marker: 'gemini__pro' });
  assert.equal(report.agreementLabel, '');
  assert.equal(report.criteria.get(0).label, 'Single marker');
  assert.equal(report.criteria.get(0).disputed, false);
  assert.equal(report.disputedCount, 0);
});

test('labels and helpers', () => {
  assert.equal(markerLabel({ provider_id: 'openai', model: 'gpt' }), 'openai:gpt');
  assert.equal(markerLabel({ providerId: 'openai' }), 'openai');
  assert.equal(markerLabel({ key: 'k' }), 'k');
  assert.equal(markerLabel(null), '');
  assert.equal(markerInitial(0), 'A');
  assert.equal(markerInitial(2), 'C');
  assert.deepEqual(describeResolution('agreed'), { label: 'Markers agreed', tone: 'neutral' });
  assert.deepEqual(describeResolution('tie_break:first_marker'), { label: 'Tie-break (first marker)', tone: 'warning' });
  assert.deepEqual(describeResolution(''), { label: 'Unknown', tone: 'neutral' });
  assert.equal(describeAgreement({ total: 25, agreed: 22 }), '22/25 agreed');
  assert.equal(describeAgreement({ total: 25, agreed: 22, cohen_kappa: null }), '22/25 agreed');
  assert.equal(describeAgreement(null), '');
});

test('the CSV gains a votes column and a decided-by column', () => {
  const report = panelReport(PANEL_SHEET);
  assert.deepEqual(panelCsvColumns(report, 1), ['A: Yes / B: No', 'Adjudicated']);
  assert.deepEqual(panelCsvColumns(report, 9), ['', '']);
  assert.deepEqual(panelCsvColumns(null, 1), []);
});
