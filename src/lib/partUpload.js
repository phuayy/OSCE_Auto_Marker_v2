// Multipart upload as a pure scheduler.
//
// The component used to walk a file one part at a time with its own retry
// loop. Two things about the server changed what the client may do:
//
// 1. `PUT /uploads/{id}/parts/{n}` is now serialised per upload on the server,
//    so parts may be sent in parallel — the record cannot lose one to a
//    read-modify-write race any more. A bounded pool (`concurrency`) is the
//    throughput win that was previously unsafe.
// 2. Storing a part is idempotent (a same-numbered part replaces the previous
//    one), so a part whose response was lost can simply be sent again, and the
//    server's own ledger (`GET /uploads/{id}`) says which parts it already has.
//    That is what `completedParts` is for: a resume skips them.
//
// Nothing here touches the network. The caller passes `putPart`, which is
// where `apiFetch` (classification, jittered retries, reachability) lives, and
// this module only decides what to send, in what order, and how many at once.

export const DEFAULT_PART_CONCURRENCY = 3;

/**
 * The parts a file of `fileSize` bytes splits into at `partSize`. Part numbers
 * are 1-based, matching the API.
 */
export function planParts(fileSize, partSize) {
  const size = Number(fileSize);
  const step = Number(partSize);
  if (!Number.isFinite(size) || size < 0) {
    throw new Error('File size must be a non-negative number.');
  }
  if (!Number.isFinite(step) || step <= 0) {
    throw new Error('Part size must be a positive number.');
  }
  const parts = [];
  for (let offset = 0, partNumber = 1; offset < size; offset += step, partNumber += 1) {
    parts.push({ partNumber, offset, end: Math.min(offset + step, size) });
  }
  return parts;
}

/**
 * Part numbers the server has recorded for `fileId`, read from the body of
 * `GET /uploads/{id}`. Missing or malformed input yields an empty set: the
 * caller then re-sends everything, which is correct if slower.
 */
export function recordedPartNumbers(uploadStatusBody, fileId) {
  const files = uploadStatusBody?.upload?.files;
  if (!Array.isArray(files)) return new Set();
  const record = files.find((item) => String(item?.fileId) === String(fileId));
  const parts = Array.isArray(record?.parts) ? record.parts : [];
  return new Set(
    parts.map((part) => Number(part?.partNumber)).filter((n) => Number.isInteger(n) && n > 0),
  );
}

/**
 * Send every part of `file` not already in `completedParts`, at most
 * `concurrency` at a time. `putPart(partNumber, chunk)` performs one transfer
 * and rejects on failure; the first rejection stops new sends and is re-thrown
 * once in-flight sends settle, so the caller sees one error and no part is
 * left half-reported.
 *
 * `onProgress(uploadedBytes)` is called with the running byte total, counting
 * skipped parts as already uploaded so a resume starts where it left off.
 */
export async function uploadParts({
  file,
  partSize,
  putPart,
  onProgress,
  completedParts = new Set(),
  concurrency = DEFAULT_PART_CONCURRENCY,
}) {
  const parts = planParts(file.size, partSize);
  const pending = parts.filter((part) => !completedParts.has(part.partNumber));
  let uploadedBytes = parts
    .filter((part) => completedParts.has(part.partNumber))
    .reduce((sum, part) => sum + (part.end - part.offset), 0);
  onProgress?.(uploadedBytes);

  const workers = Math.max(1, Math.min(Number(concurrency) || 1, pending.length || 1));
  let next = 0;
  let failure = null;

  async function worker() {
    while (failure === null && next < pending.length) {
      const part = pending[next];
      next += 1;
      try {
        await putPart(part.partNumber, file.slice(part.offset, part.end));
      } catch (error) {
        failure = failure ?? error;
        return;
      }
      uploadedBytes += part.end - part.offset;
      onProgress?.(uploadedBytes);
    }
  }

  await Promise.all(Array.from({ length: workers }, () => worker()));
  if (failure !== null) {
    throw failure;
  }
  return { uploadedBytes, sentParts: pending.length, skippedParts: parts.length - pending.length };
}
