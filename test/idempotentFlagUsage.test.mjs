// D3: `idempotent: true` opts a write into apiFetch's automatic retry (see
// the "Unsafe methods are never retried automatically" note in
// lib/apiFetch.js). The default already protects the common mistake — a
// caller that says nothing gets zero retries, so forgetting the flag on a
// non-idempotent mutation is safe by construction. The opposite mistake is
// the one nothing catches: someone adds `idempotent: true` to a write that
// is *not* actually safe to repeat (a call that creates a row, starts a job,
// or otherwise has a side effect keyed by "how many times this ran" rather
// than "did this happen at least once"), and apiFetch silently retries it
// on a lost response.
//
// This is a structural test, the same shape as apiClientCoverage.test.mjs:
// every `idempotent: true` in the app must carry a comment, within a few
// lines, saying *why* the write is safe to repeat — so a reviewer (or this
// test's next reader) can check the claim against the endpoint's actual
// behaviour instead of taking the flag on faith. It does not prove the
// claim true; it makes the claim impossible to omit.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = fileURLToPath(new URL('../src/', import.meta.url));

// apiFetch.js declares and documents the option itself, not a call site.
const EXEMPT = new Set(['lib/apiFetch.js']);

// The justifying comment sits above the whole call expression, not
// necessarily right above the flag itself — an object literal's other
// fields (method, headers, body) can separate them by a dozen-odd lines.
const LOOKBACK_LINES = 15;

function sourceFiles(dir, prefix = '') {
  const found = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      found.push(...sourceFiles(full, `${prefix}${entry}/`));
    } else if (/\.(js|jsx)$/.test(entry)) {
      found.push([`${prefix}${entry}`, full]);
    }
  }
  return found;
}

function isCommentLine(line) {
  const trimmed = line.trim();
  return trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*');
}

function hasJustificationAbove(lines, flagLineIndex) {
  const start = Math.max(0, flagLineIndex - LOOKBACK_LINES);
  for (let i = flagLineIndex; i >= start; i -= 1) {
    if (isCommentLine(lines[i])) return true;
  }
  return false;
}

test('every idempotent:true opt-in carries a nearby comment justifying it', () => {
  const offenders = [];
  for (const [name, path] of sourceFiles(SRC)) {
    if (EXEMPT.has(name)) continue;
    const lines = readFileSync(path, 'utf8').split('\n');
    lines.forEach((line, index) => {
      if (isCommentLine(line)) return;
      if (!/idempotent\s*:\s*true/.test(line)) return;
      if (!hasJustificationAbove(lines, index)) {
        offenders.push(`${name}:${index + 1}`);
      }
    });
  }

  assert.deepEqual(
    offenders,
    [],
    `idempotent: true must be justified by a nearby comment explaining why the write is safe to repeat:\n  ${offenders.join('\n  ')}`,
  );
});
