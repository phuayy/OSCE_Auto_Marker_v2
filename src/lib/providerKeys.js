// Pure helpers behind the API-keys settings card.
//
// The card is a credential *writer*, never a reader: the backend answers with a
// masked tail, a source and a timestamp, and there is no endpoint that returns a
// key. Everything here therefore reasons about metadata only — which is also why
// it is all testable without a browser (see test/providerKeys.test.mjs).
//
// The provider list comes from the same GET /api/settings/llm-providers that
// drives the model dropdowns, so a provider added to the backend registry gets
// a key field here with no change to this file.

// One key per *platform*, not per model. OpenRouter, NVIDIA and Gemini each
// authenticate a whole catalogue with a single credential, so the key belongs to
// the provider row and every model selected under it inherits it.
export const KEY_SOURCE = {
  APP: 'app',
  ENVIRONMENT: 'environment',
  NONE: 'none',
};

// Mirrors ProviderCredentialService.MIN_KEY_LENGTH / MAX_KEY_LENGTH. Checked
// here so an obvious paste error is caught before a round trip; the server
// re-checks because a browser is not a trust boundary.
export const MIN_KEY_LENGTH = 8;
export const MAX_KEY_LENGTH = 512;

const MASK_ONLY = /^[•*….\s]+$/;

export function credentialOf(provider) {
  return provider?.credential || { source: KEY_SOURCE.NONE, configured: false };
}

export function keySource(provider) {
  return credentialOf(provider).source || KEY_SOURCE.NONE;
}

export function isKeyStoredInApp(provider) {
  return keySource(provider) === KEY_SOURCE.APP;
}

// Whether this server can hold keys at all. Without an encryption key the card
// hides its fields rather than offering a save that would always fail.
export function canStoreKeys(description) {
  return description?.credentialStorage?.available !== false;
}

export function storageBlockedReason(description) {
  if (canStoreKeys(description)) return '';
  return (
    description?.credentialStorage?.reason ||
    'This server has no credential encryption key, so API keys must stay in its environment.'
  );
}

// What the operator is told about where a provider's key lives. The distinction
// matters: a key saved here *shadows* one in the environment, and someone who
// edited .env and saw no effect needs to be told that rather than left guessing.
export function describeKeySource(provider) {
  const credential = credentialOf(provider);
  switch (credential.source) {
    case KEY_SOURCE.APP:
      return {
        tone: 'ok',
        label: 'Saved here',
        detail: credential.updatedAt
          ? `Set ${formatWhen(credential.updatedAt)}${credential.updatedBy ? ` by ${credential.updatedBy}` : ''}. Overrides any value in the server environment.`
          : 'Overrides any value in the server environment.',
      };
    case KEY_SOURCE.ENVIRONMENT:
      return {
        tone: 'info',
        label: 'From server environment',
        detail: `Configured as ${(provider?.apiKeyEnv || []).join(' or ') || 'an environment variable'}. Saving a key here replaces it without a restart.`,
      };
    default:
      return {
        tone: 'warn',
        label: 'Not configured',
        detail: 'This provider cannot be used for scoring until a key is saved.',
      };
  }
}

// A stored row this deployment's encryption key can no longer open — the master
// key was rotated or lost. Reported plainly, because "unconfigured" would send
// the operator looking for the wrong problem.
export function isKeyUnreadable(provider) {
  const credential = credentialOf(provider);
  return credential.source === KEY_SOURCE.APP && credential.readable === false;
}

export function validateKeyInput(value) {
  const key = String(value || '').trim();
  if (!key) return 'Enter an API key.';
  if (MASK_ONLY.test(key)) return 'That is the masked preview, not a key. Paste the real value.';
  if (/\s/.test(key)) return 'An API key cannot contain spaces or line breaks.';
  if (key.length < MIN_KEY_LENGTH) return `That key is only ${key.length} characters — it looks truncated.`;
  if (key.length > MAX_KEY_LENGTH) return `An API key cannot be longer than ${MAX_KEY_LENGTH} characters.`;
  return '';
}

export function keyEndpoint(providerId) {
  return `/api/admin/settings/llm-providers/${encodeURIComponent(String(providerId || ''))}/key`;
}

// The last connection test recorded against the stored key, so a reload does not
// forget that a credential was verified ten minutes ago.
export function describeLastTest(provider) {
  const credential = credentialOf(provider);
  if (credential.lastTestOk === true) {
    return { ok: true, text: `Verified ${formatWhen(credential.lastTestedAt)}.` };
  }
  if (credential.lastTestOk === false) {
    return {
      ok: false,
      text: credential.lastTestError
        ? `Last test failed ${formatWhen(credential.lastTestedAt)}: ${credential.lastTestError}`
        : `Last test failed ${formatWhen(credential.lastTestedAt)}.`,
    };
  }
  return null;
}

// The model a per-provider connection test should use: whatever the operator
// already selected for that provider if it is one of the routing targets,
// otherwise the provider's own default. Testing a provider must never require
// first selecting it as the primary.
export function testModelFor(description, providerId) {
  const targets = [description?.selected?.primary, ...(description?.selected?.fallbacks || [])];
  const match = targets.find((target) => target && target.providerId === providerId && target.model);
  if (match) return match.model;
  const provider = (description?.providers || []).find((candidate) => candidate.id === providerId);
  return provider?.defaultModelId || '';
}

export function formatWhen(iso) {
  const text = String(iso || '').trim();
  if (!text) return 'at an unknown time';
  const when = new Date(text);
  if (Number.isNaN(when.getTime())) return 'at an unknown time';
  return when.toLocaleString();
}

// Providers whose key is missing while the routing actually points at them. The
// card leads with these: an unconfigured provider nobody selected is a note, but
// an unconfigured *primary* is the reason the next run will fail.
export function unconfiguredRoutedProviders(description) {
  const routed = new Set(
    [description?.selected?.primary, ...(description?.selected?.fallbacks || [])]
      .filter((target) => target && target.providerId)
      .map((target) => target.providerId),
  );
  return (description?.providers || []).filter(
    (provider) => routed.has(provider.id) && !credentialOf(provider).configured,
  );
}
