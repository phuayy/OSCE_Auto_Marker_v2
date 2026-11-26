import React, { useRef, useState } from 'react';
import { AnimatePresence } from 'framer-motion';
import { Download, Loader2, RotateCw, Trash2, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Modal } from '@/components/ui/dialog';
import { apiFetch, apiJson, readErrorMessage } from '@/lib/apiFetch';
import { downloadBlob } from '@/lib/download';
import {
  BulkAction,
  bulkAvailability,
  eligibleIds,
  exportFileName,
  exportUrl,
  summarizeBulkResult,
} from '@/lib/sessionSelection';

const TONE_CLASSES = {
  success: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  warning: 'border-amber-200 bg-amber-50 text-amber-800',
  danger: 'border-rose-200 bg-rose-50 text-rose-700',
};

// The bar that appears over the Saved sessions list while rows are selected.
// What each button may do is lib/sessionSelection.js's decision; this runs the
// request, shows what the server said per session, and reports back which ids
// were dealt with so the dashboard can clear them and refresh the list once.
export function BulkActionBar({ entries, identity, onClear, onSettled }) {
  const [busy, setBusy] = useState('');
  const [outcome, setOutcome] = useState(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const cancelRef = useRef(null);
  const namesRef = useRef(new Map());

  const availability = bulkAvailability(entries, identity);
  const deleteCount = availability[BulkAction.DELETE].count;

  if (entries.length === 0 && !outcome) return null;

  async function runBulk(action) {
    const ids = eligibleIds(action, entries, identity);
    if (ids.length === 0 || busy) return;
    namesRef.current = new Map(entries.map((entry) => [entry.id, entry.name || entry.id]));
    setBusy(action);
    setOutcome(null);
    try {
      const response = await apiJson('/api/sessions/bulk', {
        method: 'POST',
        json: { action, sessionIds: ids },
        fallbackMessage: 'The bulk request failed.',
      });
      const summary = summarizeBulkResult(response, action);
      setOutcome(summary);
      onSettled(summary.succeededIds);
    } catch (error) {
      // The whole request was refused (offline, rate limited): nothing ran,
      // so nothing is cleared from the selection.
      setOutcome({ failures: [], tone: 'danger', message: error.message || 'The bulk request failed.' });
    } finally {
      setBusy('');
    }
  }

  async function exportResults() {
    const ids = eligibleIds(BulkAction.EXPORT, entries, identity);
    if (ids.length === 0 || busy) return;
    setBusy(BulkAction.EXPORT);
    setOutcome(null);
    try {
      const response = await apiFetch(exportUrl(ids));
      if (!response.ok) {
        let body = null;
        try { body = await response.json(); } catch { /* a non-JSON refusal */ }
        throw new Error(readErrorMessage(body, 'The export failed.'));
      }
      const blob = await response.blob();
      downloadBlob(exportFileName(response.headers.get('Content-Disposition')), blob);
      setOutcome({ failures: [], tone: 'success', message: `Exported results for ${ids.length === 1 ? '1 session' : `${ids.length} sessions`}.` });
    } catch (error) {
      setOutcome({ failures: [], tone: 'danger', message: error.message || 'The export failed.' });
    } finally {
      setBusy('');
    }
  }

  const buttons = [
    { action: BulkAction.RERUN, label: 'Re-run failed', icon: RotateCw, variant: 'outline', run: () => runBulk(BulkAction.RERUN) },
    { action: BulkAction.EXPORT, label: 'Export results', icon: Download, variant: 'outline', run: exportResults },
    { action: BulkAction.DELETE, label: 'Delete', icon: Trash2, variant: 'destructive', run: () => setConfirmingDelete(true) },
  ];

  return (
    <div className="space-y-2 rounded-xl border border-cyan-200 bg-cyan-50 p-3" role="region" aria-label="Bulk actions">
      {entries.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-slate-800">{entries.length} selected</span>
          <div className="flex flex-wrap items-center gap-2">
            {buttons.map(({ action, label, icon: Icon, variant, run }) => {
              const state = availability[action];
              return (
                <Button
                  key={action}
                  size="sm"
                  variant={variant}
                  disabled={!state.allowed || Boolean(busy)}
                  title={state.reason || undefined}
                  onClick={run}
                >
                  {busy === action ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : <Icon className="mr-1.5 h-3.5 w-3.5" aria-hidden="true" />}
                  {label} ({state.count})
                </Button>
              );
            })}
          </div>
          <Button size="sm" variant="ghost" className="ml-auto" onClick={onClear} disabled={Boolean(busy)}>
            Clear selection
          </Button>
        </div>
      ) : null}

      {outcome ? (
        <div role="status" className={`rounded-lg border p-2 text-xs ${TONE_CLASSES[outcome.tone] || TONE_CLASSES.danger}`}>
          <div className="flex items-start justify-between gap-2">
            <span className="font-medium">{outcome.message}</span>
            <button
              type="button"
              onClick={() => setOutcome(null)}
              aria-label="Dismiss result"
              className="rounded p-0.5 hover:bg-black/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
          {outcome.failures.length > 0 ? (
            <ul className="mt-1 list-disc space-y-0.5 pl-4">
              {outcome.failures.map((failure) => (
                <li key={failure.sessionId}>
                  {namesRef.current.get(failure.sessionId) || failure.sessionId}: {failure.error}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      <AnimatePresence>
        {confirmingDelete ? (
          <Modal
            onClose={() => setConfirmingDelete(false)}
            labelledBy="bulk-delete-title"
            describedBy="bulk-delete-description"
            initialFocus={cancelRef}
          >
            <Card className="border-slate-200 bg-white shadow-xl">
              <CardHeader>
                <CardTitle id="bulk-delete-title" className="text-lg">
                  Delete {deleteCount === 1 ? '1 session' : `${deleteCount} sessions`}?
                </CardTitle>
                <CardDescription id="bulk-delete-description">
                  This permanently removes their transcripts, scores and files. For a long recording, the
                  clip assessments under it are deleted too. This cannot be undone.
                </CardDescription>
              </CardHeader>
              <CardContent className="flex justify-end gap-2">
                <Button ref={cancelRef} variant="outline" onClick={() => setConfirmingDelete(false)}>
                  Cancel
                </Button>
                <Button
                  variant="destructive"
                  onClick={() => {
                    setConfirmingDelete(false);
                    runBulk(BulkAction.DELETE);
                  }}
                >
                  Delete {deleteCount === 1 ? 'session' : `${deleteCount} sessions`}
                </Button>
              </CardContent>
            </Card>
          </Modal>
        ) : null}
      </AnimatePresence>
    </div>
  );
}

export default BulkActionBar;
