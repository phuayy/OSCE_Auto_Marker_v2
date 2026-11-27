import test, { beforeEach } from 'node:test';
import assert from 'node:assert/strict';

import {
  assessPdfFile,
  assessVideoFile,
  bestCanPlay,
  blockingReason,
  canPlayTypeFor,
  checkPdfHeader,
  classifyVideoFile,
  describeCodecs,
  fetchUploadLimits,
  formatFileSize,
  normalizeUploadLimits,
  readPdfHeaderCheck,
  resetUploadLimitsCache,
  startBlockingReason,
  shouldReadContainer,
} from '../src/lib/uploadPreview.js';

const GB = 1024 ** 3;
const LIMITS = {
  maxVideoBytes: 4 * GB,
  maxPdfBytes: 20 * 1024 * 1024,
  videoExtensions: ['.mp4', '.mov', '.mkv', '.webm', '.m4v', '.avi'],
};
const file = (overrides = {}) => ({ name: 'station.mp4', type: 'video/mp4', size: 500 * 1024 * 1024, ...overrides });
const video = (overrides = {}) => ({
  handler: 'vide', width: 1920, height: 1080, rotation: 0, codec: 'avc1', codecString: 'avc1.64001F', bitDepth: 8, ...overrides,
});
const audio = (overrides = {}) => ({ handler: 'soun', width: 0, height: 0, rotation: 0, codec: 'mp4a', ...overrides });
const container = (tracks, durationSeconds = 600) => ({ durationSeconds, tracks });
const loaded = { canPlay: 'probably', metadataLoaded: true, decodeError: false, durationSeconds: 600, videoWidth: 1920, videoHeight: 1080 };
const codes = (result) => result.warnings.map((item) => item.code);
const assess = (args) => assessVideoFile({ file: file(), limits: LIMITS, ...args });

beforeEach(() => resetUploadLimitsCache());

test('formats sizes readably', () => {
  assert.equal(formatFileSize(0), '0 B');
  assert.equal(formatFileSize(512), '512 B');
  assert.equal(formatFileSize(4 * GB), '4 GB');
  assert.equal(formatFileSize(1.5 * GB), '1.5 GB');
  assert.equal(formatFileSize(250 * 1024 * 1024), '250 MB');
  assert.equal(formatFileSize(NaN), '0 B');
});

test('classifies by extension, falling back to the declared type', () => {
  assert.equal(classifyVideoFile(file({ name: 'A.MOV' }), LIMITS).family, 'mov');
  assert.equal(classifyVideoFile(file({ name: 'A.MOV' }), LIMITS).extension, '.mov');
  assert.equal(classifyVideoFile(file({ name: 'a.m4v' }), LIMITS).family, 'mp4');
  assert.equal(classifyVideoFile(file({ name: 'a.MKV', type: '' }), LIMITS).family, 'mkv');
  assert.equal(classifyVideoFile(file({ name: 'noext', type: 'video/webm' }), LIMITS).family, 'webm');
  assert.equal(classifyVideoFile(file({ name: 'noext', type: '' }), LIMITS).family, 'other');
  assert.equal(classifyVideoFile(file({ name: '.mp4', type: '' }), LIMITS).extension, '', 'a dotfile has no extension');
});

test('shouldReadContainer is for mp4 and mov families only', () => {
  assert.equal(shouldReadContainer(file({ name: 'a.mov' })), true);
  assert.equal(shouldReadContainer(file({ name: 'a.m4v' })), true);
  assert.equal(shouldReadContainer(file({ name: 'a.mkv' })), false);
});

test('H.264 + AAC with metadata loaded plays', () => {
  const result = assess({ container: container([video(), audio()]), browser: loaded });
  assert.equal(result.verdict, 'plays');
  assert.deepEqual(codes(result), []);
  assert.deepEqual(result.facts, {
    durationSeconds: 600, width: 1920, height: 1080, rotation: 0,
    videoCodec: 'avc1', audioCodec: 'mp4a', audioTracks: 1, bitDepth: 8,
  });
});

test('HEVC converts unless the browser decodes it', () => {
  for (const codec of ['hvc1', 'hev1']) {
    const tracks = [video({ codec, codecString: `${codec}.1.6.L93.B0`, bitDepth: 8 }), audio()];
    const refused = assess({ container: container(tracks), browser: { ...loaded, canPlay: '', metadataLoaded: false } });
    assert.equal(refused.verdict, 'will-convert');
    assert.equal(refused.warnings[0].code, 'will_convert');
    assert.equal(assess({ container: container(tracks), browser: { ...loaded, canPlay: '' } }).verdict, 'will-convert', 'canPlay empty');
    assert.equal(assess({ container: container(tracks), browser: { ...loaded, canPlay: 'maybe', metadataLoaded: false } }).verdict, 'will-convert', 'not loaded');
    assert.equal(assess({ container: container(tracks), browser: loaded }).verdict, 'plays', 'Safari');
    assert.equal(assess({ container: container(tracks), browser: { ...loaded, canPlay: 'maybe' } }).verdict, 'plays');
  }
});

test('10-bit H.264 and G.711 audio convert', () => {
  assert.equal(assess({ container: container([video({ bitDepth: 10 }), audio()]), browser: { ...loaded, canPlay: '' } }).verdict, 'will-convert');
  assert.equal(assess({ container: container([video(), audio({ codec: 'alaw' })]), browser: loaded }).verdict, 'will-convert');
});

test('a decode error after canPlay maybe converts', () => {
  const result = assess({ container: container([video(), audio()]), browser: { ...loaded, canPlay: 'maybe', metadataLoaded: false, decodeError: true } });
  assert.equal(result.verdict, 'will-convert');
});

test('mkv and avi the browser cannot open are unknown, not rejected', () => {
  const pending = { canPlay: '', metadataLoaded: false, decodeError: false };
  for (const name of ['a.mkv', 'a.avi']) {
    const result = assess({ file: file({ name, type: '' }), browser: pending });
    assert.equal(result.verdict, 'unknown');
    assert.equal(blockingReason(result), null);
  }
  assert.equal(assess({ file: file({ name: 'a.mkv', type: '' }), browser: { ...pending, decodeError: true } }).verdict, 'will-convert');
});

test('a browser-readable mkv plays and its facts come from the element', () => {
  const result = assess({ file: file({ name: 'a.mkv', type: '' }), browser: { ...loaded, canPlay: 'maybe', durationSeconds: 90 } });
  assert.equal(result.verdict, 'plays');
  assert.equal(result.facts.durationSeconds, 90);
  assert.equal(result.facts.width, 1920);
  assert.equal(result.facts.audioTracks, null, 'unknown without the container');
  assert.deepEqual(codes(result), []);
});

test('an mp4 whose structure cannot be read, failing to decode, is unknown with a warning', () => {
  const result = assess({ container: null, browser: { canPlay: 'maybe', metadataLoaded: false, decodeError: true } });
  assert.equal(result.verdict, 'unknown');
  assert.ok(codes(result).includes('unknown_container'));
});

test('unaccepted extensions and over-limit sizes are rejected, with a reason', () => {
  const wmv = assess({ file: file({ name: 'a.WMV', type: 'video/x-ms-wmv' }), browser: loaded });
  assert.equal(wmv.verdict, 'rejected');
  assert.deepEqual(codes(wmv), ['extension_not_accepted']);
  assert.match(blockingReason(wmv), /\.wmv files cannot be uploaded.*MP4, MOV, MKV/);
  const none = assess({ file: file({ name: 'station', type: '' }) });
  assert.equal(none.verdict, 'rejected');
  const big = assess({ file: file({ size: 4 * GB + 1 }), browser: loaded });
  assert.equal(big.verdict, 'rejected');
  assert.match(blockingReason(big), /4 GB upload limit/);
});

test('exactly at the limit passes; one byte over does not', () => {
  assert.equal(assess({ file: file({ size: 4 * GB }), browser: loaded }).verdict, 'plays');
  assert.equal(assess({ file: file({ size: 4 * GB + 1 }), browser: loaded }).verdict, 'rejected');
});

test('unknown limits skip every limit check', () => {
  const result = assessVideoFile({ file: file({ name: 'a.wmv', size: 900 * GB }), limits: null, browser: loaded });
  assert.notEqual(result.verdict, 'rejected');
  assert.equal(codes(result).includes('over_size_limit'), false);
});

test('a zero-byte file is rejected', () => {
  const result = assess({ file: file({ size: 0 }) });
  assert.equal(result.verdict, 'rejected');
  assert.deepEqual(codes(result), ['empty_file']);
});

test('track warnings: no audio, no video, several audio tracks', () => {
  assert.ok(codes(assess({ container: container([video()]), browser: loaded })).includes('no_audio_track'));
  const audioOnly = assess({ container: container([audio()]), browser: loaded });
  assert.deepEqual(codes(audioOnly), ['no_video_track']);
  assert.equal(audioOnly.facts.audioTracks, 1);
  const two = assess({ container: container([video(), audio(), audio()]), browser: loaded });
  assert.ok(codes(two).includes('multiple_audio_tracks'));
  assert.match(two.warnings.find((item) => item.code === 'multiple_audio_tracks').message, /2 audio tracks/);
});

test('rotation of 90 or 270 swaps the displayed size; 180 does not', () => {
  for (const rotation of [90, 270]) {
    const result = assess({ container: container([video({ rotation }), audio()]), browser: loaded });
    assert.equal(result.facts.width, 1080);
    assert.equal(result.facts.height, 1920);
    assert.equal(result.facts.rotation, rotation);
    assert.ok(codes(result).includes('rotated'));
  }
  const upside = assess({ container: container([video({ rotation: 180 }), audio()]), browser: loaded });
  assert.equal(upside.facts.width, 1920);
});

test('very long and very short recordings are flagged; boundaries are not', () => {
  const at = (seconds) => assess({ container: container([video(), audio()], seconds), browser: { ...loaded, durationSeconds: seconds } });
  assert.ok(codes(at(3 * 3600 + 1)).includes('very_long'));
  assert.equal(codes(at(3 * 3600)).includes('very_long'), false);
  assert.ok(codes(at(29)).includes('very_short'));
  assert.equal(codes(at(30)).includes('very_short'), false);
  const unknown = assess({ browser: { ...loaded, durationSeconds: Infinity }, file: file({ name: 'a.mkv' }) });
  assert.equal(unknown.facts.durationSeconds, null);
  assert.equal(codes(unknown).includes('very_short'), false);
});

test('codec names stay in the details line, not in a headline', () => {
  const facts = assess({ container: container([video({ codec: 'hvc1', bitDepth: 10 }), audio()]), browser: loaded }).facts;
  assert.equal(describeCodecs(facts), 'HEVC (H.265) video, AAC audio, 10-bit');
  const result = assess({ container: container([video({ codec: 'hvc1' }), audio()]), browser: { ...loaded, canPlay: '' } });
  assert.doesNotMatch(result.warnings[0].message, /HEVC|H\.26/);
});

test('canPlayTypeFor prefers the sniffed codecs, then the declared type, then the extension', () => {
  assert.equal(canPlayTypeFor(file(), container([video(), audio()])), 'video/mp4; codecs="avc1.64001F, mp4a.40.2"');
  assert.equal(canPlayTypeFor(file(), container([video()])), 'video/mp4; codecs="avc1.64001F"');
  assert.equal(canPlayTypeFor(file({ type: 'video/quicktime' }), container([video({ codecString: null, codec: 'vp09' })])), 'video/quicktime');
  assert.equal(canPlayTypeFor(file({ name: 'a.mkv', type: '' })), 'video/x-matroska');
  assert.equal(canPlayTypeFor(file({ name: 'a.xyz', type: '' })), null);
});

test('bestCanPlay takes the strongest answer', () => {
  assert.equal(bestCanPlay(['', 'maybe']), 'maybe');
  assert.equal(bestCanPlay(['maybe', 'probably', '']), 'probably');
  assert.equal(bestCanPlay([]), '');
});

test('PDF header check: true, false and short buffers', () => {
  const bytes = (text) => Uint8Array.from([...text].map((c) => c.charCodeAt(0)));
  assert.equal(checkPdfHeader(bytes('%PDF-1.7\n')), true);
  assert.equal(checkPdfHeader(bytes('%PDF-1.7').buffer), true);
  assert.equal(checkPdfHeader(bytes('PK\u0003\u0004zip')), false);
  assert.equal(checkPdfHeader(bytes('%PDF')), false);
  assert.equal(checkPdfHeader(new Uint8Array(0)), false);
  assert.equal(checkPdfHeader(null), false);
  assert.equal(checkPdfHeader(bytes('xx%PDF-')), false);
});

test('readPdfHeaderCheck reads the first bytes, and answers null when it cannot', async () => {
  const text = (value) => ({ slice: (s, e) => ({ arrayBuffer: async () => Uint8Array.from([...value.slice(s, e)].map((c) => c.charCodeAt(0))).buffer }) });
  assert.equal(await readPdfHeaderCheck(text('%PDF-1.4 ...')), true);
  assert.equal(await readPdfHeaderCheck(text('hello world')), false);
  assert.equal(await readPdfHeaderCheck({ slice() { throw new Error('gone'); } }), null);
});

test('assessPdfFile warns about empty, not-a-PDF and oversize files', () => {
  const pdf = { name: 'case.pdf', type: 'application/pdf', size: 1000 };
  assert.deepEqual(assessPdfFile({ file: pdf, limits: LIMITS, headerOk: true }), { ok: true, warnings: [] });
  assert.equal(assessPdfFile({ file: pdf, headerOk: null }).ok, true, 'unreadable header is not a verdict');
  assert.deepEqual(assessPdfFile({ file: pdf, headerOk: false }).warnings.map((w) => w.code), ['not_a_pdf']);
  assert.deepEqual(assessPdfFile({ file: { ...pdf, size: 0 }, headerOk: false }).warnings.map((w) => w.code), ['empty_file']);
  assert.deepEqual(assessPdfFile({ file: { ...pdf, size: 21 * 1024 * 1024 }, limits: LIMITS, headerOk: true }).warnings.map((w) => w.code), ['over_size_limit']);
  assert.equal(assessPdfFile({ file: { ...pdf, size: 21 * 1024 * 1024 }, limits: null, headerOk: true }).ok, true);
});

test('normalizeUploadLimits trusts only well-formed fields', () => {
  assert.equal(normalizeUploadLimits(null), null);
  assert.equal(normalizeUploadLimits('x'), null);
  assert.deepEqual(
    normalizeUploadLimits({ maxVideoBytes: 5, maxPdfBytes: -1, videoExtensions: ['MP4', '.mov', 3, ''], maxConcurrentUploads: 2.5, maxBatchSize: 10 }),
    { maxVideoBytes: 5, maxPdfBytes: null, videoExtensions: ['.mp4', '.mov'], maxConcurrentUploads: null, maxBatchSize: 10 },
  );
  assert.equal(normalizeUploadLimits({}).videoExtensions, null);
});

test('fetchUploadLimits asks once, shares an in-flight request, and retries after a failure', async () => {
  let calls = 0;
  const ok = async (url) => {
    calls += 1;
    assert.equal(url, '/api/uploads/limits');
    return { maxVideoBytes: 10, videoExtensions: ['.mp4'] };
  };
  const [a, b] = await Promise.all([fetchUploadLimits(ok), fetchUploadLimits(ok)]);
  assert.equal(calls, 1);
  assert.equal(a.maxVideoBytes, 10);
  assert.equal(b, a);
  await fetchUploadLimits(ok);
  assert.equal(calls, 1, 'cached for the page');

  resetUploadLimitsCache();
  const failing = async () => { throw new Error('404'); };
  assert.equal(await fetchUploadLimits(failing), null);
  assert.equal((await fetchUploadLimits(ok)).maxVideoBytes, 10, 'a failure is not remembered');
});

test('Start is blocked by a definitely unusable case study, but not by a check that could not run', () => {
  const pdf = { name: 'case.pdf', type: 'application/pdf', size: 1000 };
  const goodVideo = assess({ browser: loaded });
  const reason = (args) => startBlockingReason(goodVideo, assessPdfFile({ limits: LIMITS, file: pdf, ...args }));
  assert.equal(reason({ headerOk: true }), null);
  assert.equal(reason({ headerOk: null }), null, 'unreadable header');
  assert.equal(startBlockingReason(goodVideo, assessPdfFile({ file: { ...pdf, size: 99 * GB }, limits: null, headerOk: true })), null, 'limits unknown');
  assert.equal(startBlockingReason(goodVideo, null), null, 'no case study yet');
  assert.match(reason({ headerOk: false }), /does not look like a PDF/);
  assert.match(reason({ headerOk: true, file: { ...pdf, size: 0 } }), /PDF is empty/);
  assert.match(reason({ headerOk: true, file: { ...pdf, size: 21 * 1024 * 1024 } }), /over the 20 MB limit/);
});

test('when both files are bad the reason names the recording first', () => {
  const badVideo = assess({ file: file({ name: 'a.wmv' }), browser: loaded });
  const badPdf = assessPdfFile({ file: { name: 'c.pdf', size: 10 }, headerOk: false });
  assert.match(startBlockingReason(badVideo, badPdf), /\.wmv files cannot be uploaded/);
  assert.match(startBlockingReason(assess({ browser: loaded }), badPdf), /PDF/);
});

test('batch mode shows only the case study; the single-file state is passed through otherwise', async () => {
  const { previewFiles } = await import('../src/lib/uploadPreview.js');
  const input = { videoFile: { name: 'a.mp4' }, videoUrl: 'blob:x', caseStudyFile: { name: 'c.pdf' } };
  assert.deepEqual(previewFiles({ ...input, batchMode: true }), { videoFile: null, videoUrl: null, caseStudyFile: input.caseStudyFile });
  assert.deepEqual(previewFiles({ ...input, batchMode: false }), input);
});

test('the preview shows a placeholder, not an empty black player, for a file that will not decode', async () => {
  const { previewSurface, Verdict } = await import('../src/lib/uploadPreview.js');
  const surface = (state) => previewSurface(state);
  assert.equal(surface({ verdict: Verdict.PLAYS, loaded: true, videoWidth: 320 }), 'player');
  assert.equal(surface({ verdict: Verdict.UNKNOWN }), 'player', 'still checking: the player stays');
  assert.equal(surface({ verdict: Verdict.PLAYS, failed: true }), 'placeholder', 'the element errored');
  assert.equal(surface({ verdict: Verdict.PLAYS, loaded: true, videoWidth: 0 }), 'placeholder', 'loaded with no picture');
  assert.equal(surface({ verdict: Verdict.WILL_CONVERT }), 'placeholder', 'HEVC and similar, before any picture');
  assert.equal(surface({ verdict: Verdict.WILL_CONVERT, loaded: true, videoWidth: 640 }), 'player', 'a conversion that this browser decodes anyway');
});
