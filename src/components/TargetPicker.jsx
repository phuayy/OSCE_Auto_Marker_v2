// One provider + model choice, as the settings screen asks for it.
//
// The scoring-model card asks for two of these (primary, fallback) and the
// marking-mode card for three (two markers, an adjudicator). They used to be a
// render function inside the routing card; every rule that lives here — a
// provider switch resets the model, a provider with no shortlist asks for a
// typed id, a provider with no key is warned about, a test button probes the
// exact pair shown — has to hold for all of them, so it lives once.
//
// The component is controlled: it owns no target state, only the callbacks.
// `onChange(next)` receives `{ providerId, model }`; a `noneOption` turns the
// provider dropdown into "may be empty", for the roles where nothing is a
// legitimate choice.
import React from 'react';
import { CheckCircle2, Loader2, PlugZap, XCircle } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { apiJson } from '@/lib/apiFetch';
import {
  CUSTOM_MODEL,
  findModelSpec,
  findProvider,
  formatContextWindow,
  isProviderReady,
  providerList,
  shouldShowCustomModelField,
} from '@/lib/llmProviders';

export default function TargetPicker({
  id,
  title,
  hint,
  description,
  target,
  onChange,
  noneOption = null,
  error = '',
  testing = false,
  testResult = null,
  onTest = null,
  disabled = false,
}) {
  const provider = findProvider(description, target?.providerId);
  const modelSpec = findModelSpec(provider, target?.model);
  const providerControlId = `${id}-provider`;
  const modelControlId = `${id}-model`;

  // "Other" starts life with an empty model — the id doesn't exist until the
  // operator types it — so whether the stored model looks custom can't carry
  // that choice by itself. This is the one bit of intent the component has to
  // remember on its own; see `shouldShowCustomModelField`.
  const [customSelected, setCustomSelected] = React.useState(false);
  const providerId = target?.providerId || '';
  React.useEffect(() => {
    setCustomSelected(false);
  }, [providerId]);

  const custom = shouldShowCustomModelField(provider, target?.model, customSelected);

  function selectProvider(nextProviderId) {
    const next = findProvider(description, nextProviderId);
    // Switching provider must reset the model: a model id is provider-scoped,
    // and carrying "gpt-4.1" over to Anthropic would 404 at scoring time.
    onChange({ providerId: nextProviderId, model: next ? next.defaultModelId || '' : '' });
  }

  function selectModel(value) {
    const choosingCustom = value === CUSTOM_MODEL;
    // The "custom" option clears the field so the operator types an id; it is
    // never stored as a model name itself.
    setCustomSelected(choosingCustom);
    onChange({ providerId: target?.providerId || '', model: choosingCustom ? '' : value });
  }

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-slate-800">{title}</div>
          {hint ? <div className="text-[11px] text-slate-500">{hint}</div> : null}
        </div>
        {onTest ? (
          <Button
            variant="outline"
            size="sm"
            className="gap-1"
            disabled={disabled || !target?.providerId || testing}
            onClick={onTest}
            title="Send one small request to this provider"
          >
            {testing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <PlugZap className="h-3.5 w-3.5" />}
            Test
          </Button>
        ) : null}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <label htmlFor={providerControlId} className="text-xs font-semibold text-slate-700">
            Provider
          </label>
          <select
            id={providerControlId}
            value={target?.providerId || ''}
            disabled={disabled}
            onChange={(event) => selectProvider(event.target.value)}
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm disabled:bg-slate-100"
          >
            {noneOption ? <option value={noneOption.value}>{noneOption.label}</option> : null}
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
            value={custom ? CUSTOM_MODEL : target?.model || ''}
            disabled={disabled || !provider}
            onChange={(event) => selectModel(event.target.value)}
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm disabled:bg-slate-100"
          >
            {(provider?.models || []).map((model) => (
              <option key={model.id} value={model.id}>
                {model.label}
              </option>
            ))}
            {provider?.allowsCustomModel ? <option value={CUSTOM_MODEL}>Other — type a model id…</option> : null}
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
            value={target?.model || ''}
            disabled={disabled}
            placeholder={provider?.defaultModelId || 'vendor/model-name'}
            onChange={(event) => onChange({ providerId: target?.providerId || '', model: event.target.value })}
            className={`rounded-lg border px-3 py-1.5 text-sm ${
              error ? 'border-rose-300 bg-rose-50' : 'border-slate-200 bg-white'
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

      {error ? <div className="text-[11px] font-medium text-rose-600">{error}</div> : null}

      {provider && !isProviderReady(provider) ? (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
          No API key for {provider.label}. Add one in <span className="font-semibold">Provider API keys</span> below —
          it applies immediately, with no restart.
        </div>
      ) : null}

      {testResult ? (
        <div
          role="status"
          className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-[11px] ${
            testResult.ok
              ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
              : 'border-rose-200 bg-rose-50 text-rose-700'
          }`}
        >
          {testResult.ok ? (
            <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          ) : (
            <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          )}
          <span>
            {testResult.ok
              ? `Reached ${testResult.model} in ${testResult.elapsedSeconds}s.`
              : testResult.error || 'The provider did not answer.'}
          </span>
        </div>
      ) : null}
    </div>
  );
}

// The one POST behind every Test button: probe exactly the pair on screen.
// Returns a `{ ok, ... }` result the picker renders; never throws, because a
// failed probe is a result to show, not an error to recover from.
export async function testTarget(target) {
  try {
    return await apiJson('/api/settings/llm-providers/test', {
      method: 'POST',
      json: { providerId: target.providerId, model: target.model || '' },
      fallbackMessage: 'The connection test could not be run.',
    });
  } catch (error) {
    return { ok: false, error: error.message || 'The connection test could not be run.' };
  }
}
