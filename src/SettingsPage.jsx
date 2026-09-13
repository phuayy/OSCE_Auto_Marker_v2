import React, { useEffect, useState } from 'react';
import { ArrowLeft, Loader2, Settings as SettingsIcon, Wand2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import CorporaManager from '@/CorporaManager.jsx';
import CustomProvidersSettings from '@/CustomProvidersSettings.jsx';
import LlmRoutingSettings from '@/LlmRoutingSettings.jsx';
import MarkingModeSettings from '@/MarkingModeSettings.jsx';
import ProviderKeysSettings from '@/ProviderKeysSettings.jsx';
import TranscriptionEngineSettings from '@/TranscriptionEngineSettings.jsx';
import WebhooksManager from '@/WebhooksManager.jsx';
import { apiJson } from '@/lib/apiFetch';

// Global application settings page (#/settings). Settings live in the backend
// DB, so a toggle here applies immediately to every subsequent run — including
// per-student clip runs and the Hatchet worker — without a restart.
export default function SettingsPage({ onBack }) {
  const [settings, setSettings] = useState(null); // null = never loaded
  const [loadError, setLoadError] = useState('');
  const [saveError, setSaveError] = useState('');
  const [saving, setSaving] = useState(false);
  // Bumped whenever the model card or the API-keys card writes something the
  // other one renders. Saving a key changes which providers are usable, and
  // saving a model changes which key matters, so neither card can be the sole
  // owner of that state.
  const [providersVersion, setProvidersVersion] = useState(0);

  async function loadSettings() {
    setLoadError('');
    try {
      const body = await apiJson('/api/settings', { fallbackMessage: 'Failed to load settings.' });
      setSettings(body.settings || {});
    } catch (error) {
      setLoadError(error.message || 'Failed to load settings.');
    }
  }

  useEffect(() => {
    loadSettings();
  }, []);

  // A card's save answers with the whole stored document; adopting it keeps
  // this page's copy current without a second request. With no body to adopt,
  // re-read.
  function adoptSettings(next) {
    if (next && typeof next === 'object') setSettings(next);
    else loadSettings();
  }

  async function updateSetting(key, value) {
    const previous = settings;
    const next = { ...settings, [key]: value };
    setSettings(next);
    setSaving(true);
    setSaveError('');
    try {
      // PATCH, not PUT: this page holds a copy of the document loaded at mount,
      // and the cards above save their own keys without telling it. Sending
      // that copy back put those keys to their mount-time values — a marking
      // mode saved as "panel" reverted to "single" the moment this toggle was
      // flipped. A patch carries only the key that changed.
      const body = await apiJson('/api/settings', {
        method: 'PATCH',
        json: { [key]: value },
        fallbackMessage: 'Failed to save settings.',
      });
      setSettings(body.settings || next);
    } catch (error) {
      // Optimistic toggle reverts so the UI never lies about the stored value.
      setSettings(previous);
      setSaveError(error.message || 'Failed to save settings.');
    } finally {
      setSaving(false);
    }
  }

  const llmPreprocessOn = Boolean(settings?.llmTranscriptPreprocess);

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="sticky top-0 z-40 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-6 py-4">
          <div className="flex items-center gap-3">
            <Button variant="outline" size="sm" className="gap-2" onClick={onBack} title="Back to dashboard">
              <ArrowLeft className="h-4 w-4" />
              Back
            </Button>
            <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-slate-600 to-slate-800 text-white shadow-sm">
              <SettingsIcon className="h-5 w-5" />
            </div>
            <div>
              <div className="text-lg font-bold">Settings</div>
              <div className="text-xs text-slate-500">Global options applied to every assessment run</div>
            </div>
          </div>
        </div>
      </header>

      <main className="mx-auto flex max-w-3xl flex-col gap-6 px-6 py-8">
        {loadError ? (
          <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
            <span>{loadError}</span>
            <Button variant="outline" size="sm" onClick={loadSettings}>
              Retry
            </Button>
          </div>
        ) : null}

        <TranscriptionEngineSettings onSettingsChanged={adoptSettings} />

        <LlmRoutingSettings
          version={providersVersion}
          onProvidersChanged={() => setProvidersVersion((current) => current + 1)}
          onSettingsChanged={adoptSettings}
        />

        <MarkingModeSettings
          version={providersVersion}
          onProvidersChanged={() => setProvidersVersion((current) => current + 1)}
          onSettingsChanged={adoptSettings}
        />

        <ProviderKeysSettings
          version={providersVersion}
          onProvidersChanged={() => setProvidersVersion((current) => current + 1)}
        />

        <CustomProvidersSettings
          version={providersVersion}
          onProvidersChanged={() => setProvidersVersion((current) => current + 1)}
        />

        <Card className="border-slate-200 bg-white shadow-sm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <Wand2 className="h-5 w-5 text-violet-700" />
              LLM Transcription Preprocess
            </CardTitle>
            <CardDescription>
              After transcription and before scoring, an extra pass by the model selected above
              corrects obvious transcription errors (misheard words, garbled medical terms) using
              the clinical context of the dialogue. Timestamps, speaker labels, and segmentation
              are never altered. Applies to every future run, including each student clip.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {settings === null && !loadError ? (
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading…
              </div>
            ) : (
              <div className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
                <div>
                  <div className="text-sm font-semibold text-slate-800">
                    {llmPreprocessOn ? 'Enabled' : 'Disabled'}
                  </div>
                  <div className="text-[11px] text-slate-500">
                    {llmPreprocessOn
                      ? 'The llm_preprocess pipeline step runs on every new assessment.'
                      : 'The llm_preprocess pipeline step is skipped.'}
                  </div>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={llmPreprocessOn}
                  aria-label="Toggle LLM transcription preprocess"
                  disabled={saving || settings === null}
                  onClick={() => updateSetting('llmTranscriptPreprocess', !llmPreprocessOn)}
                  className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-60 ${
                    llmPreprocessOn ? 'bg-violet-600' : 'bg-slate-300'
                  }`}
                >
                  <span
                    className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
                      llmPreprocessOn ? 'translate-x-[22px]' : 'translate-x-0.5'
                    }`}
                  />
                </button>
              </div>
            )}
            {saveError && (
              <div className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                {saveError}
              </div>
            )}
          </CardContent>
        </Card>

        <CorporaManager />

        <WebhooksManager />
      </main>
    </div>
  );
}
