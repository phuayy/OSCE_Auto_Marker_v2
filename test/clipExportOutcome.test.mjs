// What the editor does when a clip-export job finishes — the difference
// between a whole split and a recrop, which used to be one hard-coded notice
// that moved the user's selection whatever had just happened.

import test from 'node:test';
import assert from 'node:assert/strict';

import { describeClipExportOutcome } from '../src/lib/clipExportOutcome.js';

const CLIPS = [
  { id: 'c1', label: 'Student A', kind: 'session', url: '/media/clips/s1/p1/clip-1.mp4' },
  { id: 'c2', label: 'Student B', kind: 'session', url: '/media/clips/s1/p1/clip-2.mp4' },
  { id: 'i1', label: 'Break', kind: 'intermission' },
];

test('a finished split announces the clips and hands over the first one', () => {
  const outcome = describeClipExportOutcome({
    clipExport: { status: 'completed', scope: 'plan', clipIds: null },
    clips: CLIPS,
    selectedClipId: null,
  });

  assert.equal(outcome.notice, 'Exported 2 clips — ready to assess below.');
  assert.equal(outcome.selectedClipId, 'c1');
  assert.equal(outcome.scrollToAssessments, true);
});

test('a split keeps a selection that is still in the exported list', () => {
  const outcome = describeClipExportOutcome({
    clipExport: { status: 'completed', scope: 'plan' },
    clips: CLIPS,
    selectedClipId: 'c2',
  });

  assert.equal(outcome.selectedClipId, 'c2');
  assert.equal(outcome.scrollToAssessments, true);
});

test('one exported clip reads in the singular', () => {
  const outcome = describeClipExportOutcome({
    clipExport: { status: 'completed', scope: 'plan' },
    clips: [CLIPS[0], CLIPS[2]],
  });

  assert.equal(outcome.notice, 'Exported 1 clip — ready to assess below.');
});

test('a finished recrop names the clip and leaves the view alone', () => {
  const outcome = describeClipExportOutcome({
    clipExport: { status: 'completed', scope: 'clip', clipIds: ['c2'] },
    clips: CLIPS,
    selectedClipId: 'c2',
  });

  assert.equal(outcome.notice, 'Re-cut Student B — ready to assess.');
  assert.equal(outcome.selectedClipId, 'c2', 'a recrop moved the user’s selection');
  assert.equal(outcome.scrollToAssessments, false, 'a recrop scrolled the page away');
});

test('a recrop with nothing selected selects the clip it re-cut', () => {
  const outcome = describeClipExportOutcome({
    clipExport: { status: 'completed', scope: 'clip', clipIds: ['c1'] },
    clips: CLIPS,
    selectedClipId: null,
  });

  assert.equal(outcome.selectedClipId, 'c1');
});

test('a recrop whose clip is still a draft announces nothing yet', () => {
  const outcome = describeClipExportOutcome({
    clipExport: { status: 'completed', scope: 'clip', clipIds: ['c3'] },
    clips: [...CLIPS, { id: 'c3', label: 'Student C', kind: 'session', isDraft: true }],
    selectedClipId: 'c3',
  });

  assert.equal(outcome.notice, '');
  assert.equal(outcome.selectedClipId, 'c3');
});

test('a failed export announces nothing and changes nothing', () => {
  const outcome = describeClipExportOutcome({
    clipExport: { status: 'failed', scope: 'plan', error: 'ffmpeg exploded' },
    clips: CLIPS,
    selectedClipId: 'c2',
  });

  assert.deepEqual(outcome, { notice: '', selectedClipId: 'c2', scrollToAssessments: false });
});

test('an export that produced no playable clip announces nothing', () => {
  const outcome = describeClipExportOutcome({
    clipExport: { status: 'completed', scope: 'plan' },
    clips: [{ id: 'c1', label: 'Student A', kind: 'session', isDraft: true }],
  });

  assert.equal(outcome.notice, '');
  assert.equal(outcome.scrollToAssessments, false);
});

test('a missing record is safe', () => {
  assert.deepEqual(describeClipExportOutcome(), {
    notice: '',
    selectedClipId: null,
    scrollToAssessments: false,
  });
});
