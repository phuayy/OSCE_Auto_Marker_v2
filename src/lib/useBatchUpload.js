// React binding for lib/batchUpload.js: owns the queue's state for the
// dashboard. It is called by the dashboard itself (not the panel) so a batch
// keeps running — and keeps reporting — while the user opens a session and the
// upload form is unmounted.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { apiJson } from './apiFetch.js';
import { ITEM_STATUS, planBatch, resetFailed, runBatch, summarizeBatch } from './batchUpload.js';
import { submitUpload } from './uploadSubmission.js';

const IDLE_STATUSES = new Set([ITEM_STATUS.WAITING, ITEM_STATUS.FAILED, ITEM_STATUS.SKIPPED]);

export function useBatchUpload({ enabled, caseStudy, buildRequest, tracker, transports, onSessionCreated, onItemFinished }) {
  const [limits, setLimits] = useState(null);
  const [limitsError, setLimitsError] = useState('');
  const [limitsAttempt, setLimitsAttempt] = useState(0);
  const [items, setItems] = useState([]);
  const [rejected, setRejected] = useState([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState('');
  const itemsRef = useRef(items);
  const controllerRef = useRef(null);
  // The latest of these at the time a run starts; a run keeps what it began with.
  const optionsRef = useRef({});
  optionsRef.current = { caseStudy, buildRequest, tracker, transports, onSessionCreated, onItemFinished, limits };

  const commit = useCallback((next) => {
    itemsRef.current = next;
    setItems(next);
  }, []);

  useEffect(() => {
    if (!enabled || limits) return undefined;
    let cancelled = false;
    apiJson('/api/uploads/limits', { fallbackMessage: 'Could not load the upload limits.' })
      .then((body) => {
        if (!cancelled) {
          setLimits(body);
          setLimitsError('');
        }
      })
      .catch((failure) => {
        if (!cancelled) setLimitsError(failure.message);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, limits, limitsAttempt]);

  const addFiles = useCallback(
    (files) => {
      if (!limits || running) return;
      const planned = planBatch(files, limits, itemsRef.current);
      commit(planned.items);
      setRejected(planned.rejected);
    },
    [limits, running, commit],
  );

  const rename = useCallback(
    (id, name) => commit(itemsRef.current.map((item) => (item.id === id ? { ...item, name } : item))),
    [commit],
  );

  const remove = useCallback(
    (id) => commit(itemsRef.current.filter((item) => item.id !== id || !IDLE_STATUSES.has(item.status))),
    [commit],
  );

  const clear = useCallback(() => {
    if (controllerRef.current) return;
    commit([]);
    setRejected([]);
    setError('');
  }, [commit]);

  const run = useCallback(
    async (queue) => {
      if (controllerRef.current) return;
      const options = optionsRef.current;
      const controller = new AbortController();
      controllerRef.current = controller;
      setRunning(true);
      setError('');
      setRejected([]);
      try {
        const result = await runBatch({
          items: queue,
          caseStudy: options.caseStudy,
          request: options.buildRequest(),
          limits: options.limits,
          submitUpload,
          api: { apiJson },
          tracker: options.tracker,
          transports: options.transports,
          signal: controller.signal,
          onChange: commit,
          onSessionCreated: (session) => options.onSessionCreated?.(session),
          onItemFinished: (item) => options.onItemFinished?.(item),
        });
        setError(result.error);
      } finally {
        controllerRef.current = null;
        setRunning(false);
      }
    },
    [commit],
  );

  const start = useCallback(() => run(itemsRef.current), [run]);
  const retryFailed = useCallback(() => run(resetFailed(itemsRef.current)), [run]);
  const cancel = useCallback(() => controllerRef.current?.abort(), []);

  const reloadLimits = useCallback(() => setLimitsAttempt((n) => n + 1), []);

  const summary = useMemo(() => summarizeBatch(items, { caseStudySize: caseStudy?.size || 0 }), [items, caseStudy]);

  return { limits, limitsError, items, rejected, running, error, summary, reloadLimits, addFiles, rename, remove, clear, start, retryFailed, cancel };
}
