// What the account-administration screen derives from the API's rows.
//
// The API enforces every rule again; these pin that the screen never offers a
// button the server would refuse, and explains why when it withholds one.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import { UserRole, UserStatus } from '../src/lib/enums.js';
import { canManageUsers, identityLabel, isAdminRole, roleLabel } from '../src/lib/authz.js';
import {
  countActiveAdmins,
  describeDelivery,
  describeUserStatus,
  passwordProblems,
  relativeTime,
  rowActions,
} from '../src/lib/userAdmin.js';

const NOW = Date.parse('2026-09-17T10:00:00Z');
const hours = (n) => new Date(NOW + n * 3_600_000).toISOString();

const admin = { id: 'a1', username: 'admin', role: UserRole.ADMIN, status: UserStatus.ACTIVE, email: '' };
const marker = { id: 'm1', username: 'm@x.edu', email: 'm@x.edu', role: UserRole.MARKER, status: UserStatus.ACTIVE, lastLoginAt: hours(-3) };
const invited = { id: 'i1', username: 'i@x.edu', email: 'i@x.edu', role: UserRole.MARKER, status: UserStatus.INVITED, invite: { expiresAt: hours(48), expired: false } };
const disabled = { id: 'd1', username: 'd@x.edu', email: 'd@x.edu', role: UserRole.MARKER, status: UserStatus.DISABLED };

// --- authz --------------------------------------------------------------------

test('only the admin role manages users', () => {
  assert.equal(isAdminRole('admin'), true);
  assert.equal(isAdminRole('marker'), false);
  assert.equal(isAdminRole(undefined), false);
  assert.equal(canManageUsers({ role: UserRole.ADMIN }), true);
  assert.equal(canManageUsers({ role: UserRole.MARKER }), false);
  assert.equal(canManageUsers(null), false);
  assert.equal(roleLabel(UserRole.ADMIN), 'Admin');
  assert.equal(roleLabel('anything-else'), 'Marker');
});

test('the header shows the display name, falling back to the username', () => {
  assert.equal(identityLabel({ displayName: 'Dr M', username: 'm@x.edu' }), 'Dr M');
  assert.equal(identityLabel({ displayName: '  ', username: 'm@x.edu' }), 'm@x.edu');
  assert.equal(identityLabel(null), '');
});

// --- status chips ---------------------------------------------------------------

test('each status gets a label, a badge tone and a detail line', () => {
  const active = describeUserStatus(marker, NOW);
  assert.equal(active.label, 'Active');
  assert.equal(active.tone, 'success');
  assert.equal(active.detail, 'Last signed in 3 hours ago');

  assert.equal(describeUserStatus({ ...marker, lastLoginAt: '' }, NOW).detail, 'Has not signed in yet');

  const pending = describeUserStatus(invited, NOW);
  assert.equal(pending.label, 'Invited');
  assert.equal(pending.tone, 'warning');
  assert.equal(pending.detail, 'Invitation expires in 2 days');

  const stale = describeUserStatus({ ...invited, invite: { expiresAt: hours(-1), expired: true } }, NOW);
  assert.equal(stale.label, 'Invite expired');
  assert.equal(stale.tone, 'danger');

  assert.equal(describeUserStatus({ ...invited, invite: null }, NOW).detail, 'No invitation sent yet');
  assert.equal(describeUserStatus(disabled, NOW).tone, 'neutral');
});

test('relative time reads in both directions', () => {
  assert.equal(relativeTime(hours(0.5), NOW), 'in 30 minutes');
  assert.equal(relativeTime(hours(-1), NOW), '1 hour ago');
  assert.equal(relativeTime(hours(72), NOW), 'in 3 days');
  assert.equal(relativeTime('', NOW), '');
  assert.equal(relativeTime('not a date', NOW), '');
});

// --- row actions ----------------------------------------------------------------

test('an active marker offers disable, reset and delete; an invited one offers resend', () => {
  const context = { meId: admin.id, activeAdminCount: 1 };
  const active = rowActions(marker, context);
  assert.deepEqual(
    [active.canDisable, active.canEnable, active.canSendReset, active.canDelete, active.canResendInvite, active.canChangeRole],
    [true, false, true, true, false, true],
  );
  const pending = rowActions(invited, context);
  assert.deepEqual([pending.canResendInvite, pending.canDisable, pending.canSendReset, pending.canDelete], [true, false, false, true]);
  const off = rowActions(disabled, context);
  assert.deepEqual([off.canEnable, off.canDisable, off.canSendReset], [true, false, false]);
});

test('the last active admin and your own row are locked, with the reason shown', () => {
  const sole = rowActions(admin, { meId: 'someone-else', activeAdminCount: 1 });
  assert.equal(sole.isLastAdmin, true);
  assert.deepEqual([sole.canDisable, sole.canDelete, sole.canChangeRole], [false, false, false]);
  assert.match(sole.lockedReason, /last active administrator/);

  const self = rowActions(admin, { meId: admin.id, activeAdminCount: 2 });
  assert.equal(self.isSelf, true);
  assert.equal(self.isLastAdmin, false);
  assert.deepEqual([self.canDisable, self.canDelete, self.canChangeRole], [false, false, false]);
  assert.match(self.lockedReason, /your own access/);

  // With a second admin, another admin's row is fully actionable.
  const peer = rowActions({ ...admin, id: 'a2' }, { meId: admin.id, activeAdminCount: 2 });
  assert.deepEqual([peer.canDisable, peer.canDelete, peer.canChangeRole, peer.lockedReason], [true, true, true, '']);
});

test('active admins are counted from the listing', () => {
  assert.equal(countActiveAdmins([admin, marker, invited, { ...admin, id: 'a2', status: UserStatus.DISABLED }]), 1);
  assert.equal(countActiveAdmins([admin, { ...admin, id: 'a2' }]), 2);
  assert.equal(countActiveAdmins(undefined), 0);
});

// --- password form ----------------------------------------------------------------

test('the client-side password check mirrors the server policy', () => {
  assert.deepEqual(passwordProblems({ password: 'correct horse battery', confirm: 'correct horse battery' }), []);
  assert.match(passwordProblems({ password: 'short', confirm: 'short' })[0], /at least 10/);
  assert.match(passwordProblems({ password: 'long enough here', confirm: 'different' }).at(-1), /do not match/);
  assert.match(passwordProblems({ password: 'm@x.edu', confirm: 'm@x.edu', identifiers: ['m@x.edu'] }).join(' '), /username or email/);
  assert.match(passwordProblems({ password: 'é'.repeat(40), confirm: 'é'.repeat(40) }).join(' '), /at most 72/);
});

// --- delivery banner ----------------------------------------------------------------

test('the banner says sent, failed, or copy-this-link', () => {
  const sent = describeDelivery({ mailSent: true }, { configured: true }, { email: 'm@x.edu' });
  assert.equal(sent.tone, 'success');
  assert.equal(sent.title, 'Invitation email sent to m@x.edu.');

  const failed = describeDelivery({ mailSent: false, mailError: '550 no such mailbox' }, { configured: true }, { email: 'm@x.edu' });
  assert.equal(failed.tone, 'danger');
  assert.equal(failed.detail, '550 no such mailbox');

  const failedWithLink = describeDelivery({ mailSent: false, mailError: 'relay down', inviteLink: 'https://x/#/accept-invite/t' }, {});
  assert.equal(failedWithLink.link, 'https://x/#/accept-invite/t');
  assert.match(failedWithLink.detail, /Copy the link below/);

  const unconfigured = describeDelivery({ mailSent: false, mailError: '', inviteLink: 'https://x/#/accept-invite/t' }, { configured: false });
  assert.equal(unconfigured.tone, 'warning');
  assert.equal(unconfigured.link, 'https://x/#/accept-invite/t');

  const logged = describeDelivery({ mailSent: false, mailError: '' }, { configured: false }, { kind: 'password reset' });
  assert.match(logged.title, /password reset was not emailed/);
  assert.match(logged.detail, /server log/);

  const reset = describeDelivery({ mailSent: true, resetLink: undefined }, { configured: true }, { kind: 'password reset', email: 'm@x.edu' });
  assert.equal(reset.title, 'Password reset email sent to m@x.edu.');
});
