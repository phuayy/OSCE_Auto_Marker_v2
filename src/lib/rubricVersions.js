// The communication rubric's version history, in words (pure).
//
// Every change to the active rubric — an upload, a reset, a saved AI draft, an
// in-place edit, a restore — is a version on the server; the one in use is
// `active`, every earlier one `retired`. Restoring never rewrites history: it
// records a new version carrying the old rubric (`restoredFromVersion`).

import { RubricChangeKind, RubricVersionStatus } from './enums.js';

export const VERSIONS_ENDPOINT = '/api/communication-rubric/versions';

export function versionEndpoint(versionId) {
  return `${VERSIONS_ENDPOINT}/${encodeURIComponent(String(versionId))}`;
}

export function restoreEndpoint(versionId) {
  return `/api/admin/communication-rubric/versions/${encodeURIComponent(String(versionId))}/restore`;
}

const CHANGE_LABELS = {
  [RubricChangeKind.UPLOAD]: 'Uploaded a PDF',
  [RubricChangeKind.DRAFT]: 'Saved an AI-extracted draft',
  [RubricChangeKind.EDIT]: 'Edited the rubric',
  [RubricChangeKind.RESET]: 'Reset to the bundled default',
  [RubricChangeKind.DEFAULT]: 'Bundled default',
  [RubricChangeKind.ADOPTED]: 'In use before version history',
};

/** What this version changed, as a short phrase. */
export function describeVersionChange(version) {
  if (!version || typeof version !== 'object') return '';
  if (version.changeKind === RubricChangeKind.RESTORE) {
    return Number.isInteger(version.restoredFromVersion)
      ? `Restored version ${version.restoredFromVersion}`
      : 'Restored an earlier version';
  }
  return CHANGE_LABELS[version.changeKind] || 'Changed the rubric';
}

/** Who made the version, or '' for a system change (first boot, adoption). */
export function versionAuthor(version) {
  const by = version?.createdBy;
  if (!by || typeof by !== 'object') return '';
  const name = typeof by.displayName === 'string' && by.displayName.trim() ? by.displayName.trim() : '';
  const username = typeof by.username === 'string' ? by.username.trim() : '';
  return name || username;
}

export function isActiveVersion(version) {
  return version?.status === RubricVersionStatus.ACTIVE;
}

/** Only an administrator, and never onto the version already in use. */
export function canRestoreVersion(version, isAdmin) {
  return Boolean(isAdmin && version?.id && !isActiveVersion(version));
}

/** "7 criteria · max 21 · pass 11", leaving out what the version does not say. */
export function summariseVersion(version) {
  if (!version) return '';
  const parts = [];
  if (Number.isFinite(version.criteriaCount)) {
    parts.push(`${version.criteriaCount} ${version.criteriaCount === 1 ? 'criterion' : 'criteria'}`);
  }
  if (Number.isFinite(version.maxScore)) parts.push(`max ${version.maxScore}`);
  if (Number.isFinite(version.passThreshold)) parts.push(`pass ${version.passThreshold}`);
  return parts.join(' · ');
}

/** The sentence a restore confirmation asks, naming both versions. */
export function restorePrompt(version, activeVersion) {
  if (!version) return '';
  const target = `version ${version.versionNumber}`;
  const current = activeVersion ? `version ${activeVersion.versionNumber}` : 'the current rubric';
  return (
    `Make ${target} the active rubric again? ${current[0].toUpperCase()}${current.slice(1)} is retired ` +
    `and kept in the history, and ${target} is recorded as a new version. ` +
    'New communication scoring runs use it; sessions already scored keep their marks until re-run.'
  );
}

/**
 * Which rubric version a communication score sheet was marked against, as a
 * label ("Rubric version 3"), or '' for a sheet scored before versions existed.
 */
export function sheetRubricVersionLabel(sheet) {
  const number = sheet?.rubric_version?.number;
  return Number.isInteger(number) && number > 0 ? `Rubric version ${number}` : '';
}
