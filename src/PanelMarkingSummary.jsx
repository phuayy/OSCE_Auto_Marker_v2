// How a panel marked this sheet, for the content-scores tab.
//
// Two pieces. `PanelMarkingSummary` sits above the criteria and says who
// marked, who adjudicated, how alike the markers were, and — loudly — whether
// the panel degraded to a single marker. `PanelVotes` sits inside each
// criterion row and shows every marker's vote beside the final verdict, with
// how the item was decided; the disputed ones are the rows an examiner should
// look at first.
//
// Both render nothing for a sheet that carries no panel block, so a
// single-model sheet's tab is unchanged.
import React, { useState } from 'react';
import { AlertTriangle, ChevronDown, ChevronUp, FileJson, Users } from 'lucide-react';

import { resolveMediaUrl } from '@/auth';

const TONE_CLASSES = Object.freeze({
  neutral: 'bg-slate-200 text-slate-700',
  attention: 'bg-violet-100 text-violet-800',
  warning: 'bg-amber-100 text-amber-800',
});

function voteClasses(vote) {
  return vote === 'Yes' ? 'bg-emerald-100 text-emerald-700' : 'bg-rose-100 text-rose-700';
}

// `artifacts` is the session output's `panelArtifacts` — the API's record of
// where each marker's own sheet and the adjudication record are served from.
// Optional: a sheet adopted from disk on a later run carries none.
export function PanelMarkingSummary({ report, artifacts = null }) {
  if (!report) return null;
  const markerSheets = artifacts?.markers || {};

  return (
    <div className="rounded-xl border border-violet-200 bg-violet-50/60 p-4 text-sm text-slate-700">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 font-semibold text-violet-900">
          <Users className="h-4 w-4" />
          Panel marking
        </div>
        {report.agreementLabel ? (
          <div className="text-xs font-medium text-violet-900" title="How often the markers gave the same verdict">
            {report.agreementLabel}
          </div>
        ) : null}
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        {report.markers.map((marker) => (
          <div key={marker.key || marker.initial} className="rounded-lg border border-violet-100 bg-white px-3 py-2">
            <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
              Marker {marker.initial}
            </div>
            <div className="font-mono text-xs text-slate-800">{marker.label || marker.key}</div>
            {marker.passFail ? (
              <div className="mt-1 text-[11px] text-slate-500">
                On its own: {marker.passFail}
                {marker.yesCount !== null && marker.yesCount !== undefined ? ` · ${marker.yesCount} Yes` : ''}
              </div>
            ) : null}
            {markerSheets[marker.key]?.url ? (
              <a
                href={resolveMediaUrl(markerSheets[marker.key].url)}
                target="_blank"
                rel="noreferrer"
                className="mt-1 inline-flex items-center gap-1 text-[11px] font-medium text-violet-700 hover:text-violet-900"
              >
                <FileJson className="h-3 w-3" /> Marker&apos;s own sheet
              </a>
            ) : null}
          </div>
        ))}
      </div>

      <div className="mt-2 text-[11px] text-slate-600">
        {report.degraded ? null : report.adjudicator.called ? (
          <>
            Adjudicated by <span className="font-mono">{report.adjudicator.label}</span>
            {report.disputedCount
              ? ` on ${report.disputedCount} disputed ${report.disputedCount === 1 ? 'criterion' : 'criteria'}.`
              : '.'}
            {report.adjudicator.ok === false ? ' It could not settle every dispute; see the tie-break rows below.' : ''}
          </>
        ) : report.disputedCount ? (
          <>
            No adjudicator answered: {report.disputedCount} disputed{' '}
            {report.disputedCount === 1 ? 'criterion' : 'criteria'} fell to the{' '}
            <span className="font-mono">{report.tieBreak}</span> tie-break.
          </>
        ) : (
          'The markers agreed on every criterion; no adjudication was needed.'
        )}
        {report.adjudicator.feedbackSource && report.adjudicator.feedbackSource !== 'merged' ? (
          <> Coaching feedback is one marker&apos;s ({report.adjudicator.feedbackSource.replace(/^marker:/, '')}).</>
        ) : null}
        {artifacts?.adjudication?.url ? (
          <>
            {' '}
            <a
              href={resolveMediaUrl(artifacts.adjudication.url)}
              target="_blank"
              rel="noreferrer"
              className="font-medium text-violet-700 hover:text-violet-900"
            >
              Adjudication record
            </a>
          </>
        ) : null}
      </div>

      {report.degraded ? (
        <div
          role="status"
          className="mt-3 flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900"
        >
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            <span className="font-semibold">Single marker only.</span> {report.degraded.reason} Re-run the assessment to
            retry the panel; the surviving sheet is reused.
          </span>
        </div>
      ) : null}

      {report.warnings.filter((warning) => !report.degraded || !warning.startsWith('Marker ')).map((warning) => (
        <div key={warning} className="mt-2 flex items-start gap-2 text-[11px] text-amber-800">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span>{warning}</span>
        </div>
      ))}
    </div>
  );
}

export function PanelVotes({ report, index }) {
  const [open, setOpen] = useState(false);
  const detail = report?.criteria.get(index);
  if (!report || !detail) return null;

  return (
    <div className="mt-3 rounded-lg border border-slate-200 bg-white px-3 py-2">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <span className={`rounded-full px-2 py-0.5 font-semibold ${TONE_CLASSES[detail.tone] || TONE_CLASSES.neutral}`}>
          {detail.label}
        </span>
        {detail.votes.map((vote, position) => (
          <span
            key={position}
            className={`rounded-full px-2 py-0.5 font-semibold ${voteClasses(vote)}`}
            title={report.markers[position]?.label || ''}
          >
            {report.markers[position]?.initial || position + 1}: {vote}
          </span>
        ))}
        {detail.disputed && detail.sidedWith !== null ? (
          <span className="text-slate-500">sided with {report.markers[detail.sidedWith]?.initial || detail.sidedWith + 1}</span>
        ) : null}
        {detail.disputed && detail.reasons.length ? (
          <button
            type="button"
            onClick={() => setOpen((current) => !current)}
            className="ml-auto inline-flex items-center gap-1 font-medium text-violet-700 hover:text-violet-900"
            aria-expanded={open}
          >
            {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            {open ? 'Hide' : 'Show'} each marker&apos;s reasoning
          </button>
        ) : null}
      </div>
      {open ? (
        <div className="mt-2 grid gap-2 text-xs text-slate-700 sm:grid-cols-2">
          {detail.reasons.map((reason, position) => (
            <div key={position} className="rounded-md bg-slate-50 px-2 py-1.5">
              <div className="text-[11px] font-semibold text-slate-500">
                Marker {report.markers[position]?.initial || position + 1}
                {detail.timestamps[position] && detail.timestamps[position] !== '00:00:00'
                  ? ` · ${detail.timestamps[position]}`
                  : ''}
              </div>
              <div>{reason || 'No reason given.'}</div>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
