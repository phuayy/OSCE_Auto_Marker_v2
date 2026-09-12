import { apiFetch } from './apiFetch.js';

export async function uploadFileToResumableSession(file, plan, onProgress, { signal, request = apiFetch } = {}) {
  if (!plan.uploadUrl) throw new Error('Upload plan did not include a resumable session URL.');
  const chunkSize = Number(plan.partSizeBytes);
  if (!Number.isSafeInteger(chunkSize) || chunkSize <= 0) {
    throw new Error('Upload plan did not include a valid chunk size.');
  }
  const send = (range, body) => request(plan.uploadUrl, {
    method: 'PUT', headers: { 'Content-Range': range }, body, signal,
    idempotent: true, reportConnection: false,
  });
  const offsetFromResponse = (response) => {
    if (response.ok) return file.size;
    if (response.status !== 308) throw new Error(`Upload failed (HTTP ${response.status}).`);
    const range = response.headers.get('Range');
    if (!range) return 0;
    const match = /^bytes=0-(\d+)$/.exec(range);
    const offset = match ? Number(match[1]) + 1 : NaN;
    if (!Number.isSafeInteger(offset) || offset < 1 || offset > file.size) {
      throw new Error('Upload server returned an invalid acknowledged range.');
    }
    return offset;
  };
  let offset = offsetFromResponse(await send(`bytes */${file.size}`));
  onProgress?.(offset, file.size, plan);
  while (offset < file.size) {
    const end = Math.min(offset + chunkSize, file.size);
    let response;
    try {
      response = await send(`bytes ${offset}-${end - 1}/${file.size}`, file.slice(offset, end));
    } catch (error) {
      if (signal?.aborted) throw error;
      response = await send(`bytes */${file.size}`);
    }
    const next = offsetFromResponse(response);
    if (next <= offset) throw new Error('Upload made no progress. Retry to resume from the saved offset.');
    if (!response.ok && next > end) throw new Error('Upload acknowledged bytes beyond the sent chunk.');
    offset = next;
    onProgress?.(offset, file.size, plan);
  }
}
