// Small presentational pieces shared by the session workspace's panels.
//
// They live in the workspace folder rather than components/ui because they are
// this view's vocabulary, not the design system's: a labelled row in the Run
// Status card, a Keep/Start/Stop block, the empty state an examiner sees when
// a sheet was never produced.
import React from 'react';
import { FEEDBACK_NOT_PROVIDED, contentSheetEmptyCopy } from '@/lib/scoreSheet';

// Empty state for the Content Scores and Feedback tabs when the session has no
// content sheet. The copy says why (failed run, demo bundle, not produced yet)
// and never stands in for the sheet with template numbers or canned sentences:
// an examiner must be able to trust everything these tabs show.
export function ContentSheetEmptyState({ state, error, subject }) {
  const copy = contentSheetEmptyCopy(state, { error, subject });
  return (
    <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/90 p-6 text-center shadow-inner">
      <p className="text-sm font-medium text-slate-700">{copy.title}</p>
      {copy.hint ? <p className="mt-2 text-xs text-slate-500">{copy.hint}</p> : null}
      {copy.detail ? (
        <p className="mt-2 rounded border border-rose-100 bg-rose-50 px-2 py-1 text-[11px] leading-snug text-rose-700">
          {copy.detail}
        </p>
      ) : null}
    </div>
  );
}

// A Keep/Start/Stop block. A field the model left empty is said to be empty,
// in muted text; the view never substitutes a sentence of its own.
export function FeedbackBlock({ title, lines }) {
  const provided = lines.map((line) => String(line || '').trim()).filter(Boolean);
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
      <div className="mb-2 text-sm font-semibold text-slate-900">{title}</div>
      <div className="space-y-1.5 text-sm text-slate-800">
        {provided.length ? (
          provided.map((line) => <div key={line}>- {line}</div>)
        ) : (
          <div className="text-sm italic text-slate-500">{FEEDBACK_NOT_PROVIDED}</div>
        )}
      </div>
    </div>
  );
}

export function StatusRow({ label, value }) {
  return (
    <div className="flex items-center justify-between rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
      <span className="text-slate-600">{label}</span>
      <span className="max-w-[60%] truncate font-medium text-slate-900" title={value}>
        {value}
      </span>
    </div>
  );
}

export function Metric({ label, value }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-2xl font-bold text-slate-900">{value}</div>
    </div>
  );
}

export function IndicatorList({ title, items, color = 'text-slate-700' }) {
  if (!items?.length) {
    return null;
  }
  return (
    <div>
      <div className={`text-[11px] font-semibold uppercase tracking-wide ${color}`}>{title}</div>
      <ul className="mt-1 space-y-0.5 pl-3">
        {items.map((item, index) => (
          <li key={`${title}-${index}`} className="list-disc">
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}
