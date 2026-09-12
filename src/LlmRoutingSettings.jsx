import React, { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Brain, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import TargetPicker, { testTarget } from '@/components/TargetPicker.jsx';
import {
  NO_FALLBACK,
  buildSettingsPayload,
  describeRouting,
  effectiveDiffers,
  findProvider,
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
export default function LlmRoutingSettings({ version = 0, onProvidersChanged }) {
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
    // Re-reads when the API-keys card saves or removes a key: availability, and
    // therefore which targets would actually run, changes underneath this form.
  }, [version]);

  const errors = useMemo(() => validateRouting({ description, primary }), [description, primary]);
  const warnings = useMemo(
    () => routingWarnings({ description, primary, fallback }),
    [description, primary, fallback],
  );
  const hasErrors = Object.keys(errors).length > 0;

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
      if (onProvidersChanged) onProvidersChanged();
    } catch (error) {
      setSaveError(error.message || 'Failed to save the scoring model.');
    } finally {
      setSaving(false);
    }
  }

  async function runTest(role) {
    const target = role === 'primary' ? primary : fallback;
    if (!target.providerId) return;
    setTesting(role);
    setTestResults((current) => ({ ...current, [role]: null }));
    const result = await testTarget(target);
    setTestResults((current) => ({ ...current, [role]: result }));
    setTesting('');
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
            <TargetPicker
              id="llm-primary"
              title="Primary model"
              hint="Tried first for every scoring call."
              description={description}
              target={primary}
              onChange={setPrimary}
              error={errors.primaryModel || ''}
              testing={testing === 'primary'}
              testResult={testResults.primary}
              onTest={() => runTest('primary')}
            />
            <TargetPicker
              id="llm-fallback"
              title="Fallback model"
              hint="Used only when the primary keeps failing — a different vendor is the strongest choice."
              description={description}
              target={fallback}
              onChange={setFallback}
              noneOption={{ value: NO_FALLBACK, label: 'None — fail instead of switching' }}
              testing={testing === 'fallback'}
              testResult={testResults.fallback}
              onTest={() => runTest('fallback')}
            />

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
