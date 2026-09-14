import React, { useEffect, useMemo, useRef, useState } from 'react';
import { motion } from 'framer-motion';
import {
  ArrowLeft,
  BarChart3,
  CalendarDays,
  ChevronDown,
  GraduationCap,
  Layers,
  ListChecks,
  Loader2,
  RefreshCcw,
  Table2,
  Users,
  X,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { apiJson } from '@/lib/apiFetch';
import {
  DATE_PRESETS,
  applyFilter,
  buildFilterCatalog,
  describeSessionSelection,
  emptyFilter,
  patchFilter,
  reconcileFilter,
  rootSessionOf,
  studentOptions,
  toggleSession,
} from '@/lib/analyticsFilters';

// Series colors — validated with the dataviz palette checker (CVD ΔE and
// contrast on white). A/B compare the two filter sets in the histograms;
// content/communication identify the result types in the per-student chart.
const SERIES_A = '#2a78d6'; // blue — primary filter set
const SERIES_B = '#eb6834'; // orange — comparison filter set
const SERIES_CONTENT = '#4a3aa7'; // violet
const SERIES_COMMUNICATION = '#1baf7a'; // aqua (sub-3:1 on white — relieved by value labels + table view)

const BUCKET_COUNT = 10;

function scorePercent(row) {
  const total = Number(row.scoreTotal);
  const max = Number(row.scoreMax);
  if (!Number.isFinite(total) || !Number.isFinite(max) || max <= 0) return null;
  return Math.min(100, Math.max(0, (total / max) * 100));
}

function mean(values) {
  return values.length ? values.reduce((sum, v) => sum + v, 0) / values.length : null;
}

function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function summarize(rows, resultType) {
  const typed = rows.filter((row) => row.resultType === resultType);
  const percents = typed.map(scorePercent).filter((v) => v !== null);
  const decided = typed.filter((row) => /^(pass|fail)$/i.test(String(row.passFail || '')));
  const passed = decided.filter((row) => /^pass$/i.test(String(row.passFail)));
  return {
    count: typed.length,
    mean: mean(percents),
    median: median(percents),
    passRate: decided.length ? (passed.length / decided.length) * 100 : null,
    failRate: decided.length ? ((decided.length - passed.length) / decided.length) * 100 : null,
  };
}

function histogram(rows, resultType) {
  const buckets = Array.from({ length: BUCKET_COUNT }, () => 0);
  rows
    .filter((row) => row.resultType === resultType)
    .forEach((row) => {
      const pct = scorePercent(row);
      if (pct === null) return;
      buckets[Math.min(BUCKET_COUNT - 1, Math.floor(pct / 10))] += 1;
    });
  return buckets;
}

function formatPct(value, digits = 1) {
  return value === null || value === undefined ? '—' : `${value.toFixed(digits)}%`;
}

function formatDate(value) {
  if (!value) return '—';
  try {
    return new Date(value).toLocaleDateString();
  } catch (_error) {
    return String(value);
  }
}

/* ---------- generic bits ---------- */

function LegendKey({ color, label }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-600">
      <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: color }} aria-hidden />
      {label}
    </span>
  );
}

function SkeletonBlock({ className = '' }) {
  return <div className={`animate-pulse rounded-xl bg-slate-200/80 ${className}`} aria-hidden />;
}

function ChartTooltip({ tooltip }) {
  if (!tooltip) return null;
  return (
    <div
      className="pointer-events-none absolute z-20 -translate-x-1/2 -translate-y-full rounded-lg border border-slate-200 bg-white px-3 py-2 shadow-lg"
      style={{ left: tooltip.x, top: tooltip.y - 8 }}
      role="status"
    >
      <div className="text-[11px] font-medium text-slate-500">{tooltip.title}</div>
      {tooltip.lines.map((line) => (
        <div key={line.label} className="flex items-center gap-2 text-xs">
          <span className="inline-block h-0.5 w-3 rounded" style={{ background: line.color }} aria-hidden />
          <span className="font-semibold text-slate-900">{line.value}</span>
          <span className="text-slate-500">{line.label}</span>
        </div>
      ))}
    </div>
  );
}

/* ---------- filter controls ---------- */

/** One recording per line — a long session once, not once per clip. */
function SessionMultiSelect({ catalog, filter, onChange }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const { sessions } = catalog;
  const selectedIds = filter.sessionIds;

  useEffect(() => {
    if (!open) return undefined;
    function onDocDown(event) {
      if (rootRef.current && !rootRef.current.contains(event.target)) setOpen(false);
    }
    document.addEventListener('mousedown', onDocDown);
    return () => document.removeEventListener('mousedown', onDocDown);
  }, [open]);

  const summary = describeSessionSelection(filter, catalog);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex h-9 w-44 items-center justify-between rounded-md border border-slate-200 bg-white px-3 text-sm text-slate-700 hover:bg-slate-50"
        aria-haspopup="listbox"
        aria-expanded={open}
        title={summary}
      >
        <span className="truncate">{summary}</span>
        <ChevronDown className="h-4 w-4 shrink-0 text-slate-400" />
      </button>
      {open ? (
        <div className="absolute z-30 mt-1 max-h-64 w-72 overflow-auto rounded-lg border border-slate-200 bg-white p-1 shadow-xl" role="listbox">
          <button
            type="button"
            className="w-full rounded-md px-2 py-1.5 text-left text-xs font-medium text-blue-700 hover:bg-slate-50"
            onClick={() => onChange(patchFilter(filter, { sessionIds: [] }, catalog))}
          >
            Clear selection (all sessions)
          </button>
          {sessions.map((session) => (
            <label key={session.id} className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-slate-50">
              <input
                type="checkbox"
                className="h-3.5 w-3.5 accent-blue-600"
                checked={selectedIds.includes(session.id)}
                onChange={() => onChange(toggleSession(filter, session.id, catalog))}
              />
              <span className="truncate text-slate-700">{session.name}</span>
              <span className="ml-auto shrink-0 text-[10px] text-slate-400">
                {session.studentIds.length} student{session.studentIds.length === 1 ? '' : 's'} · {formatDate(session.createdAt)}
              </span>
            </label>
          ))}
          {sessions.length === 0 ? <div className="px-2 py-2 text-xs text-slate-400">No sessions yet.</div> : null}
        </div>
      ) : null}
    </div>
  );
}

function FilterRow({ label, color, filter, onChange, catalog, onRemove }) {
  // Every write goes through the cascade: a session choice that no longer
  // offers the selected student clears it in the same change.
  function patch(partial) {
    onChange(patchFilter(filter, partial, catalog));
  }
  const students = useMemo(() => studentOptions(catalog, filter.sessionIds), [catalog, filter.sessionIds]);
  const scoped = filter.sessionIds.length > 0;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="inline-flex w-24 items-center gap-1.5 text-xs font-semibold text-slate-600">
        <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: color }} aria-hidden />
        {label}
      </span>
      <select
        className="h-9 rounded-md border border-slate-200 bg-white px-2 text-sm text-slate-700"
        value={filter.preset}
        onChange={(e) => patch({ preset: e.target.value })}
        aria-label={`${label} date range`}
      >
        {DATE_PRESETS.map((preset) => (
          <option key={preset.value} value={preset.value}>{preset.label}</option>
        ))}
      </select>
      {filter.preset === 'custom' ? (
        <>
          <input
            type="date"
            className="h-9 rounded-md border border-slate-200 bg-white px-2 text-sm text-slate-700"
            value={filter.dateFrom}
            onChange={(e) => patch({ dateFrom: e.target.value })}
            aria-label={`${label} from date`}
          />
          <span className="text-xs text-slate-400">to</span>
          <input
            type="date"
            className="h-9 rounded-md border border-slate-200 bg-white px-2 text-sm text-slate-700"
            value={filter.dateTo}
            onChange={(e) => patch({ dateTo: e.target.value })}
            aria-label={`${label} to date`}
          />
        </>
      ) : null}
      <SessionMultiSelect catalog={catalog} filter={filter} onChange={onChange} />
      <select
        className="h-9 max-w-52 rounded-md border border-slate-200 bg-white px-2 text-sm text-slate-700"
        value={filter.studentId}
        onChange={(e) => patch({ studentId: e.target.value })}
        aria-label={`${label} student`}
        title={scoped ? 'Students scored in the selected session(s)' : 'Students across every session'}
      >
        <option value="">{scoped ? `All students in selection (${students.length})` : 'All students'}</option>
        {students.map((student) => (
          <option key={student.id} value={student.id}>{student.name}</option>
        ))}
      </select>
      {onRemove ? (
        <Button variant="ghost" size="sm" className="gap-1 text-slate-500 hover:text-rose-600" onClick={onRemove}>
          <X className="h-3.5 w-3.5" />
          Remove
        </Button>
      ) : null}
    </div>
  );
}

/* ---------- charts ---------- */

function StatTile({ icon: Icon, label, primary, secondary, hint, compare }) {
  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardContent className="flex flex-col gap-2 p-5 pt-6">
        <div className="flex items-center gap-2 text-xs font-medium text-slate-500">
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-blue-50 text-blue-600">
            <Icon className="h-3.5 w-3.5" aria-hidden />
          </span>
          {label}
        </div>
        <div className="flex items-baseline gap-2">
          {compare ? <span className="h-2 w-2 rounded-sm" style={{ background: SERIES_A }} aria-hidden /> : null}
          <span className="text-2xl font-semibold text-slate-900">{primary}</span>
        </div>
        {compare ? (
          <div className="flex items-baseline gap-2">
            <span className="h-2 w-2 rounded-sm" style={{ background: SERIES_B }} aria-hidden />
            <span className="text-sm font-semibold text-slate-700">{secondary}</span>
          </div>
        ) : null}
        {hint ? <div className="text-[11px] text-slate-400">{hint}</div> : null}
      </CardContent>
    </Card>
  );
}

/** Column histogram: score-% buckets × assessment count, 1–2 series. */
function DistributionChart({ title, description, seriesA, seriesB, compare }) {
  const [tooltip, setTooltip] = useState(null);
  const containerRef = useRef(null);
  const maxCount = Math.max(1, ...seriesA, ...(compare && seriesB ? seriesB : []));
  const chartHeight = 176;
  const totalA = seriesA.reduce((sum, v) => sum + v, 0);
  const totalB = compare && seriesB ? seriesB.reduce((sum, v) => sum + v, 0) : 0;
  const isEmpty = totalA === 0 && totalB === 0;

  function showTooltip(event, bucketIndex) {
    const bounds = containerRef.current?.getBoundingClientRect();
    if (!bounds) return;
    const target = event.currentTarget.getBoundingClientRect();
    const lines = [{ label: compare ? 'Primary' : 'Assessments', value: String(seriesA[bucketIndex]), color: SERIES_A }];
    if (compare && seriesB) lines.push({ label: 'Comparison', value: String(seriesB[bucketIndex]), color: SERIES_B });
    setTooltip({
      x: target.left - bounds.left + target.width / 2,
      y: target.top - bounds.top,
      title: `${bucketIndex * 10}–${bucketIndex * 10 + 10}% score`,
      lines,
    });
  }

  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle className="text-base">{title}</CardTitle>
            <CardDescription>{description}</CardDescription>
          </div>
          {compare ? (
            <div className="flex items-center gap-3">
              <LegendKey color={SERIES_A} label="Primary" />
              <LegendKey color={SERIES_B} label="Comparison" />
            </div>
          ) : null}
        </div>
      </CardHeader>
      <CardContent>
        {isEmpty ? (
          <div className="flex h-52 items-center justify-center text-sm text-slate-400">
            No completed assessments match this filter.
          </div>
        ) : (
          <div ref={containerRef} className="relative">
            <ChartTooltip tooltip={tooltip} />
            <div className="flex items-end gap-1.5 border-b border-slate-200 pb-px" style={{ height: chartHeight }}>
              {seriesA.map((countA, index) => {
                const countB = compare && seriesB ? seriesB[index] : null;
                return (
                  <div
                    key={index}
                    className="group flex h-full flex-1 cursor-default items-end justify-center gap-0.5 rounded-t hover:bg-slate-50"
                    onPointerMove={(event) => showTooltip(event, index)}
                    onPointerLeave={() => setTooltip(null)}
                    onFocus={(event) => showTooltip(event, index)}
                    onBlur={() => setTooltip(null)}
                    tabIndex={0}
                    aria-label={`${index * 10} to ${index * 10 + 10} percent: ${countA}${compare ? ` primary, ${countB} comparison` : ''} assessment(s)`}
                  >
                    {[{ count: countA, color: SERIES_A }, ...(compare ? [{ count: countB ?? 0, color: SERIES_B }] : [])].map(
                      (series, seriesIndex) => (
                        <div key={seriesIndex} className="flex w-full max-w-6 flex-col items-center justify-end self-end">
                          {series.count > 0 ? (
                            <span className="mb-0.5 text-[10px] font-medium leading-none text-slate-500">{series.count}</span>
                          ) : null}
                          <div
                            className="w-full rounded-t"
                            style={{
                              background: series.color,
                              height: Math.max(series.count > 0 ? 3 : 0, (series.count / maxCount) * (chartHeight - 22)),
                            }}
                          />
                        </div>
                      ),
                    )}
                  </div>
                );
              })}
            </div>
            <div className="mt-1 flex gap-1.5 text-[10px] text-slate-400">
              {seriesA.map((_, index) => (
                <div key={index} className="flex-1 text-center">{index * 10}{index === BUCKET_COUNT - 1 ? '–100' : ''}</div>
              ))}
            </div>
            <div className="mt-1 text-center text-[10px] uppercase tracking-wide text-slate-400">Score (%)</div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** Horizontal grouped bars: per-student mean % for content + communication. */
function StudentBreakdownChart({ rows }) {
  const students = useMemo(() => {
    const byStudent = new Map();
    rows.forEach((row) => {
      const pct = scorePercent(row);
      if (pct === null) return;
      if (!byStudent.has(row.studentId)) {
        byStudent.set(row.studentId, { name: row.studentName || row.studentId, content: [], communication: [] });
      }
      const bucket = byStudent.get(row.studentId);
      if (row.resultType === 'content') bucket.content.push(pct);
      if (row.resultType === 'communication') bucket.communication.push(pct);
    });
    return [...byStudent.values()]
      .map((entry) => ({ name: entry.name, content: mean(entry.content), communication: mean(entry.communication) }))
      .sort((a, b) => (b.content ?? -1) - (a.content ?? -1));
  }, [rows]);

  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle className="text-base">Per-student mean score</CardTitle>
            <CardDescription>
              {students.length} student{students.length === 1 ? '' : 's'} in the primary filter
            </CardDescription>
          </div>
          <div className="flex items-center gap-3">
            <LegendKey color={SERIES_CONTENT} label="Content" />
            <LegendKey color={SERIES_COMMUNICATION} label="Communication" />
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {students.length === 0 ? (
          <div className="flex h-24 items-center justify-center text-sm text-slate-400">
            No scored students match this filter.
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {students.map((student) => (
              <div key={student.name} className="grid grid-cols-[minmax(8rem,14rem)_1fr] items-center gap-3">
                <div className="truncate text-sm text-slate-700" title={student.name}>{student.name}</div>
                <div className="flex flex-col gap-1">
                  {[
                    { value: student.content, color: SERIES_CONTENT, label: 'Content' },
                    { value: student.communication, color: SERIES_COMMUNICATION, label: 'Communication' },
                  ].map((series) => (
                    <div key={series.label} className="flex items-center gap-2">
                      <div className="h-2.5 flex-1 overflow-hidden rounded-full bg-slate-100">
                        <div
                          className="h-full rounded-full"
                          style={{ width: `${series.value ?? 0}%`, background: series.color }}
                          role="img"
                          aria-label={`${series.label}: ${formatPct(series.value)}`}
                        />
                      </div>
                      <span className="w-12 shrink-0 text-right text-[11px] tabular-nums text-slate-600">
                        {formatPct(series.value, 0)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ResultsTable({ rows }) {
  const sorted = useMemo(
    () => [...rows].sort((a, b) => String(b.createdAt || '').localeCompare(String(a.createdAt || ''))),
    [rows],
  );
  return (
    <div className="max-h-96 overflow-auto rounded-lg border border-slate-200">
      <table className="w-full text-left text-sm">
        <thead className="sticky top-0 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-3 py-2 font-medium">Session</th>
            <th className="px-3 py-2 font-medium">Student</th>
            <th className="px-3 py-2 font-medium">Type</th>
            <th className="px-3 py-2 font-medium text-right">Score</th>
            <th className="px-3 py-2 font-medium text-right">%</th>
            <th className="px-3 py-2 font-medium">Result</th>
            <th className="px-3 py-2 font-medium">Date</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {sorted.map((row) => {
            const pct = scorePercent(row);
            const isPass = /^pass$/i.test(String(row.passFail || ''));
            return (
              <tr key={`${row.sessionId}-${row.resultType}`} className="bg-white">
                <td className="max-w-56 truncate px-3 py-2 text-slate-700" title={rootSessionOf(row).name}>
                  {rootSessionOf(row).name}
                </td>
                <td className="max-w-44 truncate px-3 py-2 text-slate-700">{row.studentName || '—'}</td>
                <td className="px-3 py-2 capitalize text-slate-600">{String(row.resultType).replace(/_/g, ' ')}</td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-700">
                  {row.scoreTotal ?? '—'}{row.scoreMax ? ` / ${row.scoreMax}` : ''}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-700">{formatPct(pct, 0)}</td>
                <td className="px-3 py-2">
                  {row.passFail ? (
                    <Badge className={isPass ? 'bg-emerald-100 text-emerald-700' : 'bg-rose-100 text-rose-700'}>
                      {row.passFail}
                    </Badge>
                  ) : (
                    <span className="text-slate-400">—</span>
                  )}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-slate-500">{formatDate(row.createdAt)}</td>
              </tr>
            );
          })}
          {sorted.length === 0 ? (
            <tr>
              <td colSpan={7} className="px-3 py-6 text-center text-sm text-slate-400">No rows match this filter.</td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}

/* ---------- page ---------- */

export default function AnalyticsPage({ onBack }) {
  const [rows, setRows] = useState(null); // null = never loaded
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');
  const [storedFilterA, setFilterA] = useState(emptyFilter);
  const [storedFilterB, setFilterB] = useState(emptyFilter);
  const [compare, setCompare] = useState(false);
  const [showTable, setShowTable] = useState(false);

  useEffect(() => {
    loadData();
  }, []);

  async function loadData() {
    setIsLoading(true);
    setError('');
    try {
      const body = await apiJson('/api/analytics/assessments', {
        fallbackMessage: 'Failed to load analytics data.',
      });
      // Atomic swap: incomplete/in-flight fetches never render partial data.
      setRows(Array.isArray(body.results) ? body.results : []);
    } catch (loadError) {
      setError(loadError.message || 'Failed to load analytics data.');
    } finally {
      setIsLoading(false);
    }
  }

  const scoredRows = useMemo(
    () => (rows || []).filter((row) => row.resultType === 'content' || row.resultType === 'communication'),
    [rows],
  );
  const catalog = useMemo(() => buildFilterCatalog(scoredRows), [scoredRows]);
  // A refresh can remove a recording or a student the stored filter still
  // names; reconciling during render (not in an effect) means no frame is
  // drawn from a selection the controls could not show.
  const filterA = useMemo(() => reconcileFilter(storedFilterA, catalog), [storedFilterA, catalog]);
  const filterB = useMemo(() => reconcileFilter(storedFilterB, catalog), [storedFilterB, catalog]);

  const rowsA = useMemo(() => applyFilter(scoredRows, filterA), [scoredRows, filterA]);
  const rowsB = useMemo(() => (compare ? applyFilter(scoredRows, filterB) : []), [scoredRows, filterB, compare]);

  const statsA = useMemo(
    () => ({
      students: new Set(rowsA.map((row) => row.studentId)).size,
      assessments: new Set(rowsA.map((row) => row.sessionId)).size,
      content: summarize(rowsA, 'content'),
      communication: summarize(rowsA, 'communication'),
    }),
    [rowsA],
  );
  const statsB = useMemo(
    () =>
      compare
        ? {
            students: new Set(rowsB.map((row) => row.studentId)).size,
            assessments: new Set(rowsB.map((row) => row.sessionId)).size,
            content: summarize(rowsB, 'content'),
            communication: summarize(rowsB, 'communication'),
          }
        : null,
    [rowsB, compare],
  );

  const contentHistA = useMemo(() => histogram(rowsA, 'content'), [rowsA]);
  const contentHistB = useMemo(() => histogram(rowsB, 'content'), [rowsB]);
  const communicationHistA = useMemo(() => histogram(rowsA, 'communication'), [rowsA]);
  const communicationHistB = useMemo(() => histogram(rowsB, 'communication'), [rowsB]);

  const isFirstLoad = isLoading && rows === null;
  const isRefreshing = isLoading && rows !== null;

  function summaryPair(getValue, format) {
    return {
      primary: format(getValue(statsA)),
      secondary: statsB ? format(getValue(statsB)) : null,
    };
  }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="sticky top-0 z-40 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-6 py-4">
          <div className="flex items-center gap-3">
            <Button variant="outline" size="sm" className="gap-2" onClick={onBack} title="Back to dashboard">
              <ArrowLeft className="h-4 w-4" />
              Back
            </Button>
            <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-blue-600 to-indigo-700 text-white shadow-sm">
              <BarChart3 className="h-5 w-5" />
            </div>
            <div>
              <div className="text-lg font-bold">Score Analytics</div>
              <div className="text-xs text-slate-500">Assessment results stored in the database</div>
            </div>
          </div>
          <Button variant="outline" size="sm" className="gap-2" onClick={loadData} disabled={isLoading}>
            {isLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCcw className="h-4 w-4" />}
            Refresh
          </Button>
        </div>
      </header>

      <main className="mx-auto flex max-w-7xl flex-col gap-6 px-6 py-8">
        {error ? (
          <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
            <span>{error}</span>
            <Button variant="outline" size="sm" onClick={loadData}>Retry</Button>
          </div>
        ) : null}

        {isFirstLoad ? (
          /* First load: skeleton placeholders — nothing partial is ever shown. */
          <div className="flex flex-col gap-6" aria-busy="true" aria-label="Loading analytics">
            <SkeletonBlock className="h-24" />
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              {[0, 1, 2, 3].map((i) => <SkeletonBlock key={i} className="h-24" />)}
            </div>
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <SkeletonBlock className="h-72" />
              <SkeletonBlock className="h-72" />
            </div>
            <SkeletonBlock className="h-56" />
          </div>
        ) : rows !== null ? (
          <motion.div
            className={`flex flex-col gap-6 transition-opacity ${isRefreshing ? 'pointer-events-none opacity-50' : ''}`}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: isRefreshing ? 0.5 : 1, y: 0 }}
            transition={{ duration: 0.3 }}
            aria-busy={isRefreshing}
          >
            {/* Filters — one row per filter set, scoping everything below. */}
            <Card className="border-slate-200 bg-white shadow-sm">
              <CardContent className="flex flex-col gap-3 p-5 pt-6">
                <FilterRow
                  label={compare ? 'Primary' : 'Filters'}
                  color={SERIES_A}
                  filter={filterA}
                  onChange={setFilterA}
                  catalog={catalog}
                />
                {compare ? (
                  <FilterRow
                    label="Comparison"
                    color={SERIES_B}
                    filter={filterB}
                    onChange={setFilterB}
                    catalog={catalog}
                    onRemove={() => setCompare(false)}
                  />
                ) : (
                  <div>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="gap-2 text-blue-700 hover:text-blue-800"
                      onClick={() => {
                        setFilterB(filterA);
                        setCompare(true);
                      }}
                    >
                      <Layers className="h-4 w-4" />
                      Add comparison overlay
                    </Button>
                  </div>
                )}
              </CardContent>
            </Card>

            {/* KPI tiles */}
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              <StatTile icon={Users} label="Students" compare={compare} {...summaryPair((s) => s.students, String)} />
              <StatTile icon={GraduationCap} label="Assessments" compare={compare} {...summaryPair((s) => s.assessments, String)} />
              <StatTile
                icon={ListChecks}
                label="Content pass rate"
                hint={`mean ${formatPct(statsA.content.mean, 0)} · median ${formatPct(statsA.content.median, 0)}`}
                compare={compare}
                {...summaryPair((s) => s.content.passRate, (v) => formatPct(v, 0))}
              />
              <StatTile
                icon={ListChecks}
                label="Communication pass rate"
                hint={`mean ${formatPct(statsA.communication.mean, 0)} · median ${formatPct(statsA.communication.median, 0)}`}
                compare={compare}
                {...summaryPair((s) => s.communication.passRate, (v) => formatPct(v, 0))}
              />
            </div>

            {/* Distributions */}
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <DistributionChart
                title="Content score distribution"
                description={`mean ${formatPct(statsA.content.mean)} · median ${formatPct(statsA.content.median)} · pass ${formatPct(statsA.content.passRate, 0)} / fail ${formatPct(statsA.content.failRate, 0)}`}
                seriesA={contentHistA}
                seriesB={contentHistB}
                compare={compare}
              />
              <DistributionChart
                title="Communication score distribution"
                description={`mean ${formatPct(statsA.communication.mean)} · median ${formatPct(statsA.communication.median)} · pass ${formatPct(statsA.communication.passRate, 0)} / fail ${formatPct(statsA.communication.failRate, 0)}`}
                seriesA={communicationHistA}
                seriesB={communicationHistB}
                compare={compare}
              />
            </div>

            <StudentBreakdownChart rows={rowsA} />

            {/* Table view — every charted value reachable without hover. */}
            <Card className="border-slate-200 bg-white shadow-sm">
              <CardHeader className="pb-2">
                <button
                  type="button"
                  className="flex w-full items-center justify-between text-left"
                  onClick={() => setShowTable((v) => !v)}
                  aria-expanded={showTable}
                >
                  <div className="flex items-center gap-2">
                    <Table2 className="h-4 w-4 text-slate-500" />
                    <CardTitle className="text-base">Data table</CardTitle>
                    <CardDescription>
                      {rowsA.length} row{rowsA.length === 1 ? '' : 's'} (primary filter)
                    </CardDescription>
                  </div>
                  <ChevronDown className={`h-4 w-4 text-slate-400 transition-transform ${showTable ? 'rotate-180' : ''}`} />
                </button>
              </CardHeader>
              {showTable ? (
                <CardContent>
                  <ResultsTable rows={rowsA} />
                </CardContent>
              ) : null}
            </Card>

            <div className="flex items-center gap-2 text-xs text-slate-400">
              <CalendarDays className="h-3.5 w-3.5" />
              Data reloads only when you press Refresh — newly completed assessments appear after a refresh.
            </div>
          </motion.div>
        ) : null}
      </main>
    </div>
  );
}
