// canMutateSession must agree with the server's app.domain.access.may_mutate:
// admin always, the creator always, and a legacy session (no createdBy)
// stays open to anyone — see test_session_ownership.py for the server side.

import assert from 'node:assert/strict';
import test from 'node:test';
import { canManageUsers, canMutateSession, isAdminRole, roleLabel } from '../src/lib/authz.js';

const ADMIN = { userId: 'u-admin', role: 'admin' };
const OWNER = { userId: 'u-1', role: 'marker' };
const OTHER = { userId: 'u-2', role: 'marker' };

const OWNED = { createdBy: { userId: 'u-1', username: 'm@x.edu', displayName: '' } };
const LEGACY = { createdBy: null };

test('an admin may mutate any session', () => {
  assert.equal(canMutateSession(ADMIN, OWNED), true);
});

test('the creator may mutate their own session', () => {
  assert.equal(canMutateSession(OWNER, OWNED), true);
});

test('a different marker may not mutate someone else’s session', () => {
  assert.equal(canMutateSession(OTHER, OWNED), false);
});

test('a session with no recorded creator is open to any signed-in marker', () => {
  assert.equal(canMutateSession(OTHER, LEGACY), true);
  assert.equal(canMutateSession(OTHER, {}), true);
});

test('no identity at all may not mutate an owned session', () => {
  assert.equal(canMutateSession(null, OWNED), false);
  assert.equal(canMutateSession(undefined, OWNED), false);
});

test('isAdminRole/roleLabel/canManageUsers are unaffected', () => {
  assert.equal(isAdminRole('admin'), true);
  assert.equal(isAdminRole('marker'), false);
  assert.equal(roleLabel('admin'), 'Admin');
  assert.equal(roleLabel('marker'), 'Marker');
  assert.equal(canManageUsers(ADMIN), true);
  assert.equal(canManageUsers(OWNER), false);
});
