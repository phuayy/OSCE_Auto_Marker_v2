// What the account-administration screen derives from the API's user rows.
//
// Pure, so the rules an administrator relies on — which buttons a row offers,
// why one is missing, what an invitation's state means — are pinned by
// test/userAdmin.test.mjs rather than discovered by clicking. The API enforces
// every rule again (last admin, own account, wrong state); this module only
// keeps the screen from offering what the server would refuse.
import { UserRole, UserStatus } from './enums.js';

// The client-side mirror of the server's password policy, so a form can say
// what is wrong before the round trip. The server's answer wins if they differ.
export const PASSWORD_MIN_LENGTH = 10;
export const PASSWORD_MAX_BYTES = 72;

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** "in 2 days" / "3 hours ago" style wording for a timestamp; '' when absent. */
export function relativeTime(iso, now = Date.now()) {
  if (!iso) return '';
  const at = new Date(iso).getTime();
  if (!Number.isFinite(at)) return '';
  const delta = at - now;
  const abs = Math.abs(delta);
  let amount;
  let unit;
  if (abs < MINUTE) {
    return delta >= 0 ? 'in a moment' : 'just now';
  }
  if (abs < HOUR) {
    amount = Math.round(abs / MINUTE);
    unit = 'minute';
  } else if (abs < DAY) {
    amount = Math.round(abs / HOUR);
    unit = 'hour';
  } else {
    amount = Math.round(abs / DAY);
    unit = 'day';
  }
  const label = `${amount} ${unit}${amount === 1 ? '' : 's'}`;
  return delta >= 0 ? `in ${label}` : `${label} ago`;
}

export function countActiveAdmins(users) {
  return (Array.isArray(users) ? users : []).filter(
    (user) => user?.role === UserRole.ADMIN && user?.status === UserStatus.ACTIVE,
  ).length;
}

/**
 * The status chip and the line under it.
 *
 * `tone` is a Badge tone (lib vocabulary: success / warning / danger /
 * neutral); the screen never chooses a colour itself.
 */
export function describeUserStatus(user, now = Date.now()) {
  const status = user?.status;
  if (status === UserStatus.INVITED) {
    const invite = user?.invite;
    if (!invite) {
      return { label: 'Invited', tone: 'warning', detail: 'No invitation sent yet' };
    }
    const expired = Boolean(invite.expired) || (invite.expiresAt && new Date(invite.expiresAt).getTime() <= now);
    if (expired) {
      return { label: 'Invite expired', tone: 'danger', detail: 'Send a new invitation' };
    }
    return { label: 'Invited', tone: 'warning', detail: `Invitation expires ${relativeTime(invite.expiresAt, now)}` };
  }
  if (status === UserStatus.DISABLED) {
    return { label: 'Disabled', tone: 'neutral', detail: 'Cannot sign in' };
  }
  if (status === UserStatus.ACTIVE) {
    const lastLogin = user?.lastLoginAt;
    return {
      label: 'Active',
      tone: 'success',
      detail: lastLogin ? `Last signed in ${relativeTime(lastLogin, now)}` : 'Has not signed in yet',
    };
  }
  return { label: String(status || 'Unknown'), tone: 'neutral', detail: '' };
}

/**
 * Which actions a row may offer, and why the others are withheld.
 *
 * `meId` is the signed-in administrator; `activeAdminCount` comes from the
 * same listing (countActiveAdmins) so the last-admin rule is judged on what
 * the screen is showing.
 */
export function rowActions(user, { meId = '', activeAdminCount = 0 } = {}) {
  const isSelf = Boolean(meId) && user?.id === meId;
  const isActiveAdmin = user?.role === UserRole.ADMIN && user?.status === UserStatus.ACTIVE;
  const isLastAdmin = isActiveAdmin && activeAdminCount <= 1;
  const invited = user?.status === UserStatus.INVITED;
  const active = user?.status === UserStatus.ACTIVE;
  const disabled = user?.status === UserStatus.DISABLED;

  const lockedReason = isLastAdmin
    ? 'This is the last active administrator. Promote someone else first.'
    : isSelf
      ? 'You cannot change your own access. Ask another administrator.'
      : '';

  return {
    isSelf,
    isLastAdmin,
    canResendInvite: invited,
    canEnable: disabled,
    canDisable: active && !isSelf && !isLastAdmin,
    canSendReset: active && Boolean(user?.email),
    canDelete: !isSelf && !isLastAdmin,
    canChangeRole: !isSelf && !isLastAdmin,
    lockedReason,
  };
}

/** Everything wrong with a password form before it is sent; [] when fine. */
export function passwordProblems({ password = '', confirm = '', identifiers = [] } = {}) {
  const problems = [];
  const text = String(password || '');
  if (text.length < PASSWORD_MIN_LENGTH) {
    problems.push(`Use at least ${PASSWORD_MIN_LENGTH} characters.`);
  }
  if (new TextEncoder().encode(text).length > PASSWORD_MAX_BYTES) {
    problems.push(`Use at most ${PASSWORD_MAX_BYTES} bytes.`);
  }
  const lowered = text.trim().toLowerCase();
  if (lowered && identifiers.some((value) => String(value || '').trim().toLowerCase() === lowered)) {
    problems.push('The password must not be your username or email address.');
  }
  if (confirm !== undefined && confirm !== null && String(confirm) !== text) {
    problems.push('The two passwords do not match.');
  }
  return problems;
}

/**
 * The banner after an invitation or reset was requested: was it sent, did
 * it fail, is there a link to hand over by other means.
 *
 * `outcome` is the API response ({mailSent, mailError, inviteLink|resetLink});
 * `mail` is the listing's `mail` block ({configured, inviteLinkVisible}).
 */
export function describeDelivery(outcome, mail = {}, { kind = 'invitation', email = '' } = {}) {
  const link = outcome?.inviteLink || outcome?.resetLink || '';
  const target = email ? ` to ${email}` : '';
  if (outcome?.mailSent) {
    return { tone: 'success', title: `${capitalise(kind)} email sent${target}.`, detail: '', link };
  }
  if (outcome?.mailError) {
    return {
      tone: 'danger',
      title: `The ${kind} email could not be sent${target}.`,
      detail: link ? `${outcome.mailError} Copy the link below and hand it over another way.` : outcome.mailError,
      link,
    };
  }
  if (link) {
    return {
      tone: 'warning',
      title: `No mail server is configured, so the ${kind} was not emailed.`,
      detail: 'Copy this link and send it to them yourself. It works once.',
      link,
    };
  }
  if (mail && mail.configured === false) {
    return {
      tone: 'warning',
      title: `No mail server is configured, so the ${kind} was not emailed.`,
      detail: 'The link was written to the server log.',
      link: '',
    };
  }
  return { tone: 'neutral', title: `${capitalise(kind)} recorded.`, detail: '', link };
}

function capitalise(text) {
  const value = String(text || '');
  return value ? value[0].toUpperCase() + value.slice(1) : value;
}
