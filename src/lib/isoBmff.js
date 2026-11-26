// A small, read-only ISO-BMFF (MP4 / MOV / M4V) metadata reader.
//
// The upload form wants to say what a chosen recording is — how long, how big
// on screen, whether it is rotated, which codecs, whether it carries sound —
// before a multi-gigabyte transfer starts. The browser's <video> answers some
// of that only for files it can decode, and says nothing about a track it
// cannot play. The container itself knows all of it, in the `moov` box, so this
// module reads that box and nothing else.
//
// Two layers, both defensive. A user can pick any file at all, and a camera
// can write a damaged one, so neither layer ever throws: anything it cannot
// parse is `null`.
//
//   * `parseMoovBox(bytes)` is pure: the bytes of one whole `moov` box in, a
//     description out. The walk down moov -> trak -> mdia -> minf -> stbl ->
//     stsd is a fixed path, not a recursion, so a hostile file cannot nest it
//     deeper, and every box count is capped.
//   * `readIsoBmffInfo(file)` finds the `moov` box in a File through
//     `slice()`. It lies at the start of a "fast start" file and at the end of
//     a camera's, so it hops from box header to box header (never reading an
//     `mdat` payload) under a byte budget.
//
// No DOM beyond `Blob.slice`/`arrayBuffer`, which the caller can replace
// (`readRange`) — the tests run it over plain buffers.

// The first read, which also serves every header that lies inside it.
export const PREFIX_BYTES = 1024 * 1024;
// Everything one call may read, header hops and the moov together. A moov grows
// with the sample tables, i.e. with the recording's length: tens of megabytes
// would be a multi-hour recording, which is not worth stalling the form for.
export const MAX_READ_BYTES = 32 * 1024 * 1024;

const MAX_TOP_LEVEL_BOXES = 256;
const MAX_CHILD_BOXES = 4096;
const MAX_TRACKS = 64;
// A 64-bit size whose high word is above this would not fit a safe integer.
const MAX_SIZE_HIGH_WORD = 0x1fffff;

const FIXED_16_16 = 65536;
const FLAG_BYTES_VERSION = 4;

const AVC_FAMILY = new Set(['avc1', 'avc3']);
const HEVC_FAMILY = new Set(['hvc1', 'hev1']);
// AVCDecoderConfigurationRecord carries bit depth only for these profiles.
const AVC_PROFILES_WITH_EXTENSION = new Set([100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135]);

function fourcc(view, offset) {
  return String.fromCharCode(
    view.getUint8(offset),
    view.getUint8(offset + 1),
    view.getUint8(offset + 2),
    view.getUint8(offset + 3),
  );
}

function hex(value) {
  return value.toString(16).toUpperCase().padStart(2, '0');
}

function viewOf(bytes) {
  if (bytes instanceof ArrayBuffer) return new DataView(bytes);
  if (ArrayBuffer.isView(bytes)) return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  return null;
}

/**
 * The header of the box at `offset`: `{ type, headerSize, size }`, where `size`
 * is the whole box and `0` means "to the end of the file" (a size field of 0).
 * `null` when the header is cut off or cannot be a box (size smaller than its
 * own header, or too large for a safe integer).
 */
export function readBoxHeader(view, offset) {
  if (offset < 0 || offset + 8 > view.byteLength) return null;
  const size32 = view.getUint32(offset);
  const type = fourcc(view, offset + 4);
  if (size32 === 0) return { type, headerSize: 8, size: 0 };
  if (size32 !== 1) {
    return size32 < 8 ? null : { type, headerSize: 8, size: size32 };
  }
  if (offset + 16 > view.byteLength) return null;
  const high = view.getUint32(offset + 8);
  if (high > MAX_SIZE_HIGH_WORD) return null;
  const size = high * 2 ** 32 + view.getUint32(offset + 12);
  return size < 16 ? null : { type, headerSize: 16, size };
}

// The well-formed children of the box spanning [start, end): each as
// `{ type, start, payloadStart, end }`. Stops at the first child that does not
// fit, rather than guessing at what follows it.
function childBoxes(view, start, end) {
  const boxes = [];
  let offset = start;
  while (offset + 8 <= end && boxes.length < MAX_CHILD_BOXES) {
    const header = readBoxHeader(view, offset);
    if (!header) break;
    const boxEnd = header.size === 0 ? end : offset + header.size;
    if (boxEnd > end || boxEnd <= offset) break;
    boxes.push({ type: header.type, start: offset, payloadStart: offset + header.headerSize, end: boxEnd });
    offset = boxEnd;
  }
  return boxes;
}

function firstChild(view, box, type) {
  return childBoxes(view, box.payloadStart, box.end).find((child) => child.type === type) || null;
}

// A 64-bit big-endian integer as a Number (precision beyond 2^53 is irrelevant
// for a duration), or null when it is out of range.
function readUint64(view, offset) {
  const high = view.getUint32(offset);
  if (high > MAX_SIZE_HIGH_WORD) return null;
  return high * 2 ** 32 + view.getUint32(offset + 4);
}

function fits(box, length) {
  return box.end - box.payloadStart >= length;
}

function readMovieHeader(view, box) {
  if (!fits(box, FLAG_BYTES_VERSION)) return null;
  const version = view.getUint8(box.payloadStart);
  const base = box.payloadStart + FLAG_BYTES_VERSION;
  if (version === 1) {
    if (!fits(box, FLAG_BYTES_VERSION + 28)) return null;
    return { timescale: view.getUint32(base + 16), duration: readUint64(view, base + 20) };
  }
  if (!fits(box, FLAG_BYTES_VERSION + 16)) return null;
  const duration = view.getUint32(base + 12);
  return { timescale: view.getUint32(base + 8), duration: duration === 0xffffffff ? null : duration };
}

function secondsOf(header) {
  if (!header || !header.timescale || header.duration === null || header.duration === undefined) return null;
  return header.duration / header.timescale;
}

// Degrees clockwise a player must turn the picture, from the 3x3 matrix's
// rotation part (a, b / c, d in 16.16). Anything that is not a pure quarter
// turn (a flip, a skew, an all-zero matrix some writers leave) counts as 0.
function rotationOf(view, matrixOffset) {
  const a = view.getInt32(matrixOffset) / FIXED_16_16;
  const b = view.getInt32(matrixOffset + 4) / FIXED_16_16;
  const c = view.getInt32(matrixOffset + 12) / FIXED_16_16;
  const d = view.getInt32(matrixOffset + 16) / FIXED_16_16;
  const near = (value, target) => Math.abs(value - target) < 0.01;
  if (near(a, 1) && near(b, 0) && near(c, 0) && near(d, 1)) return 0;
  if (near(a, 0) && near(b, 1) && near(c, -1) && near(d, 0)) return 90;
  if (near(a, -1) && near(b, 0) && near(c, 0) && near(d, -1)) return 180;
  if (near(a, 0) && near(b, -1) && near(c, 1) && near(d, 0)) return 270;
  return 0;
}

function readTrackHeader(view, box) {
  if (!fits(box, FLAG_BYTES_VERSION)) return null;
  const version = view.getUint8(box.payloadStart);
  const matrix = box.payloadStart + (version === 1 ? 52 : 40);
  const size = version === 1 ? 96 : 84;
  if (!fits(box, size)) return null;
  const dimensions = matrix + 36;
  return {
    width: Math.floor(view.getUint32(dimensions) / FIXED_16_16),
    height: Math.floor(view.getUint32(dimensions + 4) / FIXED_16_16),
    rotation: rotationOf(view, matrix),
  };
}

function readMediaHeader(view, box) {
  if (!fits(box, FLAG_BYTES_VERSION)) return null;
  const version = view.getUint8(box.payloadStart);
  const base = box.payloadStart + FLAG_BYTES_VERSION;
  if (version === 1) {
    return fits(box, FLAG_BYTES_VERSION + 28)
      ? { timescale: view.getUint32(base + 16), duration: readUint64(view, base + 20) }
      : null;
  }
  return fits(box, FLAG_BYTES_VERSION + 16)
    ? { timescale: view.getUint32(base + 8), duration: view.getUint32(base + 12) }
    : null;
}

// The AVC profile, compatibility and level bytes name the codec to
// `canPlayType` ("avc1.64001F"); the optional extension carries the bit depth.
function readAvcConfig(view, box, sampleEntryType) {
  if (!fits(box, 4)) return {};
  const at = box.payloadStart;
  const profile = view.getUint8(at + 1);
  const codecString = `${sampleEntryType}.${hex(profile)}${hex(view.getUint8(at + 2))}${hex(view.getUint8(at + 3))}`;
  let bitDepth = AVC_PROFILES_WITH_EXTENSION.has(profile) ? null : 8;
  if (AVC_PROFILES_WITH_EXTENSION.has(profile) && fits(box, 6)) {
    let offset = at + 5;
    const spsCount = view.getUint8(at + 5) & 0x1f;
    offset += 1;
    let ok = true;
    for (let i = 0; i < spsCount && ok; i += 1) {
      if (offset + 2 > box.end) ok = false;
      else offset += 2 + view.getUint16(offset);
    }
    if (ok && offset + 1 <= box.end) {
      const ppsCount = view.getUint8(offset);
      offset += 1;
      for (let i = 0; i < ppsCount && ok; i += 1) {
        if (offset + 2 > box.end) ok = false;
        else offset += 2 + view.getUint16(offset);
      }
    }
    if (ok && offset + 3 <= box.end) bitDepth = (view.getUint8(offset + 1) & 0x7) + 8;
  }
  return { codecString, bitDepth };
}

// HEVCDecoderConfigurationRecord -> the RFC 6381 string ("hvc1.2.4.L93.B0") and
// the luma bit depth. Main10 (profile 2) is the 10-bit a browser may refuse.
function readHevcConfig(view, box, sampleEntryType) {
  if (!fits(box, 13)) return {};
  const at = box.payloadStart;
  const profileByte = view.getUint8(at + 1);
  const profileSpace = ['', 'A', 'B', 'C'][profileByte >> 6];
  const profileIdc = profileByte & 0x1f;
  const tier = (profileByte & 0x20) === 0 ? 'L' : 'H';
  let compat = view.getUint32(at + 2);
  let reversed = 0;
  for (let i = 0; i < 32; i += 1) {
    reversed = (reversed * 2 + (compat & 1)) >>> 0;
    compat >>>= 1;
  }
  const constraints = [];
  for (let i = 0; i < 6; i += 1) constraints.push(view.getUint8(at + 6 + i));
  while (constraints.length > 1 && constraints[constraints.length - 1] === 0) constraints.pop();
  const level = view.getUint8(at + 12);
  const codecString = [
    `${sampleEntryType}.${profileSpace}${profileIdc}`,
    reversed.toString(16).toUpperCase(),
    `${tier}${level}`,
    ...constraints.map((byte) => hex(byte)),
  ].join('.');
  const bitDepth = fits(box, 18) ? (view.getUint8(at + 17) & 0x7) + 8 : profileIdc === 2 ? 10 : 8;
  return { codecString, bitDepth };
}

// The first sample description of a track: its fourcc, and for H.264/H.265 the
// codec string and bit depth from the configuration box inside it.
function readSampleEntry(view, stsd, handler) {
  // version/flags + entry_count, then the entries.
  const entries = childBoxes(view, stsd.payloadStart + 8, stsd.end);
  const entry = entries[0];
  if (!entry) return { codec: null, codecString: null, bitDepth: null };
  const codec = entry.type;
  if (handler !== 'vide' || !(AVC_FAMILY.has(codec) || HEVC_FAMILY.has(codec))) {
    return { codec, codecString: null, bitDepth: null };
  }
  // The visual sample entry's fixed fields (86 bytes with the box header) come
  // before its child boxes.
  const children = childBoxes(view, entry.start + 86, entry.end);
  const config = children.find((child) => child.type === (AVC_FAMILY.has(codec) ? 'avcC' : 'hvcC'));
  if (!config) return { codec, codecString: null, bitDepth: null };
  const read = AVC_FAMILY.has(codec) ? readAvcConfig(view, config, codec) : readHevcConfig(view, config, codec);
  return { codec, codecString: read.codecString || null, bitDepth: read.bitDepth ?? null };
}

function readTrack(view, trak) {
  const mdia = firstChild(view, trak, 'mdia');
  const tkhd = firstChild(view, trak, 'tkhd');
  if (!mdia || !tkhd) return null;
  const hdlr = firstChild(view, mdia, 'hdlr');
  if (!hdlr || !fits(hdlr, 12)) return null;
  const handler = fourcc(view, hdlr.payloadStart + 8);
  const header = readTrackHeader(view, tkhd);
  const mdhd = firstChild(view, mdia, 'mdhd');
  const minf = firstChild(view, mdia, 'minf');
  const stbl = minf ? firstChild(view, minf, 'stbl') : null;
  const stsd = stbl ? firstChild(view, stbl, 'stsd') : null;
  const entry = stsd && fits(stsd, 8)
    ? readSampleEntry(view, stsd, handler)
    : { codec: null, codecString: null, bitDepth: null };
  return {
    handler,
    width: header?.width ?? 0,
    height: header?.height ?? 0,
    rotation: header?.rotation ?? 0,
    durationSeconds: mdhd ? secondsOf(readMediaHeader(view, mdhd)) : null,
    ...entry,
  };
}

/**
 * Describe one whole `moov` box (its header included).
 *
 * `{ durationSeconds, tracks: [{ handler, width, height, rotation, codec,
 * codecString, bitDepth, durationSeconds }] }`, `handler` being `vide` or
 * `soun` (or whatever else the file declares, e.g. `text`). Width and height
 * are the track's stored size, before `rotation` (degrees clockwise) is
 * applied. `null` when the bytes are not a moov box.
 */
export function parseMoovBox(bytes) {
  try {
    const view = viewOf(bytes);
    if (!view) return null;
    const header = readBoxHeader(view, 0);
    if (!header || header.type !== 'moov') return null;
    const end = header.size === 0 ? view.byteLength : header.size;
    if (end > view.byteLength) return null;
    const moov = { type: 'moov', start: 0, payloadStart: header.headerSize, end };
    const children = childBoxes(view, moov.payloadStart, moov.end);
    const mvhd = children.find((child) => child.type === 'mvhd');
    const tracks = children
      .filter((child) => child.type === 'trak')
      .slice(0, MAX_TRACKS)
      .map((trak) => readTrack(view, trak))
      .filter(Boolean);
    if (!mvhd && tracks.length === 0) return null;
    const movieSeconds = mvhd ? secondsOf(readMovieHeader(view, mvhd)) : null;
    const trackSeconds = tracks.map((track) => track.durationSeconds).filter((seconds) => seconds !== null);
    return {
      durationSeconds: movieSeconds ?? (trackSeconds.length ? Math.max(...trackSeconds) : null),
      tracks,
    };
  } catch {
    return null;
  }
}

/**
 * Read a File's (or any slice-able's) `moov` and describe it, or null.
 *
 * `readRange(start, end)` resolves the bytes in [start, end) as an ArrayBuffer;
 * it defaults to `file.slice(...).arrayBuffer()`. Header hops that land inside
 * the first `PREFIX_BYTES` cost no further read, and the total read is capped
 * at `MAX_READ_BYTES`; a moov that does not fit the budget gives null.
 */
export async function readIsoBmffInfo(file, { readRange = defaultReadRange(file) } = {}) {
  try {
    const fileSize = file.size;
    if (!Number.isFinite(fileSize) || fileSize < 8) return null;
    let spent = 0;
    const read = async (start, end) => {
      const stop = Math.min(end, fileSize);
      if (stop <= start || spent + (stop - start) > MAX_READ_BYTES) return null;
      spent += stop - start;
      const buffer = await readRange(start, stop);
      return buffer && buffer.byteLength === stop - start ? buffer : null;
    };
    const prefixEnd = Math.min(PREFIX_BYTES, fileSize);
    const prefix = await read(0, prefixEnd);
    if (!prefix) return null;
    const prefixView = new DataView(prefix);

    let offset = 0;
    for (let hops = 0; hops < MAX_TOP_LEVEL_BOXES && offset + 8 <= fileSize; hops += 1) {
      const inPrefix = offset + 16 <= prefixEnd;
      const headerBytes = inPrefix ? prefix : await read(offset, offset + 16);
      if (!headerBytes) return null;
      const header = readBoxHeader(inPrefix ? prefixView : new DataView(headerBytes), inPrefix ? offset : 0);
      if (!header) return null;
      const size = header.size === 0 ? fileSize - offset : header.size;
      if (offset + size > fileSize) return null;
      if (header.type === 'moov') {
        const whole = offset + size <= prefixEnd ? prefix.slice(offset, offset + size) : await read(offset, offset + size);
        return whole ? parseMoovBox(whole) : null;
      }
      offset += size;
    }
    return null;
  } catch {
    return null;
  }
}

function defaultReadRange(file) {
  return (start, end) => file.slice(start, end).arrayBuffer();
}
