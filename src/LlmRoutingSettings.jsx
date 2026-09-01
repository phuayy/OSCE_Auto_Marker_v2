import React, { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Brain, CheckCircle2, Loader2, PlugZap, XCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  CUSTOM_MODEL,
  NO_FALLBACK,
  buildSettingsPayload,
  describeRouting,
  effectiveDiffers,
  findModelSpec,
  findProvider,
  formatContextWindow,
  isCustomModel,
  isProviderReady,
  providerList,
  resolveFallbackTarget,
  resolveModelId,
  resolvePrimaryProviderId,
  routingWarnings,
  validateRouting,
} from '@/lib/llmProviders';

// Scoring-model picker for the settings page.
//
// Two dropdown pairs — provider + model for the primary, and the same for the
// fallback the router uses when the primary fails. The provider list, its
// models and its availability all come from GET /api/settings/llm-providers,
// so a provider added to the backend registry appears here with no change to
// this file. The choice applies to every future run: content scoring,
// communication scoring and transcript preprocessing, including per-clip runs
// and the Hatchet worker.
export default function LlmRoutingSettings() {
  const [description, setDescription] = useState(null); // null = never loaded
  const [loadError, setLoadError] = useState('');
  const [saveError, setSaveError] = useState('');
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(0);
  const [primary, setPrimary] = useState({ providerId: '', model: '' });
  const [fallback, setFallback] = useState({ providerId: NO_FALLBACK, model: '' });
  const [testing, setTesting] = useState('');
  const [testResults, setTestResults] = useState({});

  async function loadProviders() {
    setLoadError('');
    try {
      const response = await fetch('/api/settings/llm-providers');
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || 'Failed to load scoring providers.');

      setDescription(body);
      const primaryProviderId = resolvePrimaryProviderId(body);
      const primaryProvider = findProvider(body, primaryProviderId);
      setPrimary({
        providerId: primaryProviderId,
        model: resolveModelId(primaryProvider, body.selected?.primary?.model),
      });
      const storedFallback = resolveFallbackTarget(body);
      const fallbackProvider = findProvider(body, storedFallback.providerId);
      setFallback({
        providerId: storedFallback.providerId,
        model: fallbackProvider ? resolveModelId(fallbackProvider, storedFallback.model) : '',
      });
    } catch (error) {
      setLoadError(error.message || 'Failed to load scoring providers.');
    }
  }

  useEffect(() => {
    loadProviders();
  }, []);

  const primaryProvider = useMemo(
    () => findProvider(description, primary.providerId),
    [description, primary.providerId],
  );
  const fallbackProvider = useMemo(
    () => findProvider(description, fallback.providerId),
    [description, fallback.providerId],
  );
  const errors = useMemo(() => validateRouting({ description, primary }), [description, primary]);
  const warnings = useMemo(
    () => routingWarnings({ description, primary, fallback }),
    [description, primary, fallback],
  );
  const hasErrors = Object.keys(errors).length > 0;

  function selectProvider(role, providerId) {
    const provider = findProvider(description, providerId);
    const next = {
      providerId,
      // Switching provider must reset the model: a model id is provider-scoped,
      // and carrying "gpt-4.1" over to Anthropic would 404 at scoring time.
      model: provider ? provider.defaultModelId || '' : '',
    };
    if (role === 'primary') setPrimary(next);
    else setFallback(next);
  }

  function selectModel(role, value) {
    const setter = role === 'primary' ? setPrimary : setFallback;
    // The "custom" option clears the field so the operator types an id; it is
    // never stored as a model name itself.
    setter((current) => ({ ...current, model: value === CUSTOM_MODEL ? '' : value }));
  }

  async function save() {
    setSaving(true);
    setSaveError('');
    try {
      const current = await fetch('/api/settings');
      const currentBody = await current.json().catch(() => ({}));
      if (!current.ok) throw new Error(currentBody.error || 'Failed to read current settings.');

      const payload = buildSettingsPayload(currentBody.settings, { primary, fallback });
      const response = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail?.[0]?.msg || body.error || 'Failed to save the scoring model.');
      }
      setSavedAt(Date.now());
      // Re-read so the "what will actually run" line reflects the server's own
      // filtering rather than what this form believes it just saved.
      await loadProviders();
    } catch (error) {
      setSaveError(error.message || 'Failed to save the scoring model.');
    } finally {
      setSaving(false);
    }
  }

  async function testTarget(role) {
    const target = role === 'primary' ? primary : fallback;
    if (!target.providerId) return;
    setTesting(role);
    setTestResults((current) => ({ ...current, [role]: null }));
    try {
      const response = await fetch('/api/settings/llm-providers/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ providerId: target.providerId, model: target.model || '' }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail?.[0]?.msg || body.error || 'The connection test could not be run.');
      }
      setTestResults((current) => ({ ...current, [role]: body }));
    } catch (error) {
      setTestResults((current) => ({
        ...current,
        [role]: { ok: false, error: error.message || 'The connection test could not be run.' },
      }));
    } finally {
      setTesting('');
    }
  }

  function renderTargetControls(role) {
    const isPrimary = role === 'primary';
    const target = isPrimary ? primary : fallback;
    const provider = isPrimary ? primaryProvider : fallbackProvider;
    const custom = isCustomModel(provider, target.model);
    const modelSpec = findModelSpec(provider, target.model);
    const result = testResults[role];
    const providerControlId = `llm-${role}-provider`;
    const modelControlId = `llm-${role}-model`;

    return (
      <div className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-slate-800">
              {isPrimary ? 'Primary model' : 'Fallback model'}
            </div>
            <div className="text-[11px] text-slate-500">
              {isPrimary
                ? 'Tried first for every scoring call.'
                : 'Used only when the primary keeps failing — a different vendor is the strongest choice.'}
            </div>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="gap-1"
            disabled={!target.providerId || testing === role}
            onClick={() => testTarget(role)}
            title="Send one small request to this provider"
          >
            {testing === role ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <PlugZap className="h-3.5 w-3.5" />
            )}
            Test
          </Button>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <label htmlFor={providerControlId} className="text-xs font-semibold text-slate-700">
              Provider
            </label>
            <select
              id={providerControlId}
              value={target.providerId}
              onChange={(event) => selectProvider(role, event.target.value)}
              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm"
            >
              {!isPrimary ? <option value={NO_FALLBACK}>None — fail instead of switching</option> : null}
              {providerList(description).map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  {candidate.label}
                  {isProviderReady(candidate) ? '' : ' (no API key)'}
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-col gap-1">
            <label htmlFor={modelControlId} className="text-xs font-semibold text-slate-700">
              Model
            </label>
            <select
              id={modelControlId}
              value={custom ? CUSTOM_MODEL : target.model || ''}
              disabled={!provider}
              onChange={(event) => selectModel(role, event.target.value)}
              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm disabled:bg-slate-100"
            >
              {(provider?.models || []).map((model) => (
                <option key={model.id} value={model.id}>
                  {model.label}
                </option>
              ))}
              {provider?.allowsCustomModel ? (
                <option value={CUSTOM_MODEL}>Other — type a model id…</option>
              ) : null}
            </select>
          </div>
        </div>

        {custom ? (
          <div className="flex flex-col gap-1">
            <label htmlFor={`${modelControlId}-custom`} className="text-xs font-semibold text-slate-700">
              Model id
            </label>
            <input
              id={`${modelControlId}-custom`}
              type="text"
              value={target.model || ''}
              placeholder={provider?.defaultModelId || 'vendor/model-name'}
              onChange={(event) =>
                (isPrimary ? setPrimary : setFallback)((current) => ({
                  ...current,
                  model: event.target.value,
                }))
              }
              className={`rounded-lg border px-3 py-1.5 text-sm ${
                isPrimary && errors.primaryModel ? 'border-rose-300 bg-rose-50' : 'border-slate-200 bg-white'
              }`}
            />
            <div className="text-[11px] text-slate-500">
              Sent to {provider?.label || 'the provider'} exactly as typed. An id it does not recognise fails at
              scoring time with the provider&apos;s own error.
            </div>
          </div>
        ) : null}

        {modelSpec?.notes || formatContextWindow(modelSpec) ? (
          <div className="text-[11px] text-slate-500">
            {[formatContextWindow(modelSpec), modelSpec?.notes].filter(Boolean).join(' · ')}
          </div>
        ) : null}

        {isPrimary && errors.primaryModel ? (
          <div className="text-[11px] font-medium text-rose-600">{errors.primaryModel}</div>
        ) : null}

        {provider && !isProviderReady(provider) ? (
          <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
            {provider.requirements || provider.availability?.reason}
          </div>
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
                ? `Reached ${result.model} in ${result.elapsedSeconds}s.`
                : result.error || 'The provider did not answer.'}
            </span>
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <Brain className="h-5 w-5 text-violet-700" />
          Scoring model
        </CardTitle>
        <CardDescription>
          The language model that marks content and communication, and cleans up transcripts. If the primary
          provider is down, rate-limited, or rejects the request, the fallback runs instead of the assessment
          failing. Applies to every future run, including each student clip.
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
            {renderTargetControls('primary')}
            {renderTargetControls('fallback')}

            {warnings.map((warning) => (
              <div
                key={warning}
                className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
              >
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{warning}</span>
              </div>
            ))}

            {effectiveDiffers(description) ? (
              <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-600">
                Saved selection: <span className="font-mono">{describeRouting(description.selected)}</span>. What
                would actually run right now: <span className="font-mono">{describeRouting(description.effective)}</span>{' '}
                — the server dropped targets it has no API key for.
              </div>
            ) : null}

            <div className="flex items-center justify-between gap-3">
              <div className="text-[11px] text-slate-500">
                {savedAt ? 'Saved. Applies to every run started from now on.' : 'Changes apply to future runs.'}
              </div>
              <Button size="sm" onClick={save} disabled={saving || hasErrors}>
                {saving ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
                Save models
              </Button>
            </div>

            {errors.primaryProvider ? (
              <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                {errors.primaryProvider}
              </div>
            ) : null}
            {saveError ? (
              <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                {saveError}
              </div>
            ) : null}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
