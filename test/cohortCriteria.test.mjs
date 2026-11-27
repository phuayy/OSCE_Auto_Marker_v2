// Cohort criteria: criterion ids are renumbered by every rubric save, so the
// chart's rows are merged by id only within one rubric revision.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import { groupCriteriaByRubricRevision } from '../src/lib/cohortCriteria.js';

function summary(revision, number, criteria) {
  return {
    communication: {
      rubricRevision: revision,
      rubricVersionNumber: number,
      perCriterionPoints: criteria.map(([id, label, scoreLabel, points]) => ({
        id,
        label,
        section: null,
        scoreLabel,
        points,
      })),
    },
  };
}

test('clips marked under one revision merge by id into one group, as before', () => {
  const groups = groupCriteriaByRubricRevision([
    summary('r1', 1, [[1, 'Opens', 'All', 3], [2, 'Closes', 'Some', 1]]),
    summary('r1', 1, [[1, 'Opens', 'Most', 2], [2, 'Closes', 'None', 0]]),
  ]);
  assert.equal(groups.length, 1);
  const [group] = groups;
  assert.equal(group.studentCount, 2);
  assert.deepEqual(group.rows.map((row) => row.label), ['Opens', 'Closes']);
  assert.equal(group.rows[0].studentCount, 2);
  assert.equal(group.rows[0].totalPoints, 5);
  assert.deepEqual(group.rows[0].counts, { All: 1, Most: 1, Some: 0, None: 0 });
});

test('criteria renumbered between revisions are not merged by id', () => {
  // Revision 2 dropped "Opens": id 1 is now "Closes".
  const groups = groupCriteriaByRubricRevision([
    summary('r2', 2, [[1, 'Closes', 'All', 3]]),
    summary('r1', 1, [[1, 'Opens', 'None', 0], [2, 'Closes', 'Some', 1]]),
  ]);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups.map((group) => group.versionNumber), [1, 2], 'oldest rubric first');
  const [first, second] = groups;
  assert.deepEqual(first.rows.map((row) => row.label), ['Opens', 'Closes']);
  assert.deepEqual(second.rows.map((row) => row.label), ['Closes']);
  assert.equal(second.rows[0].studentCount, 1, 'revision 2 id 1 did not absorb revision 1 id 1');
  assert.equal(first.rows[0].counts.None, 1);
});

test('the label shown is the one from the group\'s own revision', () => {
  const groups = groupCriteriaByRubricRevision([
    summary('r1', 1, [[1, 'Greets patient', 'All', 3]]),
    summary('r2', 2, [[1, 'Introduces self', 'All', 3]]),
  ]);
  assert.deepEqual(groups.map((group) => group.rows[0].label), ['Greets patient', 'Introduces self']);
});

test('sheets with no recorded revision form one group, before any versioned one', () => {
  const groups = groupCriteriaByRubricRevision([
    summary('r3', 3, [[1, 'A', 'All', 3]]),
    summary(null, null, [[1, 'A', 'Some', 1]]),
    summary(undefined, undefined, [[1, 'A', 'All', 3]]),
  ]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].revision, null);
  assert.equal(groups[0].studentCount, 2);
  assert.equal(groups[1].versionNumber, 3);
});

test('clips without criteria rows and empty input add no group', () => {
  assert.deepEqual(groupCriteriaByRubricRevision(undefined), []);
  assert.deepEqual(groupCriteriaByRubricRevision([{ communication: null }, { communication: { perCriterionPoints: [] } }]), []);
});

test('an unknown label counts as None and a non-numeric points value as zero', () => {
  const [group] = groupCriteriaByRubricRevision([summary('r1', 1, [[1, 'A', 'Partially', 'x']])]);
  assert.deepEqual(group.rows[0].counts, { All: 0, Most: 0, Some: 0, None: 1 });
  assert.equal(group.rows[0].totalPoints, 0);
});

test('each group carries its own per-criterion maximum, defaulting for a legacy sheet', () => {
  const edited = summary('r2', 2, [[1, 'A', 'All', 4]]);
  edited.communication.scoringScale = { All: 4, Most: 3, Some: 1, None: 0 };
  const standard = summary('r1', 1, [[1, 'A', 'All', 3]]);
  standard.communication.scoringScale = { All: 3, Most: 2, Some: 1, None: 0 };
  const legacy = summary(null, null, [[1, 'A', 'All', 3]]);
  const groups = groupCriteriaByRubricRevision([edited, standard, legacy]);
  assert.deepEqual(groups.map((group) => group.maxPoints), [3, 3, 4]);
  assert.equal(groups[2].versionNumber, 2);
});
