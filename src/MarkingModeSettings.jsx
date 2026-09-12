import React, { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Loader2, Scale, Users } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import TargetPicker, { testTarget } from '@/components/TargetPicker.jsx';
import {
  MIN_PANEL_MARKERS,
  MarkingMode,
  NO_ADJUDICATOR,
  TIE_BREAK_LABELS,
  TieBreak,
  buildMarkingPayload,
  describePanel,
  effectiveMarkingDiffers,
  panelWarnings,
  resolvePanelState,
  validatePanel,
} from '@/lib/llmProviders';

// How content is marked: by one model (the scoring model above) or by a panel.
//
// A panel is two markers that score the same transcript with the same prompt,
// independently and in parallel, plus an adjudicator that is asked only about
// the criteria they disagree on. Everything the card shows — the modes, the
// tie-break policies, what the server would actually run — comes from
// GET /api/settings/llm-providers, so this file knows no vendor names.
export default function MarkingModeSettings({ version = 0, onProvidersChanged }) {
  const [description, setDescription] = useState(null); // null = never loaded
  const [loadError, setLoadError] = useState('');
  const [saveError, setSaveError] = useState('');
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(0);
  const [mode, setMode] = useState(MarkingMode.SINGLE);
  const [markers, setMarkers] = useState([]);
  const [adjudicator, setAdjudicator] = useState({ providerId: NO_ADJUDICATOR, model: '' });
  const [tieBreak, setTieBreak] = useState(TieBreak.LENIENT);
  const [testing, setTesting] = useState('');
  const [testResults, setTestResults] = useState({});

  async function loadProviders() {
    setLoadError('');
    try {
      const response = await fetch('/api/settings/llm-providers');
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || 'Failed to load scoring providers.');
      setDescription(body);
      const state = resolvePanelState(body);
      setMode(state.mode);
      setMarkers(state.markers);
      setAdjudicator(state.adjudicator);
      setTieBreak(state.tieBreak);
    } catch (error) {
      setLoadError(error.message || 'Failed to load scoring providers.');
    }
  }

  useEffect(() => {
    loadProviders();
    // Re-reads when a key is saved or removed: which markers can run changes
    // underneath this form, and the "what would run" line has to follow.
  }, [version]);

  const form = { description, mode, markers, adjudicator };
  const errors = useMemo(() => validatePanel(form), [description, mode, markers, adjudicator]);
  const warnings = useMemo(() => panelWarnings(form), [description, mode, markers, adjudicator]);
  const hasErrors = Object.keys(errors).length > 0;
  const isPanel = mode === MarkingMode.PANEL;
  const effective = description?.marking?.effective;

  function updateMarker(index, next) {
    setMarkers((current) => current.map((target, position) => (position === index ? next : target)));
  }

  async function save() {
    setSaving(true);
    setSaveError('');
    try {
      const current = await fetch('/api/settings');
      const currentBody = await current.json().catch(() => ({}));
      if (!current.ok) throw new Error(currentBody.error || 'Failed to read current settings.');

      const payload = buildMarkingPayload(currentBody.settings, { mode, markers, adjudicator, tieBreak });
      const response = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail?.[0]?.msg || body.detail || body.error || 'Failed to save the marking mode.');
      }
      setSavedAt(Date.now());
      await loadProviders();
      if (onProvidersChanged) onProvidersChanged();
    } catch (error) {
      setSaveError(error.message || 'Failed to save the marking mode.');
    } finally {
      setSaving(false);
    }
  }

  async function runTest(role, target) {
    if (!target?.providerId) return;
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
          <Users className="h-5 w-5 text-violet-700" />
          Marking mode
        </CardTitle>
        <CardDescription>
          <span className="font-semibold text-slate-700">Single model</span> marks content with the scoring model
          above. <span className="font-semibold text-slate-700">Panel</span> has two models mark the same transcript
          independently with the same prompt; the criteria they agree on are settled, and a third model — the
          adjudicator — decides only the ones they split on and merges their coaching feedback. Every vote is kept
          on the sheet. Applies to every future run, including each student clip.
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
            <div role="radiogroup" aria-label="Marking mode" className="grid gap-2 sm:grid-cols-2">
              {[
                [MarkingMode.SINGLE, 'Single model', 'One marker, one sheet. The scoring model above, with its fallback.'],
                [MarkingMode.PANEL, 'Panel', 'Two independent markers and an adjudicator for their disagreements.'],
              ].map(([value, label, blurb]) => {
                const selected = mode === value;
                return (
                  <button
                    key={value}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    onClick={() => setMode(value)}
                    className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                      selected
                        ? 'border-violet-400 bg-violet-50 ring-1 ring-violet-300'
                        : 'border-slate-200 bg-slate-50 hover:border-slate-300'
                    }`}
                  >
                    <div className="text-sm font-semibold text-slate-800">{label}</div>
                    <div className="text-[11px] text-slate-500">{blurb}</div>
                  </button>
                );
              })}
            </div>

            {isPanel ? (
              <>
                {markers.map((target, index) => (
                  <TargetPicker
                    key={index}
                    id={`marking-marker-${index + 1}`}
                    title={`Marker ${index + 1}`}
                    hint={
                      index === 0
                        ? 'Marks the whole sheet against the rubric. Its own fallback chain does not apply here.'
                        : 'Marks the same transcript with the same prompt. A different vendor catches the most.'
                    }
                    description={description}
                    target={target}
                    onChange={(next) => updateMarker(index, next)}
                    error={errors[`marker${index}`] || ''}
                    testing={testing === `marker${index}`}
                    testResult={testResults[`marker${index}`]}
                    onTest={() => runTest(`marker${index}`, target)}
                  />
                ))}
                <TargetPicker
                  id="marking-adjudicator"
                  title="Adjudicator"
                  hint="Asked only about the criteria the markers split on, with both rationales and the transcript around the moments they cited. Merges the coaching feedback."
                  description={description}
                  target={adjudicator}
                  onChange={setAdjudicator}
                  noneOption={{ value: NO_ADJUDICATOR, label: 'Choose an adjudicator…' }}
                  error={errors.adjudicator || ''}
                  testing={testing === 'adjudicator'}
                  testResult={testResults.adjudicator}
                  onTest={() => runTest('adjudicator', adjudicator)}
                />

                <div className="flex flex-col gap-2 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
                  <div className="flex items-center gap-2">
                    <Scale className="h-4 w-4 text-slate-600" />
                    <div>
                      <div className="text-sm font-semibold text-slate-800">Tie-break</div>
                      <div className="text-[11px] text-slate-500">
                        What settles a disputed criterion when the adjudicator cannot answer. Every criterion decided
                        this way is labelled as such on the sheet.
                      </div>
                    </div>
                  </div>
                  <select
                    id="marking-tie-break"
                    aria-label="Tie-break policy"
                    value={tieBreak}
                    onChange={(event) => setTieBreak(event.target.value)}
                    className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm"
                  >
                    {(description.marking?.tieBreaks || Object.values(TieBreak)).map((policy) => (
                      <option key={policy} value={policy}>
                        {TIE_BREAK_LABELS[policy] || policy}
                      </option>
                    ))}
                  </select>
                </div>

                {errors.markers ? (
                  <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                    {errors.markers}
                  </div>
                ) : null}
              </>
            ) : null}

            {warnings.map((warning) => (
              <div
                key={warning}
                className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
              >
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{warning}</span>
              </div>
            ))}

            {effectiveMarkingDiffers(description) || (effective?.warnings || []).length ? (
              <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-600">
                <div>
                  Saved: <span className="font-mono">{description.marking?.mode}</span>
                  {description.marking?.mode === MarkingMode.PANEL ? (
                    <>
                      {' '}
                      (<span className="font-mono">{describePanel(description.marking?.selected)}</span>)
                    </>
                  ) : null}
                  . What would actually run right now: <span className="font-mono">{effective?.mode}</span>.
                </div>
                {(effective?.warnings || []).map((warning) => (
                  <div key={warning} className="mt-1">
                    — {warning}
                  </div>
                ))}
              </div>
            ) : null}

            <div className="flex items-center justify-between gap-3">
              <div className="text-[11px] text-slate-500">
                {savedAt
                  ? 'Saved. Applies to every run started from now on.'
                  : isPanel
                    ? `A panel needs at least ${MIN_PANEL_MARKERS} markers and an adjudicator.`
                    : 'Changes apply to future runs.'}
              </div>
              <Button size="sm" onClick={save} disabled={saving || hasErrors}>
                {saving ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
                Save marking mode
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
