// Unit tests for the marking-mode (panel) settings form logic.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  MarkingMode,
  NO_ADJUDICATOR,
  TieBreak,
  buildMarkingPayload,
  describePanel,
  effectiveMarkingDiffers,
  panelWarnings,
  resolvePanelState,
  validatePanel,
} from '../src/lib/llmProviders.js';

const provider = (id, { ready = true, models = [`${id}-model`] } = {}) => ({
  id,
  label: id.toUpperCase(),
  defaultModelId: models[0] || '',
  allowsCustomModel: true,
  availability: { available: ready, reason: ready ? '' : `No API key configured. Set ${id.toUpperCase()}_API_KEY.` },
  models: models.map((model) => ({ id: model, label: model })),
});

const NVIDIA = provider('nvidia');
const GEMINI = provider('gemini');
const DEEPSEEK = provider('deepseek');
const OPENAI = provider('openai', { ready: false });

const DESCRIPTION = {
  providers: [NVIDIA, GEMINI, DEEPSEEK, OPENAI],
  defaultProviderId: 'nvidia',
  selected: { primary: { providerId: 'nvidia', model: 'nvidia-model' }, fallbacks: [] },
  marking: {
    mode: 'single',
    modes: ['single', 'panel'],
    tieBreaks: ['lenient', 'strict', 'first_marker'],
    selected: { markers: [], adjudicator: { providerId: '', model: '' }, tieBreak: 'lenient' },
    effective: { mode: 'single', warnings: [] },
    warnings: [],
  },
};

const STORED_PANEL = {
  ...DESCRIPTION,
  marking: {
    ...DESCRIPTION.marking,
    mode: 'panel',
    selected: {
      markers: [{ providerId: 'nvidia', model: 'nvidia-model' }, { providerId: 'gemini', model: '' }],
      adjudicator: { providerId: 'deepseek', model: 'deepseek-model' },
      tieBreak: 'strict',
    },
    effective: { mode: 'panel', warnings: [] },
  },
};

test('with nothing stored the form proposes the primary, another keyed provider and a third as adjudicator', () => {
  const state = resolvePanelState(DESCRIPTION);
  assert.equal(state.mode, MarkingMode.SINGLE);
  assert.deepEqual(state.markers, [
    { providerId: 'nvidia', model: 'nvidia-model' },
    { providerId: 'gemini', model: 'gemini-model' },
  ]);
  assert.deepEqual(state.adjudicator, { providerId: 'deepseek', model: 'deepseek-model' });
  assert.equal(state.tieBreak, TieBreak.LENIENT);
});

test('a stored panel is shown as stored, with provider defaults filled in for blank models', () => {
  const state = resolvePanelState(STORED_PANEL);
  assert.equal(state.mode, MarkingMode.PANEL);
  assert.deepEqual(state.markers, [
    { providerId: 'nvidia', model: 'nvidia-model' },
    { providerId: 'gemini', model: 'gemini-model' },
  ]);
  assert.deepEqual(state.adjudicator, { providerId: 'deepseek', model: 'deepseek-model' });
  assert.equal(state.tieBreak, TieBreak.STRICT);
});

test('a stored marker from a provider this build no longer ships is dropped and its slot refilled', () => {
  const state = resolvePanelState({
    ...STORED_PANEL,
    marking: {
      ...STORED_PANEL.marking,
      selected: { ...STORED_PANEL.marking.selected, markers: [{ providerId: 'retired', model: 'x' }] },
    },
  });
  assert.equal(state.markers.length, 2);
  assert.equal(state.markers.some((target) => target.providerId === 'retired'), false);
});

test('when only one provider has a key the second marker is left for the operator to fill', () => {
  const state = resolvePanelState({
    ...DESCRIPTION,
    providers: [NVIDIA, OPENAI],
  });
  assert.deepEqual(state.markers[1], { providerId: '', model: '' });
  assert.deepEqual(state.adjudicator, { providerId: NO_ADJUDICATOR, model: '' });
});

test('single mode needs no panel to be valid', () => {
  assert.deepEqual(validatePanel({ description: DESCRIPTION, mode: MarkingMode.SINGLE, markers: [], adjudicator: null }), {});
});

test('panel mode refuses too few, duplicate or model-less markers and a missing adjudicator', () => {
  const base = { description: DESCRIPTION, mode: MarkingMode.PANEL };
  const good = {
    ...base,
    markers: [{ providerId: 'nvidia', model: 'a' }, { providerId: 'gemini', model: 'b' }],
    adjudicator: { providerId: 'deepseek', model: 'c' },
  };
  assert.deepEqual(validatePanel(good), {});

  assert.match(validatePanel({ ...good, markers: [good.markers[0], { providerId: '', model: '' }] }).markers, /at least 2/);
  assert.match(validatePanel({ ...good, markers: [good.markers[0], { ...good.markers[0] }] }).markers, /same model/);
  assert.match(validatePanel({ ...good, markers: [good.markers[0], { providerId: 'gemini', model: '' }] }).marker1, /Choose a model/);
  assert.match(validatePanel({ ...good, markers: [good.markers[0], { providerId: 'ghost', model: 'x' }] }).marker1, /not available/);
  assert.match(validatePanel({ ...good, adjudicator: { providerId: '', model: '' } }).adjudicator, /Choose an adjudicator/);
  assert.match(validatePanel({ ...good, adjudicator: { providerId: 'deepseek', model: '' } }).adjudicator, /Choose a model/);
});

test('warnings name keyless markers, shared vendors and a self-judging adjudicator', () => {
  const form = {
    description: DESCRIPTION,
    mode: MarkingMode.PANEL,
    markers: [{ providerId: 'nvidia', model: 'a' }, { providerId: 'nvidia', model: 'b' }],
    adjudicator: { providerId: 'nvidia', model: 'a' },
  };
  const warnings = panelWarnings(form);
  assert.equal(warnings.some((warning) => /share a provider/.test(warning)), true);
  assert.equal(warnings.some((warning) => /also a marker/.test(warning)), true);

  const keyless = panelWarnings({
    ...form,
    markers: [{ providerId: 'nvidia', model: 'a' }, { providerId: 'openai', model: 'g' }],
    adjudicator: { providerId: 'openai', model: 'g' },
  });
  assert.equal(keyless.filter((warning) => /OPENAI has no API key/.test(warning)).length, 2);
  assert.deepEqual(panelWarnings({ ...form, mode: MarkingMode.SINGLE }), []);
});

test('the PUT body merges onto current settings and drops blank marker rows', () => {
  const payload = buildMarkingPayload(
    { llmPrimary: { providerId: 'nvidia', model: 'm' }, transcriptionEngine: 'whisperx' },
    {
      mode: MarkingMode.PANEL,
      markers: [{ providerId: 'nvidia', model: ' a ' }, { providerId: '', model: '' }, { providerId: 'gemini', model: 'b' }],
      adjudicator: { providerId: 'deepseek', model: 'c' },
      tieBreak: TieBreak.STRICT,
    },
  );
  assert.equal(payload.transcriptionEngine, 'whisperx');
  assert.deepEqual(payload.llmPrimary, { providerId: 'nvidia', model: 'm' });
  assert.equal(payload.llmMarkingMode, 'panel');
  assert.deepEqual(payload.llmPanel, {
    markers: [{ providerId: 'nvidia', model: 'a' }, { providerId: 'gemini', model: 'b' }],
    adjudicator: { providerId: 'deepseek', model: 'c' },
    tieBreak: 'strict',
  });
  assert.equal(buildMarkingPayload({}, { mode: 'bogus', markers: [], adjudicator: null, tieBreak: 'bogus' }).llmMarkingMode, 'single');
  assert.equal(buildMarkingPayload({}, { mode: 'bogus', markers: [], adjudicator: null, tieBreak: 'bogus' }).llmPanel.tieBreak, 'lenient');
});

test('panel summaries read as markers then adjudicator', () => {
  assert.equal(describePanel(STORED_PANEL.marking.selected), 'nvidia:nvidia-model + gemini → deepseek:deepseek-model');
  assert.equal(describePanel({ markers: [] }), 'Not configured');
});

test('a server that would run a different mode than the one saved is surfaced', () => {
  assert.equal(effectiveMarkingDiffers(DESCRIPTION), false);
  assert.equal(
    effectiveMarkingDiffers({ marking: { mode: 'panel', effective: { mode: 'single', warnings: ['no key'] } } }),
    true,
  );
  assert.equal(effectiveMarkingDiffers({}), false);
});
