import React, { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  PlugZap,
  ShieldCheck,
  Trash2,
  XCircle,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { LoadingRegion, ProviderKeysSkeleton } from '@/components/skeletons.jsx';
import { apiJson } from '@/lib/apiFetch';
import {
  canStoreKeys,
  credentialOf,
  describeKeySource,
  describeLastTest,
  isKeyStoredInApp,
  isKeyUnreadable,
  keyEndpoint,
  storageBlockedReason,
  testModelFor,
  unconfiguredRoutedProviders,
  validateKeyInput,
} from '@/lib/providerKeys';

// Per-platform API keys for the scoring providers.
//
// One key per provider, not per model: OpenRouter, NVIDIA and Gemini each
// authenticate their whole catalogue with a single credential, so the key lives
// on the provider row and every model chosen under it inherits it.
//
// The card only ever writes. The server returns a masked tail, where the key
// came from and how the last test went — never the key — so a compromised
// browser session cannot exfiltrate a credential that was set on another day.
// Saving applies to the next scoring run in every process, with no restart,
// which is what makes rotating a leaked key a thirty-second job.
export default function ProviderKeysSettings({ version = 0, onProvidersChanged }) {
  const [description, setDescription] = useState(null); // null = never loaded
  const [loadError, setLoadError] = useState('');
  const [drafts, setDrafts] = useState({});
  const [revealed, setRevealed] = useState({});
  const [busy, setBusy] = useState({}); // providerId -> 'saving' | 'clearing' | 'testing'
  const [errors, setErrors] = useState({});
  const [results, setResults] = useState({});

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
    // `version` is bumped by the model card after it saves, so the two cards
    // never show contradictory availability.
  }, [version]);

  const providers = description?.providers || [];
  const storageAvailable = canStoreKeys(description);
  const routedWithoutKeys = useMemo(() => unconfiguredRoutedProviders(description), [description]);

  function setProviderBusy(providerId, value) {
    setBusy((current) => ({ ...current, [providerId]: value }));
  }

  // Every write answers with the full provider description, so the card
  // refreshes from the server's own view rather than from what the form
  // believes it just did.
  function adoptDescription(body) {
    setDescription(body);
    if (onProvidersChanged) onProvidersChanged();
  }

  async function saveKey(providerId) {
    const draft = String(drafts[providerId] || '');
    const message = validateKeyInput(draft);
    if (message) {
      setErrors((current) => ({ ...current, [providerId]: message }));
      return;
    }
    setErrors((current) => ({ ...current, [providerId]: '' }));
    setProviderBusy(providerId, 'saving');
    try {
      const body = await apiJson(keyEndpoint(providerId), {
        method: 'PUT',
        json: { apiKey: draft.trim() },
        fallbackMessage: 'Failed to save the API key.',
      });
      // Cleared the moment it is stored: a key left sitting in component state
      // survives navigation and ends up in a React devtools dump.
      setDrafts((current) => ({ ...current, [providerId]: '' }));
      setRevealed((current) => ({ ...current, [providerId]: false }));
      setResults((current) => ({ ...current, [providerId]: null }));
      adoptDescription(body);
    } catch (error) {
      setErrors((current) => ({ ...current, [providerId]: error.message || 'Failed to save the API key.' }));
    } finally {
      setProviderBusy(providerId, '');
    }
  }

  async function clearKey(providerId) {
    setProviderBusy(providerId, 'clearing');
    setErrors((current) => ({ ...current, [providerId]: '' }));
    try {
      const body = await apiJson(keyEndpoint(providerId), {
        method: 'DELETE',
        fallbackMessage: 'Failed to remove the API key.',
      });
      setResults((current) => ({ ...current, [providerId]: null }));
      adoptDescription(body);
    } catch (error) {
      setErrors((current) => ({ ...current, [providerId]: error.message || 'Failed to remove the API key.' }));
    } finally {
      setProviderBusy(providerId, '');
    }
  }

  async function testKey(providerId) {
    const draft = String(drafts[providerId] || '').trim();
    setProviderBusy(providerId, 'testing');
    setResults((current) => ({ ...current, [providerId]: null }));
    try {
      const body = await apiJson('/api/settings/llm-providers/test', {
        method: 'POST',
        json: {
          providerId,
          model: testModelFor(description, providerId),
          // A key typed but not yet saved is probed as-is, so a mistyped
          // credential is caught before it replaces a working one. The server
          // holds it for the one call and never writes it.
          apiKey: draft,
        },
        fallbackMessage: 'The connection test could not be run.',
      });
      setResults((current) => ({ ...current, [providerId]: { ...body, probe: Boolean(draft) } }));
      // A saved-key test updates the row's verdict server-side; re-read so the
      // "verified just now" line survives a reload.
      if (!draft) await loadProviders();
    } catch (error) {
      setResults((current) => ({
        ...current,
        [providerId]: { ok: false, error: error.message || 'The connection test could not be run.' },
      }));
    } finally {
      setProviderBusy(providerId, '');
    }
  }

  function renderProvider(provider) {
    const credential = credentialOf(provider);
    const source = describeKeySource(provider);
    const lastTest = describeLastTest(provider);
    const result = results[provider.id];
    const state = busy[provider.id] || '';
    const draft = String(drafts[provider.id] || '');
    const fieldId = `provider-key-${provider.id}`;
    const toneClass =
      source.tone === 'ok'
        ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
        : source.tone === 'info'
          ? 'border-cyan-200 bg-cyan-50 text-cyan-800'
          : 'border-amber-200 bg-amber-50 text-amber-800';

    return (
      <div key={provider.id} className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <div className="text-sm font-semibold text-slate-800">{provider.label}</div>
            <div className="text-[11px] text-slate-500">
              {provider.vendor}
              {credential.maskedKey ? ` · key ${credential.maskedKey}` : ''}
            </div>
          </div>
          <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${toneClass}`}>
            {source.label}
          </span>
        </div>

        <div className="text-[11px] text-slate-500">{source.detail}</div>

        {isKeyUnreadable(provider) ? (
          <div className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] text-rose-700">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              A key is stored for this provider but this server&apos;s encryption key can no longer open it. Paste
              the key again to replace it.
            </span>
          </div>
        ) : null}

        {storageAvailable ? (
          <div className="flex flex-col gap-1">
            <label htmlFor={fieldId} className="text-xs font-semibold text-slate-700">
              {isKeyStoredInApp(provider) ? 'Replace key' : 'API key'}
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative min-w-[220px] flex-1">
                <input
                  id={fieldId}
                  type={revealed[provider.id] ? 'text' : 'password'}
                  value={draft}
                  autoComplete="off"
                  spellCheck={false}
                  placeholder={credential.maskedKey || `Paste your ${provider.label} key`}
                  onChange={(event) => {
                    const value = event.target.value;
                    setDrafts((current) => ({ ...current, [provider.id]: value }));
                    setErrors((current) => ({ ...current, [provider.id]: '' }));
                  }}
                  className={`w-full rounded-lg border px-3 py-1.5 pr-9 font-mono text-sm ${
                    errors[provider.id] ? 'border-rose-300 bg-rose-50' : 'border-slate-200 bg-white'
                  }`}
                />
                <button
                  type="button"
                  onClick={() => setRevealed((current) => ({ ...current, [provider.id]: !current[provider.id] }))}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
                  title={revealed[provider.id] ? 'Hide the key' : 'Show what you typed'}
                  aria-label={revealed[provider.id] ? 'Hide the key' : 'Show what you typed'}
                >
                  {revealed[provider.id] ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>

              <Button size="sm" disabled={!draft.trim() || Boolean(state)} onClick={() => saveKey(provider.id)}>
                {state === 'saving' ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
                Save
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="gap-1"
                disabled={Boolean(state) || (!draft.trim() && !credential.configured)}
                onClick={() => testKey(provider.id)}
                title={
                  draft.trim()
                    ? 'Send one small request using the key you just typed, without saving it'
                    : 'Send one small request using the key this server would actually use'
                }
              >
                {state === 'testing' ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <PlugZap className="h-3.5 w-3.5" />
                )}
                Test
              </Button>
              {isKeyStoredInApp(provider) ? (
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-1 text-rose-700"
                  disabled={Boolean(state)}
                  onClick={() => clearKey(provider.id)}
                  title="Stop this server sending the stored key. Revoke it at the vendor as well."
                >
                  {state === 'clearing' ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Trash2 className="h-3.5 w-3.5" />
                  )}
                  Remove
                </Button>
              ) : null}
            </div>
            {draft.trim() ? (
              <div className="text-[11px] text-slate-500">
                Test runs against the key typed above without saving it. Save to make it the key every future run
                uses.
              </div>
            ) : null}
          </div>
        ) : null}

        {errors[provider.id] ? (
          <div className="text-[11px] font-medium text-rose-600">{errors[provider.id]}</div>
        ) : null}

        {result ? (
          <div
            role="status"
            className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-[11px] ${
              result.ok
                ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                : 'border-rose-200 bg-rose-50 text-rose-700'
            }`}
          >
            {result.ok ? (
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            ) : (
              <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            )}
            <span>
              {result.ok
                ? `${result.probe ? 'The key you typed reached' : 'Reached'} ${result.model} in ${result.elapsedSeconds}s.`
                : result.error || 'The provider did not answer.'}
            </span>
          </div>
        ) : lastTest ? (
          <div className={`text-[11px] ${lastTest.ok ? 'text-emerald-700' : 'text-rose-600'}`}>{lastTest.text}</div>
        ) : null}
      </div>
    );
  }

  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <KeyRound className="h-5 w-5 text-violet-700" />
          Provider API keys
        </CardTitle>
        <CardDescription>
          One key per platform — it authorises every model that platform offers, so an OpenRouter key covers each
          model you can select above. Keys are stored encrypted, never sent back to this screen, and apply to the
          next run without restarting the server, so a key can be rotated the moment it is suspected.
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
          <LoadingRegion label="Loading provider keys">
            <ProviderKeysSkeleton />
          </LoadingRegion>
        ) : (
          <div className="flex flex-col gap-4">
            {!storageAvailable ? (
              <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{storageBlockedReason(description)}</span>
              </div>
            ) : null}

            {routedWithoutKeys.length ? (
              <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>
                  {routedWithoutKeys.map((provider) => provider.label).join(' and ')}{' '}
                  {routedWithoutKeys.length === 1 ? 'is' : 'are'} selected for scoring but{' '}
                  {routedWithoutKeys.length === 1 ? 'has' : 'have'} no key. Runs will skip{' '}
                  {routedWithoutKeys.length === 1 ? 'it' : 'them'} until one is saved.
                </span>
              </div>
            ) : null}

            {providers.map(renderProvider)}

            <div className="flex items-start gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-600">
              <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                Keys are encrypted with a key held by this deployment, not by the database, and are forwarded only
                to the scoring processes that call the provider they belong to. Removing a key here stops this
                server sending it — revoke it at the vendor as well if you believe it leaked.
              </span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
