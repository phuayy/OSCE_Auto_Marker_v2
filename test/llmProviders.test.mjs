// Unit tests for the scoring-model settings form logic.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  CUSTOM_MODEL,
  NO_FALLBACK,
  buildSettingsPayload,
  describeRouting,
  effectiveDiffers,
  findProvider,
  isCustomModel,
  resolveFallbackTarget,
  resolveModelId,
  resolvePrimaryProviderId,
  routingWarnings,
  validateRouting,
} from '../src/lib/llmProviders.js';

const NVIDIA = {
  id: 'nvidia',
  label: 'NVIDIA NIM',
  defaultModelId: 'nvidia/nemotron-3-super-120b-a12b',
  allowsCustomModel: true,
  availability: { available: true, reason: '' },
  models: [
    { id: 'nvidia/nemotron-3-super-120b-a12b', label: 'Nemotron 3 Super 120B', contextWindow: 128000 },
    { id: 'meta/llama-3.3-70b-instruct', label: 'Llama 3.3 70B' },
  ],
};

const OPENAI = {
  id: 'openai',
  label: 'OpenAI',
  defaultModelId: 'gpt-4.1',
  allowsCustomModel: true,
  availability: { available: false, reason: 'No API key configured. Set OPENAI_API_KEY.' },
  requirements: 'Set OPENAI_API_KEY.',
  models: [{ id: 'gpt-4.1', label: 'GPT-4.1', contextWindow: 1000000 }],
};

const DESCRIPTION = {
  providers: [NVIDIA, OPENAI],
  defaultProviderId: 'nvidia',
  selected: { primary: { providerId: 'nvidia', model: 'meta/llama-3.3-70b-instruct' }, fallbacks: [] },
  effective: { primary: { providerId: 'nvidia', model: 'meta/llama-3.3-70b-instruct' }, fallbacks: [] },
};

test('primary selection falls back to the deployment default, then to the first provider', () => {
  assert.equal(resolvePrimaryProviderId(DESCRIPTION), 'nvidia');
  assert.equal(
    resolvePrimaryProviderId({ ...DESCRIPTION, selected: { primary: { providerId: 'retired' } } }),
    'nvidia',
  );
  assert.equal(
    resolvePrimaryProviderId({ providers: [OPENAI], selected: {}, defaultProviderId: 'gone' }),
    'openai',
  );
  assert.equal(resolvePrimaryProviderId({ providers: [] }), '');
});

test('a fallback naming a provider this build dropped reads as "no fallback"', () => {
  const description = { ...DESCRIPTION, selected: { primary: {}, fallbacks: [{ providerId: 'retired', model: 'x' }] } };

  assert.deepEqual(resolveFallbackTarget(description), { providerId: NO_FALLBACK, model: '' });
});

test('only the first stored fallback is surfaced, so the list can grow later', () => {
  const description = {
    ...DESCRIPTION,
    selected: {
      primary: {},
      fallbacks: [
        { providerId: 'openai', model: 'gpt-4.1' },
        { providerId: 'nvidia', model: 'x' },
      ],
    },
  };

  assert.deepEqual(resolveFallbackTarget(description), { providerId: 'openai', model: 'gpt-4.1' });
});

test('an empty stored model resolves to the provider default', () => {
  assert.equal(resolveModelId(NVIDIA, ''), 'nvidia/nemotron-3-super-120b-a12b');
  assert.equal(resolveModelId(NVIDIA, 'meta/llama-3.3-70b-instruct'), 'meta/llama-3.3-70b-instruct');
});

test('a model id outside the shortlist needs the free-text field', () => {
  assert.equal(isCustomModel(NVIDIA, 'nvidia/nemotron-3-super-120b-a12b'), false);
  assert.equal(isCustomModel(NVIDIA, 'some/new-model-2027'), true);
  assert.equal(isCustomModel(NVIDIA, ''), false);
});

test('validation requires a provider and a model', () => {
  assert.deepEqual(validateRouting({ description: DESCRIPTION, primary: { providerId: 'nvidia', model: 'x' } }), {});
  assert.ok(validateRouting({ description: DESCRIPTION, primary: { providerId: '' } }).primaryProvider);
  assert.ok(
    validateRouting({ description: DESCRIPTION, primary: { providerId: 'nvidia', model: '  ' } }).primaryModel,
  );
  assert.ok(
    validateRouting({ description: DESCRIPTION, primary: { providerId: 'retired', model: 'x' } }).primaryProvider,
  );
});

test('warnings name the traps: no fallback, unusable key, identical targets', () => {
  const noFallback = routingWarnings({
    description: DESCRIPTION,
    primary: { providerId: 'nvidia', model: 'a' },
    fallback: { providerId: NO_FALLBACK, model: '' },
  });
  assert.ok(noFallback.some((warning) => warning.includes('No fallback selected')));

  const keylessFallback = routingWarnings({
    description: DESCRIPTION,
    primary: { providerId: 'nvidia', model: 'a' },
    fallback: { providerId: 'openai', model: 'gpt-4.1' },
  });
  assert.ok(keylessFallback.some((warning) => warning.includes('cannot actually stand in')));

  const identical = routingWarnings({
    description: DESCRIPTION,
    primary: { providerId: 'nvidia', model: 'a' },
    fallback: { providerId: 'nvidia', model: 'a' },
  });
  assert.ok(identical.some((warning) => warning.includes('identical to the primary')));

  const sameVendor = routingWarnings({
    description: DESCRIPTION,
    primary: { providerId: 'nvidia', model: 'a' },
    fallback: { providerId: 'nvidia', model: 'b' },
  });
  assert.ok(sameVendor.some((warning) => warning.includes('same provider')));
});

test('a primary with no key warns that scoring will fail over', () => {
  const warnings = routingWarnings({
    description: DESCRIPTION,
    primary: { providerId: 'openai', model: 'gpt-4.1' },
    fallback: { providerId: 'nvidia', model: 'a' },
  });

  assert.ok(warnings.some((warning) => warning.includes('OPENAI_API_KEY')));
});

test('the saved payload preserves unrelated settings and drops an empty fallback', () => {
  const payload = buildSettingsPayload(
    { llmTranscriptPreprocess: true, transcriptionEngine: 'whisperx' },
    { primary: { providerId: 'nvidia', model: '  m1  ' }, fallback: { providerId: NO_FALLBACK, model: 'x' } },
  );

  assert.equal(payload.llmTranscriptPreprocess, true);
  assert.equal(payload.transcriptionEngine, 'whisperx');
  assert.deepEqual(payload.llmPrimary, { providerId: 'nvidia', model: 'm1' });
  assert.deepEqual(payload.llmFallbacks, []);
});

test('the saved payload keeps a real fallback', () => {
  const payload = buildSettingsPayload(
    {},
    { primary: { providerId: 'nvidia', model: 'm1' }, fallback: { providerId: 'openai', model: 'gpt-4.1' } },
  );

  assert.deepEqual(payload.llmFallbacks, [{ providerId: 'openai', model: 'gpt-4.1' }]);
});

test('routing summaries read as a chain, and an unconfigured one says so', () => {
  assert.equal(
    describeRouting({ primary: { providerId: 'nvidia', model: 'm1' }, fallbacks: [{ providerId: 'openai', model: 'g' }] }),
    'nvidia:m1 → openai:g',
  );
  assert.equal(describeRouting({}), 'Not configured');
});

test('a server-side drop of a keyless target is surfaced as a difference', () => {
  assert.equal(effectiveDiffers(DESCRIPTION), false);
  assert.equal(
    effectiveDiffers({
      selected: { primary: { providerId: 'openai', model: 'g' }, fallbacks: [] },
      effective: { primary: { providerId: 'nvidia', model: 'm1' }, fallbacks: [] },
    }),
    true,
  );
});

test('the custom-model sentinel is never a real model id', () => {
  assert.equal(findProvider(DESCRIPTION, 'nvidia').models.some((model) => model.id === CUSTOM_MODEL), false);
});
