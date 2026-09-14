// A session's status reads as a word, in one tone, everywhere.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import { SessionStatus } from '../src/lib/enums.js';
import { describeSessionStatus, sessionStatusLabel } from '../src/lib/sessionStatus.js';

test('every status the backend can write has a label and a tone', () => {
  for (const status of Object.values(SessionStatus)) {
    const described = describeSessionStatus(status);
    assert.notEqual(described.label, 'Unknown', `${status} should be named`);
    assert.notEqual(described.label, status, `${status} should not be shown as its machine value`);
    assert.ok(['neutral', 'info', 'accent', 'success', 'warning', 'danger'].includes(described.tone));
  }
});

test('the states a user waits on are the busy ones', () => {
  for (const status of [SessionStatus.QUEUED, SessionStatus.PROCESSING, SessionStatus.ASSEMBLING]) {
    assert.equal(describeSessionStatus(status).busy, true, status);
  }
  for (const status of [SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CROPPED, SessionStatus.UPLOADED]) {
    assert.equal(describeSessionStatus(status).busy, false, status);
  }
});

test('terminal outcomes take the tones the rest of the app uses for them', () => {
  assert.equal(describeSessionStatus(SessionStatus.COMPLETED).tone, 'success');
  assert.equal(describeSessionStatus(SessionStatus.FAILED).tone, 'danger');
  assert.equal(describeSessionStatus(SessionStatus.CROPPED).tone, 'accent');
});

test('the job vocabulary that leaks into older rows reads as completed', () => {
  assert.equal(sessionStatusLabel('succeeded'), 'Completed');
});

test('an unknown status is still readable rather than a machine value', () => {
  assert.equal(sessionStatusLabel('some_new_state'), 'Some new state');
  assert.equal(sessionStatusLabel(''), 'Unknown');
  assert.equal(sessionStatusLabel(undefined), 'Unknown');
});
