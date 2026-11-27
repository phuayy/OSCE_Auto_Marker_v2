// Which workspace an upload settling may close.
//
// An upload runs for minutes, and the user is free to open another session
// meanwhile (the overlay can be dismissed; the list stays browsable). When the
// upload then succeeds or fails, its handlers return the screen to the list —
// but only if the screen is still *theirs*: the upload's own session (set when
// it was initiated) or nothing at all. A session the user opened since is not
// the upload's to close. Pure so the rule is testable
// (see test/uploadCompletion.test.mjs).

/**
 * @param {string|number|null|undefined} openSessionId  the session the workspace state holds now
 * @param {string|number|null|undefined} uploadSessionId  the session being uploaded (null if it failed before one existed)
 */
export function workspaceBelongsToUpload(openSessionId, uploadSessionId) {
  if (openSessionId == null) {
    return true;
  }
  return uploadSessionId != null && String(openSessionId) === String(uploadSessionId);
}

export default workspaceBelongsToUpload;
