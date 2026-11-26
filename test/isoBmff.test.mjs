import test from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_READ_BYTES,
  PREFIX_BYTES,
  parseMoovBox,
  readBoxHeader,
  readIsoBmffInfo,
} from '../src/lib/isoBmff.js';

// ---- A tiny MP4 writer: just enough boxes to be read back. ----

const u8 = (...values) => Uint8Array.from(values);
function u32(value) {
  const out = new Uint8Array(4);
  new DataView(out.buffer).setUint32(0, value);
  return out;
}
function i32(value) {
  const out = new Uint8Array(4);
  new DataView(out.buffer).setInt32(0, value);
  return out;
}
function u64(value) {
  const out = new Uint8Array(8);
  new DataView(out.buffer).setBigUint64(0, BigInt(value));
  return out;
}
function u16(value) {
  return u8(value >> 8, value & 0xff);
}
function concat(...parts) {
  const flat = parts.flat(Infinity);
  const total = flat.reduce((sum, part) => sum + part.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const part of flat) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
}
const ascii = (text) => Uint8Array.from([...text].map((char) => char.charCodeAt(0)));
const zeros = (count) => new Uint8Array(count);
function box(type, ...payload) {
  const body = concat(payload);
  return concat(u32(8 + body.length), ascii(type), body);
}

const ROTATIONS = {
  0: [1, 0, 0, 1],
  90: [0, 1, -1, 0],
  180: [-1, 0, 0, -1],
  270: [0, -1, 1, 0],
};
function matrix(rotation) {
  const [a, b, c, d] = ROTATIONS[rotation].map((value) => value * 65536);
  return concat(i32(a), i32(b), i32(0), i32(c), i32(d), i32(0), i32(0), i32(0), i32(0x40000000));
}

function mvhd(timescale, duration, version = 0) {
  return version === 1
    ? box('mvhd', u8(1, 0, 0, 0), u64(0), u64(0), u32(timescale), u64(duration), zeros(80))
    : box('mvhd', u8(0, 0, 0, 0), u32(0), u32(0), u32(timescale), u32(duration), zeros(80));
}
function tkhd(width, height, rotation = 0, version = 0) {
  return version === 1
    ? box('tkhd', u8(1, 0, 0, 3), u64(0), u64(0), u32(1), u32(0), u64(0), zeros(8), zeros(8), matrix(rotation), u32(width * 65536), u32(height * 65536))
    : box('tkhd', u8(0, 0, 0, 3), u32(0), u32(0), u32(1), u32(0), u32(0), zeros(8), zeros(8), matrix(rotation), u32(width * 65536), u32(height * 65536));
}
const mdhd = (timescale, duration) => box('mdhd', u8(0, 0, 0, 0), u32(0), u32(0), u32(timescale), u32(duration), zeros(4));
const hdlr = (handler) => box('hdlr', u8(0, 0, 0, 0), zeros(4), ascii(handler), zeros(12), u8(0));

function avcC(profile, { depth = null } = {}) {
  const sps = u8(0x67, profile, 0, 31);
  const pps = u8(0x68, 0xee);
  const extension = depth === null ? [] : [u8(0xfc | 1, 0xf8 | (depth - 8), 0xf8 | (depth - 8), 0)];
  return box('avcC', u8(1, profile, 0x00, 0x1f, 0xff, 0xe1), u16(sps.length), sps, u8(1), u16(pps.length), pps, extension);
}
function hvcC({ profileIdc, depth = 8, level = 93 }) {
  // 22 fixed bytes of the record, with the fields a reader needs filled in.
  const record = zeros(23);
  record[0] = 1;
  record[1] = profileIdc;
  record.set([0x60, 0, 0, 0], 2); // compatibility flags 0x60000000
  record.set([0x90, 0, 0, 0, 0, 0], 6);
  record[12] = level;
  record[17] = 0xf8 | (depth - 8);
  record[18] = 0xf8 | (depth - 8);
  return box('hvcC', record);
}
function visualEntry(type, config) {
  return box(type, zeros(6), u16(1), zeros(16), u16(1920), u16(1080), zeros(50), config);
}
function audioEntry(type) {
  return box(type, zeros(6), u16(1), zeros(8), u16(2), u16(16), zeros(4), u32(48000 * 65536));
}
const stsd = (entry) => box('stsd', u8(0, 0, 0, 0), u32(1), entry);

function trak({ handler, width = 0, height = 0, rotation = 0, entry, timescale = 1000, duration = 60000, version = 0 }) {
  return box(
    'trak',
    tkhd(width, height, rotation, version),
    box('mdia', mdhd(timescale, duration), hdlr(handler), box('minf', box('stbl', entry ? stsd(entry) : box('stts', zeros(8))))),
  );
}
const videoTrak = (options = {}) => trak({
  handler: 'vide',
  width: 1920,
  height: 1080,
  entry: visualEntry('avc1', avcC(0x64, { depth: 8 })),
  ...options,
});
const audioTrak = (options = {}) => trak({ handler: 'soun', entry: audioEntry('mp4a'), timescale: 48000, duration: 48000 * 60, ...options });

const moov = (...children) => box('moov', mvhd(1000, 60000), children);
const ftyp = () => box('ftyp', ascii('isom'), u32(0), ascii('isom'));
const mdat = (length = 100) => box('mdat', zeros(length));

function fileOf(bytes) {
  return { size: bytes.length, slice: (start, end) => ({ arrayBuffer: async () => bytes.slice(start, end).buffer }) };
}
const read = (...parts) => readIsoBmffInfo(fileOf(concat(parts)));

// ---- parseMoovBox ----

test('reads duration, the video track and the audio track', () => {
  const info = parseMoovBox(moov(videoTrak(), audioTrak()));
  assert.equal(info.durationSeconds, 60);
  assert.equal(info.tracks.length, 2);
  assert.deepEqual(
    { ...info.tracks[0], durationSeconds: undefined },
    {
      handler: 'vide', width: 1920, height: 1080, rotation: 0, codec: 'avc1',
      codecString: 'avc1.64001F', bitDepth: 8, durationSeconds: undefined,
    },
  );
  assert.equal(info.tracks[1].handler, 'soun');
  assert.equal(info.tracks[1].codec, 'mp4a');
  assert.equal(info.tracks[1].durationSeconds, 60);
});

test('reads the 64-bit forms of the movie and track headers', () => {
  const info = parseMoovBox(box('moov', mvhd(600, 600 * 90, 1), videoTrak({ version: 1 })));
  assert.equal(info.durationSeconds, 90);
  assert.equal(info.tracks[0].width, 1920);
  assert.equal(info.tracks[0].height, 1080);
});

test('an all-ones movie duration means unknown, and the track duration stands in', () => {
  const unknown = box('mvhd', u8(0, 0, 0, 0), u32(0), u32(0), u32(1000), u32(0xffffffff), zeros(80));
  const info = parseMoovBox(box('moov', unknown, videoTrak()));
  assert.equal(info.durationSeconds, 60);
});

for (const rotation of [0, 90, 180, 270]) {
  test(`reads a ${rotation} degree rotation from the matrix`, () => {
    const info = parseMoovBox(moov(videoTrak({ rotation })));
    assert.equal(info.tracks[0].rotation, rotation);
    // The stored size is reported as stored; applying the rotation is the caller's.
    assert.equal(info.tracks[0].width, 1920);
  });
}

test('a matrix that is not a quarter turn counts as no rotation', () => {
  const skew = box('tkhd', u8(0, 0, 0, 3), u32(0), u32(0), u32(1), u32(0), u32(0), zeros(8), zeros(8),
    concat(i32(65536), i32(30000), i32(0), i32(0), i32(65536), i32(0), i32(0), i32(0), i32(0x40000000)),
    u32(640 * 65536), u32(480 * 65536));
  const info = parseMoovBox(box('moov', mvhd(1000, 1000), box('trak', skew, box('mdia', hdlr('vide')))));
  assert.equal(info.tracks[0].rotation, 0);
  assert.equal(info.tracks[0].width, 640);
});

test('H.264 High profile 4:2:0 with the bit-depth extension reads 8 and 10 bit', () => {
  for (const depth of [8, 10]) {
    const info = parseMoovBox(moov(videoTrak({ entry: visualEntry('avc1', avcC(110, { depth })) })));
    assert.equal(info.tracks[0].bitDepth, depth);
    assert.equal(info.tracks[0].codecString, 'avc1.6E001F');
  }
});

test('H.264 Baseline has no extension and is 8-bit; High without one is unknown', () => {
  assert.equal(parseMoovBox(moov(videoTrak({ entry: visualEntry('avc3', avcC(66)) }))).tracks[0].bitDepth, 8);
  assert.equal(parseMoovBox(moov(videoTrak({ entry: visualEntry('avc1', avcC(100)) }))).tracks[0].bitDepth, null);
});

test('HEVC hvc1 and hev1 read their codec string and bit depth', () => {
  const main10 = parseMoovBox(moov(videoTrak({ entry: visualEntry('hvc1', hvcC({ profileIdc: 2, depth: 10 })) }))).tracks[0];
  assert.equal(main10.codec, 'hvc1');
  assert.equal(main10.bitDepth, 10);
  assert.equal(main10.codecString, 'hvc1.2.6.L93.90');
  const main = parseMoovBox(moov(videoTrak({ entry: visualEntry('hev1', hvcC({ profileIdc: 1 })) }))).tracks[0];
  assert.equal(main.codec, 'hev1');
  assert.equal(main.bitDepth, 8);
  assert.match(main.codecString, /^hev1\.1\./);
});

test('a track with no sample description still reports its handler', () => {
  const info = parseMoovBox(moov(trak({ handler: 'vide', width: 100, height: 50 })));
  assert.equal(info.tracks[0].codec, null);
  assert.equal(info.tracks[0].width, 100);
});

test('other handlers (text, tmcd) are kept so the caller can ignore them', () => {
  const info = parseMoovBox(moov(videoTrak(), trak({ handler: 'tmcd' })));
  assert.deepEqual(info.tracks.map((track) => track.handler), ['vide', 'tmcd']);
});

test('parseMoovBox never throws and answers null for what is not a moov', () => {
  assert.equal(parseMoovBox(null), null);
  assert.equal(parseMoovBox(new Uint8Array(0)), null);
  assert.equal(parseMoovBox(ascii('not an mp4 at all, just text')), null);
  assert.equal(parseMoovBox(ftyp()), null);
  assert.equal(parseMoovBox(box('moov')), null);
  // Truncated: the header promises more than the buffer holds.
  const whole = moov(videoTrak(), audioTrak());
  assert.equal(parseMoovBox(whole.slice(0, whole.length - 10)), null);
  assert.equal(parseMoovBox(new Uint8Array(1000).fill(0xff)), null);
});

test('a damaged track inside an otherwise good moov is dropped, not fatal', () => {
  const good = videoTrak();
  const broken = box('trak', u32(0xfffffff0), ascii('tkhd'));
  const info = parseMoovBox(moov(good, broken));
  assert.equal(info.tracks.length, 1);
});

test('a child that claims more than its parent holds ends the walk there', () => {
  const lying = concat(u32(5000), ascii('trak'), zeros(20));
  const info = parseMoovBox(box('moov', mvhd(1000, 5000), lying));
  assert.deepEqual(info, { durationSeconds: 5, tracks: [] });
});

// ---- readBoxHeader ----

test('box headers: 32-bit, 64-bit, to-end, and the nonsense that must be refused', () => {
  const view = (bytes) => new DataView(bytes.buffer);
  assert.deepEqual(readBoxHeader(view(concat(u32(24), ascii('free'), zeros(16))), 0), { type: 'free', headerSize: 8, size: 24 });
  assert.deepEqual(readBoxHeader(view(concat(u32(1), ascii('mdat'), u64(5_000_000_000))), 0), { type: 'mdat', headerSize: 16, size: 5_000_000_000 });
  assert.deepEqual(readBoxHeader(view(concat(u32(0), ascii('mdat'))), 0), { type: 'mdat', headerSize: 8, size: 0 });
  assert.equal(readBoxHeader(view(concat(u32(7), ascii('free'))), 0), null, 'smaller than its own header');
  assert.equal(readBoxHeader(view(concat(u32(1), ascii('mdat'), u64(8))), 0), null, 'largesize below its header');
  assert.equal(readBoxHeader(view(concat(u32(1), ascii('mdat'), u64(2n ** 60n))), 0), null, 'absurd 64-bit size');
  assert.equal(readBoxHeader(view(concat(u32(1), ascii('mdat'))), 0), null, 'cut off inside the largesize');
  assert.equal(readBoxHeader(view(u8(0, 0, 0)), 0), null);
});

// ---- readIsoBmffInfo ----

test('finds a moov at the start of the file (fast start)', async () => {
  const info = await read(ftyp(), moov(videoTrak(), audioTrak()), mdat(5000));
  assert.equal(info.tracks.length, 2);
});

test('finds a moov at the end, hopping over an mdat larger than the first read', async () => {
  const bigMdat = mdat(PREFIX_BYTES * 2);
  const reads = [];
  const bytes = concat(ftyp(), bigMdat, moov(videoTrak(), audioTrak()));
  const file = fileOf(bytes);
  const info = await readIsoBmffInfo(file, {
    readRange: async (start, end) => {
      reads.push(end - start);
      return bytes.slice(start, end).buffer;
    },
  });
  assert.equal(info.durationSeconds, 60);
  // The prefix, one small header read after the mdat, then the moov: never the mdat payload.
  assert.ok(reads.reduce((sum, length) => sum + length, 0) < PREFIX_BYTES + 4096, `read ${reads}`);
});

test('follows a 64-bit mdat header to the moov behind it', async () => {
  const payload = zeros(3000);
  const wide = concat(u32(1), ascii('mdat'), u64(16 + payload.length), payload);
  const info = await read(ftyp(), wide, moov(videoTrak()));
  assert.equal(info.tracks.length, 1);
});

test('a last box with size 0 runs to the end of the file', async () => {
  const toEnd = concat(u32(0), ascii('mdat'), zeros(500));
  assert.equal(await read(ftyp(), toEnd), null, 'a to-end mdat hides no moov');
  const moovToEnd = moov(videoTrak());
  new DataView(moovToEnd.buffer).setUint32(0, 0);
  assert.equal((await read(ftyp(), mdat(), moovToEnd)).tracks.length, 1);
});

test('a truncated moov is null', async () => {
  const whole = moov(videoTrak(), audioTrak());
  assert.equal(await read(ftyp(), mdat(), whole.slice(0, whole.length - 20)), null);
});

test('garbage, tiny and empty files are null', async () => {
  assert.equal(await read(ascii('MZ this is an exe'), zeros(200)), null);
  assert.equal(await read(new Uint8Array(0)), null);
  assert.equal(await read(u8(0, 0, 0)), null);
  assert.equal(await read(new Uint8Array(4096).fill(0xff)), null);
});

test('no moov within the box budget is null', async () => {
  // Hundreds of tiny boxes and no moov.
  const many = concat(Array.from({ length: 600 }, () => box('free', zeros(4))));
  assert.equal(await read(ftyp(), many), null);
});

test('a moov larger than the read budget is not read', async () => {
  const bytes = concat(ftyp(), concat(u32(MAX_READ_BYTES + 1000), ascii('moov')));
  let largest = 0;
  const info = await readIsoBmffInfo(
    { size: ftyp().length + MAX_READ_BYTES + 1000 + 8, slice() {} },
    {
      readRange: async (start, end) => {
        largest = Math.max(largest, end - start);
        return bytes.slice(start, end).buffer;
      },
    },
  );
  assert.equal(info, null);
  assert.ok(largest <= PREFIX_BYTES);
});

test('a reader that fails or returns the wrong length yields null, not a throw', async () => {
  const bytes = concat(ftyp(), moov(videoTrak()));
  assert.equal(await readIsoBmffInfo(fileOf(bytes), { readRange: async () => { throw new Error('disk gone'); } }), null);
  assert.equal(await readIsoBmffInfo(fileOf(bytes), { readRange: async () => new ArrayBuffer(3) }), null);
  assert.equal(await readIsoBmffInfo({ size: NaN }, { readRange: async () => new ArrayBuffer(0) }), null);
});

test('video-only and audio-only files describe only what they carry', async () => {
  const videoOnly = await read(ftyp(), moov(videoTrak()), mdat());
  assert.deepEqual(videoOnly.tracks.map((track) => track.handler), ['vide']);
  const audioOnly = await read(ftyp(), moov(audioTrak()), mdat());
  assert.deepEqual(audioOnly.tracks.map((track) => track.handler), ['soun']);
});

test('two audio tracks are both listed', async () => {
  const info = await read(ftyp(), moov(videoTrak(), audioTrak(), audioTrak()), mdat());
  assert.equal(info.tracks.filter((track) => track.handler === 'soun').length, 2);
});
