import React, { useRef, useState } from 'react';
import { Loader2, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Modal } from '@/components/ui/dialog';
import { apiJson } from '@/lib/apiFetch';
import {
  RESCORE_BRANCH_OPTIONS,
  RESCORE_KEPT_NOTE,
  rescoreEndpoint,
  rescorePayload,
} from '@/lib/rescoreAction';

// Ask which marking to redo, send it, and hand the answer back.
//
// Mounted only while open, so each opening starts with both branches ticked.
// `clips` re-scores every assessed clip of a long recording instead of one
// session. The dialog owns its own request (busy and error states live here, so
// the three places that offer Re-score stay a button and a callback);
// `onDone(response)` runs after the server accepted it, and the caller words
// the notice and refreshes whatever it shows.
export function RescoreDialog({ sessionId, clips = false, onClose, onDone }) {
  const [choice, setChoice] = useState(() =>
    Object.fromEntries(RESCORE_BRANCH_OPTIONS.map(({ branch }) => [branch, true])),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const firstOptionRef = useRef(null);
  const payload = rescorePayload(choice);
  const subject = clips ? 'all clips' : 'this session';

  async function submit() {
    if (!payload || busy) return;
    setBusy(true);
    setError('');
    try {
      const response = await apiJson(rescoreEndpoint(sessionId, { clips }), {
        method: 'POST',
        json: payload,
        fallbackMessage: 'The re-score could not be queued.',
      });
      onDone(response);
    } catch (failure) {
      setError(failure.message || 'The re-score could not be queued.');
      setBusy(false);
    }
  }

  return (
    <Modal
      onClose={() => (busy ? null : onClose())}
      labelledBy="rescore-title"
      describedBy="rescore-description"
      initialFocus={firstOptionRef}
    >
      <div className="space-y-4 p-6">
        <h2 id="rescore-title" className="text-lg font-semibold text-slate-900">
          Re-score {subject}?
        </h2>
        <p id="rescore-description" className="text-sm text-slate-600">
          {RESCORE_KEPT_NOTE}
        </p>
        <fieldset className="space-y-2" disabled={busy}>
          <legend className="text-xs font-semibold text-slate-500">Redo</legend>
          {RESCORE_BRANCH_OPTIONS.map(({ branch, label }, index) => (
            <label key={branch} className="flex items-center gap-2 text-sm text-slate-800">
              <input
                ref={index === 0 ? firstOptionRef : undefined}
                type="checkbox"
                checked={Boolean(choice[branch])}
                onChange={(event) => setChoice((previous) => ({ ...previous, [branch]: event.target.checked }))}
              />
              {label}
            </label>
          ))}
          {!payload ? <p className="text-xs text-slate-500">Choose at least one.</p> : null}
        </fieldset>
        {error ? (
          <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700" role="alert">
            {error}
          </div>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button className="gap-2" onClick={submit} disabled={!payload || busy}>
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
            )}
            Re-score
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export default RescoreDialog;
