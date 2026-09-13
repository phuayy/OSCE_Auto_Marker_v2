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
  buildCommunicationSummary,
  buildContentCriteria,
  buildKeepStartStop,
  buildScoringSummary,
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
