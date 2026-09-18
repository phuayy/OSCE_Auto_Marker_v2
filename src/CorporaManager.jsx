import React, { useEffect, useState } from 'react';
import { Loader2, Mic, Sparkles, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ListRowsSkeleton, LoadingRegion } from '@/components/skeletons.jsx';
import { apiJson } from '@/lib/apiFetch';

// Transcription corpora CRUD (list + inline editor), extracted from the
// dashboard modal so the exact same UI serves both the Settings page and the
// upload pre-flight "Manage corpora" modal. Self-contained: owns its own
// fetching; parents that track the corpus picker pass onChanged/onCreated to
// stay in sync.
export default function CorporaManager({ onClose = null, onCreated = null, onChanged = null, titleId }) {
  const [corpora, setCorpora] = useState([]);
  // False until the first list response, success or failure. `corpora` starts
  // empty, so without this the card said "No corpora yet" for the whole first
  // fetch — a false empty state, not a wait.
  const [hasLoaded, setHasLoaded] = useState(false);
  // null = list view; {id: null} = creating; {id} = editing that corpus.
  const [editor, setEditor] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  async function refresh({ notify = true } = {}) {
    try {
      const body = await apiJson('/api/corpora', { fallbackMessage: 'Failed to load corpora.' });
      const list = Array.isArray(body.corpora) ? body.corpora : [];
      setCorpora(list);
      if (notify && typeof onChanged === 'function') {
        onChanged(list);
      }
    } catch (loadError) {
      setError(loadError.message || 'Failed to load corpora.');
    } finally {
      setHasLoaded(true);
    }
  }

  useEffect(() => {
    refresh({ notify: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function openEditor(corpus = null) {
    setError('');
    setEditor(
      corpus
        ? { id: corpus.id, name: corpus.name || '', termsText: (corpus.terms || []).join('\n') }
        : { id: null, name: '', termsText: '' },
    );
  }

  async function saveEditor() {
    if (!editor) return;
    const name = editor.name.trim();
    if (!name) {
      setError('Corpus name is required.');
      return;
    }
    const terms = editor.termsText
      .split('\n')
      .map((term) => term.trim())
      .filter(Boolean);
    setSaving(true);
    setError('');
    try {
      const body = await apiJson(editor.id ? `/api/admin/corpora/${editor.id}` : '/api/admin/corpora', {
        method: editor.id ? 'PUT' : 'POST',
        json: { name, terms },
        fallbackMessage: 'Failed to save corpus.',
      });
      if (body.corpus?.id && !editor.id && typeof onCreated === 'function') {
        onCreated(body.corpus);
      }
      setEditor(null);
      await refresh();
    } catch (saveError) {
      setError(saveError.message || 'Failed to save corpus.');
    } finally {
      setSaving(false);
    }
  }

  async function removeCorpus(corpus) {
    if (!window.confirm(`Delete corpus "${corpus.name}"? Sessions already created keep their own copy of the terms.`)) {
      return;
    }
    setError('');
    try {
      await apiJson(`/api/admin/corpora/${corpus.id}`, {
        method: 'DELETE',
        fallbackMessage: 'Failed to delete corpus.',
      });
      await refresh();
    } catch (deleteError) {
      setError(deleteError.message || 'Failed to delete corpus.');
    }
  }

  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle id={titleId} className="flex items-center gap-2 text-lg">
          <Mic className="h-5 w-5 text-cyan-700" aria-hidden="true" />
          {editor ? (editor.id ? 'Edit corpus' : 'New corpus') : 'Transcription corpora'}
        </CardTitle>
        <CardDescription>
          Term lists that bias transcription towards case-specific vocabulary. Sessions keep a
          copy of the terms they were created with, so edits never change past results.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {error && (
          <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
            {error}
          </div>
        )}
        {editor ? (
          <>
            <div>
              <label htmlFor="corpus-name-input" className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                Corpus name
              </label>
              <input
                id="corpus-name-input"
                type="text"
                value={editor.name}
                maxLength={120}
                autoFocus
                onChange={(event) => setEditor({ ...editor, name: event.target.value })}
                placeholder="e.g. Asthma (Adult)"
                className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 outline-none focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100"
              />
            </div>
            <div>
              <label htmlFor="corpus-terms-input" className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                Terms — one per line
              </label>
              <textarea
                id="corpus-terms-input"
                value={editor.termsText}
                onChange={(event) => setEditor({ ...editor, termsText: event.target.value })}
                rows={10}
                placeholder={'nasal block\nrunny nose\nparacetamol'}
                className="mt-1 w-full resize-y rounded-lg border border-slate-300 bg-white px-3 py-2 font-mono text-xs text-slate-800 outline-none focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100"
              />
              <p className="mt-1 text-xs text-slate-500">
                Up to 200 terms; symptoms, medicines and phrases from this case study's rubric work best.
              </p>
            </div>
            <div className="flex items-center justify-end gap-2 pt-1">
              <Button variant="outline" size="sm" onClick={() => setEditor(null)} disabled={saving}>
                Back
              </Button>
              <Button size="sm" className="gap-2" onClick={saveEditor} disabled={saving}>
                {saving && <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
                Save corpus
              </Button>
            </div>
          </>
        ) : (
          <>
            <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
              {!hasLoaded ? (
                <LoadingRegion label="Loading corpora">
                  <ListRowsSkeleton rows={2} />
                </LoadingRegion>
              ) : null}
              {hasLoaded && corpora.length === 0 && (
                <div className="rounded-xl border border-dashed border-slate-300 p-4 text-center text-sm text-slate-500">
                  No corpora yet. Create one for this case study.
                </div>
              )}
              {corpora.map((corpus) => (
                <div
                  key={corpus.id}
                  className="flex items-center justify-between gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-slate-800">{corpus.name}</div>
                    <div className="text-[11px] text-slate-500">{(corpus.terms || []).length} terms</div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <Button variant="outline" size="sm" onClick={() => openEditor(corpus)}>
                      Edit
                    </Button>
                    <Button
                      variant="destructive"
                      size="sm"
                      aria-label={`Delete corpus ${corpus.name}`}
                      onClick={() => removeCorpus(corpus)}
                    >
                      <Trash2 className="h-4 w-4" aria-hidden="true" />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
            <div className="flex items-center justify-between gap-2 pt-1">
              {onClose ? (
                <Button variant="outline" size="sm" onClick={onClose}>
                  Close
                </Button>
              ) : (
                <span />
              )}
              <Button size="sm" className="gap-2" onClick={() => openEditor()}>
                <Sparkles className="h-4 w-4" aria-hidden="true" />
                New corpus
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
