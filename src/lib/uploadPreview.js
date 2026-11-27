// What the upload form can tell an examiner about a chosen file before a
// multi-gigabyte transfer starts.
//
// The server judges a source by extension and MIME, and converts anything a
// browser cannot play after upload (the prepare_media job), so the message here
// is "will be converted", never "not supported" — except for what the server
// will refuse: an extension outside its list, a file over its size limit, an
// empty file. Those are `rejected`, and the only verdict that blocks Start.
//
// Pure: no React, no DOM, no fetch of its own (`fetchUploadLimits` is handed
// the client). See test/uploadPreview.test.mjs.

export const UPLOAD_LIMITS_ENDPOINT = '/api/uploads/limits';

export const VERY_LONG_SECONDS = 3 * 60 * 60;
export const VERY_SHORT_SECONDS = 30;

export const Verdict = Object.freeze({
  PLAYS: 'plays',
  WILL_CONVERT: 'will-convert',
  UNKNOWN: 'unknown',
  REJECTED: 'rejected',
});

export const Severity = Object.freeze({ ERROR: 'error', WARNING: 'warning', INFO: 'info' });

// Containers whose `moov` box src/lib/isoBmff.js can read.
const ISO_FAMILIES = new Set(['mp4', 'mov']);

const FAMILY_BY_EXTENSION = {
  '.mp4': 'mp4', '.m4v': 'mp4', '.mov': 'mov', '.mkv': 'mkv', '.webm': 'webm', '.avi': 'avi',
};
const FAMILY_BY_MIME = {
  'video/mp4': 'mp4', 'video/quicktime': 'mov', 'video/x-matroska': 'mkv',
  'video/webm': 'webm', 'video/x-msvideo': 'avi',
};
const MIME_BY_FAMILY = {
  mp4: 'video/mp4', mov: 'video/quicktime', mkv: 'video/x-matroska', webm: 'video/webm', avi: 'video/x-msvideo',
};

// What the server's own browser-playability rule accepts
// (fastapi_backend/app/pipeline/media.py): anything else gets a converted copy.
const SAFE_VIDEO_CODECS = new Set(['avc1', 'avc3', 'vp08', 'vp09', 'av01']);
const SAFE_AUDIO_CODECS = new Set(['mp4a', 'Opus', 'opus', 'fLaC', '.mp3', 'mp3 ']);

const CODEC_NAMES = {
  avc1: 'H.264', avc3: 'H.264', hvc1: 'HEVC (H.265)', hev1: 'HEVC (H.265)', dvh1: 'Dolby Vision', dvhe: 'Dolby Vision',
  vp08: 'VP8', vp09: 'VP9', av01: 'AV1', mp4v: 'MPEG-4 Visual',
  apch: 'ProRes', apcn: 'ProRes', apcs: 'ProRes', apco: 'ProRes', ap4h: 'ProRes', ap4x: 'ProRes',
  mp4a: 'AAC', 'ac-3': 'Dolby Digital', 'ec-3': 'Dolby Digital Plus', Opus: 'Opus', opus: 'Opus', fLaC: 'FLAC',
  '.mp3': 'MP3', 'mp3 ': 'MP3', alaw: 'G.711 A-law', ulaw: 'G.711 u-law', samr: 'AMR',
  sowt: 'PCM', twos: 'PCM', ipcm: 'PCM', lpcm: 'PCM', in24: 'PCM', in32: 'PCM', fl32: 'PCM', fl64: 'PCM',
};

const CAN_PLAY_RANK = { '': 0, maybe: 1, probably: 2 };

function positive(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

export function formatFileSize(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const text = value >= 10 || unit === 0 ? String(Math.round(value)) : value.toFixed(1).replace(/\.0$/, '');
  return `${text} ${units[unit]}`;
}

export function fileExtension(name) {
  const text = String(name || '');
  const dot = text.lastIndexOf('.');
  return dot > 0 && dot < text.length - 1 ? text.slice(dot).toLowerCase() : '';
}

/**
 * Normalise the `GET /api/uploads/limits` body. Every field the browser cannot
 * trust becomes null, and a limit that is null is never checked.
 */
export function normalizeUploadLimits(body) {
  if (!body || typeof body !== 'object') return null;
  const count = (value) => (Number.isInteger(value) && value > 0 ? value : null);
  const extensions = Array.isArray(body.videoExtensions)
    ? body.videoExtensions
      .filter((item) => typeof item === 'string' && item.trim())
      .map((item) => {
        const text = item.trim().toLowerCase();
        return text.startsWith('.') ? text : `.${text}`;
      })
    : [];
  return {
    maxVideoBytes: count(body.maxVideoBytes),
    maxPdfBytes: count(body.maxPdfBytes),
    videoExtensions: extensions.length ? extensions : null,
    maxConcurrentUploads: count(body.maxConcurrentUploads),
    maxBatchSize: count(body.maxBatchSize),
  };
}

let limitsRequest = null;

/**
 * The server's upload limits, or null when it cannot say (an older server
 * without the route, or unreachable). Asked once per page; a failure is not
 * remembered, so the next form to mount asks again.
 */
export function fetchUploadLimits(apiJson) {
  if (!limitsRequest) {
    limitsRequest = apiJson(UPLOAD_LIMITS_ENDPOINT, { fallbackMessage: 'Could not load the upload limits.' })
      .then(normalizeUploadLimits)
      .catch(() => null)
      .then((limits) => {
        if (!limits) limitsRequest = null;
        return limits;
      });
  }
  return limitsRequest;
}

export function resetUploadLimitsCache() {
  limitsRequest = null;
}

/** What the name, declared type and size say, before anything is read. */
export function classifyVideoFile({ name, type, size }, limits = null) {
  const extension = fileExtension(name);
  const family = FAMILY_BY_EXTENSION[extension] || FAMILY_BY_MIME[String(type || '').toLowerCase()] || 'other';
  const accepted = limits?.videoExtensions || null;
  const maxBytes = limits?.maxVideoBytes ?? null;
  const sizeBytes = Number.isFinite(size) && size > 0 ? size : 0;
  return {
    extension,
    family,
    extensionAccepted: accepted ? accepted.includes(extension) : true,
    acceptedExtensions: accepted,
    sizeBytes,
    sizeLabel: formatFileSize(sizeBytes),
    empty: sizeBytes === 0,
    overLimit: maxBytes !== null && sizeBytes > maxBytes,
    limitLabel: maxBytes !== null ? formatFileSize(maxBytes) : null,
  };
}

/** Whether the form should read the file's `moov` box. */
export function shouldReadContainer(file, limits = null) {
  return ISO_FAMILIES.has(classifyVideoFile(file, limits).family);
}

export function codecName(fourcc) {
  return CODEC_NAMES[fourcc] || fourcc || null;
}

function firstTrack(container, handler) {
  return container?.tracks?.find((track) => track.handler === handler) || null;
}

/**
 * The type string to ask `canPlayType` about: the exact codecs when the
 * container named them, else the declared (or extension-implied) MIME type.
 * The audio part is only spelled for AAC, the one audio codec whose string we
 * can state without parsing its descriptor.
 */
export function canPlayTypeFor(file, container = null, limits = null) {
  const video = firstTrack(container, 'vide');
  if (video?.codecString) {
    const codecs = [video.codecString];
    if (firstTrack(container, 'soun')?.codec === 'mp4a') codecs.push('mp4a.40.2');
    return `video/mp4; codecs="${codecs.join(', ')}"`;
  }
  const { family } = classifyVideoFile(file, limits);
  return String(file.type || '').trim() || MIME_BY_FAMILY[family] || null;
}

/** The strongest of several `canPlayType` answers ('' | 'maybe' | 'probably'). */
export function bestCanPlay(answers) {
  return (answers || []).reduce((best, answer) => (
    (CAN_PLAY_RANK[answer] || 0) > (CAN_PLAY_RANK[best] || 0) ? answer : best
  ), '');
}

function describeFacts(container, browser) {
  const video = firstTrack(container, 'vide');
  const audio = container?.tracks?.filter((track) => track.handler === 'soun') || [];
  const rotation = video?.rotation || 0;
  const turned = rotation === 90 || rotation === 270;
  let width = positive(turned ? video?.height : video?.width);
  let height = positive(turned ? video?.width : video?.height);
  if (!width || !height) {
    // The element reports the picture as displayed: no rotation to apply.
    width = positive(browser.videoWidth);
    height = positive(browser.videoHeight);
  }
  return {
    durationSeconds: positive(container?.durationSeconds) ?? positive(browser.durationSeconds),
    width: width && height ? width : null,
    height: width && height ? height : null,
    rotation,
    videoCodec: video?.codec || null,
    audioCodec: audio[0]?.codec || null,
    audioTracks: container ? audio.length : null,
    bitDepth: video?.bitDepth ?? null,
  };
}

/** "HEVC (H.265) video, AAC audio, 10-bit" — the details line, for those who want it. */
export function describeCodecs(facts) {
  const parts = [];
  if (facts.videoCodec) parts.push(`${codecName(facts.videoCodec)} video`);
  if (facts.audioCodec) parts.push(`${codecName(facts.audioCodec)} audio`);
  if (facts.bitDepth && facts.bitDepth > 8) parts.push(`${facts.bitDepth}-bit`);
  return parts.join(', ');
}

function warning(code, severity, message) {
  return { code, severity, message };
}

function videoNeedsConversion(facts) {
  if (!facts.videoCodec) return false;
  return !SAFE_VIDEO_CODECS.has(facts.videoCodec) || (facts.bitDepth !== null && facts.bitDepth > 8);
}

/**
 * Judge a chosen video.
 *
 * `file` is `{ name, type, size }`; `container` is src/lib/isoBmff.js's answer
 * or null; `browser` is what the <video> element said: `canPlay`
 * (`canPlayType` of `canPlayTypeFor`), `metadataLoaded`, `decodeError`, and the
 * `durationSeconds` / `videoWidth` / `videoHeight` it read. `limits` null skips
 * every limit check.
 *
 * Returns `{ verdict, facts, warnings: [{ code, severity, message }] }`.
 */
export function assessVideoFile({ file, limits = null, container = null, browser = {} }) {
  const info = classifyVideoFile(file, limits);
  const facts = describeFacts(container, browser);
  const warnings = [];

  if (info.empty) {
    warnings.push(warning('empty_file', Severity.ERROR, 'This file is empty. Choose the recording again.'));
  }
  if (!info.extensionAccepted) {
    const list = info.acceptedExtensions.map((item) => item.slice(1).toUpperCase()).join(', ');
    const label = info.extension ? `${info.extension} files` : 'A file with no extension';
    warnings.push(warning('extension_not_accepted', Severity.ERROR, `${label} cannot be uploaded here. Accepted formats: ${list}.`));
  }
  if (info.overLimit) {
    warnings.push(warning('over_size_limit', Severity.ERROR, `This file is ${info.sizeLabel}, over the ${info.limitLabel} upload limit.`));
  }
  const rejected = warnings.length > 0;

  const isoFamily = ISO_FAMILIES.has(info.family);
  if (container) {
    if (!firstTrack(container, 'vide')) {
      warnings.push(warning('no_video_track', Severity.WARNING, 'No picture was found in this file, only sound.'));
    } else if (facts.audioTracks === 0) {
      warnings.push(warning('no_audio_track', Severity.WARNING, 'No sound was found in this recording. Marking relies on the spoken conversation.'));
    }
    if (facts.audioTracks > 1) {
      warnings.push(warning('multiple_audio_tracks', Severity.INFO, `This recording has ${facts.audioTracks} audio tracks. Marking listens to one of them.`));
    }
    if (facts.rotation) {
      warnings.push(warning('rotated', Severity.INFO, `This video is stored rotated by ${facts.rotation} degrees. It is shown upright here; check the preview looks right.`));
    }
  } else if (isoFamily && !rejected) {
    warnings.push(warning('unknown_container', Severity.WARNING, "This file's structure could not be read here. The server checks it after upload."));
  }
  if (facts.durationSeconds !== null) {
    if (facts.durationSeconds > VERY_LONG_SECONDS) {
      warnings.push(warning('very_long', Severity.INFO, 'This recording is over 3 hours long. Uploading and processing it will take a while.'));
    } else if (facts.durationSeconds < VERY_SHORT_SECONDS) {
      warnings.push(warning('very_short', Severity.INFO, 'This recording is under 30 seconds long. Is it the whole station?'));
    }
  }

  const verdict = rejected ? Verdict.REJECTED : decideVerdict({ container, facts, browser, isoFamily });
  if (verdict === Verdict.WILL_CONVERT) {
    warnings.unshift(warning(
      'will_convert',
      Severity.INFO,
      'This recording will be converted automatically after upload so it can be played in the browser. Marking does not wait for it.',
    ));
  }
  return { verdict, facts, warnings };
}

function decideVerdict({ container, facts, browser, isoFamily }) {
  const browserOk = Boolean(browser.metadataLoaded) && !browser.decodeError;
  // The server converts any audio outside its browser-safe list whatever this
  // browser thinks, so the preview says the same.
  if (container?.tracks.some((track) => track.handler === 'soun' && track.codec && !SAFE_AUDIO_CODECS.has(track.codec))) {
    return Verdict.WILL_CONVERT;
  }
  if (videoNeedsConversion(facts)) {
    return browserOk && CAN_PLAY_RANK[browser.canPlay || ''] > 0 ? Verdict.PLAYS : Verdict.WILL_CONVERT;
  }
  if (browser.decodeError) {
    // A box tree we could not read, failing to decode, is a file we cannot vouch for.
    return isoFamily && !container ? Verdict.UNKNOWN : Verdict.WILL_CONVERT;
  }
  return browserOk ? Verdict.PLAYS : Verdict.UNKNOWN;
}

/** The sentence for why Start is blocked, or null. */
export function blockingReason(assessment) {
  if (assessment?.verdict !== Verdict.REJECTED) return null;
  return assessment.warnings.find((item) => item.severity === Severity.ERROR)?.message || null;
}

/**
 * The one answer to "why is Start disabled": the recording's reason when it is
 * refused, else the case study's, else null. A check that could not run (an
 * unreadable header, unknown limits) never blocks.
 */
export function startBlockingReason(videoAssessment, pdfAssessment) {
  const pdfReason = pdfAssessment?.warnings.find((item) => item.severity === Severity.ERROR)?.message || null;
  return blockingReason(videoAssessment) || pdfReason;
}

/** The chip text for a verdict. */
export function describeVerdict(verdict) {
  switch (verdict) {
    case Verdict.PLAYS: return 'Plays here';
    case Verdict.WILL_CONVERT: return 'Will be converted automatically after upload';
    case Verdict.REJECTED: return 'Cannot be uploaded';
    default: return "Can't check this format here. The server checks it after upload";
  }
}

const PDF_MAGIC = [0x25, 0x50, 0x44, 0x46, 0x2d];

/** True when the bytes start with `%PDF-`, which the server requires at assembly. */
export function checkPdfHeader(bytes) {
  let view = null;
  if (bytes instanceof ArrayBuffer) view = new Uint8Array(bytes);
  else if (ArrayBuffer.isView(bytes)) view = new Uint8Array(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  return Boolean(view) && view.length >= PDF_MAGIC.length && PDF_MAGIC.every((byte, index) => view[index] === byte);
}

/** Read a File's first bytes and check them: true, false, or null when unreadable. */
export async function readPdfHeaderCheck(file, { readRange = (start, end) => file.slice(start, end).arrayBuffer() } = {}) {
  try {
    return checkPdfHeader(await readRange(0, PDF_MAGIC.length));
  } catch {
    return null;
  }
}

/**
 * Judge the case-study PDF. `headerOk` is `readPdfHeaderCheck`'s answer.
 * `{ ok, warnings }`; an error-severity warning is a definite refusal (see
 * `startBlockingReason`), and an unreadable header is no warning at all.
 */
export function assessPdfFile({ file, limits = null, headerOk = null }) {
  const warnings = [];
  const size = Number.isFinite(file.size) ? file.size : 0;
  if (size === 0) {
    warnings.push(warning('empty_file', Severity.ERROR, 'This PDF is empty. Choose the case study again.'));
  } else if (headerOk === false) {
    warnings.push(warning('not_a_pdf', Severity.ERROR, "This file does not look like a PDF. The server will refuse it after the video has uploaded."));
  }
  if (limits?.maxPdfBytes && size > limits.maxPdfBytes) {
    warnings.push(warning('over_size_limit', Severity.ERROR, `This PDF is ${formatFileSize(size)}, over the ${formatFileSize(limits.maxPdfBytes)} limit.`));
  }
  return { ok: warnings.length === 0, warnings };
}

/**
 * What the upload form hands its file preview. In batch mode the single-file
 * state (a recording that is not part of the batch) is left alone but not
 * shown: only the case study, which the batch shares, is judged.
 */
export function previewFiles({ batchMode, videoFile, videoUrl, caseStudyFile }) {
  return batchMode
    ? { videoFile: null, videoUrl: null, caseStudyFile }
    : { videoFile, videoUrl, caseStudyFile };
}

/**
 * Whether the preview shows the player or a placeholder card. A file this
 * browser cannot decode (the element errored, loaded with no picture, or the
 * verdict says it will be converted and the element has not shown a picture)
 * would otherwise be an empty black box.
 */
export function previewSurface({ verdict, loaded = false, failed = false, videoWidth = 0 }) {
  const hasPicture = Boolean(loaded) && Number(videoWidth) > 0;
  if (failed || (loaded && !hasPicture)) return 'placeholder';
  if (verdict === Verdict.WILL_CONVERT && !hasPicture) return 'placeholder';
  return 'player';
}

export const PREVIEW_UNAVAILABLE_TEXT =
  "Preview isn't available in this browser. It will be converted automatically after upload.";
