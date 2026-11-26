import React, { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Check, Loader2, Mic, RotateCcw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { EngineSettingsSkeleton, LoadingRegion } from '@/components/skeletons.jsx';
import {
  buildDeploymentOptionsPayload,
  buildTranscriptionPatch,
  effectiveValue,
  engineWarnings,
  findEngine,
  isDeploymentScoped,
  resolveSelectedEngineId,
  splitParameters,
  userEditableOptions,
  validateEngineOptions,
} from '@/lib/transcriptionEngines';
import { apiJson } from '@/lib/apiFetch';

// Transcription engine picker for the settings page.
//
// The engine list, its parameters and this deployment's defaults all come from
// GET /api/settings/transcription-engines, so a newly shipped engine appears
// here without a frontend change. The chosen engine applies to every future
// run, including per-student clip runs and the Hatchet worker.
//
// Parameters the backend marks deploymentScoped (the model checkpoint, the ffmpeg
// filter chain) decide what the server loads or runs, so they belong to the
// deployment document: an administrator edits them here and they are saved
// through the admin route; everyone else sees them read-only.
export default function TranscriptionEngineSettings({ onSettingsChanged, isAdmin = false }) {
  const [description, setDescription] = useState(null); // null = never loaded
  const [loadError, setLoadError] = useState('');
  const [saveError, setSaveError] = useState('');
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(0);
  const [selectedEngineId, setSelectedEngineId] = useState('');
  const [optionsByEngine, setOptionsByEngine] = useState({});
  // The deployment document's own engine options: what a read-only control
  // shows, and what an administrator edits.
  const [deploymentOptions, setDeploymentOptions] = useState({});
  const [showAdvanced, setShowAdvanced] = useState(false);

  async function loadEngines() {
    setLoadError('');
    try {
      const [enginesBody, settingsBody] = await Promise.all([
        apiJson('/api/settings/transcription-engines', {
          fallbackMessage: 'Failed to load transcription engines.',
        }),
        apiJson('/api/settings', { fallbackMessage: 'Failed to load settings.' }),
      ]);

      setDescription(enginesBody);
      setSelectedEngineId(resolveSelectedEngineId(enginesBody));
      setOptionsByEngine(userEditableOptions(enginesBody, settingsBody.overrides?.transcriptionEngineOptions));
      setDeploymentOptions(settingsBody.defaults?.transcriptionEngineOptions || {});
    } catch (error) {
      setLoadError(error.message || 'Failed to load transcription engines.');
    }
  }

  useEffect(() => {
    loadEngines();
  }, []);

  const engine = useMemo(() => findEngine(description, selectedEngineId), [description, selectedEngineId]);
  const overrides = optionsByEngine[selectedEngineId] || {};
  const deploymentBag = deploymentOptions[selectedEngineId] || {};
  const errors = useMemo(
    () => validateEngineOptions(engine, isAdmin ? { ...overrides, ...deploymentBag } : overrides),
    [engine, overrides, deploymentBag, isAdmin],
  );
  const { basic, advanced } = splitParameters(engine);
  const warnings = engineWarnings(engine);
  const hasErrors = Object.keys(errors).length > 0;

  function setOption(parameter, value) {
    const update = isDeploymentScoped(parameter) ? setDeploymentOptions : setOptionsByEngine;
    update((current) => ({
      ...current,
      [selectedEngineId]: { ...(current[selectedEngineId] || {}), [parameter.name]: value },
    }));
  }

  function resetOption(parameter) {
    const update = isDeploymentScoped(parameter) ? setDeploymentOptions : setOptionsByEngine;
    update((current) => {
      const next = { ...(current[selectedEngineId] || {}) };
      delete next[parameter.name];
      return { ...current, [selectedEngineId]: next };
    });
  }

  async function save() {
    setSaving(true);
    setSaveError('');
    try {
      const payload = buildTranscriptionPatch(description, selectedEngineId, optionsByEngine);
      if (isAdmin) {
        const deploymentPayload = buildDeploymentOptionsPayload(description, deploymentOptions);
        const saved = await apiJson('/api/admin/settings', {
          method: 'PATCH',
          json: deploymentPayload,
          fallbackMessage: 'Failed to save the deployment transcription options.',
        });
        setDeploymentOptions(saved.settings?.transcriptionEngineOptions || {});
      }
      const body = await apiJson('/api/settings', {
        method: 'PATCH',
        json: payload,
        fallbackMessage: 'Failed to save the transcription engine.',
      });
      setOptionsByEngine(userEditableOptions(description, optionsByEngine));
      setSavedAt(Date.now());
      if (onSettingsChanged) onSettingsChanged(body.settings);
    } catch (error) {
      setSaveError(error.message || 'Failed to save the transcription engine.');
    } finally {
      setSaving(false);
    }
  }

  function renderControl(parameter) {
    const scoped = isDeploymentScoped(parameter);
    const readOnly = scoped && !isAdmin;
    const source = scoped ? deploymentBag : overrides;
    const value = effectiveValue(engine, parameter, source);
    const isOverridden = !readOnly && source[parameter.name] !== undefined;
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
              onClick={() => resetOption(parameter)}
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
            disabled={readOnly}
            onClick={() => setOption(parameter, !value)}
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
            disabled={readOnly}
            value={String(value ?? '')}
            onChange={(event) => setOption(parameter, event.target.value)}
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
            disabled={readOnly}
            type={parameter.type === 'int' || parameter.type === 'float' ? 'number' : 'text'}
            step={parameter.type === 'float' ? '0.1' : undefined}
            min={parameter.minimum}
            max={parameter.maximum}
            value={value ?? ''}
            onChange={(event) => setOption(parameter, event.target.value)}
            className={`rounded-lg border px-3 py-1.5 text-sm ${
              error ? 'border-rose-300 bg-rose-50' : 'border-slate-200 bg-white'
            }`}
          />
        )}

        {parameter.help ? <div className="text-[11px] text-slate-500">{parameter.help}</div> : null}
        {scoped ? (
          <div className="text-[11px] font-medium text-slate-500">
            {readOnly ? 'Set by an administrator for every marker.' : 'Deployment setting: applies to every marker.'}
          </div>
        ) : null}
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
          <LoadingRegion label="Loading transcription engines">
            <EngineSettingsSkeleton />
          </LoadingRegion>
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
                    <span className="text-[11px] uppercase tracking-wide text-slate-500">
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
