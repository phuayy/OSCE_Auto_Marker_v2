// Pure helpers behind the scoring-model settings card.
//
// The backend describes each provider (models, credentials contract,
// availability) and the UI renders dropdowns from that description — so
// shipping a new provider needs no frontend change. Everything that decides
// what to render or what to send lives here, separately from the component, so
// it is unit-testable (see test/llmProviders.test.mjs).

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

// The settings body to PUT. Merges onto the settings the server currently
// holds so saving the model choice never clobbers an unrelated setting the
// screen does not render.
export function buildSettingsPayload(currentSettings, { primary, fallback }) {
  const fallbacks = [];
  if (fallback?.providerId) {
    fallbacks.push({
      providerId: String(fallback.providerId),
      model: String(fallback.model || '').trim(),
    });
  }
  return {
    ...(currentSettings || {}),
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
