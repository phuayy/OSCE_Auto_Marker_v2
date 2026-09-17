// Hash routes, including the ones an emailed link opens before sign-in.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import { ADMIN_VIEWS, PUBLIC_VIEWS, buildRoute, isAdminView, isPublicView, parseRoute } from '../src/lib/navigation.js';

test('the dashboard and session routes parse as before', () => {
  assert.deepEqual(parseRoute(''), { view: 'dashboard', sessionId: null, token: null });
  assert.deepEqual(parseRoute('#/'), { view: 'dashboard', sessionId: null, token: null });
  assert.deepEqual(parseRoute('#/session/abc%20def'), { view: 'dashboard', sessionId: 'abc def', token: null });
  assert.deepEqual(parseRoute('#/settings'), { view: 'settings', sessionId: null, token: null });
  assert.deepEqual(parseRoute('#/analytics'), { view: 'analytics', sessionId: null, token: null });
  assert.deepEqual(parseRoute('#/rubric'), { view: 'rubric', sessionId: null, token: null });
});

test('the account routes parse and build symmetrically', () => {
  assert.deepEqual(parseRoute('#/users'), { view: 'users', sessionId: null, token: null });
  assert.deepEqual(parseRoute('#/account'), { view: 'account', sessionId: null, token: null });
  assert.equal(buildRoute({ view: 'users' }), '#/users');
  assert.equal(buildRoute({ view: 'account' }), '#/account');
});

test('emailed links carry their token in the fragment and round-trip through the encoder', () => {
  // The backend's app/mail/links.py spells the same two paths.
  const token = 'abc-DEF_123~x';
  assert.deepEqual(parseRoute(`#/accept-invite/${token}`), { view: 'acceptInvite', sessionId: null, token });
  assert.deepEqual(parseRoute(`#/reset-password/${token}`), { view: 'resetPassword', sessionId: null, token });
  assert.equal(buildRoute({ view: 'acceptInvite', token }), `#/accept-invite/${encodeURIComponent(token)}`);
  assert.equal(buildRoute({ view: 'resetPassword', token }), `#/reset-password/${encodeURIComponent(token)}`);
  assert.equal(parseRoute(buildRoute({ view: 'acceptInvite', token: 'a/b c' })).token, 'a/b c');
  // A link with no token still lands on the screen, which then explains itself.
  assert.deepEqual(parseRoute('#/accept-invite'), { view: 'acceptInvite', sessionId: null, token: '' });
  assert.equal(buildRoute({ view: 'acceptInvite' }), '#/accept-invite');
});

test('forgot-password is a public route with no token', () => {
  assert.deepEqual(parseRoute('#/forgot-password'), { view: 'forgotPassword', sessionId: null, token: null });
  assert.equal(buildRoute({ view: 'forgotPassword' }), '#/forgot-password');
});

test('public and admin views are classified once, for AppShell to gate on', () => {
  assert.deepEqual([...PUBLIC_VIEWS].sort(), ['acceptInvite', 'forgotPassword', 'resetPassword']);
  assert.deepEqual([...ADMIN_VIEWS], ['users']);
  assert.equal(isPublicView('acceptInvite'), true);
  assert.equal(isPublicView('dashboard'), false);
  assert.equal(isAdminView('users'), true);
  assert.equal(isAdminView('account'), false);
  assert.equal(isAdminView('settings'), false);
});

test('an unknown hash falls back to the dashboard', () => {
  assert.deepEqual(parseRoute('#/nowhere/at/all'), { view: 'dashboard', sessionId: null, token: null });
  assert.equal(buildRoute({ view: 'nowhere' }), '#/');
});
