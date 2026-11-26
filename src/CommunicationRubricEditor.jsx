import React, { useMemo, useRef, useState } from 'react';
import { AnimatePresence } from 'framer-motion';
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Loader2,
  Plus,
  Save,
  Trash2,
  X,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Modal } from '@/components/ui/dialog.jsx';
import { apiJson } from '@/lib/apiFetch';
import { describeRubricExtraction } from '@/lib/rubricExtraction';
import {
  MAX_CRITERIA,
  MAX_INDICATORS_PER_CRITERION,
  SCORE_LABELS,
  addCriterion,
  addIndicator,
  deriveRubricTotals,
  fromEditableRubric,
  isRubricDirty,
  moveCriterion,
  removeCriterion,
  removeIndicator,
  sectionSuggestions,
  setPassThreshold,
  setRubricField,
  setScalePoints,
  toEditableRubric,
  updateCriterion,
  updateIndicator,
  validateEditableRubric,
} from '@/lib/rubricEditor';
import { RUBRIC_ENDPOINT, draftEndpoint, extractRubricIssues, formatWhen } from '@/lib/rubricApi';

const FIELD =
  'w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm placeholder:text-slate-500 focus:border-cyan-500 focus:outline-none focus:ring-2 focus:ring-cyan-500/30 disabled:opacity-60';
const FIELD_ERROR = 'border-rose-400 focus:border-rose-500 focus:ring-rose-500/30';
const LABEL = 'text-xs font-semibold text-slate-600';

function FieldError({ id, message }) {
  if (!message) return null;
  return (
    <p id={id} className="text-[11px] text-rose-700">
      {message}
    </p>
  );
}

/**
 * Review and edit a communication rubric: either a draft an AI model just
 * extracted (`draft` set) or the active rubric edited in place (`draft` null).
 * All editing rules live in lib/rubricEditor.js; this holds state and calls them.
 */
export default function CommunicationRubricEditor({ rubric, draft = null, onSaved, onClose }) {
  const original = useMemo(() => toEditableRubric(rubric), [rubric]);
  const [editable, setEditable] = useState(original);
  const [isSaving, setIsSaving] = useState(false);
  const [isDiscarding, setIsDiscarding] = useState(false);
  const [serverError, setServerError] = useState({ message: '', issues: [] });
  const [confirmOpen, setConfirmOpen] = useState(false);
  const keepEditingRef = useRef(null);

  const validation = useMemo(() => validateEditableRubric(editable), [editable]);
  const totals = useMemo(() => deriveRubricTotals(editable), [editable]);
  const suggestions = useMemo(() => sectionSuggestions(editable), [editable]);
  const dirty = useMemo(() => isRubricDirty(original, editable), [original, editable]);
  const extractionNotice = draft ? describeRubricExtraction(rubric?.extraction) : null;
  const busy = isSaving || isDiscarding;
  const errors = validation.fieldErrors;
  const datalistId = 'rubric-section-suggestions';

  const change = (fn) => {
    setServerError({ message: '', issues: [] });
    setEditable((current) => fn(current));
  };

  async function handleSave() {
    if (!validation.valid || busy) return;
    setIsSaving(true);
    setServerError({ message: '', issues: [] });
    try {
      const body = await apiJson(RUBRIC_ENDPOINT, {
        method: 'PUT',
        json: { rubric: fromEditableRubric(editable), draftId: draft?.draftId ?? null },
        fallbackMessage: 'Could not save the rubric.',
      });
      onSaved?.(body);
    } catch (error) {
      const { message, issues } = extractRubricIssues(error);
      setServerError({ message: message || 'Could not save the rubric.', issues });
    } finally {
      setIsSaving(false);
    }
  }

  async function discard() {
    setConfirmOpen(false);
    if (draft?.draftId) {
      setIsDiscarding(true);
      try {
        await apiJson(draftEndpoint(draft.draftId), {
          method: 'DELETE',
          fallbackMessage: 'Could not discard the draft.',
        });
      } catch (_error) {
        // A draft that is already gone (404) or expired is as discarded as it
        // gets; nothing the admin can do about any other failure here.
      } finally {
        setIsDiscarding(false);
      }
    }
    onClose?.();
  }

  function requestClose() {
    if (busy) return;
    if (dirty) {
      setConfirmOpen(true);
    } else {
      discard();
    }
  }

  const criteria = editable.criteria;

  return (
    <div className="space-y-6">
      <Card className="border-slate-200 bg-white shadow-sm">
        <CardHeader className="border-b border-slate-100 bg-slate-50/60">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-1">
              <CardTitle className="text-lg">
                {draft ? 'Review AI-extracted rubric' : 'Edit communication rubric'}
              </CardTitle>
              <CardDescription>
                {draft
                  ? `Draft from ${draft.originalName || 'the uploaded PDF'}${
                      draft.createdAt ? `, created ${formatWhen(draft.createdAt)}` : ''
                    }. Nothing is used for scoring until you save.`
                  : 'Changes apply to new communication scoring runs once saved.'}
              </CardDescription>
            </div>
            <div className="flex flex-wrap gap-2" aria-label="Derived totals">
              <Badge variant="info">{totals.criteriaCount} criteria</Badge>
              <Badge variant="info">Max {totals.maxScore}</Badge>
              <Badge variant="info">
                Pass at {totals.passThreshold ?? '—'}
                {totals.isAutomatic ? ' (auto)' : ''}
              </Badge>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4 pt-5">
          {extractionNotice ? (
            <div
              role="note"
              className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-700"
            >
              <div className="flex items-center gap-2 font-semibold">
                <AlertTriangle className="h-4 w-4" aria-hidden="true" />
                {extractionNotice.title}
              </div>
              <p className="mt-1">{extractionNotice.detail}</p>
              {extractionNotice.warnings.length ? (
                <ul className="mt-2 list-disc space-y-0.5 pl-5">
                  {extractionNotice.warnings.map((warning, index) => (
                    <li key={`${index}-${warning}`}>{warning}</li>
                  ))}
                </ul>
              ) : null}
              {extractionNotice.reason ? (
                <details className="mt-2">
                  <summary className="cursor-pointer text-xs font-medium">
                    Why the standard parser failed
                  </summary>
                  <p className="mt-1 text-xs">{extractionNotice.reason}</p>
                </details>
              ) : null}
            </div>
          ) : null}

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <label htmlFor="rubric-title" className={LABEL}>
                Title
              </label>
              <input
                id="rubric-title"
                className={`${FIELD} ${errors.title ? FIELD_ERROR : ''}`}
                value={editable.title}
                aria-invalid={Boolean(errors.title)}
                aria-describedby={errors.title ? 'rubric-title-error' : undefined}
                onChange={(event) => change((c) => setRubricField(c, 'title', event.target.value))}
                disabled={busy}
              />
              <FieldError id="rubric-title-error" message={errors.title} />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="rubric-notes" className={LABEL}>
                Notes
              </label>
              <textarea
                id="rubric-notes"
                rows={2}
                className={`${FIELD} ${errors.notes ? FIELD_ERROR : ''}`}
                value={editable.notes}
                aria-invalid={Boolean(errors.notes)}
                aria-describedby={errors.notes ? 'rubric-notes-error' : undefined}
                onChange={(event) => change((c) => setRubricField(c, 'notes', event.target.value))}
                disabled={busy}
              />
              <FieldError id="rubric-notes-error" message={errors.notes} />
            </div>
          </div>

          <fieldset className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <legend className="px-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
              Scoring scale (points per criterion)
            </legend>
            <div className="mt-1 grid grid-cols-2 gap-3 sm:grid-cols-4">
              {SCORE_LABELS.map((label) => (
                <div key={label} className="flex flex-col gap-1.5">
                  <label htmlFor={`rubric-scale-${label}`} className={LABEL}>
                    {label}
                  </label>
                  <input
                    id={`rubric-scale-${label}`}
                    type="number"
                    inputMode="numeric"
                    min={0}
                    step={1}
                    className={`${FIELD} ${errors.scale ? FIELD_ERROR : ''}`}
                    value={editable.scoringScale[label] ?? ''}
                    aria-invalid={Boolean(errors.scale)}
                    aria-describedby={errors.scale ? 'rubric-scale-error' : undefined}
                    onChange={(event) =>
                      change((c) => setScalePoints(c, label, event.target.value))
                    }
                    disabled={busy}
                  />
                </div>
              ))}
            </div>
            <div className="mt-2">
              <FieldError id="rubric-scale-error" message={errors.scale} />
            </div>
          </fieldset>

          <fieldset className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <legend className="px-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
              Pass mark
            </legend>
            <div className="flex flex-wrap items-end gap-3">
              {totals.isAutomatic ? (
                <>
                  <p className="text-sm text-slate-700">
                    Automatic: <span className="font-semibold">{totals.passThreshold}</span> of{' '}
                    {totals.maxScore}
                  </p>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() =>
                      change((c) => setPassThreshold(c, String(totals.defaultPassThreshold)))
                    }
                    disabled={busy}
                  >
                    Set explicitly
                  </Button>
                </>
              ) : (
                <>
                  <div className="flex flex-col gap-1.5">
                    <label htmlFor="rubric-pass" className={LABEL}>
                      Pass mark (out of {totals.maxScore})
                    </label>
                    <input
                      id="rubric-pass"
                      type="number"
                      inputMode="numeric"
                      min={0}
                      step={1}
                      className={`${FIELD} w-32 ${errors.passThreshold ? FIELD_ERROR : ''}`}
                      value={editable.passThreshold ?? ''}
                      aria-invalid={Boolean(errors.passThreshold)}
                      aria-describedby={errors.passThreshold ? 'rubric-pass-error' : undefined}
                      onChange={(event) =>
                        change((c) => setPassThreshold(c, event.target.value))
                      }
                      disabled={busy}
                    />
                  </div>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => change((c) => setPassThreshold(c, null))}
                    disabled={busy}
                  >
                    Use automatic ({totals.defaultPassThreshold})
                  </Button>
                </>
              )}
            </div>
            <div className="mt-2">
              <FieldError id="rubric-pass-error" message={errors.passThreshold} />
            </div>
          </fieldset>
        </CardContent>
      </Card>

      <Card className="border-slate-200 bg-white shadow-sm">
        <CardHeader className="flex flex-row items-start justify-between gap-3 border-b border-slate-100">
          <div>
            <CardTitle className="text-base">Criteria and indicators</CardTitle>
            <CardDescription>
              Each criterion is scored None, Some, Most or All against its indicators.
            </CardDescription>
          </div>
          <Button
            size="sm"
            variant="outline"
            className="shrink-0 gap-1.5"
            onClick={() => change((c) => addCriterion(c))}
            disabled={busy || criteria.length >= MAX_CRITERIA}
          >
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            Add criterion
          </Button>
        </CardHeader>
        <CardContent className="space-y-4 pt-5">
          <datalist id={datalistId}>
            {suggestions.map((section) => (
              <option key={section} value={section} />
            ))}
          </datalist>
          {criteria.length === 0 ? (
            <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50 p-6 text-sm text-slate-500">
              No criteria yet. Add at least two.
            </div>
          ) : null}
          {criteria.map((criterion, index) => {
            const n = index + 1;
            const cErrors = errors.criteria[criterion.key] || {};
            const labelId = `criterion-${criterion.key}-label`;
            return (
              <section
                key={criterion.key}
                aria-label={`Criterion ${n}`}
                className="space-y-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="inline-flex h-7 w-7 items-center justify-center rounded-lg bg-slate-800 text-xs font-bold text-slate-50">
                    {n}
                  </span>
                  <div className="flex gap-1">
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      aria-label={`Move criterion ${n} up`}
                      onClick={() => change((c) => moveCriterion(c, criterion.key, -1))}
                      disabled={busy || index === 0}
                    >
                      <ArrowUp className="h-4 w-4" aria-hidden="true" />
                    </Button>
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      aria-label={`Move criterion ${n} down`}
                      onClick={() => change((c) => moveCriterion(c, criterion.key, 1))}
                      disabled={busy || index === criteria.length - 1}
                    >
                      <ArrowDown className="h-4 w-4" aria-hidden="true" />
                    </Button>
                    <Button
                      size="icon-sm"
                      variant="destructive"
                      aria-label={`Remove criterion ${n}`}
                      onClick={() => change((c) => removeCriterion(c, criterion.key))}
                      disabled={busy}
                    >
                      <Trash2 className="h-4 w-4" aria-hidden="true" />
                    </Button>
                  </div>
                </div>

                <div className="grid gap-3 sm:grid-cols-3">
                  <div className="flex flex-col gap-1.5">
                    <label htmlFor={`criterion-${criterion.key}-section`} className={LABEL}>
                      Section {n}
                    </label>
                    <input
                      id={`criterion-${criterion.key}-section`}
                      list={datalistId}
                      className={`${FIELD} ${cErrors.section ? FIELD_ERROR : ''}`}
                      value={criterion.section}
                      aria-invalid={Boolean(cErrors.section)}
                      onChange={(event) =>
                        change((c) => updateCriterion(c, criterion.key, { section: event.target.value }))
                      }
                      disabled={busy}
                    />
                    <FieldError message={cErrors.section} />
                  </div>
                  <div className="flex flex-col gap-1.5 sm:col-span-2">
                    <label htmlFor={labelId} className={LABEL}>
                      Criterion {n} label
                    </label>
                    <input
                      id={labelId}
                      className={`${FIELD} ${cErrors.label ? FIELD_ERROR : ''}`}
                      value={criterion.label}
                      aria-invalid={Boolean(cErrors.label)}
                      onChange={(event) =>
                        change((c) => updateCriterion(c, criterion.key, { label: event.target.value }))
                      }
                      disabled={busy}
                    />
                    <FieldError message={cErrors.label} />
                  </div>
                </div>

                <div className="space-y-2">
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                    Performance indicators ({criterion.indicators.length})
                  </div>
                  {criterion.indicators.map((indicator, indicatorIndex) => (
                    <div key={indicator.key} className="space-y-1">
                      <div className="flex items-start gap-2">
                        <input
                          aria-label={`Criterion ${n} indicator ${indicatorIndex + 1}`}
                          className={`${FIELD} ${cErrors.indicators?.[indicator.key] ? FIELD_ERROR : ''}`}
                          value={indicator.text}
                          aria-invalid={Boolean(cErrors.indicators?.[indicator.key])}
                          onChange={(event) =>
                            change((c) =>
                              updateIndicator(c, criterion.key, indicator.key, event.target.value),
                            )
                          }
                          disabled={busy}
                        />
                        <Button
                          size="icon-sm"
                          variant="ghost"
                          className="mt-1 shrink-0"
                          aria-label={`Remove criterion ${n} indicator ${indicatorIndex + 1}`}
                          onClick={() => change((c) => removeIndicator(c, criterion.key, indicator.key))}
                          disabled={busy}
                        >
                          <X className="h-4 w-4" aria-hidden="true" />
                        </Button>
                      </div>
                      <FieldError message={cErrors.indicators?.[indicator.key]} />
                    </div>
                  ))}
                  <Button
                    size="sm"
                    variant="ghost"
                    className="gap-1.5"
                    onClick={() => change((c) => addIndicator(c, criterion.key))}
                    disabled={busy || criterion.indicators.length >= MAX_INDICATORS_PER_CRITERION}
                  >
                    <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                    Add indicator to criterion {n}
                  </Button>
                </div>
              </section>
            );
          })}
        </CardContent>
      </Card>

      {!validation.valid ? (
        <div
          className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-700"
          aria-label="Problems to fix before saving"
        >
          <div className="font-semibold">Fix before saving</div>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {validation.issues.map((issue, index) => (
              <li key={`${index}-${issue}`}>{issue}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {serverError.message ? (
        <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
          <div className="font-semibold">{serverError.message}</div>
          {serverError.issues.length ? (
            <ul className="mt-1 list-disc space-y-0.5 pl-5">
              {serverError.issues.map((issue, index) => (
                <li key={`${index}-${issue}`}>{issue}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button variant="outline" onClick={requestClose} disabled={busy}>
          {isDiscarding ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" /> : null}
          {draft ? 'Discard draft' : 'Cancel'}
        </Button>
        <Button className="gap-2" onClick={handleSave} disabled={!validation.valid || busy}>
          {isSaving ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          ) : (
            <Save className="h-4 w-4" aria-hidden="true" />
          )}
          {isSaving ? 'Saving…' : 'Save rubric'}
        </Button>
      </div>

      <AnimatePresence>
        {confirmOpen ? (
          <Modal
            onClose={() => setConfirmOpen(false)}
            labelledBy="rubric-discard-title"
            describedBy="rubric-discard-body"
            initialFocus={keepEditingRef}
            size="sm"
          >
            <div className="space-y-3 rounded-xl border border-slate-200 bg-white p-5 shadow-xl">
              <h2 id="rubric-discard-title" className="text-base font-semibold text-slate-900">
                Discard your changes?
              </h2>
              <p id="rubric-discard-body" className="text-sm text-slate-600">
                {draft
                  ? 'The AI draft and your edits will be thrown away. The current rubric stays in use.'
                  : 'Your edits will be lost. The current rubric stays in use.'}
              </p>
              <div className="flex justify-end gap-2">
                <Button ref={keepEditingRef} variant="outline" onClick={() => setConfirmOpen(false)}>
                  Keep editing
                </Button>
                <Button variant="destructive" onClick={discard}>
                  Discard
                </Button>
              </div>
            </div>
          </Modal>
        ) : null}
      </AnimatePresence>
    </div>
  );
}
