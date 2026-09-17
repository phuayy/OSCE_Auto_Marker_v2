// What a signed-in identity may open in the browser.
//
// Two roles only (the backend's UserRole): an administrator is a marker plus
// user management. The API enforces every one of these with a 403; this
// module exists so the *screen* can hide what a click could not do, and so
// that decision is made in one place rather than at every nav button.
import { UserRole } from './enums.js';

export function isAdminRole(role) {
  return String(role || '') === UserRole.ADMIN;
}

/** May this identity open the account-administration screen? */
export function canManageUsers(identity) {
  return isAdminRole(identity?.role);
}

/** The label shown in the header pill and on the Account page. */
export function identityLabel(identity) {
  const name = String(identity?.displayName || '').trim();
  return name || String(identity?.username || '').trim();
}

/** A short human word for a role, for badges. */
export function roleLabel(role) {
  return isAdminRole(role) ? 'Admin' : 'Marker';
}
