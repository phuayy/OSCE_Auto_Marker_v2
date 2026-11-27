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
  formatOutputLimit,
  findProvider,
  isCustomModel,
  resolveFallbackTarget,
  resolveModelId,
  resolvePrimaryProviderId,
  routingWarnings,
  shouldShowCustomModelField,
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

test('picking "Other" shows the free-text field even before anything is typed', () => {
  // Regression: isCustomModel(NVIDIA, '') is false by design (a fresh, empty
  // target isn't "custom", just unset yet) — so the picker cannot derive
  // "operator just chose Other" from the stored model alone. It has to carry
  // that bit itself, and shouldShowCustomModelField is where it is combined
  // with the data-driven answer.
  assert.equal(shouldShowCustomModelField(NVIDIA, '', true), true);
  assert.equal(shouldShowCustomModelField(NVIDIA, '', false), false);
  // A model already outside the shortlist still needs the field regardless
  // of whether "Other" was the thing that put it there this session.
  assert.equal(shouldShowCustomModelField(NVIDIA, 'some/new-model-2027', false), true);
  // A recognised model wins once it is actually chosen from the dropdown —
  // TargetPicker clears the intent flag itself when that happens, but the
  // helper is defensive either way for a real, listed id.
  assert.equal(shouldShowCustomModelField(NVIDIA, 'nvidia/nemotron-3-super-120b-a12b', false), false);
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

test('the routing patch carries only the routing keys and drops an empty fallback', () => {
  const payload = buildSettingsPayload({
    primary: { providerId: 'nvidia', model: '  m1  ' },
    fallback: { providerId: NO_FALLBACK, model: 'x' },
  });

  assert.deepEqual(Object.keys(payload).sort(), ['llmFallbacks', 'llmPrimary']);
  assert.deepEqual(payload.llmPrimary, { providerId: 'nvidia', model: 'm1' });
  assert.deepEqual(payload.llmFallbacks, []);
});

test('the saved payload keeps a real fallback', () => {
  const payload = buildSettingsPayload({
    primary: { providerId: 'nvidia', model: 'm1' },
    fallback: { providerId: 'openai', model: 'gpt-4.1' },
  });

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

test('formatOutputLimit names a model\'s output ceiling and stays silent when unknown', () => {
  assert.equal(formatOutputLimit({ maxOutputTokens: 8192 }), '8K max output');
  assert.equal(formatOutputLimit({ maxOutputTokens: 500 }), '500 max output');
  assert.equal(formatOutputLimit({ maxOutputTokens: 0 }), '');
  assert.equal(formatOutputLimit(null), '');
});

// ---- Merging a catalogue refresh into a form (M30) --------------------------

import {
  describeTestFailure,
  resolveRoutingState,
  settleMarkers,
  settleTarget,
} from '../src/lib/llmProviders.js';

const EMPTY = { providerId: '', model: '' };
const t = (providerId, model) => ({ providerId, model });

test('settleTarget: a clean field takes the refreshed value', () => {
  const baseline = t('nvidia', 'a');
  const out = settleTarget(t('nvidia', 'a'), baseline, t('openai', 'gpt-4.1'), { providers: [NVIDIA, OPENAI] }, EMPTY);
  assert.deepEqual(out, t('openai', 'gpt-4.1'));
});

test('settleTarget: a dirty field keeps the unsaved edit', () => {
  const out = settleTarget(t('openai', 'gpt-4.1'), t('nvidia', 'a'), t('nvidia', 'b'), { providers: [NVIDIA, OPENAI] }, EMPTY);
  assert.deepEqual(out, t('openai', 'gpt-4.1'));
});

test('settleTarget: a dirty selection whose provider left the catalogue is cleared', () => {
  const out = settleTarget(t('gone', 'x'), t('nvidia', 'a'), t('nvidia', 'a'), { providers: [NVIDIA] }, EMPTY);
  assert.deepEqual(out, EMPTY);
  // and the form rules flag it rather than keeping it silently
  assert.ok(validateRouting({ description: { providers: [NVIDIA] }, primary: out }).primaryProvider);
});

test('settleTarget: the fallback role clears to its own none value', () => {
  const out = settleTarget(t('gone', 'x'), t('nvidia', 'a'), t('nvidia', 'a'), { providers: [NVIDIA] }, t(NO_FALLBACK, ''));
  assert.deepEqual(out, t(NO_FALLBACK, ''));
});

test('settleMarkers: clean list refreshes, dirty list is kept minus removed providers', () => {
  const description = { providers: [NVIDIA, OPENAI] };
  const baseline = [t('nvidia', 'a'), t('openai', 'gpt-4.1')];
  const fresh = [t('nvidia', 'b'), t('openai', 'gpt-4.1')];
  assert.deepEqual(settleMarkers(baseline, baseline, fresh, description), fresh);
  const dirty = [t('nvidia', 'a'), t('gone', 'z')];
  assert.deepEqual(settleMarkers(dirty, baseline, fresh, description), [t('nvidia', 'a'), EMPTY]);
});

test('resolveRoutingState matches what the card shows on load', () => {
  const description = {
    providers: [NVIDIA, OPENAI],
    defaultProviderId: 'nvidia',
    selected: { primary: t('openai', ''), fallbacks: [t('nvidia', 'm')] },
  };
  const state = resolveRoutingState(description);
  assert.deepEqual(state.primary, t('openai', 'gpt-4.1'));
  assert.deepEqual(state.fallback, t('nvidia', 'm'));
});

// ---- A Test that the caller may not run (M32) -------------------------------

test('describeTestFailure: a 403 is a permission message, not a provider failure', () => {
  const out = describeTestFailure({ status: 403, message: 'Forbidden' });
  assert.equal(out.ok, false);
  assert.equal(out.denied, true);
  assert.match(out.error, /administrator/i);
});

test('describeTestFailure: any other error stays a failed probe', () => {
  const out = describeTestFailure({ status: 502, message: 'Bad gateway' });
  assert.equal(out.denied, undefined);
  assert.equal(out.error, 'Bad gateway');
  assert.match(describeTestFailure(new Error('')).error, /could not be run/);
});
