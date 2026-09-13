// Pure helpers behind the scoring-model and marking-mode settings cards.
//
// The backend describes each provider (models, credentials contract,
// availability) and the UI renders dropdowns from that description — so
// shipping a new provider needs no frontend change. Everything that decides
// what to render or what to send lives here, separately from the components,
// so it is unit-testable (see test/llmProviders.test.mjs).
import { MarkingMode, TieBreak } from './enums.js';

export { MarkingMode, TieBreak };

// Sentinel used by the fallback dropdown. An empty provider id means "no
// fallback"; the API drops such an entry rather than storing a target with
// nothing to call.
export const NO_FALLBACK = '';

// A model id the provider's shortlist does not contain. Selecting it reveals a
// free-text field: vendors ship models faster than this app is released, and an
// operator must not have to wait for a deploy to use one.
export const CUSTOM_MODEL = '__custom__';

export function providerList(description) {
  return description?.providers || [];
}

export function findProvider(description, providerId) {
  return providerList(description).find((provider) => provider.id === providerId) || null;
}

export function isProviderReady(provider) {
  return Boolean(provider) && provider.availability?.available !== false;
}

// The provider the primary dropdown should show: the stored choice, else the
// deployment default, else the first provider we know about.
export function resolvePrimaryProviderId(description) {
  const stored = String(description?.selected?.primary?.providerId || '');
  if (findProvider(description, stored)) return stored;
  const fallback = String(description?.defaultProviderId || '');
  if (findProvider(description, fallback)) return fallback;
  const providers = providerList(description);
  return providers.length ? providers[0].id : '';
}

// Only the first fallback is surfaced as a dropdown. The stored shape is a
// list, so a second fallback can be added later without a schema change.
export function resolveFallbackTarget(description) {
  const first = (description?.selected?.fallbacks || [])[0];
  if (!first || !findProvider(description, first.providerId)) {
    return { providerId: NO_FALLBACK, model: '' };
  }
  return { providerId: String(first.providerId), model: String(first.model || '') };
}

// The model a dropdown should show for a provider: the stored choice, else that
// provider's own default.
export function resolveModelId(provider, storedModel) {
  const stored = String(storedModel || '').trim();
  if (!provider) return stored;
  if (!stored) return provider.defaultModelId || '';
  return stored;
}

// Whether a stored model id needs the free-text field rather than an option.
//
// A provider with no shortlist always does. Operator-defined providers ship
// none by design — the record describes a *platform*, and this app has no way
// to know a private gateway's catalogue — so the dropdown would otherwise sit
// empty with no way to type the id it is waiting for.
export function isCustomModel(provider, modelId) {
  if (!provider) return false;
  if (!(provider.models || []).length) return Boolean(provider.allowsCustomModel);
  const model = String(modelId || '').trim();
  if (!model) return false;
  return !(provider.models || []).some((candidate) => candidate.id === model);
}

export function findModelSpec(provider, modelId) {
  return (provider?.models || []).find((candidate) => candidate.id === String(modelId || '')) || null;
}

// Whether the picker should show the free-text model field. `isCustomModel`
// answers this from the stored model alone, and by design says no for an
// empty one — a fresh, not-yet-loaded target isn't "custom", it just has
// nothing yet. But "Other — type a model id…" starts life with an empty
// model too (the id doesn't exist until the operator types it), so that
// stored-value question can't carry the operator's intent on its own. The
// component tracks the one bit `isCustomModel` cannot see — "I just picked
// Other" — and this is where the two are combined, so the OR lives in one
// tested place rather than being re-derived at the call site.
export function shouldShowCustomModelField(provider, storedModel, customSelected) {
  return Boolean(customSelected) || isCustomModel(provider, storedModel);
}

// Things worth telling the operator before they save this pairing.
export function routingWarnings({ description, primary, fallback }) {
  const warnings = [];
  const primaryProvider = findProvider(description, primary?.providerId);
  const fallbackProvider = findProvider(description, fallback?.providerId);

  if (primaryProvider && !isProviderReady(primaryProvider)) {
    warnings.push(
      primaryProvider.availability?.reason ||
        `${primaryProvider.label} has no API key configured on the server, so scoring will fail over to the fallback.`,
    );
  }
  if (fallbackProvider && !isProviderReady(fallbackProvider)) {
    warnings.push(
      `${fallbackProvider.label} has no API key configured on the server, so it cannot actually stand in for the primary.`,
    );
  }
  if (
    primaryProvider &&
    fallbackProvider &&
    primaryProvider.id === fallbackProvider.id &&
    String(primary?.model || '') === String(fallback?.model || '')
  ) {
    warnings.push(
      'The fallback is identical to the primary, so it adds waiting time without adding a second chance. Pick a different provider or model.',
    );
  }
  if (primaryProvider && fallbackProvider && primaryProvider.id === fallbackProvider.id) {
    warnings.push(
      'Both targets are the same provider. A provider-wide outage would take out the fallback too — a different vendor is a stronger safety net.',
    );
  }
  if (!fallbackProvider) {
    warnings.push('No fallback selected. A provider outage will fail the assessment rather than switching models.');
  }
  return warnings;
}

export function validateRouting({ description, primary }) {
  const errors = {};
  if (!primary?.providerId) {
    errors.primaryProvider = 'Choose a primary provider.';
    return errors;
  }
  if (!findProvider(description, primary.providerId)) {
    errors.primaryProvider = 'That provider is not available in this build.';
  }
  if (!String(primary.model || '').trim()) {
    errors.primaryModel = 'Choose a model, or type a model id.';
  }
  return errors;
}

// The routing card's patch: the two keys it owns and nothing else. Sent with
// PATCH, so the server merges it over what is stored — nothing this card does
// not render travels with it, and nothing it does not render can be reverted
// by it. (It used to spread the whole document; see SettingsPage.updateSetting
// for what that cost.)
export function buildSettingsPayload({ primary, fallback }) {
  const fallbacks = [];
  if (fallback?.providerId) {
    fallbacks.push({
      providerId: String(fallback.providerId),
      model: String(fallback.model || '').trim(),
    });
  }
  return {
    llmPrimary: {
      providerId: String(primary?.providerId || ''),
      model: String(primary?.model || '').trim(),
    },
    llmFallbacks: fallbacks,
  };
}

// A short "nvidia:model → openai:gpt-4.1" line for the card's summary row.
export function describeRouting(routing) {
  if (!routing?.primary?.providerId) return 'Not configured';
  const parts = [routing.primary, ...(routing.fallbacks || [])]
    .filter((target) => target && target.providerId)
    .map((target) => (target.model ? `${target.providerId}:${target.model}` : target.providerId));
  return parts.join(' → ');
}

// True when the server would run something other than what is stored — a
// fallback whose key was removed, or a promoted primary. Worth surfacing:
// the screen would otherwise show a selection that silently does not apply.
export function effectiveDiffers(description) {
  const selected = describeRouting(description?.selected);
  const effective = describeRouting(description?.effective);
  return Boolean(description?.effective) && selected !== effective;
}

export function formatContextWindow(model) {
  const size = Number(model?.contextWindow || 0);
  if (!size) return '';
  if (size >= 1_000_000) return `${Math.round(size / 1_000_000)}M context`;
  if (size >= 1_000) return `${Math.round(size / 1_000)}K context`;
  return `${size} context`;
}

// --- marking mode ------------------------------------------------------------
//
// The panel card's logic: which markers and adjudicator to show, what to warn
// about, what to refuse, and what to PATCH. The mode and tie-break vocabulary
// come from the generated enums so the browser and the backend cannot drift.

// Sentinel for "no adjudicator chosen yet" in the picker. Never stored: the
// route rejects a panel without an adjudicator when panel mode is selected.
export const NO_ADJUDICATOR = '';

export const MIN_PANEL_MARKERS = 2;

export const TIE_BREAK_LABELS = Object.freeze({
  [TieBreak.LENIENT]: 'Lenient — award the criterion (the rubric’s own rule for borderline evidence)',
  [TieBreak.STRICT]: 'Strict — withhold the criterion',
  [TieBreak.FIRST_MARKER]: 'First marker — keep what marker 1 said',
});

function targetOf(raw, description) {
  const providerId = String(raw?.providerId || '');
  const provider = findProvider(description, providerId);
  if (!provider) return null;
  return { providerId, model: resolveModelId(provider, raw?.model) };
}

function firstReadyProvider(description, exclude) {
  const excluded = new Set(exclude);
  const ready = providerList(description).find(
    (provider) => isProviderReady(provider) && !excluded.has(provider.id),
  );
  return ready ? { providerId: ready.id, model: ready.defaultModelId || '' } : null;
}

// The panel form's initial state from the server description: the stored
// panel where there is one, else a sensible starting point — the current
// primary as marker 1, the next provider with a key as marker 2, and a third
// as adjudicator — so the operator edits a proposal rather than empty rows.
export function resolvePanelState(description) {
  const marking = description?.marking || {};
  const stored = marking.selected || {};
  const mode = Object.values(MarkingMode).includes(marking.mode) ? marking.mode : MarkingMode.SINGLE;
  const tieBreak = Object.values(TieBreak).includes(stored.tieBreak) ? stored.tieBreak : TieBreak.LENIENT;

  const markers = (stored.markers || []).map((raw) => targetOf(raw, description)).filter(Boolean);
  if (!markers.length) {
    const primaryId = resolvePrimaryProviderId(description);
    const primary = targetOf({ providerId: primaryId, model: description?.selected?.primary?.model }, description);
    if (primary) markers.push(primary);
  }
  while (markers.length < MIN_PANEL_MARKERS) {
    const next = firstReadyProvider(description, markers.map((target) => target.providerId));
    if (!next) break;
    markers.push(next);
  }
  while (markers.length < MIN_PANEL_MARKERS) {
    markers.push({ providerId: '', model: '' });
  }

  let adjudicator = targetOf(stored.adjudicator, description);
  if (!adjudicator) {
    adjudicator =
      firstReadyProvider(description, markers.map((target) => target.providerId)) || {
        providerId: NO_ADJUDICATOR,
        model: '',
      };
  }
  return { mode, markers, adjudicator, tieBreak };
}

function targetKey(target) {
  return `${target?.providerId || ''}:${String(target?.model || '').trim()}`;
}

// Hard errors, keyed by control: the form cannot be saved in panel mode with
// any of these. Mirrors PanelConfig.validate() so the server's 422 is never
// the first the operator hears of it.
export function validatePanel({ description, mode, markers, adjudicator }) {
  const errors = {};
  if (mode !== MarkingMode.PANEL) return errors;

  const chosen = (markers || []).filter((target) => target?.providerId);
  if (chosen.length < MIN_PANEL_MARKERS) {
    errors.markers = `Choose at least ${MIN_PANEL_MARKERS} markers.`;
  }
  (markers || []).forEach((target, index) => {
    if (!target?.providerId) return;
    if (!findProvider(description, target.providerId)) {
      errors[`marker${index}`] = 'That provider is not available in this build.';
    } else if (!String(target.model || '').trim()) {
      errors[`marker${index}`] = 'Choose a model, or type a model id.';
    }
  });
  const keys = chosen.map(targetKey);
  keys.forEach((key, index) => {
    if (keys.indexOf(key) !== index) {
      errors.markers = 'Two markers name the same model. Two samples of one model are not a panel.';
    }
  });

  if (!adjudicator?.providerId) {
    errors.adjudicator = 'Choose an adjudicator to settle the criteria the markers disagree on.';
  } else if (!findProvider(description, adjudicator.providerId)) {
    errors.adjudicator = 'That provider is not available in this build.';
  } else if (!String(adjudicator.model || '').trim()) {
    errors.adjudicator = 'Choose a model, or type a model id.';
  }
  return errors;
}

// Softer than errors: a panel that will run but is weaker than it looks.
export function panelWarnings({ description, mode, markers, adjudicator }) {
  const warnings = [];
  if (mode !== MarkingMode.PANEL) return warnings;

  const chosen = (markers || []).filter((target) => target?.providerId);
  chosen.forEach((target) => {
    const provider = findProvider(description, target.providerId);
    if (provider && !isProviderReady(provider)) {
      warnings.push(
        `${provider.label} has no API key configured on the server, so that marker will be dropped and the run may fall back to single-model marking.`,
      );
    }
  });
  const providers = new Set(chosen.map((target) => target.providerId));
  if (chosen.length >= MIN_PANEL_MARKERS && providers.size < chosen.length) {
    warnings.push(
      'Two markers share a provider. Marks from one vendor’s models tend to agree with each other, so the panel catches less than a cross-vendor pair would.',
    );
  }
  const adjudicatorProvider = findProvider(description, adjudicator?.providerId);
  if (adjudicatorProvider && !isProviderReady(adjudicatorProvider)) {
    warnings.push(
      `${adjudicatorProvider.label} has no API key configured on the server, so disputed criteria will fall to the tie-break policy instead of being adjudicated.`,
    );
  }
  if (adjudicator?.providerId && chosen.some((target) => targetKey(target) === targetKey(adjudicator))) {
    warnings.push(
      'The adjudicator is also a marker. A model tends to side with its own earlier answer, so disputes will lean towards that marker.',
    );
  }
  return warnings;
}

// The marking card's patch: the mode and the panel, nothing else — see
// buildSettingsPayload.
export function buildMarkingPayload({ mode, markers, adjudicator, tieBreak }) {
  return {
    llmMarkingMode: Object.values(MarkingMode).includes(mode) ? mode : MarkingMode.SINGLE,
    llmPanel: {
      markers: (markers || [])
        .filter((target) => target?.providerId)
        .map((target) => ({ providerId: String(target.providerId), model: String(target.model || '').trim() })),
      adjudicator: {
        providerId: String(adjudicator?.providerId || ''),
        model: String(adjudicator?.model || '').trim(),
      },
      tieBreak: Object.values(TieBreak).includes(tieBreak) ? tieBreak : TieBreak.LENIENT,
    },
  };
}

function describeTarget(target) {
  if (!target?.providerId) return '';
  return target.model ? `${target.providerId}:${target.model}` : target.providerId;
}

// "nvidia:nemotron + gemini:gemini-2.5-pro → deepseek:deepseek-chat" for the
// card's summary row.
export function describePanel(panel) {
  const markers = (panel?.markers || []).map(describeTarget).filter(Boolean);
  if (!markers.length) return 'Not configured';
  const adjudicator = describeTarget(panel?.adjudicator);
  return adjudicator ? `${markers.join(' + ')} → ${adjudicator}` : markers.join(' + ');
}

// True when the server would run a different mode than the one selected —
// a panel whose markers have no keys here, an incomplete stored panel.
export function effectiveMarkingDiffers(description) {
  const marking = description?.marking;
  if (!marking?.effective) return false;
  return String(marking.effective.mode) !== String(marking.mode);
}
