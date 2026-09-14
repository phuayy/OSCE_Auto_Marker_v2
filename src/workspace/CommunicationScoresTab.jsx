import React from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { IndicatorList } from '@/workspace/primitives.jsx';

const COMMUNICATION_LABEL_META = {
  All: {
    color: 'bg-emerald-100 text-emerald-700',
    barColor: 'bg-emerald-500',
    points: 3,
  },
  Most: {
    color: 'bg-cyan-100 text-cyan-700',
    barColor: 'bg-cyan-500',
    points: 2,
  },
  Some: {
    color: 'bg-amber-100 text-amber-700',
    barColor: 'bg-amber-500',
    points: 1,
  },
  None: {
    color: 'bg-rose-100 text-rose-700',
    barColor: 'bg-rose-500',
    points: 0,
  },
};

export default function CommunicationScoresTab({ criteria, summary, overallSummary, sessionStatus, onSeek }) {
  const hasCriteria = Array.isArray(criteria) && criteria.length > 0;

  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle className="text-base">Communication scores</CardTitle>
        <CardDescription>
          Communication rubric scoring on a None / Some / Most / All scale. All=3, Most=2, Some=1, None=0.
          Pass threshold is {summary?.passThreshold ?? 11}/{summary?.maxScore ?? 21}.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {summary ? (
          <div
            className={`rounded-xl border p-4 ${
              summary.passFail === 'Pass'
                ? 'border-emerald-200 bg-emerald-50'
                : 'border-rose-200 bg-rose-50'
            }`}
          >
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div>
                <div className="text-sm text-slate-700">Overall Communication Result</div>
                <div
                  className={`text-3xl font-bold ${
                    summary.passFail === 'Pass' ? 'text-emerald-800' : 'text-rose-800'
                  }`}
                >
                  {summary.passFail}
                </div>
              </div>
              <div className="text-right">
                <div className="text-xs uppercase tracking-wide text-slate-500">Total</div>
                <div className="text-2xl font-bold text-slate-900">
                  {summary.totalScore} / {summary.maxScore}
                </div>
                <div className="text-[11px] text-slate-500">
                  Pass at {summary.passThreshold}/{summary.maxScore}
                </div>
              </div>
            </div>
            <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-slate-200">
              <div
                className={`h-full rounded-full ${
                  summary.passFail === 'Pass' ? 'bg-emerald-500' : 'bg-rose-500'
                }`}
                style={{
                  width: `${Math.max(0, Math.min(100, (summary.totalScore / Math.max(1, summary.maxScore)) * 100))}%`,
                }}
              />
            </div>
            {summary.decisionReason ? (
              <div className="mt-2 text-xs text-slate-600">{summary.decisionReason}</div>
            ) : null}
            {summary.labelCounts && Object.keys(summary.labelCounts).length ? (
              <div className="mt-3 flex flex-wrap gap-2 text-xs">
                {['All', 'Most', 'Some', 'None'].map((label) => {
                  const meta = COMMUNICATION_LABEL_META[label];
                  return (
                    <span
                      key={label}
                      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-semibold ${meta.color}`}
                    >
                      {label} · {Number(summary.labelCounts[label] || 0)}
                    </span>
                  );
                })}
              </div>
            ) : null}
          </div>
        ) : !hasCriteria ? (
          <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/90 p-6 text-center shadow-inner">
            <p className="text-sm font-medium text-slate-700">
              Communication scores appear here once the pipeline runs.
            </p>
            <p className="mt-2 text-xs text-slate-500">
              Make sure the communication scorer is enabled (set <code className="rounded bg-white px-1.5 py-0.5 text-[11px]">ENABLE_COMMUNICATION_SCORING=true</code>)
              and the rubric is loaded under Settings → Communication Rubric.
            </p>
            {sessionStatus ? (
              <p className="mt-2 text-[11px] text-slate-500">Current session status: {sessionStatus}</p>
            ) : null}
          </div>
        ) : null}

        {hasCriteria
          ? criteria.map((criterion) => {
              const meta = COMMUNICATION_LABEL_META[criterion.scoreLabel] || COMMUNICATION_LABEL_META.None;
              const hasEvidence = Number.isFinite(criterion.timestampSeconds);
              return (
                <div
                  key={criterion.id}
                  className="rounded-xl border border-slate-200 bg-slate-50 p-4 transition hover:border-cyan-200 hover:bg-white"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="inline-flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-cyan-600 to-blue-700 text-xs font-bold text-white shadow-sm">
                          {criterion.id}
                        </span>
                        <div className="font-semibold text-slate-900">{criterion.label}</div>
                      </div>
                      {criterion.section ? (
                        <div className="ml-9 text-[11px] uppercase tracking-wider text-slate-500">
                          {criterion.section}
                        </div>
                      ) : null}
                    </div>
                    <div className="flex flex-col items-end gap-1.5">
                      <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-bold uppercase ${meta.color}`}>
                        {criterion.scoreLabel}
                      </span>
                      <span className="text-[11px] text-slate-500">{criterion.points}/3 marks</span>
                    </div>
                  </div>

                  <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
                    <div className={`h-full rounded-full ${meta.barColor}`} style={{ width: `${(criterion.points / 3) * 100}%` }} />
                  </div>

                  {criterion.evidence ? (
                    <div className="mt-3 rounded-lg bg-white p-3 text-sm text-slate-700 ring-1 ring-slate-200">
                      {criterion.evidence}
                    </div>
                  ) : null}

                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => onSeek?.(criterion.timestampSeconds)}
                      disabled={!hasEvidence}
                    >
                      View evidence
                    </Button>
                    <span className="text-[11px] text-slate-500">
                      {hasEvidence ? `Timestamp: ${criterion.timestamp}` : 'No timestamp'}
                    </span>
                  </div>

                  {(criterion.indicatorsObserved.length
                    || criterion.indicatorsMissing.length
                    || criterion.indicatorsNotObservable.length) ? (
                    <details className="mt-3 rounded-lg border border-slate-200 bg-white p-3">
                      <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-slate-600">
                        Indicator breakdown
                      </summary>
                      <div className="mt-2 space-y-2 text-xs text-slate-700">
                        {criterion.indicatorsObserved.length ? (
                          <IndicatorList title="Observed" color="text-emerald-700" items={criterion.indicatorsObserved} />
                        ) : null}
                        {criterion.indicatorsMissing.length ? (
                          <IndicatorList title="Missing" color="text-rose-700" items={criterion.indicatorsMissing} />
                        ) : null}
                        {criterion.indicatorsNotObservable.length ? (
                          <IndicatorList
                            title="Not observable from audio/video"
                            color="text-slate-500"
                            items={criterion.indicatorsNotObservable}
                          />
                        ) : null}
                      </div>
                    </details>
                  ) : null}
                </div>
              );
            })
          : null}

        {overallSummary ? (
          <div className="rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-700">
            <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Model Summary
            </div>
            {overallSummary}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
