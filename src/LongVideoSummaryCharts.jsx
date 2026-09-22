import React, { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  BarChart3,
  CheckCircle2,
  GraduationCap,
  Maximize2,
  TrendingUp,
  X,
} from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatScore, isRawScoreDisplay } from '@/lib/scoreDisplay';
import { useScoreDisplay } from '@/lib/useScoreDisplay';

const SCORE_LABEL_COLORS = {
  All: '#16a34a',
  Most: '#22c55e',
  Some: '#f59e0b',
  None: '#ef4444',
};

const SCORE_LABEL_POINTS = { All: 3, Most: 2, Some: 1, None: 0 };

function safeNumber(value, fallback = 0) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function clampPercent(raw) {
  const n = Number(raw);
  if (!Number.isFinite(n)) return 0;
  return Math.min(100, Math.max(0, n));
}

function truncate(text, max = 60) {
  if (!text) return '';
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

function ChartLegend({ items }) {
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs text-slate-600">
      {items.map((item) => (
        <div key={item.label} className="flex items-center gap-1.5">
          <span
            className="inline-block h-2.5 w-2.5 rounded-sm"
            style={{ background: item.color }}
            aria-hidden
          />
          <span>{item.label}</span>
        </div>
      ))}
    </div>
  );
}

function ChartFullscreenShell({ title, open, onClose, children }) {
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open || typeof document === 'undefined') {
    return null;
  }

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 z-[230] flex flex-col bg-black/90 backdrop-blur-sm px-4 py-4 text-slate-900 sm:p-8"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="flex shrink-0 items-center justify-between gap-4 pb-4 text-white">
        <span className="text-lg font-semibold tracking-tight">{title}</span>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          className="gap-2 rounded-full px-4"
          onClick={onClose}
        >
          <X className="h-4 w-4 shrink-0" />
          Close
        </Button>
      </div>
      <div className="relative min-h-0 flex-1 overflow-auto rounded-2xl border border-white/10 bg-white p-4 shadow-2xl sm:p-8">
        {children}
      </div>
      <div className="shrink-0 pt-4 text-center text-xs text-white/60">Press Escape to close.</div>
    </div>,
    document.body,
  );
}

/** Grouped bar chart — content vs communication. Bars are always % so every
 *  student shares one axis; the label above each bar follows the account's
 *  score-display preference (points in raw mode). */
function StudentScoreComparisonChart({ summaries, mode }) {
  const data = summaries.map((summary) => {
    const contentPercent = clampPercent(summary.content?.percentYes ?? 0);
    const communicationMaxScore = safeNumber(summary.communication?.maxScore, 0);
    const communicationMax = communicationMaxScore > 0 ? communicationMaxScore : 1;
    const communicationPercentRaw = summary.communication
      ? (safeNumber(summary.communication.totalScore) / communicationMax) * 100
      : 0;
    const communicationPercent = clampPercent(
      Number.isFinite(communicationPercentRaw) ? Math.round(communicationPercentRaw * 10) / 10 : 0,
    );
    return {
      label: summary.clipLabel,
      content: contentPercent,
      communication: communicationPercent,
      contentLabel: formatScore(summary.content?.yesCount, summary.content?.totalCriteria, mode),
      communicationLabel: formatScore(summary.communication?.totalScore, summary.communication?.maxScore, mode),
      contentPass: summary.content?.passFail || '',
      communicationPass: summary.communication?.passFail || '',
    };
  });

  const width = Math.max(960, data.length * 160);
  const height = 360;
  const margin = { top: 28, right: 32, bottom: 72, left: 52 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;

  const groupWidth = innerWidth / data.length;
  const barWidth = Math.min(32, (groupWidth - 24) / 2);
  const yTicks = [0, 25, 50, 75, 100];

  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="min-w-full touch-pan-x"
        style={{ minWidth: width, height: 'auto' }}
        role="img"
        aria-label="Student score comparison chart"
      >
        <g transform={`translate(${margin.left}, ${margin.top})`}>
          {yTicks.map((tick) => {
            const yPos = innerHeight - (tick / 100) * innerHeight;
            return (
              <g key={tick}>
                <line
                  x1={0}
                  x2={innerWidth}
                  y1={yPos}
                  y2={yPos}
                  className="stroke-slate-200"
                  strokeDasharray="4 4"
                />
                <text x={-10} y={yPos + 4} textAnchor="end" className="fill-slate-500 text-[11px]">
                  {tick}%
                </text>
              </g>
            );
          })}

          {data.map((row, index) => {
            const groupX = index * groupWidth;
            const centerX = groupX + groupWidth / 2;
            const contentBarHeight = Math.max(0, (row.content / 100) * innerHeight);
            const communicationBarHeight = Math.max(0, (row.communication / 100) * innerHeight);
            const contentX = centerX - barWidth - 4;
            const communicationX = centerX + 4;

            return (
              <g key={`${row.label}-${index}`}>
                <rect
                  x={contentX}
                  y={innerHeight - contentBarHeight}
                  width={barWidth}
                  height={contentBarHeight}
                  rx={4}
                  fill="#2563eb"
                />
                <rect
                  x={communicationX}
                  y={innerHeight - communicationBarHeight}
                  width={barWidth}
                  height={communicationBarHeight}
                  rx={4}
                  fill="#7c3aed"
                />
                <text
                  x={contentX + barWidth / 2}
                  y={innerHeight - contentBarHeight - 6}
                  textAnchor="middle"
                  className="fill-slate-700 text-[11px] font-semibold"
                >
                  {row.contentLabel}
                </text>
                <text
                  x={communicationX + barWidth / 2}
                  y={innerHeight - communicationBarHeight - 6}
                  textAnchor="middle"
                  className="fill-slate-700 text-[11px] font-semibold"
                >
                  {row.communicationLabel}
                </text>
                <text
                  x={centerX}
                  y={innerHeight + 20}
                  textAnchor="middle"
                  className="fill-slate-700 text-[12px] font-medium"
                >
                  {truncate(row.label, 16)}
                </text>
                <text
                  x={centerX}
                  y={innerHeight + 42}
                  textAnchor="middle"
                  className={`text-[11px] font-semibold ${
                    row.contentPass === 'Pass' && row.communicationPass === 'Pass'
                      ? 'fill-emerald-600'
                      : 'fill-rose-600'
                  }`}
                >
                  C:{row.contentPass || '-'} / Com:{row.communicationPass || '-'}
                </text>
              </g>
            );
          })}
        </g>
      </svg>
    </div>
  );
}

/** Stacked criterion rows */
function CommunicationCriteriaChart({ summaries }) {
  const criteriaMap = new Map();

  summaries.forEach((summary) => {
    const list = summary.communication?.perCriterionPoints || [];
    list.forEach((criterion) => {
      const key = String(criterion.id ?? criterion.label);
      if (!criteriaMap.has(key)) {
        criteriaMap.set(key, {
          id: criterion.id,
          label: criterion.label,
          section: criterion.section,
          counts: { All: 0, Most: 0, Some: 0, None: 0 },
          totalPoints: 0,
          studentCount: 0,
        });
      }
      const entry = criteriaMap.get(key);
      const label = ['All', 'Most', 'Some', 'None'].includes(criterion.scoreLabel)
        ? criterion.scoreLabel
        : 'None';
      entry.counts[label] += 1;
      entry.totalPoints += safeNumber(criterion.points, SCORE_LABEL_POINTS[label] || 0);
      entry.studentCount += 1;
    });
  });

  const rows = [...criteriaMap.values()].sort((a, b) => {
    const idA = Number(a.id) || 0;
    const idB = Number(b.id) || 0;
    return idA - idB;
  });

  if (rows.length === 0) {
    return null;
  }

  const totalStudents = summaries.length;
  const rowHeight = 42;
  const margin = { top: 28, right: 220, bottom: 28, left: 248 };
  const width = Math.max(1024, rows.length ? 980 : 400);
  const innerWidth = width - margin.left - margin.right;
  const height = margin.top + margin.bottom + rows.length * rowHeight;

  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="min-w-full touch-pan-x"
        style={{ minWidth: width, height: 'auto' }}
        role="img"
        aria-label="Communication rubric breakdown"
      >
        <g transform={`translate(${margin.left}, ${margin.top})`}>
          {rows.map((row, rowIndex) => {
            let offset = 0;
            const averagePoints = row.studentCount
              ? row.totalPoints / row.studentCount
              : 0;
            const segments = ['All', 'Most', 'Some', 'None']
              .map((label) => ({
                label,
                count: row.counts[label] || 0,
              }))
              .filter((segment) => segment.count > 0);

            return (
              <g key={row.label} transform={`translate(0, ${rowIndex * rowHeight})`}>
                <text
                  x={-12}
                  y={rowHeight / 2 + 5}
                  textAnchor="end"
                  className="fill-slate-700 text-[12px] font-medium"
                >
                  {truncate(`${row.id ? `${row.id}. ` : ''}${row.label}`, 36)}
                </text>
                {segments.map((segment) => {
                  const ratio = segment.count / totalStudents;
                  const segmentWidth = ratio * innerWidth;
                  const start = offset;

                  offset += segmentWidth;

                  return (
                    <g key={`${segment.label}-${start}`}>
                      <rect
                        x={start}
                        y={8}
                        width={segmentWidth}
                        height={rowHeight - 16}
                        fill={SCORE_LABEL_COLORS[segment.label]}
                      />
                      {segmentWidth > 40 ? (
                        <text
                          x={start + segmentWidth / 2}
                          y={rowHeight / 2 + 6}
                          textAnchor="middle"
                          className="fill-white text-[11px] font-semibold"
                        >
                          {segment.count}
                        </text>
                      ) : null}
                    </g>
                  );
                })}
                <text
                  x={innerWidth + 16}
                  y={rowHeight / 2 + 5}
                  className="fill-slate-700 text-[12px] font-semibold"
                >
                  avg {averagePoints.toFixed(2)} / 3
                </text>
              </g>
            );
          })}
        </g>
      </svg>
    </div>
  );
}

function LabelDistributionDonut({ totals, studentCount }) {
  const entries = ['All', 'Most', 'Some', 'None'].map((label) => ({
    label,
    count: safeNumber(totals[label]),
  }));
  const sum = entries.reduce((acc, item) => acc + item.count, 0);

  if (sum === 0) {
    return null;
  }

  const radius = 88;
  const stroke = 26;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;

  return (
    <div className="flex flex-col items-center gap-6 lg:flex-row lg:items-start lg:gap-12">
      <svg
        width={radius * 2 + stroke}
        height={radius * 2 + stroke}
        viewBox={`0 0 ${radius * 2 + stroke} ${radius * 2 + stroke}`}
        className="shrink-0"
      >
        <g transform={`translate(${radius + stroke / 2}, ${radius + stroke / 2}) rotate(-90)`}>
          {entries.map((entry) => {
            const ratio = entry.count / sum;
            const dash = ratio * circumference;
            const segment = (
              <circle
                key={entry.label}
                r={radius}
                fill="transparent"
                stroke={SCORE_LABEL_COLORS[entry.label]}
                strokeWidth={stroke}
                strokeDasharray={`${dash} ${circumference - dash}`}
                strokeDashoffset={-offset}
              />
            );
            offset += dash;
            return segment;
          })}
        </g>
      </svg>
      <div className="max-w-xl space-y-2">
        {entries.map((entry) => {
          const percent = ((entry.count / sum) * 100).toFixed(0);
          return (
            <div key={entry.label} className="flex items-center gap-3 text-sm text-slate-600">
              <span
                className="inline-block h-3.5 w-3.5 rounded-sm"
                style={{ background: SCORE_LABEL_COLORS[entry.label] }}
                aria-hidden
              />
              <span className="w-14 font-semibold text-slate-800">{entry.label}</span>
              <span className="font-mono text-slate-900">{entry.count}</span>
              <span className="text-slate-500">({percent}%)</span>
            </div>
          );
        })}
        <div className="pt-3 text-xs text-slate-500">
          Across {studentCount} {studentCount === 1 ? 'student' : 'students'} ×{' '}
          {Math.round(sum / Math.max(studentCount, 1))} criteria.
        </div>
      </div>
    </div>
  );
}

function ExpandChartButton({ label, expanded, chartKey, onToggle }) {
  const isActive = expanded === chartKey;
  return (
    <Button
      type="button"
      size="sm"
      variant="outline"
      className={`h-9 shrink-0 gap-2 rounded-xl border-slate-200 bg-white px-3 ${isActive ? 'ring-2 ring-cyan-300' : ''}`}
      aria-pressed={isActive}
      title={`View ${label} in fullscreen`}
      onClick={() => onToggle(chartKey)}
    >
      <Maximize2 className="h-4 w-4 shrink-0" />
      <span className="hidden sm:inline">{label}</span>
    </Button>
  );
}

export default function LongVideoSummaryCharts({ data }) {
  const [fullscreenChart, setFullscreenChart] = useState(null);
  const scoreDisplay = useScoreDisplay();
  const isRaw = isRawScoreDisplay(scoreDisplay);

  const summaries = useMemo(() => {
    return Array.isArray(data?.summaries)
      ? data.summaries.filter((item) => item.content && item.communication)
      : [];
  }, [data]);

  const cohortTotals = useMemo(() => {
    const totals = { All: 0, Most: 0, Some: 0, None: 0 };
    summaries.forEach((summary) => {
      const counts = summary.communication?.labelCounts || {};
      totals.All += safeNumber(counts.All);
      totals.Most += safeNumber(counts.Most);
      totals.Some += safeNumber(counts.Some);
      totals.None += safeNumber(counts.None);
    });
    return totals;
  }, [summaries]);

  const passStats = useMemo(() => {
    let contentPass = 0;
    let communicationPass = 0;
    summaries.forEach((summary) => {
      if (summary.content?.passFail === 'Pass') contentPass += 1;
      if (summary.communication?.passFail === 'Pass') communicationPass += 1;
    });
    return {
      contentPass,
      communicationPass,
      total: summaries.length,
    };
  }, [summaries]);

  const hasCriterionRows = summaries.some((s) => Array.isArray(s.communication?.perCriterionPoints) && s.communication.perCriterionPoints.length > 0);
  const hasDonut =
    summaries.length > 0 &&
    cohortTotals.All + cohortTotals.Most + cohortTotals.Some + cohortTotals.None > 0;

  const handleToggleFullscreen = (key) => {
    setFullscreenChart((current) => (current === key ? null : key));
  };

  if (summaries.length < 2) {
    return null;
  }

  return (
    <>
      <ChartFullscreenShell
        title="Overall score per student"
        open={fullscreenChart === 'student'}
        onClose={() => setFullscreenChart(null)}
      >
        <StudentScoreComparisonChart summaries={summaries} mode={scoreDisplay} />
      </ChartFullscreenShell>
      <ChartFullscreenShell
        title="Communication rubric — criterion-by-criterion"
        open={fullscreenChart === 'criteria'}
        onClose={() => setFullscreenChart(null)}
      >
        <div className="mb-8">
          <ChartLegend
            items={[
              { label: 'All', color: SCORE_LABEL_COLORS.All },
              { label: 'Most', color: SCORE_LABEL_COLORS.Most },
              { label: 'Some', color: SCORE_LABEL_COLORS.Some },
              { label: 'None', color: SCORE_LABEL_COLORS.None },
            ]}
          />
        </div>
        <CommunicationCriteriaChart summaries={summaries} />
      </ChartFullscreenShell>
      <ChartFullscreenShell
        title="Cohort communication-label distribution"
        open={fullscreenChart === 'distribution'}
        onClose={() => setFullscreenChart(null)}
      >
        <LabelDistributionDonut totals={cohortTotals} studentCount={summaries.length} />
      </ChartFullscreenShell>

      <Card className="border-slate-200 bg-white shadow-sm">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <CardTitle className="flex items-center gap-2 text-base">
                <BarChart3 className="h-5 w-5 text-cyan-700" aria-hidden="true" />
                Cohort summary
              </CardTitle>
              <CardDescription>
                Charts appear once at least two students have been fully assessed. Currently showing {summaries.length}{' '}
                {summaries.length === 1 ? 'student' : 'students'}.
              </CardDescription>
            </div>
            <div className="flex flex-wrap items-center gap-2 rounded-xl bg-slate-50 px-3 py-2 text-xs text-slate-600">
              <span className="flex items-center gap-1.5">
                <CheckCircle2 className="h-4 w-4 text-emerald-600" />
                Content Pass:{' '}
                <span className="font-semibold text-slate-900">
                  {passStats.contentPass} / {passStats.total}
                </span>
              </span>
              <span className="text-slate-300 max-sm:hidden">•</span>
              <span className="flex items-center gap-1.5">
                <GraduationCap className="h-4 w-4 text-violet-600" />
                Communication Pass:{' '}
                <span className="font-semibold text-slate-900">
                  {passStats.communicationPass} / {passStats.total}
                </span>
              </span>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-8">
          <section className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 pb-2">
              <div className="flex items-center gap-2 text-sm font-semibold text-slate-700">
                <TrendingUp className="h-4 w-4 text-blue-600" />
                Overall score per student
              </div>
              <div className="flex flex-wrap items-center justify-end gap-2">
                <ChartLegend
                  items={[
                    { label: isRaw ? 'Content rubric (Yes / criteria)' : 'Content rubric (% Yes)', color: '#2563eb' },
                    { label: isRaw ? 'Communication rubric (points / max)' : 'Communication rubric (% of max)', color: '#7c3aed' },
                  ]}
                />
                <ExpandChartButton
                  label="Fullscreen"
                  expanded={fullscreenChart}
                  chartKey="student"
                  onToggle={handleToggleFullscreen}
                />
              </div>
            </div>
            <StudentScoreComparisonChart summaries={summaries} mode={scoreDisplay} />
          </section>

          {hasCriterionRows ? (
            <section className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 pb-2">
                <div className="text-sm font-semibold text-slate-700">
                  Communication rubric — criterion-by-criterion breakdown
                </div>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <ChartLegend
                    items={[
                      { label: 'All', color: SCORE_LABEL_COLORS.All },
                      { label: 'Most', color: SCORE_LABEL_COLORS.Most },
                      { label: 'Some', color: SCORE_LABEL_COLORS.Some },
                      { label: 'None', color: SCORE_LABEL_COLORS.None },
                    ]}
                  />
                  <ExpandChartButton
                    label="Fullscreen"
                    expanded={fullscreenChart}
                    chartKey="criteria"
                    onToggle={handleToggleFullscreen}
                  />
                </div>
              </div>
              <CommunicationCriteriaChart summaries={summaries} />
            </section>
          ) : null}

          {hasDonut ? (
            <section className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 pb-2">
                <div className="text-sm font-semibold text-slate-700">Cohort communication-label distribution</div>
                <ExpandChartButton
                  label="Fullscreen"
                  expanded={fullscreenChart}
                  chartKey="distribution"
                  onToggle={handleToggleFullscreen}
                />
              </div>
              <LabelDistributionDonut totals={cohortTotals} studentCount={summaries.length} />
            </section>
          ) : null}
        </CardContent>
      </Card>
    </>
  );
}
