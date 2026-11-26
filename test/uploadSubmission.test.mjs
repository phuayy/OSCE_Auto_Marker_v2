// submitUpload against fake api / transports / tracker: the order of calls, the
// combined-bytes gauge, the one resume from the server's ledger, and the
// guarantee that two submissions share nothing.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ApiError, ERROR_KIND } from '../src/lib/apiFetch.js';
import { UPLOAD_PHASE } from '../src/lib/uploadTracking.js';
import { submitUpload } from '../src/lib/uploadSubmission.js';

const video = { name: 'v.mp4', type: '', size: 100 };
const caseStudy = { name: 'c.pdf', type: '', size: 40 };

function plans(strategy = 'local_multipart') {
  const extra = strategy === 'local_multipart' ? { partUrlTemplate: '/api/uploads/u1/parts/{partNumber}', partSizeBytes: 50 } : {};
  return [
    { kind: 'video', fileId: 'fv', strategy, ...extra },
    { kind: 'caseStudy', fileId: 'fc', strategy, ...extra },
  ];
}

function harness({ strategy, initiate, transferImpl, completeError, statusBody } = {}) {
  const calls = [];
  const tracker = {
    begin: (...a) => calls.push(['begin', ...a]),
    reportProgress: (...a) => calls.push(['progress', ...a]),
    setPhase: (...a) => calls.push(['phase', ...a]),
    note: (...a) => calls.push(['note', ...a]),
    forget: (...a) => calls.push(['forget', ...a]),
  };
  const api = {
    apiJson: async (url, options = {}) => {
      calls.push(['api', url, options.method || 'GET']);
      if (url === '/api/uploads/initiate') {
        return initiate ?? { uploadId: 'u1', session: { id: 's1' }, fileUploads: plans(strategy) };
      }
      if (url.endsWith('/complete')) {
        if (completeError) throw completeError;
        return {};
      }
      if (url === '/api/uploads/u1') return statusBody ?? { files: [] };
      return {};
    },
  };
  const transfers = [];
  const transports = {
    uploadParts: async (args) => {
      transfers.push({ file: args.file, completedParts: args.completedParts, concurrency: args.concurrency });
      args.onProgress(args.file.size / 2);
      if (transferImpl) await transferImpl(transfers.length, args);
      args.onProgress(args.file.size);
    },
    recordedPartNumbers: (status, fileId) => new Set([`${fileId}:1`]),
    uploadFileToResumableSession: async (file, plan, onProgress) => {
      transfers.push({ file, gcs: true });
      onProgress(file.size, file.size, plan);
    },
  };
  return { calls, tracker, api, transports, transfers };
}

const run = (h, extra = {}) =>
  submitUpload({
    request: { workflow: 'standard', autoProcess: true },
    files: { video, caseStudy },
    tracker: h.tracker,
    api: h.api,
    transports: h.transports,
    ...extra,
  });

const networkError = () => new ApiError('down', { kind: ERROR_KIND.NETWORK });

test('happy path: initiate, track, both files, finalizing, complete, done, forget', async () => {
  const h = harness();
  const created = [];
  const result = await run(h, { onSessionCreated: (s) => created.push(s) });

  assert.deepEqual(result, { sessionId: 's1', uploadId: 'u1', session: { id: 's1' } });
  assert.deepEqual(created, [{ id: 's1' }]);
  const names = h.calls.map((c) => c[0] + (c[1] && c[0] !== 'progress' ? `:${c[1]}` : ''));
  assert.equal(names[0], 'api:/api/uploads/initiate');
  assert.equal(names[1], 'begin:s1');
  assert.deepEqual(h.calls[1], ['begin', 's1', 140, { uploadId: 'u1' }]);
  assert.deepEqual(h.calls.slice(-4), [
    ['phase', 's1', UPLOAD_PHASE.FINALIZING],
    ['api', '/api/uploads/u1/complete', 'POST'],
    ['phase', 's1', UPLOAD_PHASE.DONE],
    ['forget', 's1'],
  ]);
});

test('onSessionCreated fires after the track opens and before the first byte', async () => {
  const h = harness();
  let seen = null;
  await run(h, {
    onSessionCreated: () => {
      seen = h.calls.map((c) => c[0]);
    },
  });
  assert.deepEqual(seen, ['api', 'begin']);
});

test('initiate body carries the request plus both file descriptors with default mime types', async () => {
  const h = harness();
  let body;
  const original = h.api.apiJson;
  h.api.apiJson = async (url, options) => {
    if (url === '/api/uploads/initiate') body = options.json;
    return original(url, options);
  };
  await run(h);
  assert.equal(body.workflow, 'standard');
  assert.deepEqual(body.files, [
    { kind: 'video', originalName: 'v.mp4', mimeType: 'video/mp4', sizeBytes: 100 },
    { kind: 'caseStudy', originalName: 'c.pdf', mimeType: 'application/pdf', sizeBytes: 40 },
  ]);
});

test('progress is reported against the combined size, labelled per file', async () => {
  const h = harness();
  await run(h);
  const progress = h.calls.filter((c) => c[0] === 'progress').map((c) => [c[2], c[3]]);
  assert.deepEqual(progress, [
    [50, 'video'],
    [100, 'video'],
    [120, 'case study'],
    [140, 'case study'],
  ]);
});

test('a network error resumes once from the ledger with the recorded parts', async () => {
  const h = harness({
    transferImpl: async (n) => {
      if (n === 1) throw networkError();
    },
  });
  await run(h);
  assert.equal(h.transfers.length, 3);
  assert.equal(h.transfers[0].completedParts, undefined);
  assert.deepEqual([...h.transfers[1].completedParts], ['fv:1']);
  assert.ok(h.calls.some((c) => c[0] === 'note'));
  assert.ok(h.calls.some((c) => c[1] === '/api/uploads/u1' && c[2] === 'GET'));
});

test('a second failure propagates', async () => {
  const h = harness({
    transferImpl: async () => {
      throw networkError();
    },
  });
  await assert.rejects(run(h), /down/);
  assert.equal(h.transfers.length, 2);
});

test('a refused request (4xx) and an abort are not resumed', async () => {
  for (const kind of [ERROR_KIND.CLIENT, ERROR_KIND.ABORT]) {
    const h = harness({
      transferImpl: async () => {
        throw new ApiError('no', { kind, status: 403 });
      },
    });
    await assert.rejects(run(h), /no/);
    assert.equal(h.transfers.length, 1);
    assert.equal(h.calls.some((c) => c[0] === 'note'), false);
  }
});

test('a non-ApiError failure is resumed like a dropped connection', async () => {
  const h = harness({
    transferImpl: async (n) => {
      if (n === 1) throw new TypeError('socket');
    },
  });
  await run(h);
  assert.equal(h.transfers.length, 3);
});

test('gcs_resumable transfers through the resumable transport and never resumes', async () => {
  const h = harness({ strategy: 'gcs_resumable' });
  await run(h);
  assert.equal(h.transfers.length, 2);
  assert.ok(h.transfers.every((t) => t.gcs));

  const failing = harness({ strategy: 'gcs_resumable' });
  failing.transports.uploadFileToResumableSession = async () => {
    throw networkError();
  };
  await assert.rejects(run(failing), /down/);
});

test('an unsupported strategy or incomplete local plan is an error', async () => {
  await assert.rejects(run(harness({ strategy: 'carrier_pigeon' })), /Unsupported upload strategy: carrier_pigeon\./);
  const noTemplate = harness({
    initiate: { uploadId: 'u1', session: { id: 's1' }, fileUploads: plans().map((p) => ({ ...p, partUrlTemplate: '' })) },
  });
  await assert.rejects(run(noTemplate), /part URL template/);
  const noSize = harness({
    initiate: { uploadId: 'u1', session: { id: 's1' }, fileUploads: plans().map((p) => ({ ...p, partSizeBytes: 0 })) },
  });
  await assert.rejects(run(noSize), /valid part size/);
});

test('initiate without a session, or without both plans, fails before anything is tracked', async () => {
  const noSession = harness({ initiate: { uploadId: 'u1', fileUploads: plans() } });
  await assert.rejects(run(noSession), /did not return a session\./);
  const onePlan = harness({ initiate: { uploadId: 'u1', session: { id: 's1' }, fileUploads: plans().slice(0, 1) } });
  let created = false;
  await assert.rejects(run(onePlan, { onSessionCreated: () => (created = true) }), /both file upload plans/);
  assert.equal(created, false);
  assert.equal(onePlan.calls.some((c) => c[0] === 'begin'), false);
});

test('a failed complete rejects and leaves the track for the caller to fail', async () => {
  const h = harness({ completeError: new Error('Upload finalization failed.') });
  await assert.rejects(run(h), /finalization failed/);
  const phases = h.calls.filter((c) => c[0] === 'phase').map((c) => c[2]);
  assert.deepEqual(phases, [UPLOAD_PHASE.FINALIZING]);
  assert.equal(h.calls.some((c) => c[0] === 'forget'), false);
});

test('concurrent submissions keep their own sessions, bytes and results', async () => {
  const mk = (id, size) => {
    const h = harness({
      initiate: {
        uploadId: `u-${id}`,
        session: { id },
        fileUploads: plans(),
      },
    });
    return { h, files: { video: { name: `${id}.mp4`, size }, caseStudy: { name: 'c.pdf', size: 10 } } };
  };
  const a = mk('A', 200);
  const b = mk('B', 300);
  const [ra, rb] = await Promise.all(
    [a, b].map(({ h, files }) => run(h, { files, concurrency: 2 })),
  );
  assert.equal(ra.sessionId, 'A');
  assert.equal(rb.sessionId, 'B');
  for (const { h } of [a, b]) {
    const owners = new Set(h.calls.filter((c) => ['begin', 'progress', 'phase', 'forget'].includes(c[0])).map((c) => c[1]));
    assert.equal(owners.size, 1);
  }
  assert.equal(a.h.transfers[0].concurrency, 2);
  assert.deepEqual(a.h.calls[1], ['begin', 'A', 210, { uploadId: 'u-A' }]);
  assert.deepEqual(b.h.calls[1], ['begin', 'B', 310, { uploadId: 'u-B' }]);
});
