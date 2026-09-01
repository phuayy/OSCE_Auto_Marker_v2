// Unit tests for the transcription-engine settings form logic.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  buildEngineOptionsPayload,
  coerceParameterValue,
  effectiveValue,
  engineWarnings,
  findEngine,
  resolveSelectedEngineId,
  splitParameters,
  validateEngineOptions,
  validateParameter,
} from '../src/lib/transcriptionEngines.js';

const WHISPERX = {
  id: 'whisperx',
  label: 'WhisperX',
  vendor: 'OpenAI Whisper + pyannote',
  capabilities: { diarization: true, wordTimestamps: true, subtitles: true, progress: true },
  availability: { available: true, reason: '' },
  defaults: { model: 'large-v3', batchSize: 1, chunkSize: 20, initialPrompt: '' },
  parameters: [
    { name: 'model', label: 'Whisper model', type: 'string', default: 'large-v3', advanced: false },
    { name: 'batchSize', label: 'Batch size', type: 'int', default: 1, minimum: 1, maximum: 64, advanced: false },
    { name: 'chunkSize', label: 'VAD chunk size', type: 'int', default: 20, minimum: 5, maximum: 30, advanced: false },
    { name: 'initialPrompt', label: 'Initial prompt', type: 'string', default: '', advanced: true },
  ],
};

const CANARY = {
  id: 'canary-qwen',
  label: 'Canary-Qwen 2.5B',
  vendor: 'NVIDIA',
  capabilities: { diarization: false, wordTimestamps: false, subtitles: false, progress: true },
  availability: { available: false, reason: 'The NeMo toolkit is not installed.' },
  defaults: { chunkSeconds: 30, device: 'auto', diarize: true },
  parameters: [
    { name: 'chunkSeconds', label: 'Chunk length', type: 'float', default: 30, minimum: 5, maximum: 40, advanced: false },
    { name: 'device', label: 'Device', type: 'enum', default: 'auto', options: ['auto', 'cuda', 'cpu'], advanced: false },
    { name: 'diarize', label: 'Label speakers', type: 'bool', default: true, advanced: false },
  ],
};

const DESCRIPTION = {
  engines: [WHISPERX, CANARY],
  selected: { engineId: 'canary-qwen', options: { chunkSeconds: 25 } },
  defaultEngineId: 'whisperx',
};

test('the stored selection wins when the engine still exists', () => {
  assert.equal(resolveSelectedEngineId(DESCRIPTION), 'canary-qwen');
});

test('an unknown stored selection falls back to the deployment default', () => {
  const stale = { ...DESCRIPTION, selected: { engineId: 'retired-engine', options: {} } };

  assert.equal(resolveSelectedEngineId(stale), 'whisperx');
});

test('no selection at all falls back to the first engine', () => {
  const bare = { engines: [WHISPERX, CANARY] };

  assert.equal(resolveSelectedEngineId(bare), 'whisperx');
  assert.equal(resolveSelectedEngineId({ engines: [] }), '');
  assert.equal(resolveSelectedEngineId(null), '');
});

test('findEngine returns null rather than throwing on an unknown id', () => {
  assert.equal(findEngine(DESCRIPTION, 'whisperx').label, 'WhisperX');
  assert.equal(findEngine(DESCRIPTION, 'nope'), null);
});

test('a control shows the deployment default until it is overridden', () => {
  // The deployment default, not the schema default: an operator who never
  // touched a field must see what their WHISPERX_* environment actually does.
  const engine = { ...WHISPERX, defaults: { ...WHISPERX.defaults, batchSize: 4 } };

  assert.equal(effectiveValue(engine, WHISPERX.parameters[1], {}), 4);
  assert.equal(effectiveValue(engine, WHISPERX.parameters[1], { batchSize: 8 }), 8);
});

test('numeric bounds are checked before a round trip', () => {
  const batchSize = WHISPERX.parameters[1];

  assert.equal(validateParameter(batchSize, 4), '');
  assert.match(validateParameter(batchSize, 0), /at least 1/);
  assert.match(validateParameter(batchSize, 999), /at most 64/);
  assert.match(validateParameter(batchSize, 'abc'), /must be a number/);
  assert.match(validateParameter(batchSize, 1.5), /whole number/);
});

test('an enum only accepts its options', () => {
  const device = CANARY.parameters[1];

  assert.equal(validateParameter(device, 'cuda'), '');
  assert.match(validateParameter(device, 'tpu'), /auto, cuda, cpu/);
});

test('an empty value is not an error — it means "use the default"', () => {
  assert.equal(validateParameter(WHISPERX.parameters[1], ''), '');
  assert.equal(validateParameter(WHISPERX.parameters[1], undefined), '');
});

test('validation reports every bad field at once', () => {
  const errors = validateEngineOptions(WHISPERX, { batchSize: 0, chunkSize: 99 });

  assert.deepEqual(Object.keys(errors).sort(), ['batchSize', 'chunkSize']);
});

test('values are coerced to the type the API declares', () => {
  assert.equal(coerceParameterValue(WHISPERX.parameters[1], '8'), 8);
  assert.equal(coerceParameterValue(CANARY.parameters[0], '25.5'), 25.5);
  assert.equal(coerceParameterValue(CANARY.parameters[2], true), true);
  assert.equal(coerceParameterValue(WHISPERX.parameters[0], 'large-v3-turbo'), 'large-v3-turbo');
  assert.equal(coerceParameterValue(WHISPERX.parameters[1], ''), undefined);
});

test('only genuine deviations are saved', () => {
  // Storing a value equal to the deployment default would freeze it, so a
  // later change to that default would silently stop applying.
  const payload = buildEngineOptionsPayload(WHISPERX, { model: 'large-v3', batchSize: '8', chunkSize: '' });

  assert.deepEqual(payload, { batchSize: 8 });
});

test('an empty payload is sent when nothing was changed', () => {
  assert.deepEqual(buildEngineOptionsPayload(WHISPERX, {}), {});
  assert.deepEqual(buildEngineOptionsPayload(null, { batchSize: 2 }), {});
});

test('an engine that cannot diarise warns about the extra pass', () => {
  const warnings = engineWarnings(CANARY);

  assert.equal(warnings.length, 2);
  assert.match(warnings[0], /NeMo toolkit/);
  assert.match(warnings[1], /speaker labels/);
});

test('a fully capable, installed engine warns about nothing', () => {
  assert.deepEqual(engineWarnings(WHISPERX), []);
  assert.deepEqual(engineWarnings(null), []);
});

test('advanced parameters are separated from the everyday ones', () => {
  const { basic, advanced } = splitParameters(WHISPERX);

  assert.deepEqual(basic.map((parameter) => parameter.name), ['model', 'batchSize', 'chunkSize']);
  assert.deepEqual(advanced.map((parameter) => parameter.name), ['initialPrompt']);
  assert.deepEqual(splitParameters(null), { basic: [], advanced: [] });
});
