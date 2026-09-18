// Unit tests for the custom scoring-provider form logic.
//
// Run with: npm run test:ui  (node --test, no test framework dependency)
//
// The claims worth testing are the ones an operator's connection depends on:
// that the key ends up where the platform expects it, that a definition which
// could not work is caught before a round trip, that the form's rules match the
// server's, and that a key is never something this module reads back.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  API_FORMAT,
  AUTH_SCHEME,
  applyPreset,
  buildProviderPayload,
  customProviderList,
  draftFromProvider,
  emptyDraft,
  existingCustomIds,
  parseExtraBody,
  previewRequest,
  providerEndpoint,
  resolveBaseUrl,
  rowsToMap,
  routingUsage,
  validateDraft,
} from '../src/lib/customProviders.js';
import { isCustomModel } from '../src/lib/llmProviders.js';

const GATEWAY = emptyDraft({
  id: 'campus-gateway',
  label: 'Campus AI Gateway',
  baseUrl: 'https://llm.example.edu/v1',
  apiKey: 'sk-campus-1234',
});

const DESCRIPTION = {
  providers: [
    { id: 'nvidia', label: 'NVIDIA NIM', isCustom: false, models: [{ id: 'a', label: 'A' }] },
    {
      id: 'campus-gateway',
      label: 'Campus AI Gateway',
      isCustom: true,
      allowsCustomModel: true,
      models: [],
      availability: { available: true },
      connection: {
        id: 'campus-gateway',
        label: 'Campus AI Gateway',
        baseUrl: 'https://llm.example.edu/v1',
        resolvedBaseUrl: 'https://llm.example.edu/v1',
        apiFormat: 'openai',
        authScheme: 'bearer',
        extraHeaders: { 'X-Title': 'OSCE' },
        extraQuery: {},
        extraBody: {},
        supportsJsonMode: true,
        enabled: true,
      },
    },
  ],
  selected: {
    primary: { providerId: 'campus-gateway', model: 'llama-3.3-70b' },
    fallbacks: [{ providerId: 'nvidia', model: 'a' }],
  },
};

// --- validation ------------------------------------------------------------

test('a minimal definition needs an id, a name, an endpoint and a key', () => {
  assert.deepEqual(validateDraft(GATEWAY, { isNew: true }), {});
});

test('an id that could not be used in a URL or a variable name is refused', () => {
  for (const id of ['', 'A', 'Campus Gateway', 'campus/gateway', '-leading']) {
    const errors = validateDraft({ ...GATEWAY, id }, { isNew: true });
    assert.ok(errors.id, `expected '${id}' to be rejected`);
  }
});

test('an id already in use is refused when creating but not when editing', () => {
  const taken = { existingIds: ['campus-gateway'] };
  assert.ok(validateDraft(GATEWAY, { ...taken, isNew: true }).id);
  assert.equal(validateDraft(GATEWAY, { ...taken, isNew: false }).id, undefined);
});

test('a provider with no endpoint is refused: there would be nothing to call', () => {
  assert.ok(validateDraft({ ...GATEWAY, baseUrl: '' }, { isNew: true }).baseUrl);
  assert.ok(validateDraft({ ...GATEWAY, baseUrl: 'llm.example.edu' }, { isNew: true }).baseUrl);
  assert.ok(validateDraft({ ...GATEWAY, baseUrl: 'file:///etc/passwd' }, { isNew: true }).baseUrl);
});

test('an http endpoint is allowed, because a self-hosted box is on this network', () => {
  const draft = { ...GATEWAY, baseUrl: 'http://localhost:8000/v1' };
  assert.equal(validateDraft(draft, { isNew: true }).baseUrl, undefined);
});

test('each auth scheme demands the one field that makes it work', () => {
  const header = { ...GATEWAY, authScheme: AUTH_SCHEME.HEADER };
  assert.ok(validateDraft(header, { isNew: true }).authHeaderName);
  assert.equal(
    validateDraft({ ...header, authHeaderName: 'x-api-key' }, { isNew: true }).authHeaderName,
    undefined,
  );

  const query = { ...GATEWAY, authScheme: AUTH_SCHEME.QUERY };
  assert.ok(validateDraft(query, { isNew: true }).authQueryParam);
  assert.equal(
    validateDraft({ ...query, authQueryParam: 'key' }, { isNew: true }).authQueryParam,
    undefined,
  );
});

test('a key is required to create a provider but optional when editing one', () => {
  assert.ok(validateDraft({ ...GATEWAY, apiKey: '' }, { isNew: true }).apiKey);
  // On an edit the stored key is still there; a blank field means "keep it".
  assert.equal(validateDraft({ ...GATEWAY, apiKey: '' }, { isNew: false }).apiKey, undefined);
});

test('an obviously broken key is caught before the round trip', () => {
  assert.ok(validateDraft({ ...GATEWAY, apiKey: 'sk 1234567' }, { isNew: true }).apiKey);
  assert.ok(validateDraft({ ...GATEWAY, apiKey: 'short' }, { isNew: true }).apiKey);
});

test('a timeout outside the accepted range is refused', () => {
  assert.ok(validateDraft({ ...GATEWAY, requestTimeoutSeconds: '-1' }, { isNew: true }).requestTimeoutSeconds);
  assert.ok(validateDraft({ ...GATEWAY, requestTimeoutSeconds: 'soon' }, { isNew: true }).requestTimeoutSeconds);
  assert.equal(
    validateDraft({ ...GATEWAY, requestTimeoutSeconds: '' }, { isNew: true }).requestTimeoutSeconds,
    undefined,
  );
});

test('malformed extra body JSON is reported against its own field', () => {
  const errors = validateDraft({ ...GATEWAY, extraBodyText: '{oops' }, { isNew: true });
  assert.ok(errors.extraBodyText);
  assert.ok(validateDraft({ ...GATEWAY, extraBodyText: '[1,2]' }, { isNew: true }).extraBodyText);
  assert.equal(
    validateDraft({ ...GATEWAY, extraBodyText: '{"top_k": 40}' }, { isNew: true }).extraBodyText,
    undefined,
  );
});

test('an empty body editor is not an error', () => {
  assert.deepEqual(parseExtraBody(''), { value: {}, error: '' });
  assert.deepEqual(parseExtraBody('  '), { value: {}, error: '' });
});

// --- payload ---------------------------------------------------------------

test('the payload carries the connection and, only when typed, the key', () => {
  const payload = buildProviderPayload(GATEWAY);
  assert.equal(payload.id, 'campus-gateway');
  assert.equal(payload.baseUrl, 'https://llm.example.edu/v1');
  assert.equal(payload.apiKey, 'sk-campus-1234');

  // A blank key field on an edit must not read as "clear the stored key".
  assert.equal('apiKey' in buildProviderPayload({ ...GATEWAY, apiKey: '' }), false);
});

test('the id is lowercased so it matches what the server will store', () => {
  assert.equal(buildProviderPayload({ ...GATEWAY, id: '  Campus-Gateway ' }).id, 'campus-gateway');
});

test('the auth value prefix keeps its trailing space', () => {
  // "Api-Key secret" authenticates; "Api-Keysecret" does not, and trimming here
  // would make that failure impossible to diagnose from the form.
  const payload = buildProviderPayload({
    ...GATEWAY,
    authScheme: AUTH_SCHEME.HEADER,
    authHeaderName: 'Authorization',
    authValuePrefix: 'Api-Key ',
  });
  assert.equal(payload.authValuePrefix, 'Api-Key ');
});

test('header and query rows become maps, dropping unnamed rows', () => {
  const payload = buildProviderPayload({
    ...GATEWAY,
    extraHeaders: [
      { name: 'X-Title', value: 'OSCE' },
      { name: '  ', value: 'ignored' },
    ],
    extraQuery: [{ name: 'beta', value: 'true' }],
  });
  assert.deepEqual(payload.extraHeaders, { 'X-Title': 'OSCE' });
  assert.deepEqual(payload.extraQuery, { beta: 'true' });
});

test('an empty timeout becomes 0, meaning "use the caller\'s"', () => {
  assert.equal(buildProviderPayload({ ...GATEWAY, requestTimeoutSeconds: '' }).requestTimeoutSeconds, 0);
  assert.equal(buildProviderPayload({ ...GATEWAY, requestTimeoutSeconds: '45' }).requestTimeoutSeconds, 45);
});

test('rowsToMap ignores blank names rather than creating an empty header', () => {
  assert.deepEqual(rowsToMap([{ name: '', value: 'x' }, { name: 'a', value: 'b' }]), { a: 'b' });
});

// --- presets and URL templating -------------------------------------------

test('a preset fills in the fields an operator would otherwise type from memory', () => {
  const azure = applyPreset(emptyDraft(), 'azure-openai');
  assert.equal(azure.authScheme, AUTH_SCHEME.HEADER);
  assert.equal(azure.authHeaderName, 'api-key');
  assert.equal(azure.apiVersionQueryParam, 'api-version');
});

test('a preset never overwrites an endpoint already typed', () => {
  const typed = emptyDraft({ baseUrl: 'https://my.azure.example/openai/v1' });
  assert.equal(applyPreset(typed, 'azure-openai').baseUrl, 'https://my.azure.example/openai/v1');
});

test('an unknown preset id leaves the draft alone', () => {
  const draft = emptyDraft({ label: 'x' });
  assert.deepEqual(applyPreset(draft, 'nope'), draft);
});

test('deployment identifiers are substituted into the endpoint', () => {
  const draft = emptyDraft({
    baseUrl: 'https://{region}.api.example.com/accounts/{accountId}/v1',
    region: 'eu-west',
    accountId: 'acct-42',
  });
  assert.equal(resolveBaseUrl(draft), 'https://eu-west.api.example.com/accounts/acct-42/v1');
});

test('an unfilled placeholder resolves to nothing rather than to the literal braces', () => {
  assert.equal(resolveBaseUrl(emptyDraft({ baseUrl: 'https://x.example/{region}/v1' })), 'https://x.example//v1');
});

// --- the request preview ---------------------------------------------------

test('the preview shows a bearer key on the standard header', () => {
  const preview = previewRequest(GATEWAY);
  assert.equal(preview.url, 'https://llm.example.edu/v1/chat/completions');
  assert.equal(preview.headers.Authorization, 'Bearer <your key>');
  assert.deepEqual(preview.query, {});
});

test('the preview shows an Azure-shaped request the way Azure wants it', () => {
  const preview = previewRequest({
    ...GATEWAY,
    authScheme: AUTH_SCHEME.HEADER,
    authHeaderName: 'api-key',
    apiVersion: '2024-10-21',
    apiVersionQueryParam: 'api-version',
  });
  assert.equal(preview.headers['api-key'], '<your key>');
  assert.equal(preview.headers.Authorization, undefined);
  assert.deepEqual(preview.query, { 'api-version': '2024-10-21' });
});

test('the preview shows a query-parameter key and an Anthropic path', () => {
  const preview = previewRequest({
    ...GATEWAY,
    apiFormat: API_FORMAT.ANTHROPIC,
    authScheme: AUTH_SCHEME.QUERY,
    authQueryParam: 'key',
  });
  assert.ok(preview.url.endsWith('/messages'));
  assert.deepEqual(preview.query, { key: '<your key>' });
  assert.deepEqual(preview.headers, {});
});

test('the preview never contains the real key', () => {
  const preview = previewRequest({ ...GATEWAY, apiKey: 'sk-campus-1234' });
  assert.equal(JSON.stringify(preview).includes('sk-campus-1234'), false);
});

test('organisation and project identifiers land on their conventional headers', () => {
  const preview = previewRequest({ ...GATEWAY, organizationId: 'org-1', projectId: 'proj-2' });
  assert.equal(preview.headers['OpenAI-Organization'], 'org-1');
  assert.equal(preview.headers['OpenAI-Project'], 'proj-2');
});

// --- reading the server's description -------------------------------------

test('only operator-defined providers are listed in this card', () => {
  assert.deepEqual(customProviderList(DESCRIPTION).map((provider) => provider.id), ['campus-gateway']);
  assert.deepEqual(existingCustomIds(DESCRIPTION), ['campus-gateway']);
});

test('an edit draft is rebuilt from the server, and never carries a key', () => {
  const draft = draftFromProvider(DESCRIPTION.providers[1]);
  assert.equal(draft.id, 'campus-gateway');
  assert.equal(draft.baseUrl, 'https://llm.example.edu/v1');
  assert.deepEqual(draft.extraHeaders, [{ name: 'X-Title', value: 'OSCE' }]);
  // There is no endpoint that returns a key, so a pre-filled field would be a
  // lie that invites the operator to "keep" a credential that is not there.
  assert.equal(draft.apiKey, '');
});

test('routing usage names the roles a provider is selected for', () => {
  assert.deepEqual(routingUsage(DESCRIPTION, 'campus-gateway'), ['primary']);
  assert.deepEqual(routingUsage(DESCRIPTION, 'nvidia'), ['fallback']);
  assert.deepEqual(routingUsage(DESCRIPTION, 'unused'), []);
});

test('the endpoint helper encodes the id it is given', () => {
  assert.equal(providerEndpoint('campus-gateway'), '/api/admin/settings/llm-providers/campus-gateway');
  assert.equal(providerEndpoint('a b'), '/api/admin/settings/llm-providers/a%20b');
});

// --- the model dropdown's side of it --------------------------------------

test('a custom provider always drives the free-text model field', () => {
  // It ships no shortlist by design, so the dropdown would otherwise sit empty
  // with no way to type the id it is waiting for.
  const custom = DESCRIPTION.providers[1];
  assert.equal(isCustomModel(custom, ''), true);
  assert.equal(isCustomModel(custom, 'llama-3.3-70b'), true);
  // A shipped provider is unchanged: a known id still picks the option.
  assert.equal(isCustomModel(DESCRIPTION.providers[0], 'a'), false);
});
