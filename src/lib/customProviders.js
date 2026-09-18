// Pure helpers behind the "Custom scoring providers" card.
//
// The card lets an operator add an LLM platform this build does not ship —
// endpoint, where the credential goes, API version, deployment identifiers,
// gateway headers — so a new vendor is a form rather than a release. Everything
// that decides what to render, what to validate and what to send lives here,
// separately from the component, so it is unit-testable without a browser
// (see test/customProviders.test.mjs).
//
// Two rules this file exists to keep:
//
//  1. **It never holds a key longer than the request that carries it.** The
//     server returns a masked tail and nothing else, exactly as for a shipped
//     provider, so there is nothing here that reads one back.
//  2. **Its validation mirrors the server's, and is not a substitute for it.**
//     The messages are the same so an operator is not told two different
//     stories, but the server re-checks because a browser is not a trust
//     boundary.
//
// Deliberately absent: the model id. One key authorises a whole catalogue and
// the checkpoint changes far more often than the endpoint does, so the model
// stays in the Scoring model card where it already is. This form answers only
// "how do I talk to this platform".

export const API_FORMAT = { OPENAI: 'openai', ANTHROPIC: 'anthropic' };
export const AUTH_SCHEME = { BEARER: 'bearer', HEADER: 'header', QUERY: 'query' };

// Mirrors app/llm/custom.py's ID_PATTERN. The id ends up in a URL and in an
// environment variable name, so it is restricted to something safe in both.
export const ID_PATTERN = /^[a-z0-9][a-z0-9._-]{1,63}$/;

export const API_FORMAT_OPTIONS = [
  {
    value: API_FORMAT.OPENAI,
    label: 'OpenAI-compatible (/chat/completions)',
    hint: 'Almost every platform: Together, Groq, Fireworks, Mistral, xAI, Perplexity, vLLM, Ollama, LiteLLM, Azure OpenAI.',
  },
  {
    value: API_FORMAT.ANTHROPIC,
    label: 'Anthropic Messages (/messages)',
    hint: 'Claude through a gateway or relay that speaks the native Messages API.',
  },
];

export const AUTH_SCHEME_OPTIONS = [
  {
    value: AUTH_SCHEME.BEARER,
    label: 'Authorization: Bearer <key>',
    hint: 'The default almost everywhere.',
  },
  {
    value: AUTH_SCHEME.HEADER,
    label: 'A named header',
    hint: 'Azure uses api-key; Anthropic-style endpoints use x-api-key.',
  },
  {
    value: AUTH_SCHEME.QUERY,
    label: 'A query parameter',
    hint: 'Google-style REST surfaces and some appliances.',
  },
];

// Starting points, not restrictions. Every field stays editable afterwards —
// the presets exist because most operators are configuring one of a handful of
// shapes, and typing "api-version" from memory is how a connection silently
// fails at 2am.
export const PRESETS = [
  {
    id: 'openai-compatible',
    label: 'OpenAI-compatible endpoint',
    hint: 'Together, Groq, Fireworks, Mistral, xAI, Perplexity, LiteLLM, a hosted gateway.',
    values: { apiFormat: API_FORMAT.OPENAI, authScheme: AUTH_SCHEME.BEARER },
  },
  {
    id: 'azure-openai',
    label: 'Azure OpenAI',
    hint: 'Key in an api-key header, version as a query parameter.',
    values: {
      apiFormat: API_FORMAT.OPENAI,
      authScheme: AUTH_SCHEME.HEADER,
      authHeaderName: 'api-key',
      apiVersion: '2024-10-21',
      apiVersionQueryParam: 'api-version',
      baseUrl: 'https://{region}.openai.azure.com/openai/v1',
    },
  },
  {
    id: 'anthropic-compatible',
    label: 'Anthropic Messages relay',
    hint: 'Claude behind a corporate gateway or a Bedrock-compatible proxy.',
    values: {
      apiFormat: API_FORMAT.ANTHROPIC,
      authScheme: AUTH_SCHEME.HEADER,
      authHeaderName: 'x-api-key',
      apiVersion: '2023-06-01',
      apiVersionHeader: 'anthropic-version',
    },
  },
  {
    id: 'self-hosted',
    label: 'Self-hosted (vLLM, Ollama, LM Studio, TGI)',
    hint: 'A box on your own network. JSON mode is often unsupported.',
    values: {
      apiFormat: API_FORMAT.OPENAI,
      authScheme: AUTH_SCHEME.BEARER,
      baseUrl: 'http://localhost:8000/v1',
      supportsJsonMode: false,
    },
  },
];

// The draft the form edits. Mirrors CustomProviderRequest field for field, so
// building the request body is a copy rather than a translation.
export function emptyDraft(overrides = {}) {
  return {
    id: '',
    label: '',
    vendor: '',
    description: '',
    documentationUrl: '',

    baseUrl: '',
    apiFormat: API_FORMAT.OPENAI,

    authScheme: AUTH_SCHEME.BEARER,
    authHeaderName: '',
    authValuePrefix: '',
    authQueryParam: '',

    apiVersion: '',
    apiVersionHeader: '',
    apiVersionQueryParam: '',
    organizationId: '',
    organizationHeader: '',
    projectId: '',
    projectHeader: '',
    accountId: '',
    region: '',

    extraHeaders: [],
    extraQuery: [],
    extraBodyText: '',

    requestTimeoutSeconds: '',
    supportsJsonMode: true,
    supportsReasoningControl: false,
    enabled: true,

    apiKey: '',
    ...overrides,
  };
}

export function applyPreset(draft, presetId) {
  const preset = PRESETS.find((candidate) => candidate.id === presetId);
  if (!preset) return draft;
  // Only fills blanks for the endpoint: a preset must never overwrite a URL the
  // operator has already typed.
  const values = { ...preset.values };
  if (values.baseUrl && String(draft.baseUrl || '').trim()) delete values.baseUrl;
  return { ...draft, ...values };
}

// The server's own description of a provider, turned back into an editable
// draft. Read from `connection`, which the API echoes, rather than from
// anything the browser remembered — after a save the server is the only thing
// that knows what is actually stored.
export function draftFromProvider(provider) {
  const connection = provider?.connection || {};
  return emptyDraft({
    id: String(connection.id || provider?.id || ''),
    label: String(connection.label || provider?.label || ''),
    vendor: String(connection.vendor || ''),
    description: String(connection.description || ''),
    documentationUrl: String(connection.documentationUrl || ''),
    baseUrl: String(connection.baseUrl || ''),
    apiFormat: String(connection.apiFormat || API_FORMAT.OPENAI),
    authScheme: String(connection.authScheme || AUTH_SCHEME.BEARER),
    authHeaderName: String(connection.authHeaderName || ''),
    authValuePrefix: String(connection.authValuePrefix || ''),
    authQueryParam: String(connection.authQueryParam || ''),
    apiVersion: String(connection.apiVersion || ''),
    apiVersionHeader: String(connection.apiVersionHeader || ''),
    apiVersionQueryParam: String(connection.apiVersionQueryParam || ''),
    organizationId: String(connection.organizationId || ''),
    organizationHeader: String(connection.organizationHeader || ''),
    projectId: String(connection.projectId || ''),
    projectHeader: String(connection.projectHeader || ''),
    accountId: String(connection.accountId || ''),
    region: String(connection.region || ''),
    extraHeaders: mapToRows(connection.extraHeaders),
    extraQuery: mapToRows(connection.extraQuery),
    extraBodyText: Object.keys(connection.extraBody || {}).length
      ? JSON.stringify(connection.extraBody, null, 2)
      : '',
    requestTimeoutSeconds: connection.requestTimeoutSeconds ? String(connection.requestTimeoutSeconds) : '',
    supportsJsonMode: connection.supportsJsonMode !== false,
    supportsReasoningControl: Boolean(connection.supportsReasoningControl),
    enabled: connection.enabled !== false,
    // Never pre-filled. There is no endpoint that returns a key, and a form
    // that looked like it held one would invite an operator to "keep" a
    // credential that is not there.
    apiKey: '',
  });
}

export function mapToRows(map) {
  return Object.entries(map || {}).map(([name, value]) => ({ name, value: String(value ?? '') }));
}

export function rowsToMap(rows) {
  const result = {};
  for (const row of rows || []) {
    const name = String(row?.name || '').trim();
    if (!name) continue;
    result[name] = String(row?.value ?? '');
  }
  return result;
}

// Parses the free-form body-fields editor. Returns the object and a message,
// never throws — a half-typed JSON object is the normal state of a textarea.
export function parseExtraBody(text) {
  const raw = String(text || '').trim();
  if (!raw) return { value: {}, error: '' };
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    return { value: {}, error: `That is not valid JSON: ${error.message}` };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { value: {}, error: 'Extra body fields must be a JSON object, for example {"top_k": 40}.' };
  }
  return { value: parsed, error: '' };
}

// Mirrors CustomProviderSpec.from_raw. Same rules, same wording, so the browser
// and the server never tell an operator two different stories.
export function validateDraft(draft, { existingIds = [], isNew = true } = {}) {
  const errors = {};
  const id = String(draft?.id || '').trim().toLowerCase();

  if (!ID_PATTERN.test(id)) {
    errors.id =
      "Use 2–64 characters of lowercase letters, digits, '.', '-' or '_', starting with a letter or digit.";
  } else if (isNew && existingIds.includes(id)) {
    errors.id = `A provider called '${id}' already exists. Edit it, or choose another id.`;
  }

  if (!String(draft?.label || '').trim()) {
    errors.label = 'Give the provider a name so it is recognisable in the dropdowns.';
  }

  const baseUrl = String(draft?.baseUrl || '').trim();
  if (!baseUrl) {
    errors.baseUrl = 'Enter the inference endpoint — without one there is nothing to call.';
  } else if (!/^https?:\/\/[^/\s]+/i.test(baseUrl)) {
    errors.baseUrl = 'The endpoint must start with https:// or http:// followed by a host name.';
  }

  if (draft?.authScheme === AUTH_SCHEME.HEADER && !String(draft?.authHeaderName || '').trim()) {
    errors.authHeaderName = "Name the header the key goes in, for example 'x-api-key' or 'api-key'.";
  }
  if (draft?.authScheme === AUTH_SCHEME.QUERY && !String(draft?.authQueryParam || '').trim()) {
    errors.authQueryParam = "Name the query parameter the key goes in, for example 'key'.";
  }

  const timeout = String(draft?.requestTimeoutSeconds || '').trim();
  if (timeout && (!Number.isFinite(Number(timeout)) || Number(timeout) < 0 || Number(timeout) > 3600)) {
    errors.requestTimeoutSeconds = 'Enter a number of seconds between 0 and 3600, or leave it blank.';
  }

  const body = parseExtraBody(draft?.extraBodyText);
  if (body.error) errors.extraBodyText = body.error;

  // A key is required to create a provider — every commercial platform needs
  // one, and a provider saved without it is one the router will skip. On an
  // edit it is optional, because the stored key is still there.
  const key = String(draft?.apiKey || '').trim();
  if (isNew && !key) {
    errors.apiKey = 'Enter the API key for this platform. It is stored encrypted and never shown again.';
  } else if (key && /\s/.test(key)) {
    errors.apiKey = 'An API key cannot contain spaces or line breaks.';
  } else if (key && key.length < 8) {
    errors.apiKey = `That key is only ${key.length} characters — it looks truncated.`;
  }

  return errors;
}

// The request body for POST/PUT. Numbers and maps are normalised here so the
// component never has to think about wire shape.
export function buildProviderPayload(draft) {
  const timeout = String(draft?.requestTimeoutSeconds || '').trim();
  const payload = {
    id: String(draft?.id || '').trim().toLowerCase(),
    label: String(draft?.label || '').trim(),
    vendor: String(draft?.vendor || '').trim(),
    description: String(draft?.description || '').trim(),
    documentationUrl: String(draft?.documentationUrl || '').trim(),

    baseUrl: String(draft?.baseUrl || '').trim(),
    apiFormat: String(draft?.apiFormat || API_FORMAT.OPENAI),

    authScheme: String(draft?.authScheme || AUTH_SCHEME.BEARER),
    authHeaderName: String(draft?.authHeaderName || '').trim(),
    // Not trimmed: the trailing space in "Api-Key " is the difference between
    // a header that authenticates and one that does not.
    authValuePrefix: String(draft?.authValuePrefix || ''),
    authQueryParam: String(draft?.authQueryParam || '').trim(),

    apiVersion: String(draft?.apiVersion || '').trim(),
    apiVersionHeader: String(draft?.apiVersionHeader || '').trim(),
    apiVersionQueryParam: String(draft?.apiVersionQueryParam || '').trim(),
    organizationId: String(draft?.organizationId || '').trim(),
    organizationHeader: String(draft?.organizationHeader || '').trim(),
    projectId: String(draft?.projectId || '').trim(),
    projectHeader: String(draft?.projectHeader || '').trim(),
    accountId: String(draft?.accountId || '').trim(),
    region: String(draft?.region || '').trim(),

    extraHeaders: rowsToMap(draft?.extraHeaders),
    extraQuery: rowsToMap(draft?.extraQuery),
    extraBody: parseExtraBody(draft?.extraBodyText).value,

    requestTimeoutSeconds: timeout ? Number(timeout) : 0,
    supportsJsonMode: draft?.supportsJsonMode !== false,
    supportsReasoningControl: Boolean(draft?.supportsReasoningControl),
    enabled: draft?.enabled !== false,
  };
  const key = String(draft?.apiKey || '').trim();
  // Omitted rather than sent empty, so an edit that does not touch the key
  // cannot be mistaken for a request to clear it.
  if (key) payload.apiKey = key;
  return payload;
}

// What the server will actually put on the wire, derived from the draft. Shown
// under the form because "is the key going to arrive in the right place" is the
// single question this screen exists to answer, and a preview answers it before
// a failed run does.
export function previewRequest(draft) {
  const headers = {};
  const query = {};
  const keyPlaceholder = '<your key>';

  if (draft?.authScheme === AUTH_SCHEME.BEARER) {
    headers.Authorization = `Bearer ${keyPlaceholder}`;
  } else if (draft?.authScheme === AUTH_SCHEME.HEADER && String(draft?.authHeaderName || '').trim()) {
    headers[String(draft.authHeaderName).trim()] = `${draft.authValuePrefix || ''}${keyPlaceholder}`;
  } else if (draft?.authScheme === AUTH_SCHEME.QUERY && String(draft?.authQueryParam || '').trim()) {
    query[String(draft.authQueryParam).trim()] = keyPlaceholder;
  }

  if (draft?.apiVersion && draft?.apiVersionHeader) headers[draft.apiVersionHeader] = draft.apiVersion;
  if (draft?.apiVersion && draft?.apiVersionQueryParam) query[draft.apiVersionQueryParam] = draft.apiVersion;
  if (draft?.organizationId) headers[draft.organizationHeader || 'OpenAI-Organization'] = draft.organizationId;
  if (draft?.projectId) headers[draft.projectHeader || 'OpenAI-Project'] = draft.projectId;
  Object.assign(headers, rowsToMap(draft?.extraHeaders));
  Object.assign(query, rowsToMap(draft?.extraQuery));

  const base = resolveBaseUrl(draft).replace(/\/+$/, '');
  return { url: base + pathForFormat(draft?.apiFormat), headers, query };
}

export function pathForFormat(apiFormat) {
  return apiFormat === API_FORMAT.ANTHROPIC ? '/messages' : '/chat/completions';
}

// Mirrors CustomProviderSpec.resolved_base_url: the deployment-shaped parts of
// an endpoint live in their own fields, so changing region is not a URL re-edit.
export function resolveBaseUrl(draft) {
  const substitutions = {
    region: draft?.region,
    accountId: draft?.accountId,
    organizationId: draft?.organizationId,
    projectId: draft?.projectId,
    apiVersion: draft?.apiVersion,
  };
  return Object.entries(substitutions).reduce(
    (url, [name, value]) => url.split(`{${name}}`).join(String(value || '')),
    String(draft?.baseUrl || '').trim(),
  );
}

export function customProviderList(description) {
  return (description?.providers || []).filter((provider) => provider?.isCustom);
}

export function existingCustomIds(description) {
  return customProviderList(description).map((provider) => provider.id);
}

// Which routing targets point at this provider. Shown before a delete, because
// removing the primary is a different decision from removing a vendor nobody
// selected — and the answer is already in the description.
export function routingUsage(description, providerId) {
  const roles = [];
  if (description?.selected?.primary?.providerId === providerId) roles.push('primary');
  (description?.selected?.fallbacks || []).forEach((target, index) => {
    if (target?.providerId === providerId) roles.push(index === 0 ? 'fallback' : `fallback ${index + 1}`);
  });
  return roles;
}

export function createEndpoint() {
  return '/api/admin/settings/llm-providers';
}

export function providerEndpoint(providerId) {
  return `/api/admin/settings/llm-providers/${encodeURIComponent(String(providerId || ''))}`;
}
