import React, { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  FileText,
  Layers,
  Loader2,
  RefreshCcw,
  RotateCcw,
  Sparkles,
  UploadCloud,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { LoadingRegion, RubricCriteriaSkeleton, RubricSourceSkeleton } from '@/components/skeletons.jsx';
import { PageHeader } from '@/components/PageHeader.jsx';
import { apiJson } from '@/lib/apiFetch';

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatDate(value) {
  if (!value) return '—';
  try {
    return new Date(value).toLocaleString();
  } catch (_error) {
    return String(value);
  }
}

export default function CommunicationRubricPanel({ onBack }) {
  const [rubric, setRubric] = useState(null);
  const [pdfMeta, setPdfMeta] = useState(null);
  // True from the first render: the mount effect always fetches, and a first
  // paint that said "No rubric loaded" before that fetch had even started was
  // a false empty state, not a wait.
  const [isLoading, setIsLoading] = useState(true);
  const [isUploading, setIsUploading] = useState(false);
  const [isResetting, setIsResetting] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [expandedCriteria, setExpandedCriteria] = useState(() => new Set());
  const fileInputRef = useRef(null);

  useEffect(() => {
    loadRubric();
  }, []);

  async function loadRubric() {
    setIsLoading(true);
    setError('');
    try {
      const body = await apiJson('/api/communication-rubric', {
        fallbackMessage: 'Failed to load rubric.',
      });
      setRubric(body.rubric || null);
      setPdfMeta(body.pdf || null);
    } catch (loadError) {
      setError(loadError.message || 'Failed to load rubric.');
    } finally {
      setIsLoading(false);
    }
  }

  async function handleUpload(file) {
    if (!file) {
      return;
    }

    setIsUploading(true);
    setError('');
    setNotice('');

    try {
      const formData = new FormData();
      formData.append('rubric', file);

      // No Content-Type: the browser has to set the multipart boundary, which
      // is why this passes `body` rather than `json`.
      const body = await apiJson('/api/admin/communication-rubric', {
        method: 'POST',
        body: formData,
        fallbackMessage: 'Rubric upload failed.',
      });

      setRubric(body.rubric);
      setPdfMeta(body.pdf || null);
      setNotice('Rubric replaced. Future communication scoring will use the new criteria.');
    } catch (uploadError) {
      setError(uploadError.message || 'Rubric upload failed.');
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    }
  }

  async function handleReset() {
    setIsResetting(true);
    setError('');
    setNotice('');
    try {
      const body = await apiJson('/api/admin/communication-rubric/reset', {
        method: 'POST',
        fallbackMessage: 'Reset failed.',
      });
      setRubric(body.rubric);
      setPdfMeta(body.pdf || null);
      setNotice('Restored default communication rubric.');
    } catch (resetError) {
      setError(resetError.message || 'Reset failed.');
    } finally {
      setIsResetting(false);
    }
  }

  function toggleCriterion(id) {
    setExpandedCriteria((previous) => {
      const next = new Set(previous);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function expandAll() {
    if (!rubric?.criteria) {
      return;
    }
    setExpandedCriteria(new Set(rubric.criteria.map((item) => item.id)));
  }

  function collapseAll() {
    setExpandedCriteria(new Set());
  }

  const groupedSections = (rubric?.sections || []).map((section) => ({
    ...section,
    criteria: (rubric?.criteria || []).filter((criterion) => section.criteria_ids.includes(criterion.id)),
  }));

  const fallbackUngroupedCriteria = (rubric?.criteria || []).filter(
    (criterion) =>
      !groupedSections.some((section) =>
        (section.criteria_ids || []).includes(criterion.id)
      )
  );

  // Skeletons are for a first load only. A reload with a rubric on screen
  // keeps it (the button spins) rather than replacing it with bone.
  const isFirstLoad = isLoading && !rubric;

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <PageHeader
        icon={<FileText className="h-5 w-5" />}
        title="Communication Rubric"
        subtitle="The rubric every communication score is marked against"
        onBack={onBack}
        backTitle="Back to dashboard"
      >
        <Badge variant="accent">Editable</Badge>
      </PageHeader>

      <main id="main" className="mx-auto max-w-5xl space-y-6 px-6 py-8">
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4 }}
        >
          <Card className="overflow-hidden border-slate-200 bg-white shadow-sm">
            <CardHeader className="border-b border-slate-100 bg-slate-50/60">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="space-y-1">
                  <CardTitle className="flex items-center gap-2 text-lg">
                    <FileText className="h-5 w-5 text-cyan-700" aria-hidden="true" />
                    {rubric?.title || 'Communication Rubric'}
                  </CardTitle>
                  <CardDescription>
                    Manage the rubric the model uses to score student communication. The PDF is
                    parsed server-side whenever you replace it; scores are recomputed on the next run.
                  </CardDescription>
                </div>
                <div className="flex flex-col items-end gap-2 text-right">
                  {rubric ? (
                    <Badge variant="success">
                      {rubric.criteria_count || rubric.criteria?.length || 0} criteria · Max {rubric.max_score}
                    </Badge>
                  ) : null}
                  {rubric ? (
                    <span className="text-[11px] text-slate-500">
                      Pass at {rubric.pass_threshold || 11}/{rubric.max_score || 21}
                    </span>
                  ) : null}
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-4 pt-5">
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
                  <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                    Source file
                  </div>
                  {isFirstLoad ? (
                    /* "No PDF available" is an answer; before the first
                       response there is none yet. */
                    <LoadingRegion label="Loading rubric source">
                      <RubricSourceSkeleton />
                    </LoadingRegion>
                  ) : (
                    <>
                      <div className="mt-1 truncate text-sm font-medium text-slate-800" title={pdfMeta?.fileName}>
                        {pdfMeta?.fileName || 'No PDF available'}
                      </div>
                      <div className="mt-1 text-[11px] text-slate-500">
                        {pdfMeta?.sizeBytes ? formatBytes(pdfMeta.sizeBytes) : '—'} ·{' '}
                        {pdfMeta?.updatedAt ? `Updated ${formatDate(pdfMeta.updatedAt)}` : 'No timestamp'}
                      </div>
                      {pdfMeta?.isCustomUpload === false ? (
                        <div className="mt-2 text-[11px] text-slate-500">
                          Default rubric bundled with the project.
                        </div>
                      ) : null}
                    </>
                  )}
                </div>

                <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
                  <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                    Scoring scale
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-1 text-xs">
                    <div className="flex items-center justify-between rounded-md bg-white px-2 py-1 text-slate-700">
                      <span>All</span>
                      <span className="font-semibold text-emerald-700">3 pts</span>
                    </div>
                    <div className="flex items-center justify-between rounded-md bg-white px-2 py-1 text-slate-700">
                      <span>Most</span>
                      <span className="font-semibold text-cyan-700">2 pts</span>
                    </div>
                    <div className="flex items-center justify-between rounded-md bg-white px-2 py-1 text-slate-700">
                      <span>Some</span>
                      <span className="font-semibold text-amber-700">1 pt</span>
                    </div>
                    <div className="flex items-center justify-between rounded-md bg-white px-2 py-1 text-slate-700">
                      <span>None</span>
                      <span className="font-semibold text-rose-700">0 pts</span>
                    </div>
                  </div>
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button
                  className="gap-2"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={isUploading}
                >
                  {isUploading ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <UploadCloud className="h-4 w-4" />
                  )}
                  {isUploading ? 'Parsing PDF…' : 'Upload replacement PDF'}
                </Button>
                <Button variant="outline" className="gap-2" onClick={handleReset} disabled={isResetting}>
                  {isResetting ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <RotateCcw className="h-4 w-4" />
                  )}
                  Reset to bundled default
                </Button>
                <Button variant="ghost" className="gap-2" onClick={loadRubric} disabled={isLoading}>
                  {isLoading ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <RefreshCcw className="h-4 w-4" />
                  )}
                  Refresh
                </Button>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="application/pdf"
                  className="hidden"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) {
                      handleUpload(file);
                    }
                  }}
                />
              </div>

              <AnimatePresence>
                {error ? (
                  <motion.div
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -4 }}
                    className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700"
                  >
                    {error}
                  </motion.div>
                ) : null}
                {notice ? (
                  <motion.div
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -4 }}
                    className="flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800"
                  >
                    <CheckCircle2 className="h-4 w-4" />
                    {notice}
                  </motion.div>
                ) : null}
              </AnimatePresence>
            </CardContent>
          </Card>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, delay: 0.05 }}
        >
          <Card className="border-slate-200 bg-white shadow-sm">
            <CardHeader className="flex flex-row items-start justify-between gap-3 border-b border-slate-100">
              <div>
                <CardTitle className="flex items-center gap-2 text-base">
                  <Layers className="h-4 w-4 text-cyan-600" />
                  Criteria and performance indicators
                </CardTitle>
                <CardDescription>
                  The model scores each criterion as one of None, Some, Most, or All using the
                  indicators below as the evaluation rubric.
                </CardDescription>
              </div>
              <div className="flex shrink-0 gap-2">
                <Button size="sm" variant="ghost" onClick={expandAll} disabled={!rubric?.criteria?.length}>
                  Expand all
                </Button>
                <Button size="sm" variant="ghost" onClick={collapseAll} disabled={!expandedCriteria.size}>
                  Collapse all
                </Button>
              </div>
            </CardHeader>
            <CardContent className="space-y-5 pt-5">
              {isFirstLoad ? (
                /* The same rows AppShell's RubricSkeleton drew while this chunk
                   loaded, so the hand-off from chunk to page is invisible. */
                <LoadingRegion label="Loading rubric criteria">
                  <RubricCriteriaSkeleton />
                </LoadingRegion>
              ) : !rubric?.criteria?.length ? (
                <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50 p-6 text-sm text-slate-500">
                  No rubric loaded. Upload a PDF above to populate the criteria.
                </div>
              ) : (
                <div className="space-y-5">
                  {groupedSections.length === 0 && fallbackUngroupedCriteria.length > 0 ? (
                    <CriteriaGroup
                      title="Criteria"
                      criteria={fallbackUngroupedCriteria}
                      expanded={expandedCriteria}
                      onToggle={toggleCriterion}
                    />
                  ) : null}

                  {groupedSections.map((section) => (
                    <CriteriaGroup
                      key={section.name}
                      title={section.name}
                      criteria={section.criteria}
                      expanded={expandedCriteria}
                      onToggle={toggleCriterion}
                    />
                  ))}

                  {fallbackUngroupedCriteria.length > 0 && groupedSections.length > 0 ? (
                    <CriteriaGroup
                      title="Other criteria"
                      criteria={fallbackUngroupedCriteria}
                      expanded={expandedCriteria}
                      onToggle={toggleCriterion}
                    />
                  ) : null}
                </div>
              )}
            </CardContent>
          </Card>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, delay: 0.1 }}
          className="rounded-xl border border-slate-200 bg-white/70 p-4 text-xs text-slate-600"
        >
          <div className="flex items-start gap-2">
            <Sparkles className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-cyan-600" aria-hidden="true" />
            <p>
              The parser only re-runs when you upload a new rubric PDF or click reset, so day-to-day
              scoring stays fast. Indicators that cannot be observed from audio/video alone (e.g. eye
              contact, posture) are flagged automatically by the model and are not used to penalise
              the student.
            </p>
          </div>
        </motion.div>
      </main>
    </div>
  );
}

function CriteriaGroup({ title, criteria, expanded, onToggle }) {
  if (!criteria?.length) {
    return null;
  }

  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-slate-500">
        <Layers className="h-3.5 w-3.5" />
        {title}
      </div>
      <ul className="space-y-2">
        {criteria.map((criterion) => {
          const isOpen = expanded.has(criterion.id);
          return (
            <li
              key={criterion.id}
              className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm transition hover:border-cyan-200 hover:shadow"
            >
              <button
                type="button"
                onClick={() => onToggle(criterion.id)}
                aria-expanded={isOpen}
                className="flex w-full items-start gap-3 px-4 py-3 text-left transition hover:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-cyan-500"
              >
                <span className="mt-0.5 inline-flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-cyan-600 to-blue-700 text-xs font-bold text-white shadow-sm">
                  {criterion.id}
                </span>
                <span className="flex-1 text-sm font-medium text-slate-800">
                  {criterion.label}
                </span>
                <span className="mt-1 text-slate-500" aria-hidden="true">
                  {isOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                </span>
              </button>
              <AnimatePresence initial={false}>
                {isOpen ? (
                  <motion.div
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.2 }}
                    className="border-t border-slate-100 bg-slate-50/60 px-4 py-3"
                  >
                    <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                      Performance indicators ({criterion.indicators?.length || 0})
                    </div>
                    {criterion.indicators?.length ? (
                      <ul className="mt-2 space-y-1.5 text-sm text-slate-700">
                        {criterion.indicators.map((indicator, indicatorIndex) => (
                          <li key={`${criterion.id}-${indicatorIndex}`} className="flex gap-2">
                            <span className="mt-1.5 inline-block h-1.5 w-1.5 flex-shrink-0 rounded-full bg-cyan-500" aria-hidden="true" />
                            <span>{indicator}</span>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <div className="mt-2 text-sm italic text-slate-500">
                        No performance indicators detected for this criterion.
                      </div>
                    )}
                  </motion.div>
                ) : null}
              </AnimatePresence>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
