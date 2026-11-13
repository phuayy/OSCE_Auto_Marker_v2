// Turns a <video> element's MediaError into words an operator can act on.
//
// Every playback failure used to read "Media unavailable", which sent people
// looking at tickets, proxies and disks when the real cause was a file the
// browser cannot decode (HEVC/H.265 from a phone, 10-bit H.264, ProRes, ...).
// ffmpeg on the server reads those fine, so the upload, the split and the
// scoring all succeed, and only playback fails. The two causes need different
// fixes, so the message has to tell them apart.
//
// Pure: no DOM access beyond reading the plain object it is handed.

// MediaError.code values (HTML spec).
export const MEDIA_ERR_ABORTED = 1;
export const MEDIA_ERR_NETWORK = 2;
export const MEDIA_ERR_DECODE = 3;
export const MEDIA_ERR_SRC_NOT_SUPPORTED = 4;

export const MEDIA_FAILURE = Object.freeze({
  FORMAT: 'format',
  ACCESS: 'access',
  NETWORK: 'network',
  NONE: 'none',
});

// Browser wording that names the decoder or demuxer rather than the transport.
// Chrome: DEMUXER_ERROR_NO_SUPPORTED_STREAMS, PIPELINE_ERROR_DECODE,
// DECODER_ERROR_NOT_SUPPORTED. Firefox: "No decoders for requested formats",
// "unsupported codec". Safari has little to say either way.
const FORMAT_WORDING = /DEMUX|DECOD|codec|NO_SUPPORTED_STREAMS|unsupported (video|audio|media|type)|no decoders/i;

/**
 * Classify a MediaError.
 *
 * `local` is true for a blob: URL of a file on the user's own disk. Nothing
 * can refuse access to that, so "source not supported" there can only mean
 * the format. For a server URL the same code 4 is ambiguous — Chrome also
 * reports an HTTP 401/404 as "Format error" — so it counts as a format
 * failure only when the browser's own message names the decoder.
 */
export function classifyMediaError(mediaError, { local = false } = {}) {
  if (!mediaError) return MEDIA_FAILURE.NONE;
  const code = Number(mediaError.code) || 0;
  const message = String(mediaError.message || '');
  if (code === MEDIA_ERR_ABORTED) return MEDIA_FAILURE.NONE;
  if (code === MEDIA_ERR_DECODE || FORMAT_WORDING.test(message)) return MEDIA_FAILURE.FORMAT;
  if (code === MEDIA_ERR_SRC_NOT_SUPPORTED && local) return MEDIA_FAILURE.FORMAT;
  if (code === MEDIA_ERR_NETWORK) return MEDIA_FAILURE.NETWORK;
  return MEDIA_FAILURE.ACCESS;
}

// Two places see a format failure, and the advice differs. A local file (the
// upload form's preview) has not reached the server yet: the upload is fine,
// and the job makes a browser-playable copy (app/services/playback_copy.py).
// A server URL that still fails is a session without that copy — one uploaded
// before copies existed.
const FORMAT_MESSAGE_LOCAL =
  "This browser can't preview this video's format (often HEVC/H.265 from a phone or camera). " +
  'You can still upload it: the server makes a playable copy while it processes the recording.';
const FORMAT_MESSAGE_SERVER =
  "This browser can't play this video's format (often HEVC/H.265 from a phone or camera). " +
  'Processing is unaffected. Recordings uploaded now get a playable copy automatically; ' +
  'upload an older one again to get one.';

/**
 * The sentence to show, or null when nothing failed (an abort is not a
 * failure: it is what a src change does to the previous load).
 */
export function describeMediaError(mediaError, { local = false } = {}) {
  const kind = classifyMediaError(mediaError, { local });
  if (kind === MEDIA_FAILURE.NONE) return null;
  const detail = mediaError?.message ? ` (${String(mediaError.message).slice(0, 120)})` : '';
  if (kind === MEDIA_FAILURE.FORMAT) return `${local ? FORMAT_MESSAGE_LOCAL : FORMAT_MESSAGE_SERVER}${detail}`;
  if (kind === MEDIA_FAILURE.NETWORK) {
    return `The video stopped loading part-way. Check the connection and reopen the session.${detail}`;
  }
  return `Media unavailable: the server did not hand over the video. Reopen the session to try again.${detail}`;
}
