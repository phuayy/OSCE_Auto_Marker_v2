import React, { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Check, Loader2, Mic, RotateCcw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  buildTranscriptionPatch,
  effectiveValue,
  engineWarnings,
  findEngine,
  resolveSelectedEngineId,
  splitParameters,
  validateEngineOptions,
} from '@/lib/transcriptionEngines';

// Transcription engine picker for the settings page.
//
// The engine list, its parameters and this deployment's defaults all come from
// GET /api/settings/transcription-engines, so a newly shipped engine appears
// here without a frontend change. The chosen engine applies to every future
// run, including per-student clip runs and the Hatchet worker.
export default function TranscriptionEngineSettings({ onSettingsChanged }) {
  const [description, setDescription] = useState(null); // null = never loaded
  const [loadError, setLoadError] = useState('');
  const [saveError, setSaveError] = useState('');
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(0);
  const [selectedEngineId, setSelectedEngineId] = useState('');
  const [optionsByEngine, setOptionsByEngine] = useState({});
  const [showAdvanced, setShowAdvanced] = useState(false);

  async function loadEngines() {
    setLoadError('');
    try {
      const [enginesResponse, settingsResponse] = await Promise.all([
        fetch('/api/settings/transcription-engines'),
        fetch('/api/settings'),
      ]);
      const enginesBody = await enginesResponse.json().catch(() => ({}));
      const settingsBody = await settingsResponse.json().catch(() => ({}));
      if (!enginesResponse.ok) throw new Error(enginesBody.error || 'Failed to load transcription engines.');
      if (!settingsResponse.ok) throw new Error(settingsBody.error || 'Failed to load settings.');

      setDescription(enginesBody);
      setSelectedEngineId(resolveSelectedEngineId(enginesBody));
      setOptionsByEngine(settingsBody.settings?.transcriptionEngineOptions || {});
    } catch (error) {
      setLoadError(error.message || 'Failed to load transcription engines.');
    }
  }

  useEffect(() => {
    loadEngines();
  }, []);

  const engine = useMemo(() => findEngine(description, selectedEngineId), [description, selectedEngineId]);
  const overrides = optionsByEngine[selectedEngineId] || {};
  const errors = useMemo(() => validateEngineOptions(engine, overrides), [engine, overrides]);
  const { basic, advanced } = splitParameters(engine);
  const warnings = engineWarnings(engine);
  const hasErrors = Object.keys(errors).length > 0;

  function setOption(name, value) {
    setOptionsByEngine((current) => ({
      ...current,
      [selectedEngineId]: { ...(current[selectedEngineId] || {}), [name]: value },
    }));
  }

  function resetOption(name) {
    setOptionsByEngine((current) => {
      const next = { ...(current[selectedEngineId] || {}) };
      delete next[name];
      return { ...current, [selectedEngineId]: next };
    });
  }

  async function save() {
    setSaving(true);
    setSaveError('');
    try {
      const payload = buildTranscriptionPatch(description, selectedEngineId, optionsByEngine);
      const response = await fetch('/api/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail?.[0]?.msg || body.error || 'Failed to save the transcription engine.');
      }
      setOptionsByEngine(body.settings?.transcriptionEngineOptions || {});
      setSavedAt(Date.now());
      if (onSettingsChanged) onSettingsChanged(body.settings);
    } catch (error) {
      setSaveError(error.message || 'Failed to save the transcription engine.');
    } finally {
      setSaving(false);
    }
  }

  function renderControl(parameter) {
    const value = effectiveValue(engine, parameter, overrides);
    const isOverridden = overrides[parameter.name] !== undefined;
    const error = errors[parameter.name];
    const controlId = `engine-${selectedEngineId}-${parameter.name}`;

    return (
      <div key={parameter.name} className="flex flex-col gap-1 py-2">
        <div className="flex items-center justify-between gap-3">
          <label htmlFor={controlId} className="text-xs font-semibold text-slate-700">
            {parameter.label}
          </label>
          {isOverridden ? (
            <button
              type="button"
              onClick={() => resetOption(parameter.name)}
              className="flex items-center gap-1 text-[11px] text-slate-500 hover:text-slate-800"
              title="Restore this deployment's default"
            >
              <RotateCcw className="h-3 w-3" />
              Reset
            </button>
          ) : null}
        </div>

        {parameter.type === 'bool' ? (
          <button
            id={controlId}
            type="button"
            role="switch"
            aria-checked={Boolean(value)}
            onClick={() => setOption(parameter.name, !value)}
            className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors ${
              value ? 'bg-violet-600' : 'bg-slate-300'
            }`}
          >
            <span
              className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
                value ? 'translate-x-[22px]' : 'translate-x-0.5'
              }`}
            />
          </button>
        ) : parameter.type === 'enum' ? (
          <select
            id={controlId}
            value={String(value ?? '')}
            onChange={(event) => setOption(parameter.name, event.target.value)}
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm"
          >
            {(parameter.options || []).map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        ) : (
          <input
            id={controlId}
            type={parameter.type === 'int' || parameter.type === 'float' ? 'number' : 'text'}
            step={parameter.type === 'float' ? '0.1' : undefined}
            min={parameter.minimum}
            max={parameter.maximum}
            value={value ?? ''}
            onChange={(event) => setOption(parameter.name, event.target.value)}
            className={`rounded-lg border px-3 py-1.5 text-sm ${
              error ? 'border-rose-300 bg-rose-50' : 'border-slate-200 bg-white'
            }`}
          />
        )}

        {parameter.help ? <div className="text-[11px] text-slate-500">{parameter.help}</div> : null}
        {error ? <div className="text-[11px] font-medium text-rose-600">{error}</div> : null}
      </div>
    );
  }

  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <Mic className="h-5 w-5 text-violet-700" />
          Transcription engine
        </CardTitle>
        <CardDescription>
          The speech-to-text model used for every new assessment, including each student clip.
          Options are stored per engine, so switching back and forth keeps the tuning you did for
          each one.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {loadError ? (
          <div className="flex items-center justify-between rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
            <span>{loadError}</span>
            <Button variant="outline" size="sm" onClick={loadEngines}>
              Retry
            </Button>
          </div>
        ) : description === null ? (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading…
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            <div className="grid gap-2 sm:grid-cols-2">
              {(description.engines || []).map((candidate) => {
                const isSelected = candidate.id === selectedEngineId;
                const unavailable = candidate.availability?.available === false;
                return (
                  <button
                    key={candidate.id}
                    type="button"
                    onClick={() => setSelectedEngineId(candidate.id)}
                    aria-pressed={isSelected}
                    className={`flex flex-col gap-1 rounded-xl border px-4 py-3 text-left transition-colors ${
                      isSelected
                        ? 'border-violet-400 bg-violet-50'
                        : 'border-slate-200 bg-white hover:border-slate-300'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-semibold text-slate-800">{candidate.label}</span>
                      {isSelected ? <Check className="h-4 w-4 text-violet-700" /> : null}
                    </div>
                    <span className="text-[11px] uppercase tracking-wide text-slate-400">
                      {candidate.vendor}
                    </span>
                    <span className="text-[11px] text-slate-600">{candidate.description}</span>
                    {unavailable ? (
                      <span className="mt-1 flex items-center gap-1 text-[11px] font-medium text-amber-700">
                        <AlertTriangle className="h-3 w-3" />
                        Not installed on this machine
                      </span>
                    ) : null}
                  </button>
                );
              })}
            </div>

            {warnings.map((warning) => (
              <div
                key={warning}
                className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
              >
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{warning}</span>
              </div>
            ))}

            {engine?.requirements && engine.availability?.available === false ? (
              <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
                {engine.requirements}
              </div>
            ) : null}

            <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-2">
              <div className="divide-y divide-slate-200">{basic.map(renderControl)}</div>
              {advanced.length ? (
                <>
                  <button
                    type="button"
                    onClick={() => setShowAdvanced((current) => !current)}
                    className="mt-2 text-[11px] font-semibold text-slate-600 hover:text-slate-900"
                  >
                    {showAdvanced ? 'Hide' : 'Show'} advanced options ({advanced.length})
                  </button>
                  {showAdvanced ? (
                    <div className="divide-y divide-slate-200">{advanced.map(renderControl)}</div>
                  ) : null}
                </>
              ) : null}
            </div>

            <div className="flex items-center justify-between gap-3">
              <div className="text-[11px] text-slate-500">
                {savedAt ? 'Saved. Applies to every run started from now on.' : 'Changes apply to future runs.'}
              </div>
              <Button size="sm" onClick={save} disabled={saving || hasErrors}>
                {saving ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
                Save engine
              </Button>
            </div>

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
