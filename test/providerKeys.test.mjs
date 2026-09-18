// Unit tests for the API-keys settings card logic.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  KEY_SOURCE,
  canStoreKeys,
  describeKeySource,
  describeLastTest,
  isKeyStoredInApp,
  isKeyUnreadable,
  keyEndpoint,
  storageBlockedReason,
  testModelFor,
  unconfiguredRoutedProviders,
  validateKeyInput,
} from '../src/lib/providerKeys.js';

function provider(id, credential, extra = {}) {
  return {
    id,
    label: id.toUpperCase(),
    vendor: id,
    apiKeyEnv: [`${id.toUpperCase()}_API_KEY`],
    defaultModelId: `${id}/default`,
    credential,
    ...extra,
  };
}

const SAVED = {
  source: KEY_SOURCE.APP,
  configured: true,
  maskedKey: '••••••cdef',
  updatedAt: '2026-09-09T10:00:00+00:00',
  updatedBy: 'admin',
  readable: true,
};
const FROM_ENV = { source: KEY_SOURCE.ENVIRONMENT, configured: true, maskedKey: '' };
const MISSING = { source: KEY_SOURCE.NONE, configured: false, maskedKey: '' };

test('a key saved in the app is reported as overriding the environment', () => {
  const described = describeKeySource(provider('openai', SAVED));
  assert.equal(described.tone, 'ok');
  assert.match(described.detail, /Overrides any value in the server environment/);
  assert.equal(isKeyStoredInApp(provider('openai', SAVED)), true);
});

test('an environment key names the variable and says a saved key replaces it live', () => {
  const described = describeKeySource(provider('openai', FROM_ENV));
  assert.equal(described.tone, 'info');
  assert.match(described.detail, /OPENAI_API_KEY/);
  assert.match(described.detail, /without a restart/);
  assert.equal(isKeyStoredInApp(provider('openai', FROM_ENV)), false);
});

test('a missing key is flagged as blocking scoring for that provider', () => {
  const described = describeKeySource(provider('openai', MISSING));
  assert.equal(described.tone, 'warn');
  assert.match(described.detail, /cannot be used for scoring/);
});

test('a stored key this deployment can no longer decrypt is called out separately', () => {
  // "Not configured" would send the operator hunting for the wrong problem:
  // the key is there, the encryption key that opens it is not.
  assert.equal(isKeyUnreadable(provider('openai', { ...SAVED, readable: false })), true);
  assert.equal(isKeyUnreadable(provider('openai', SAVED)), false);
  assert.equal(isKeyUnreadable(provider('openai', FROM_ENV)), false);
});

test('obvious paste errors are rejected before a round trip', () => {
  assert.match(validateKeyInput(''), /Enter an API key/);
  assert.match(validateKeyInput('   '), /Enter an API key/);
  // Saving the mask back would replace a working key with a row of dots.
  assert.match(validateKeyInput('••••••cdef'.replace(/[a-z]/g, '')), /masked preview/);
  assert.match(validateKeyInput('sk-abc def'), /cannot contain spaces/);
  assert.match(validateKeyInput('sk-1'), /truncated/);
  assert.match(validateKeyInput('x'.repeat(513)), /longer than/);
  assert.equal(validateKeyInput('  sk-live-abcdefghijkl  '), '');
});

test('the key endpoint escapes the provider id', () => {
  assert.equal(keyEndpoint('openai'), '/api/admin/settings/llm-providers/openai/key');
  assert.equal(keyEndpoint('a/b'), '/api/admin/settings/llm-providers/a%2Fb/key');
});

test('a provider test uses the model already selected for it, else its default', () => {
  const description = {
    providers: [provider('openai', SAVED), provider('deepseek', MISSING)],
    selected: {
      primary: { providerId: 'openai', model: 'gpt-4.1' },
      fallbacks: [{ providerId: 'deepseek', model: '' }],
    },
  };
  assert.equal(testModelFor(description, 'openai'), 'gpt-4.1');
  // Selected but with no explicit model, and a provider not selected at all,
  // both fall back to the provider's own default — testing a key must never
  // require first making that provider the primary.
  assert.equal(testModelFor(description, 'deepseek'), 'deepseek/default');
  assert.equal(testModelFor(description, 'nvidia'), '');
});

test('providers that scoring points at but that have no key are surfaced first', () => {
  const description = {
    providers: [provider('openai', SAVED), provider('deepseek', MISSING), provider('gemini', MISSING)],
    selected: {
      primary: { providerId: 'openai', model: 'gpt-4.1' },
      fallbacks: [{ providerId: 'deepseek', model: 'deepseek-chat' }],
    },
  };
  // gemini is unconfigured too, but nothing routes to it, so it is a note
  // rather than the reason the next run fails.
  assert.deepEqual(
    unconfiguredRoutedProviders(description).map((entry) => entry.id),
    ['deepseek'],
  );
});

test('a server with no encryption key hides the fields and says why', () => {
  const blocked = { credentialStorage: { available: false, reason: 'No CREDENTIAL_ENCRYPTION_KEY set.' } };
  assert.equal(canStoreKeys(blocked), false);
  assert.equal(storageBlockedReason(blocked), 'No CREDENTIAL_ENCRYPTION_KEY set.');
  assert.equal(canStoreKeys({ credentialStorage: { available: true } }), true);
  assert.equal(storageBlockedReason({ credentialStorage: { available: true } }), '');
});

test('the last recorded test survives a reload, with its failure text', () => {
  assert.equal(describeLastTest(provider('openai', SAVED)), null);
  const verified = describeLastTest(
    provider('openai', { ...SAVED, lastTestOk: true, lastTestedAt: '2026-09-09T10:05:00+00:00' }),
  );
  assert.equal(verified.ok, true);
  const failed = describeLastTest(
    provider('openai', {
      ...SAVED,
      lastTestOk: false,
      lastTestedAt: '2026-09-09T10:05:00+00:00',
      lastTestError: '401 Unauthorized',
    }),
  );
  assert.equal(failed.ok, false);
  assert.match(failed.text, /401 Unauthorized/);
});
