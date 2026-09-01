// Pure helpers behind the transcription-engine settings card.
//
// The backend describes each engine's parameters (name, type, range, default),
// and the UI renders controls from that description — so shipping a new engine
// needs no frontend change. Everything that decides what to render or what to
// send lives here, separately from the component, so it is unit-testable
// (see test/transcriptionEngines.test.mjs).

// Value the API uses for "whichever engine this deployment configured".
export const DEPLOYMENT_DEFAULT_ENGINE = '';

// Resolve which engine the form should show as selected: the operator's stored
// choice, else the deployment default, else the first engine we know about.
export function resolveSelectedEngineId(description) {
  const engines = description?.engines || [];
  const stored = String(description?.selected?.engineId || '');
  if (engines.some((engine) => engine.id === stored)) return stored;
  const fallback = String(description?.defaultEngineId || '');
  if (engines.some((engine) => engine.id === fallback)) return fallback;
  return engines.length ? engines[0].id : '';
}

export function findEngine(description, engineId) {
  return (description?.engines || []).find((engine) => engine.id === engineId) || null;
}

// The value a control should show: the operator's override when they have set
// one, otherwise this deployment's default for that engine.
export function effectiveValue(engine, parameter, overrides) {
  const stored = overrides?.[parameter.name];
  if (stored !== undefined && stored !== null && stored !== '') return stored;
  if (stored === '' && parameter.type === 'string') return stored;
  const deploymentDefault = engine?.defaults?.[parameter.name];
  return deploymentDefault === undefined ? parameter.default : deploymentDefault;
}

// Client-side mirror of the backend's ParameterSpec validation. The server is
// still the authority; this exists so a bad value is named before a round trip.
export function validateParameter(parameter, value) {
  if (value === '' || value === null || value === undefined) return '';
  if (parameter.type === 'int' || parameter.type === 'float') {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return `${parameter.label} must be a number.`;
    if (parameter.type === 'int' && !Number.isInteger(numeric)) {
      return `${parameter.label} must be a whole number.`;
    }
    if (parameter.minimum !== undefined && numeric < parameter.minimum) {
      return `${parameter.label} must be at least ${parameter.minimum}.`;
    }
    if (parameter.maximum !== undefined && numeric > parameter.maximum) {
      return `${parameter.label} must be at most ${parameter.maximum}.`;
    }
    return '';
  }
  if (parameter.type === 'enum' && !(parameter.options || []).includes(String(value))) {
    return `${parameter.label} must be one of ${(parameter.options || []).join(', ')}.`;
  }
  return '';
}

export function validateEngineOptions(engine, overrides) {
  const errors = {};
  for (const parameter of engine?.parameters || []) {
    const message = validateParameter(parameter, overrides?.[parameter.name]);
    if (message) errors[parameter.name] = message;
  }
  return errors;
}

// Coerce one control's value into the type the API expects. Numbers arrive
// from <input> as strings; an unparseable number is left alone so the server
// reports it rather than the UI silently sending something else.
export function coerceParameterValue(parameter, value) {
  if (parameter.type === 'bool') return Boolean(value);
  if (parameter.type === 'int' || parameter.type === 'float') {
    if (value === '' || value === null || value === undefined) return undefined;
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return value;
    return parameter.type === 'int' ? Math.trunc(numeric) : numeric;
  }
  return value === undefined || value === null ? undefined : String(value);
}

// Drop overrides equal to the deployment default so the stored settings record
// only genuine deviations — a default that changes later then still applies.
export function buildEngineOptionsPayload(engine, overrides) {
  const payload = {};
  for (const parameter of engine?.parameters || []) {
    const raw = overrides?.[parameter.name];
    if (raw === undefined || raw === null || raw === '') continue;
    const coerced = coerceParameterValue(parameter, raw);
    if (coerced === undefined) continue;
    const deploymentDefault = engine?.defaults?.[parameter.name] ?? parameter.default;
    if (coerced === deploymentDefault) continue;
    payload[parameter.name] = coerced;
  }
  return payload;
}

// Warnings worth showing next to an engine before it is selected: what the
// pipeline has to add for it, and whether it can run here at all.
export function engineWarnings(engine) {
  if (!engine) return [];
  const warnings = [];
  if (engine.availability && engine.availability.available === false) {
    warnings.push(engine.availability.reason || 'This engine is not available on this machine.');
  }
  if (engine.capabilities && !engine.capabilities.diarization) {
    warnings.push(
      'Produces text without speaker labels, so a separate diarisation pass runs to tell the student from the patient.',
    );
  }
  return warnings;
}

export function splitParameters(engine) {
  const parameters = engine?.parameters || [];
  return {
    basic: parameters.filter((parameter) => !parameter.advanced),
    advanced: parameters.filter((parameter) => parameter.advanced),
  };
}
