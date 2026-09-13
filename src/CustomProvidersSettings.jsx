import React, { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Loader2,
  Pencil,
  Plus,
  PlugZap,
  ServerCog,
  Trash2,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  API_FORMAT_OPTIONS,
  AUTH_SCHEME,
  AUTH_SCHEME_OPTIONS,
  PRESETS,
  applyPreset,
  buildProviderPayload,
  createEndpoint,
  customProviderList,
  draftFromProvider,
  emptyDraft,
  existingCustomIds,
  previewRequest,
  providerEndpoint,
  resolveBaseUrl,
  routingUsage,
  validateDraft,
} from '@/lib/customProviders';
import { apiJson } from '@/lib/apiFetch';

// Scoring providers an operator defines, rather than the six this build ships.
//
// The card is a *connection* editor, not a model picker: one key authorises a
// whole catalogue, and the checkpoint changes far more often than the endpoint
// does, so the model stays in the Scoring model card above. Everything here
// answers the one question "how do I talk to this platform".
//
// Fields are a union across what the current market needs — bearer tokens,
// x-api-key, Azure's api-key plus api-version, query-parameter keys,
// organisation/project/account identifiers, gateway headers, vendor body
// switches — and all of it is optional but the id, the endpoint and the key.
// Most vendors need three fields; the rest are there so the one vendor that
// needs the fourth is not a code change.
export default function CustomProvidersSettings({ version = 0, onProvidersChanged }) {
  const [description, setDescription] = useState(null); // null = never loaded
  const [loadError, setLoadError] = useState('');
  const [draft, setDraft] = useState(null); // null = form closed
  const [editingId, setEditingId] = useState(''); // '' = creating
  const [errors, setErrors] = useState({});
  const [formError, setFormError] = useState('');
  const [busy, setBusy] = useState('');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState('');

  async function loadProviders() {
    setLoadError('');
    try {
      const body = await apiJson('/api/settings/llm-providers', {
        fallbackMessage: 'Failed to load scoring providers.',
      });
      setDescription(body);
    } catch (error) {
      setLoadError(error.message || 'Failed to load scoring providers.');
    }
  }

  useEffect(() => {
    loadProviders();
    // Bumped by the sibling cards: adding a provider changes which models can
    // be selected, and saving a key changes whether one is usable.
  }, [version]);

  const providers = useMemo(() => customProviderList(description), [description]);
  const takenIds = useMemo(() => existingCustomIds(description), [description]);
  const isNew = !editingId;

  // Every write answers with the whole provider description, so the card
  // refreshes from the server's view rather than from what the form believes.
  function adoptDescription(body) {
    setDescription(body);
    if (onProvidersChanged) onProvidersChanged();
  }

  function openCreate() {
    setDraft(emptyDraft());
    setEditingId('');
    setErrors({});
    setFormError('');
    setAdvancedOpen(false);
  }

  function openEdit(provider) {
    setDraft(draftFromProvider(provider));
    setEditingId(provider.id);
    setErrors({});
    setFormError('');
    setAdvancedOpen(false);
  }

  function closeForm() {
    // Dropped rather than kept: the draft holds a key the operator just typed,
    // and state that survives navigation ends up in a devtools dump.
    setDraft(null);
    setEditingId('');
    setErrors({});
    setFormError('');
  }

  function update(field, value) {
    setDraft((current) => ({ ...current, [field]: value }));
    setErrors((current) => ({ ...current, [field]: '' }));
    setFormError('');
  }

  async function save() {
    const found = validateDraft(draft, { existingIds: takenIds, isNew });
    setErrors(found);
    if (Object.keys(found).length) return;

    setBusy('saving');
    setFormError('');
    try {
      const body = await apiJson(isNew ? createEndpoint() : providerEndpoint(editingId), {
        method: isNew ? 'POST' : 'PUT',
        json: buildProviderPayload(draft),
        fallbackMessage: 'Failed to save the provider.',
      });
      adoptDescription(body);
      closeForm();
    } catch (error) {
      setFormError(error.message || 'Failed to save the provider.');
    } finally {
      setBusy('');
    }
  }

  async function remove(providerId) {
    setBusy(`deleting:${providerId}`);
    setLoadError('');
    try {
      const body = await apiJson(providerEndpoint(providerId), {
        method: 'DELETE',
        fallbackMessage: 'Failed to remove the provider.',
      });
      adoptDescription(body);
      setConfirmingDelete('');
      if (editingId === providerId) closeForm();
    } catch (error) {
      setLoadError(error.message || 'Failed to remove the provider.');
    } finally {
      setBusy('');
    }
  }

  // --- small building blocks -------------------------------------------

  function field(name, label, { placeholder = '', hint = '', type = 'text', mono = false } = {}) {
    const id = `custom-provider-${name}`;
    return (
      <div className="flex flex-col gap-1">
        <label htmlFor={id} className="text-xs font-semibold text-slate-700">
          {label}
        </label>
        <input
          id={id}
          type={type}
          value={draft[name] ?? ''}
          placeholder={placeholder}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => update(name, event.target.value)}
          className={`rounded-lg border px-3 py-1.5 text-sm ${mono ? 'font-mono' : ''} ${
            errors[name] ? 'border-rose-300 bg-rose-50' : 'border-slate-200 bg-white'
          }`}
        />
        {errors[name] ? (
          <div className="text-[11px] font-medium text-rose-600">{errors[name]}</div>
        ) : hint ? (
          <div className="text-[11px] text-slate-500">{hint}</div>
        ) : null}
      </div>
    );
  }

  function toggle(name, label, hint) {
    return (
      <label className="flex items-start gap-2 text-xs text-slate-700">
        <input
          type="checkbox"
          checked={Boolean(draft[name])}
          onChange={(event) => update(name, event.target.checked)}
          className="mt-0.5"
        />
        <span>
          <span className="font-semibold">{label}</span>
          {hint ? <span className="block text-[11px] font-normal text-slate-500">{hint}</span> : null}
        </span>
      </label>
    );
  }

  function pairEditor(name, label, hint, namePlaceholder, valuePlaceholder) {
    const rows = draft[name] || [];
    return (
      <div className="flex flex-col gap-1">
        <div className="text-xs font-semibold text-slate-700">{label}</div>
        <div className="text-[11px] text-slate-500">{hint}</div>
        {rows.map((row, index) => (
          <div key={index} className="flex items-center gap-2">
            <input
              type="text"
              value={row.name}
              placeholder={namePlaceholder}
              onChange={(event) => {
                const next = rows.map((item, position) =>
                  position === index ? { ...item, name: event.target.value } : item,
                );
                update(name, next);
              }}
              className="w-2/5 rounded-lg border border-slate-200 bg-white px-2 py-1 font-mono text-xs"
            />
            <input
              type="text"
              value={row.value}
              placeholder={valuePlaceholder}
              onChange={(event) => {
                const next = rows.map((item, position) =>
                  position === index ? { ...item, value: event.target.value } : item,
                );
                update(name, next);
              }}
              className="flex-1 rounded-lg border border-slate-200 bg-white px-2 py-1 font-mono text-xs"
            />
            <button
              type="button"
              onClick={() => update(name, rows.filter((_, position) => position !== index))}
              className="text-slate-400 hover:text-rose-600"
              aria-label={`Remove ${label} row ${index + 1}`}
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        ))}
        <Button
          variant="outline"
          size="sm"
          className="w-fit gap-1"
          onClick={() => update(name, [...rows, { name: '', value: '' }])}
        >
          <Plus className="h-3.5 w-3.5" />
          Add row
        </Button>
      </div>
    );
  }

  function renderForm() {
    const preview = previewRequest(draft);
    return (
      <div className="flex flex-col gap-4 rounded-xl border border-violet-200 bg-violet-50/40 px-4 py-4">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold text-slate-800">
            {isNew ? 'Add a scoring provider' : `Edit ${editingId}`}
          </div>
          <Button variant="outline" size="sm" onClick={closeForm}>
            Cancel
          </Button>
        </div>

        {isNew ? (
          <div className="flex flex-col gap-1">
            <div className="text-xs font-semibold text-slate-700">Start from</div>
            <div className="flex flex-wrap gap-2">
              {PRESETS.map((preset) => (
                <button
                  key={preset.id}
                  type="button"
                  title={preset.hint}
                  onClick={() => setDraft((current) => applyPreset(current, preset.id))}
                  className="rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] text-slate-700 hover:border-violet-300 hover:text-violet-700"
                >
                  {preset.label}
                </button>
              ))}
            </div>
            <div className="text-[11px] text-slate-500">
              A starting point only — every field stays editable.
            </div>
          </div>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-2">
          {isNew
            ? field('id', 'Provider id', {
                placeholder: 'campus-gateway',
                hint: 'Lowercase, no spaces. Used in settings and logs; cannot be changed later.',
                mono: true,
              })
            : null}
          {field('label', 'Display name', { placeholder: 'Campus AI Gateway' })}
          {field('vendor', 'Vendor', { placeholder: 'University IT', hint: 'Optional.' })}
          {field('documentationUrl', 'Docs URL', {
            placeholder: 'https://…',
            hint: 'Optional. Shown next to the provider.',
          })}
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          {field('baseUrl', 'Inference endpoint (base URL)', {
            placeholder: 'https://api.example.com/v1',
            hint: 'Without the /chat/completions suffix. {region} and {accountId} are filled in from the fields below.',
            mono: true,
          })}
          <div className="flex flex-col gap-1">
            <label htmlFor="custom-provider-apiFormat" className="text-xs font-semibold text-slate-700">
              API format
            </label>
            <select
              id="custom-provider-apiFormat"
              value={draft.apiFormat}
              onChange={(event) => update('apiFormat', event.target.value)}
              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm"
            >
              {API_FORMAT_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <div className="text-[11px] text-slate-500">
              {API_FORMAT_OPTIONS.find((option) => option.value === draft.apiFormat)?.hint}
            </div>
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <label htmlFor="custom-provider-authScheme" className="text-xs font-semibold text-slate-700">
              Where the key goes
            </label>
            <select
              id="custom-provider-authScheme"
              value={draft.authScheme}
              onChange={(event) => update('authScheme', event.target.value)}
              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm"
            >
              {AUTH_SCHEME_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <div className="text-[11px] text-slate-500">
              {AUTH_SCHEME_OPTIONS.find((option) => option.value === draft.authScheme)?.hint}
            </div>
          </div>

          {draft.authScheme === AUTH_SCHEME.HEADER
            ? field('authHeaderName', 'Header name', { placeholder: 'x-api-key', mono: true })
            : null}
          {draft.authScheme === AUTH_SCHEME.QUERY
            ? field('authQueryParam', 'Query parameter', { placeholder: 'key', mono: true })
            : null}
          {draft.authScheme === AUTH_SCHEME.HEADER
            ? field('authValuePrefix', 'Value prefix', {
                placeholder: 'Api-Key ',
                hint: 'Optional. Trailing spaces are kept exactly as typed.',
                mono: true,
              })
            : null}
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="custom-provider-apiKey" className="text-xs font-semibold text-slate-700">
            {isNew ? 'API key' : 'Replace API key'}
          </label>
          <input
            id="custom-provider-apiKey"
            type="password"
            value={draft.apiKey}
            autoComplete="off"
            spellCheck={false}
            placeholder={isNew ? 'Paste the key for this platform' : 'Leave blank to keep the stored key'}
            onChange={(event) => update('apiKey', event.target.value)}
            className={`rounded-lg border px-3 py-1.5 font-mono text-sm ${
              errors.apiKey ? 'border-rose-300 bg-rose-50' : 'border-slate-200 bg-white'
            }`}
          />
          {errors.apiKey ? (
            <div className="text-[11px] font-medium text-rose-600">{errors.apiKey}</div>
          ) : (
            <div className="text-[11px] text-slate-500">
              Stored encrypted in the same place every other provider&apos;s key is, and never sent back to
              this screen. Rotating it later applies to the next run with no restart.
            </div>
          )}
        </div>

        <button
          type="button"
          onClick={() => setAdvancedOpen((current) => !current)}
          className="flex w-fit items-center gap-1 text-xs font-semibold text-violet-700"
        >
          {advancedOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          Deployment details and extras
        </button>

        {advancedOpen ? (
          <div className="flex flex-col gap-4 rounded-lg border border-slate-200 bg-white px-3 py-3">
            <div className="text-[11px] text-slate-500">
              All optional. Fill in only what your platform documents — Azure needs an API version, OpenAI
              accepts an organisation, Cloudflare puts an account id in the URL, and most vendors need none
              of it.
            </div>

            <div className="grid gap-3 sm:grid-cols-3">
              {field('apiVersion', 'API version', { placeholder: '2024-10-21', mono: true })}
              {field('apiVersionHeader', '…as a header', { placeholder: 'anthropic-version', mono: true })}
              {field('apiVersionQueryParam', '…as a query parameter', {
                placeholder: 'api-version',
                mono: true,
              })}
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              {field('organizationId', 'Organisation id', { placeholder: 'org-…', mono: true })}
              {field('organizationHeader', 'Organisation header', {
                placeholder: 'OpenAI-Organization',
                mono: true,
              })}
              {field('projectId', 'Project id', { placeholder: 'proj_…', mono: true })}
              {field('projectHeader', 'Project header', { placeholder: 'OpenAI-Project', mono: true })}
              {field('accountId', 'Account id', {
                placeholder: 'used by {accountId} in the URL',
                mono: true,
              })}
              {field('region', 'Region', { placeholder: 'used by {region} in the URL', mono: true })}
            </div>

            {pairEditor(
              'extraHeaders',
              'Extra headers',
              'Sent with every request — an OpenRouter HTTP-Referer, a gateway token.',
              'X-Title',
              'OSCE Marker',
            )}
            {pairEditor(
              'extraQuery',
              'Extra query parameters',
              'Appended to every request URL.',
              'name',
              'value',
            )}

            <div className="flex flex-col gap-1">
              <label htmlFor="custom-provider-extraBody" className="text-xs font-semibold text-slate-700">
                Extra request body fields (JSON)
              </label>
              <textarea
                id="custom-provider-extraBody"
                rows={3}
                value={draft.extraBodyText}
                spellCheck={false}
                placeholder={'{"chat_template_kwargs": {"enable_thinking": false}}'}
                onChange={(event) => update('extraBodyText', event.target.value)}
                className={`rounded-lg border px-3 py-1.5 font-mono text-xs ${
                  errors.extraBodyText ? 'border-rose-300 bg-rose-50' : 'border-slate-200 bg-white'
                }`}
              />
              {errors.extraBodyText ? (
                <div className="text-[11px] font-medium text-rose-600">{errors.extraBodyText}</div>
              ) : (
                <div className="text-[11px] text-slate-500">
                  Vendor-specific switches. Dropped automatically on the retry that follows a rejection, so a
                  field this endpoint does not accept costs one attempt rather than the run.
                </div>
              )}
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              {field('requestTimeoutSeconds', 'Request timeout (seconds)', {
                placeholder: 'blank = use the caller’s',
                hint: 'Only lowers a timeout; it never raises one a scoring run asked for.',
              })}
              <div className="flex flex-col gap-2 pt-5">
                {toggle(
                  'supportsJsonMode',
                  'Accepts response_format: json_object',
                  'Turn off for endpoints that reject it — self-hosted servers usually do.',
                )}
                {toggle('supportsReasoningControl', 'Exposes a reasoning control')}
                {toggle('enabled', 'Enabled', 'Off keeps the definition but takes it out of routing.')}
              </div>
            </div>
          </div>
        ) : null}

        <div className="flex flex-col gap-1 rounded-lg border border-slate-200 bg-white px-3 py-2">
          <div className="text-[11px] font-semibold text-slate-700">This provider will be called as</div>
          <div className="font-mono text-[11px] text-slate-600 break-all">POST {preview.url}</div>
          {Object.entries(preview.headers).map(([name, value]) => (
            <div key={name} className="font-mono text-[11px] text-slate-500 break-all">
              {name}: {value}
            </div>
          ))}
          {Object.keys(preview.query).length ? (
            <div className="font-mono text-[11px] text-slate-500 break-all">
              ?{Object.entries(preview.query).map(([name, value]) => `${name}=${value}`).join('&')}
            </div>
          ) : null}
        </div>

        {formError ? (
          <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] text-rose-700">
            {formError}
          </div>
        ) : null}

        <div className="flex items-center gap-2">
          <Button size="sm" disabled={busy === 'saving'} onClick={save}>
            {busy === 'saving' ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
            {isNew ? 'Add provider' : 'Save changes'}
          </Button>
          <div className="text-[11px] text-slate-500">
            Applies to the next assessment in every process — no restart.
          </div>
        </div>
      </div>
    );
  }

  function renderProvider(provider) {
    const connection = provider.connection || {};
    const roles = routingUsage(description, provider.id);
    const deleting = busy === `deleting:${provider.id}`;

    return (
      <div
        key={provider.id}
        className="flex flex-col gap-2 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3"
      >
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <div className="text-sm font-semibold text-slate-800">
              {provider.label}
              <span className="ml-2 font-mono text-[11px] font-normal text-slate-500">{provider.id}</span>
            </div>
            <div className="font-mono text-[11px] text-slate-500 break-all">
              {resolveBaseUrl(connection) || connection.resolvedBaseUrl}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {connection.enabled === false ? (
              <span className="rounded-full border border-slate-300 bg-white px-2 py-0.5 text-[11px] text-slate-600">
                Disabled
              </span>
            ) : null}
            {roles.length ? (
              <span className="rounded-full border border-violet-200 bg-violet-50 px-2 py-0.5 text-[11px] text-violet-700">
                In use as {roles.join(' and ')}
              </span>
            ) : null}
            <Button variant="outline" size="sm" className="gap-1" onClick={() => openEdit(provider)}>
              <Pencil className="h-3.5 w-3.5" />
              Edit
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="gap-1 text-rose-700"
              disabled={deleting}
              onClick={() => setConfirmingDelete(provider.id)}
            >
              {deleting ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Trash2 className="h-3.5 w-3.5" />
              )}
              Remove
            </Button>
          </div>
        </div>

        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
          <span>{connection.apiFormat === 'anthropic' ? 'Anthropic Messages' : 'OpenAI-compatible'}</span>
          <span>
            key via{' '}
            {connection.authScheme === 'query'
              ? `?${connection.authQueryParam}`
              : connection.authScheme === 'header'
                ? connection.authHeaderName
                : 'Authorization: Bearer'}
          </span>
          <span>
            {provider.availability?.available ? 'Key configured' : 'No API key — runs will skip it'}
          </span>
        </div>

        {confirmingDelete === provider.id ? (
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] text-rose-700">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            <span>
              Remove {provider.label} and its stored key?
              {roles.length
                ? ` It is the ${roles.join(' and ')} for scoring — runs will fall back to the next usable target.`
                : ''}{' '}
              Revoke the key at the vendor as well if you believe it leaked.
            </span>
            <Button size="sm" className="bg-rose-600 hover:bg-rose-700" onClick={() => remove(provider.id)}>
              Remove
            </Button>
            <Button variant="outline" size="sm" onClick={() => setConfirmingDelete('')}>
              Keep
            </Button>
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <ServerCog className="h-5 w-5 text-violet-700" />
          Custom scoring providers
        </CardTitle>
        <CardDescription>
          Connect a platform this build does not ship — a new vendor, a departmental gateway, an Azure
          deployment, a self-hosted server. You describe how to reach it and how it wants the key; the model
          itself is still chosen in Scoring model above, because one key covers a whole catalogue. A provider
          added here is usable by the next assessment, with no restart.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {loadError ? (
          <div className="flex items-center justify-between rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
            <span>{loadError}</span>
            <Button variant="outline" size="sm" onClick={loadProviders}>
              Retry
            </Button>
          </div>
        ) : description === null ? (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading…
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            {providers.length ? (
              providers.map(renderProvider)
            ) : (
              <div className="flex items-start gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-3 text-xs text-slate-600">
                <PlugZap className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>
                  No custom providers yet. The six that ship with this build are configured in the cards
                  above; add one here when your institution starts using a platform they do not cover.
                </span>
              </div>
            )}

            {draft ? (
              renderForm()
            ) : (
              <Button variant="outline" size="sm" className="w-fit gap-1" onClick={openCreate}>
                <Plus className="h-3.5 w-3.5" />
                Add a provider
              </Button>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
