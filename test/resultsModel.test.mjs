// The results model: what the workspace tabs and the downloaded CSV both read.
//
// These derivations used to be four useMemo bodies inside the 5,800-line
// dashboard component, which made them untestable and put them in the entry
// chunk. They are pure functions of the score sheets now, so the behaviour that
// actually matters to an examiner — how a Pass/Fail is reached when the model
// did not write a summary, and what happens to a field it left blank — can be
// pinned down here.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  buildCommunicationCriteria,
  buildCommunicationCsvRows,
  buildCommunicationSummary,
  buildContentCriteria,
  buildKeepStartStop,
  buildScoringSummary,
  communicationScale,
} from '../src/lib/resultsModel.js';

test('content criteria normalise the model\'s Yes/No and critical flags', () => {
  const criteria = buildContentCriteria({
    criteria: [
      { label: 'Washes hands', value: 'YES', is_critical: 'true', reason: 'At the start.' },
      { label: 'Confirms allergies', value: 'maybe', is_critical: false },
    ],
  });

  assert.equal(criteria.length, 2);
  assert.equal(criteria[0].value, 'Yes');
  assert.equal(criteria[0].isCritical, true, 'the string "true" is how some models spell the flag');
  // Anything that is not a Yes is a No: an assessment never scores on a maybe.
  assert.equal(criteria[1].value, 'No');
  assert.equal(criteria[1].isCritical, false);
});

test('an evidence timestamp becomes a label and a seekable second', () => {
  const [hms, ms, none] = buildContentCriteria({
    criteria: [
      { label: 'a', value: 'Yes', timestamp: '01:02:03' },
      { label: 'b', value: 'Yes', evidence_timestamp: '2:30' },
      { label: 'c', value: 'Yes' },
    ],
  });

  assert.equal(hms.timestampSeconds, 3723);
  assert.equal(hms.timestamp, '01:02:03');
  assert.equal(ms.timestampSeconds, 150);
  assert.equal(none.timestampSeconds, null, 'no timestamp must not seek the player to 0');
  assert.equal(none.timestamp, '');
});

test('no sheet at all yields no criteria rather than throwing', () => {
  assert.deepEqual(buildContentCriteria(null), []);
  assert.deepEqual(buildContentCriteria({ criteria: 'not a list' }), []);
});

test('the sheet\'s own scoring summary is used verbatim when it has one', () => {
  const summary = buildScoringSummary(
    {
      scoring_summary: {
        pass_fail: 'Pass',
        total_criteria: 10,
        yes_count: 9,
        no_count: 1,
        critical_total: 3,
        critical_yes: 3,
        critical_no: 0,
        decision_reason: 'All critical criteria met.',
      },
    },
    [],
  );

  assert.equal(summary.passFail, 'Pass');
  assert.equal(summary.yesCount, 9);
  assert.equal(summary.decisionReason, 'All critical criteria met.');
});

test('with no summary the rubric rule is recomputed: a critical No fails', () => {
  const criteria = buildContentCriteria({
    criteria: [
      { label: 'a', value: 'Yes', is_critical: true },
      { label: 'b', value: 'No', is_critical: true },
      { label: 'c', value: 'Yes' },
      { label: 'd', value: 'Yes' },
    ],
  });
  const summary = buildScoringSummary({}, criteria);

  assert.equal(summary.passFail, 'Fail');
  assert.equal(summary.criticalNo, 1);
  assert.match(summary.decisionReason, /critical/i);
});

test('with no summary, fewer than half Yes also fails', () => {
  const criteria = buildContentCriteria({
    criteria: [
      { label: 'a', value: 'Yes' },
      { label: 'b', value: 'No' },
      { label: 'c', value: 'No' },
    ],
  });
  const summary = buildScoringSummary({}, criteria);

  assert.equal(summary.passFail, 'Fail');
  assert.equal(summary.yesCount, 1);
  assert.match(summary.decisionReason, /half/i);
});

test('an empty sheet has no summary to show', () => {
  assert.equal(buildScoringSummary({}, []), null);
});

test('communication criteria fall back to the points their label is worth', () => {
  const criteria = buildCommunicationCriteria({
    criteria: [
      { id: 1, label: 'Opens the consultation', score_label: 'Most' },
      { id: 2, label: 'Signposts', score_label: 'nonsense' },
      { id: 3, label: 'Summarises', score_label: 'All', points: 3 },
    ],
  });

  assert.equal(criteria[0].points, 2, 'Most is worth 2 when the model omits points');
  assert.equal(criteria[1].scoreLabel, 'None', 'an unrecognised label scores nothing, never silently the max');
  assert.equal(criteria[1].points, 0);
  assert.equal(criteria[2].points, 3);
});

test('the communication summary is recomputed when the payload carries none', () => {
  const criteria = buildCommunicationCriteria({
    criteria: Array.from({ length: 7 }, (_, index) => ({
      id: index + 1,
      label: `Criterion ${index + 1}`,
      score_label: 'Most',
    })),
  });
  const summary = buildCommunicationSummary({}, criteria);

  assert.equal(summary.totalScore, 14);
  assert.equal(summary.maxScore, 21);
  // The 7-criterion communication rubric has its own published threshold.
  assert.equal(summary.passThreshold, 11);
  assert.equal(summary.passFail, 'Pass');
});

test('no communication criteria means no summary, not a zero-score Fail', () => {
  assert.equal(buildCommunicationSummary(null, []), null);
});

test('Keep/Start/Stop distinguishes "no block" from "the model left it blank"', () => {
  assert.equal(buildKeepStartStop({}), null, 'a sheet with no block has nothing to render');
  assert.deepEqual(buildKeepStartStop({ keep_start_stop: { keep: ' Good rapport ' } }), {
    keep: 'Good rapport',
    start: '',
    stop: '',
  });
});

const CUSTOM_SCALE = { All: 4, Most: 2, Some: 1, None: 0 };

test('a custom scoring scale supplies the fallback points and the per-criterion maximum', () => {
  const criteria = buildCommunicationCriteria({
    scoring_scale: CUSTOM_SCALE,
    criteria: [
      { id: 1, label: 'A', score_label: 'All' },
      { id: 2, label: 'B', score_label: 'Some', points: 1 },
    ],
  });
  assert.equal(criteria[0].points, 4);
  assert.equal(criteria[0].maxPoints, 4);
  assert.equal(criteria[1].points, 1);
});

test('the communication summary fallback honours a custom scale', () => {
  const payload = { scoring_scale: CUSTOM_SCALE };
  const criteria = buildCommunicationCriteria({
    ...payload,
    criteria: Array.from({ length: 7 }, (_, i) => ({ id: i + 1, label: `C${i}`, score_label: 'All' })),
  });
  const summary = buildCommunicationSummary(payload, criteria);
  assert.equal(summary.maxScore, 28);
  assert.equal(summary.totalScore, 28);
  assert.equal(summary.passThreshold, 14, '7 criteria but not 21 marks: ceil(max / 2)');
});

test('communicationScale rejects garbage and keeps the default for the default case', () => {
  const dflt = { All: 3, Most: 2, Some: 1, None: 0 };
  assert.deepEqual(communicationScale({}), dflt);
  assert.deepEqual(communicationScale(null), dflt);
  assert.deepEqual(communicationScale({ scoring_scale: 'x' }), dflt);
  assert.deepEqual(communicationScale({ scoring_scale: { All: 4, Most: 'a', Some: 1, None: 0 } }), dflt);
  assert.deepEqual(communicationScale({ scoring_scale: { All: 4, Most: 2, Some: -1, None: 0 } }), dflt);
  assert.deepEqual(communicationScale({ scoring_scale: { All: 4, Most: 2, Some: 1 } }), dflt);
  assert.deepEqual(communicationScale({ scoring_scale: CUSTOM_SCALE }), CUSTOM_SCALE);
  assert.equal(buildCommunicationCriteria({ criteria: [{ score_label: 'All' }] })[0].maxPoints, 3);
});

// --- the CSV reads what the tab reads -----------------------------------------

function csvFor(payload) {
  const criteria = buildCommunicationCriteria(payload);
  const summary = buildCommunicationSummary(payload, criteria);
  const rows = buildCommunicationCsvRows({ sessionId: 's1', payload, criteria, summary });
  const at = (title) => rows.findIndex((row) => row[0] === title);
  const marksOf = rows
    .slice(at('Communication Criteria') + 2)
    .filter((row) => row.length === 6 && typeof row[4] === 'number');
  return { rows, criteria, summary, marksOf, at };
}

test('under the default scale the CSV keeps the 3/2/1/0 scale table and marks', () => {
  const { rows, marksOf, at } = csvFor({
    criteria: [
      { id: 1, label: 'A', score_label: 'All' },
      { id: 2, label: 'B', score_label: 'Most' },
      { id: 3, label: 'C', score_label: 'Some' },
      { id: 4, label: 'D', score_label: 'None' },
    ],
  });
  const scaleRows = rows.slice(at('Label'), at('Label') + 5).map((row) => row.slice(0, 2));
  assert.deepEqual(scaleRows, [['Label', 'Marks'], ['All', 3], ['Most', 2], ['Some', 1], ['None', 0]]);
  assert.deepEqual(marksOf.map((row) => row[4]), [3, 2, 1, 0]);
});

test('under an edited scale the CSV marks and scale table equal the tab', () => {
  const payload = {
    scoring_scale: { All: 4, Most: 3, Some: 1, None: 0 },
    criteria: [
      { id: 1, label: 'A', score_label: 'All' },
      { id: 2, label: 'B', score_label: 'Most' },
      { id: 3, label: 'C', score_label: 'Some', points: 1 },
    ],
  };
  const { rows, criteria, marksOf, at } = csvFor(payload);
  assert.deepEqual(marksOf.map((row) => row[4]), criteria.map((criterion) => criterion.points));
  assert.deepEqual(marksOf.map((row) => row[4]), [4, 3, 1]);
  const scaleRows = rows.slice(at('Label') + 1, at('Label') + 5).map((row) => row.slice(0, 2));
  assert.deepEqual(scaleRows, [['All', 4], ['Most', 3], ['Some', 1], ['None', 0]]);
});

test('an unknown label scores zero in the CSV, as it does in the tab', () => {
  const { criteria, marksOf } = csvFor({
    scoring_scale: { All: 4, Most: 3, Some: 1, None: 0 },
    criteria: [{ id: 1, label: 'A', score_label: 'Partially' }],
  });
  assert.equal(criteria[0].points, 0);
  assert.equal(marksOf[0][3], 'None');
  assert.equal(marksOf[0][4], 0);
});

test('the CSV summary row reports the sheet\'s own max and pass mark', () => {
  const { rows, at } = csvFor({
    scoring_scale: { All: 4, Most: 3, Some: 1, None: 0 },
    scoring_summary: { total_score: 9, max_score: 12, pass_threshold: 7, pass_fail: 'Pass', label_counts: { All: 1, Most: 1, Some: 1, None: 0 } },
    criteria: [
      { id: 1, label: 'A', score_label: 'All' },
      { id: 2, label: 'B', score_label: 'Most' },
      { id: 3, label: 'C', score_label: 'Some' },
    ],
  });
  assert.deepEqual(rows[at('Result') + 1], ['Pass', 9, 12, 7, '1 / 1 / 1 / 0']);
});
