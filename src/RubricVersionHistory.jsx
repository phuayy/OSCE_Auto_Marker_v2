import React, { useEffect, useRef, useState } from 'react';
import { History, Eye, EyeOff, Loader2, RotateCcw } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Modal } from '@/components/ui/dialog';
import { LoadingRegion } from '@/components/skeletons.jsx';
import { Skeleton } from '@/components/ui/skeleton';
import { apiJson } from '@/lib/apiFetch';
import { formatWhen } from '@/lib/rubricApi';
import {
  VERSIONS_ENDPOINT,
  canRestoreVersion,
  describeVersionChange,
  isActiveVersion,
  restoreEndpoint,
  restorePrompt,
  summariseVersion,
  versionAuthor,
  versionEndpoint,
} from '@/lib/rubricVersions';

// The rubric page's version history: every version the server recorded, the
// active one first-marked, each viewable, and (for an administrator) any
// retired one restorable. `refreshKey` changes whenever the page changed the
// active rubric, so the list follows an upload, edit or reset made above it.
export default function RubricVersionHistory({ isAdmin = false, refreshKey = 0, onRestored }) {
  const [versions, setVersions] = useState(null);
  const [error, setError] = useState('');
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [preview, setPreview] = useState(null); // { id, rubric } | { id, loading: true } | { id, error }
  const [confirming, setConfirming] = useState(null);
  const [isRestoring, setIsRestoring] = useState(false);
  const [restoreError, setRestoreError] = useState('');
  const cancelRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    setIsRefreshing(true);
    apiJson(VERSIONS_ENDPOINT, { fallbackMessage: 'Failed to load the version history.' })
      .then((body) => {
        if (cancelled) return;
        setVersions(Array.isArray(body.versions) ? body.versions : []);
        setError('');
      })
      .catch((loadError) => {
        if (!cancelled) setError(loadError.message || 'Failed to load the version history.');
      })
      .finally(() => {
        if (!cancelled) setIsRefreshing(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  const activeVersion = (versions || []).find(isActiveVersion) || null;

  async function togglePreview(version) {
    if (preview?.id === version.id) {
      setPreview(null);
      return;
    }
    setPreview({ id: version.id, loading: true });
    try {
      const body = await apiJson(versionEndpoint(version.id), {
        fallbackMessage: 'Failed to load that version.',
      });
      setPreview((current) => (current?.id === version.id ? { id: version.id, rubric: body.version?.rubric || null } : current));
    } catch (previewError) {
      setPreview((current) =>
        current?.id === version.id
          ? { id: version.id, error: previewError.message || 'Failed to load that version.' }
          : current,
      );
    }
  }

  async function confirmRestore() {
    if (!confirming) return;
    setIsRestoring(true);
    setRestoreError('');
    try {
      const body = await apiJson(restoreEndpoint(confirming.id), {
        method: 'POST',
        fallbackMessage: 'Restoring that version failed.',
      });
      setConfirming(null);
      setPreview(null);
      onRestored?.(body, confirming);
    } catch (restoreFailure) {
      setRestoreError(restoreFailure.message || 'Restoring that version failed.');
    } finally {
      setIsRestoring(false);
    }
  }

  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader className="border-b border-slate-100">
        <CardTitle className="flex items-center gap-2 text-base">
          <History className="h-4 w-4 text-cyan-600" aria-hidden="true" />
          Version history
          {isRefreshing && versions ? <Loader2 className="h-3.5 w-3.5 animate-spin text-slate-400" aria-hidden="true" /> : null}
        </CardTitle>
        <CardDescription>
          Every change to the rubric is kept. Scoring always uses the active version
          {isAdmin ? '; restore an earlier one to make it active again.' : '.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-5">
        {error ? (
          <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div>
        ) : versions === null ? (
          <LoadingRegion label="Loading version history">
            <div className="space-y-2">
              {[0, 1, 2].map((row) => (
                <Skeleton key={row} className="h-14 w-full rounded-xl" />
              ))}
            </div>
          </LoadingRegion>
        ) : versions.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50 p-4 text-sm text-slate-500">
            No versions yet. The first upload or edit creates version 1.
          </div>
        ) : (
          <ol className="space-y-2" aria-label="Rubric versions, newest first">
            {versions.map((version) => {
              const active = isActiveVersion(version);
              const author = versionAuthor(version);
              const summary = summariseVersion(version);
              const isPreviewing = preview?.id === version.id;
              return (
                <li
                  key={version.id}
                  className={`rounded-xl border p-3 ${active ? 'border-emerald-200 bg-emerald-50/60' : 'border-slate-200 bg-white'}`}
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-semibold text-slate-800">Version {version.versionNumber}</span>
                        <Badge variant={active ? 'success' : 'neutral'}>{active ? 'Active' : 'Retired'}</Badge>
                        <span className="text-sm text-slate-700">{describeVersionChange(version)}</span>
                      </div>
                      <div className="text-[11px] text-slate-500">
                        {[author ? `by ${author}` : '', formatWhen(version.createdAt), summary].filter(Boolean).join(' · ')}
                        {!active && version.retiredAt ? ` · retired ${formatWhen(version.retiredAt)}` : ''}
                      </div>
                      {version.title ? (
                        <div className="truncate text-xs text-slate-600" title={version.title}>
                          {version.title}
                        </div>
                      ) : null}
                    </div>
                    <div className="flex shrink-0 gap-2">
                      <Button
                        size="sm"
                        variant="ghost"
                        className="gap-1.5"
                        aria-expanded={isPreviewing}
                        onClick={() => togglePreview(version)}
                      >
                        {isPreviewing ? <EyeOff className="h-3.5 w-3.5" aria-hidden="true" /> : <Eye className="h-3.5 w-3.5" aria-hidden="true" />}
                        {isPreviewing ? 'Hide' : 'View'}
                        <span className="sr-only"> version {version.versionNumber}</span>
                      </Button>
                      {canRestoreVersion(version, isAdmin) ? (
                        <Button
                          size="sm"
                          variant="outline"
                          className="gap-1.5"
                          onClick={() => {
                            setRestoreError('');
                            setConfirming(version);
                          }}
                        >
                          <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
                          Restore
                          <span className="sr-only"> version {version.versionNumber}</span>
                        </Button>
                      ) : null}
                    </div>
                  </div>
                  {isPreviewing ? <VersionPreview preview={preview} /> : null}
                </li>
              );
            })}
          </ol>
        )}
      </CardContent>

      {confirming ? (
        <Modal
          onClose={() => (isRestoring ? null : setConfirming(null))}
          labelledBy="restore-rubric-version-title"
          describedBy="restore-rubric-version-body"
          initialFocus={cancelRef}
        >
          <div className="space-y-4 p-6">
            <h2 id="restore-rubric-version-title" className="text-lg font-semibold text-slate-900">
              Restore version {confirming.versionNumber}?
            </h2>
            <p id="restore-rubric-version-body" className="text-sm text-slate-600">
              {restorePrompt(confirming, activeVersion)}
            </p>
            {restoreError ? (
              <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700" role="alert">
                {restoreError}
              </div>
            ) : null}
            <div className="flex justify-end gap-2">
              <Button ref={cancelRef} variant="outline" onClick={() => setConfirming(null)} disabled={isRestoring}>
                Cancel
              </Button>
              <Button className="gap-2" onClick={confirmRestore} disabled={isRestoring}>
                {isRestoring ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <RotateCcw className="h-4 w-4" aria-hidden="true" />}
                Restore version {confirming.versionNumber}
              </Button>
            </div>
          </div>
        </Modal>
      ) : null}
    </Card>
  );
}

function VersionPreview({ preview }) {
  if (preview.loading) {
    return (
      <div className="mt-3 flex items-center gap-2 text-xs text-slate-500" role="status">
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> Loading version…
      </div>
    );
  }
  if (preview.error) {
    return <div className="mt-3 text-xs text-rose-700">{preview.error}</div>;
  }
  const rubric = preview.rubric || {};
  const scale = rubric.scoring_scale || {};
  return (
    <div className="mt-3 space-y-2 border-t border-slate-100 pt-3">
      <div className="text-[11px] text-slate-500">
        Points: {['All', 'Most', 'Some', 'None'].map((label) => `${label} ${scale[label] ?? '—'}`).join(' · ')}
        {rubric.pass_threshold !== undefined ? ` · pass at ${rubric.pass_threshold}/${rubric.max_score ?? '—'}` : ''}
      </div>
      <ol className="space-y-1 text-sm text-slate-700">
        {(rubric.criteria || []).map((criterion) => (
          <li key={criterion.id} className="flex gap-2">
            <span className="w-5 shrink-0 text-right font-semibold text-slate-500">{criterion.id}.</span>
            <span>
              {criterion.label}
              {criterion.indicators?.length ? (
                <span className="text-xs text-slate-500"> ({criterion.indicators.length} indicators)</span>
              ) : null}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}
