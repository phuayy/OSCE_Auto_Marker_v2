import React, { useEffect, useState } from 'react';
import { AlertCircle, CheckCircle2, Copy, Loader2, Send, Trash2, Webhook } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { LoadingRegion, WebhookRowsSkeleton } from '@/components/skeletons.jsx';
import { apiJson } from '@/lib/apiFetch';

// Outbound webhook subscriptions: register an external HTTPS endpoint and the
// backend POSTs a signed JSON payload to it whenever a notification is raised.
//
// This is the *outbound* half of the notification system. The browser itself is
// updated by server push over the change stream (see changeStream.js) and needs
// no webhook; these exist to reach systems outside the app — a Slack relay, a
// departmental dashboard, a marking-records service.

const EVENT_LABELS = {
  'scoring.completed': 'Scoring complete',
  'clips.ready': 'Clips ready',
  'session.failed': 'Processing failed',
};

function eventLabel(type) {
  return EVENT_LABELS[type] || type;
}

function relativeTime(iso) {
  if (!iso) return 'never';
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return 'never';
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Date(iso).toLocaleString();
}

/** Health chip derived from the denormalised last-delivery summary. */
function StatusChip({ webhook }) {
  if (!webhook.active) {
    return <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[10px] font-semibold text-slate-600">Paused</span>;
  }
  if (webhook.consecutiveFailures > 0) {
    return (
      <span className="rounded-full bg-rose-100 px-2 py-0.5 text-[10px] font-semibold text-rose-700">
        {webhook.consecutiveFailures} failed
      </span>
    );
  }
  if (webhook.lastDeliveryAt) {
    return <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-semibold text-emerald-700">Healthy</span>;
  }
  return <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-500">No deliveries</span>;
}

/**
 * The one-time secret reveal.
 *
 * The server returns the plaintext signing secret only in the create/rotate
 * response — every later read is masked — so this panel is the single chance to
 * copy it. It says so explicitly rather than letting the user discover it later.
 */
function SecretReveal({ secret, onDismiss }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard is permission-gated; the value is selectable as a fallback.
    }
  }

  return (
    <div className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3">
      <div className="flex items-center gap-2 text-sm font-semibold text-amber-900">
        <AlertCircle className="h-4 w-4" />
        Copy this signing secret now
      </div>
      <p className="mt-1 text-xs leading-relaxed text-amber-800">
        It is shown once and cannot be retrieved afterwards. Your endpoint needs it to verify the{' '}
        <code className="rounded bg-amber-100 px-1">X-OSCE-Signature</code> header.
      </p>
      <div className="mt-2 flex items-center gap-2">
        <code className="min-w-0 flex-1 overflow-x-auto rounded-lg border border-amber-200 bg-white px-2 py-1.5 text-[11px] text-slate-800">
          {secret}
        </code>
        <Button variant="outline" size="sm" className="gap-1.5 shrink-0" onClick={copy}>
          <Copy className="h-3.5 w-3.5" />
          {copied ? 'Copied' : 'Copy'}
        </Button>
        <Button variant="ghost" size="sm" className="shrink-0" onClick={onDismiss}>
          Done
        </Button>
      </div>
    </div>
  );
}

export default function WebhooksManager() {
  const [webhooks, setWebhooks] = useState([]);
  const [eventTypes, setEventTypes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  // null = list view; {id: null} = creating; {id} = editing that subscription.
  const [editor, setEditor] = useState(null);
  const [saving, setSaving] = useState(false);
  const [revealedSecret, setRevealedSecret] = useState(null);
  const [testing, setTesting] = useState('');
  const [testResult, setTestResult] = useState(null);
  // subscriptionId -> delivery rows, loaded on demand.
  const [deliveries, setDeliveries] = useState({});

  async function refresh() {
    setError('');
    try {
      const body = await apiJson('/api/webhooks', { fallbackMessage: 'Failed to load webhooks.' });
      setWebhooks(Array.isArray(body.webhooks) ? body.webhooks : []);
      setEventTypes(Array.isArray(body.eventTypes) ? body.eventTypes : []);
    } catch (loadError) {
      setError(loadError.message || 'Failed to load webhooks.');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function openEditor(webhook = null) {
    setError('');
    setTestResult(null);
    setEditor(
      webhook
        ? {
            id: webhook.id,
            url: webhook.url || '',
            description: webhook.description || '',
            // An empty stored filter means "all events"; render that as every
            // box ticked so the UI states what will actually happen.
            eventTypes:
              !webhook.eventTypes?.length || webhook.eventTypes.includes('*')
                ? [...eventTypes]
                : [...webhook.eventTypes],
            active: webhook.active !== false,
          }
        : { id: null, url: '', description: '', eventTypes: [...eventTypes], active: true },
    );
  }

  function toggleEventType(type) {
    setEditor((current) => {
      if (!current) return current;
      const selected = current.eventTypes.includes(type)
        ? current.eventTypes.filter((item) => item !== type)
        : [...current.eventTypes, type];
      return { ...current, eventTypes: selected };
    });
  }

  async function saveEditor() {
    if (!editor) return;
    const url = editor.url.trim();
    if (!url) {
      setError('Endpoint URL is required.');
      return;
    }
    if (editor.eventTypes.length === 0) {
      setError('Select at least one event to send.');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const body = await apiJson(editor.id ? `/api/webhooks/${editor.id}` : '/api/webhooks', {
        method: editor.id ? 'PUT' : 'POST',
        json: {
          url,
          description: editor.description.trim(),
          // Every box ticked is semantically the wildcard, and storing it that
          // way means a future event type is included automatically.
          eventTypes: editor.eventTypes.length === eventTypes.length ? ['*'] : editor.eventTypes,
          active: editor.active,
        },
        fallbackMessage: 'Failed to save webhook.',
      });
      if (body.webhook?.secret) {
        setRevealedSecret(body.webhook.secret);
      }
      setEditor(null);
      await refresh();
    } catch (saveError) {
      setError(saveError.message || 'Failed to save webhook.');
    } finally {
      setSaving(false);
    }
  }

  async function removeWebhook(webhook) {
    if (!window.confirm(`Delete webhook for ${webhook.url}? Its delivery history is removed too.`)) {
      return;
    }
    setError('');
    try {
      await apiJson(`/api/webhooks/${webhook.id}`, {
        method: 'DELETE',
        fallbackMessage: 'Failed to delete webhook.',
      });
      await refresh();
    } catch (deleteError) {
      setError(deleteError.message || 'Failed to delete webhook.');
    }
  }

  async function rotateSecret(webhook) {
    if (
      !window.confirm(
        `Rotate the signing secret for ${webhook.url}?\n\nThe current secret stops working immediately — update your endpoint before the next event.`,
      )
    ) {
      return;
    }
    setError('');
    try {
      const body = await apiJson(`/api/webhooks/${webhook.id}/rotate-secret`, {
        method: 'POST',
        fallbackMessage: 'Failed to rotate secret.',
      });
      setRevealedSecret(body.webhook?.secret || null);
      await refresh();
    } catch (rotateError) {
      setError(rotateError.message || 'Failed to rotate secret.');
    }
  }

  async function sendTest(webhook) {
    setTesting(webhook.id);
    setTestResult(null);
    setError('');
    try {
      const body = await apiJson(`/api/webhooks/${webhook.id}/test`, {
        method: 'POST',
        fallbackMessage: 'Test delivery failed.',
      });
      setTestResult({ id: webhook.id, delivered: Boolean(body.delivered) });
      await refresh();
      // The attempt is logged either way, so show what the endpoint did.
      await loadDeliveries(webhook.id);
    } catch (testError) {
      setError(testError.message || 'Test delivery failed.');
    } finally {
      setTesting('');
    }
  }

  async function loadDeliveries(webhookId) {
    try {
      const body = await apiJson(`/api/webhooks/${webhookId}/deliveries?limit=10`);
      setDeliveries((current) => ({ ...current, [webhookId]: body.deliveries || [] }));
    } catch {
      // Non-fatal: the log is a debugging aid, not required for operation.
    }
  }

  function toggleDeliveries(webhookId) {
    if (deliveries[webhookId]) {
      setDeliveries((current) => {
        const next = { ...current };
        delete next[webhookId];
        return next;
      });
      return;
    }
    loadDeliveries(webhookId);
  }

  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <Webhook className="h-5 w-5 text-cyan-700" />
          Outbound Webhooks
        </CardTitle>
        <CardDescription>
          Send task events to an external system. Each delivery is signed with HMAC-SHA256 in the{' '}
          <code className="rounded bg-slate-100 px-1">X-OSCE-Signature</code> header so your endpoint can verify
          it came from here. Failed deliveries are retried with backoff; the in-app notification bell does not
          use these.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error ? (
          <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">{error}</div>
        ) : null}

        {revealedSecret ? (
          <SecretReveal secret={revealedSecret} onDismiss={() => setRevealedSecret(null)} />
        ) : null}

        {editor ? (
          <div className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-slate-50 p-4">
            <div className="text-sm font-semibold text-slate-800">
              {editor.id ? 'Edit webhook' : 'New webhook'}
            </div>

            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-600">Endpoint URL</span>
              <input
                type="url"
                value={editor.url}
                onChange={(event) => setEditor({ ...editor, url: event.target.value })}
                placeholder="https://example.com/hooks/osce"
                className="rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-cyan-500"
              />
              <span className="text-[11px] text-slate-500">
                Must be reachable from the server. Private and loopback addresses are refused unless
                WEBHOOK_ALLOW_PRIVATE_URLS is enabled.
              </span>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-600">Description (optional)</span>
              <input
                type="text"
                value={editor.description}
                onChange={(event) => setEditor({ ...editor, description: event.target.value })}
                placeholder="Slack #osce-results relay"
                className="rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-cyan-500"
              />
            </label>

            <fieldset className="flex flex-col gap-1.5">
              <legend className="text-xs font-medium text-slate-600">Events to send</legend>
              {eventTypes.map((type) => (
                <label key={type} className="flex items-center gap-2 text-sm text-slate-700">
                  <input
                    type="checkbox"
                    checked={editor.eventTypes.includes(type)}
                    onChange={() => toggleEventType(type)}
                    className="h-4 w-4 rounded border-slate-300"
                  />
                  {eventLabel(type)}
                  <code className="text-[10px] text-slate-400">{type}</code>
                </label>
              ))}
            </fieldset>

            <label className="flex items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={editor.active}
                onChange={(event) => setEditor({ ...editor, active: event.target.checked })}
                className="h-4 w-4 rounded border-slate-300"
              />
              Active
            </label>

            <div className="flex gap-2">
              <Button size="sm" onClick={saveEditor} disabled={saving} className="gap-1.5">
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                {editor.id ? 'Save changes' : 'Create webhook'}
              </Button>
              <Button variant="outline" size="sm" onClick={() => setEditor(null)} disabled={saving}>
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <div>
            <Button size="sm" onClick={() => openEditor()} className="gap-1.5">
              <Webhook className="h-3.5 w-3.5" />
              Add webhook
            </Button>
          </div>
        )}

        {loading ? (
          <LoadingRegion label="Loading webhooks">
            <WebhookRowsSkeleton />
          </LoadingRegion>
        ) : webhooks.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-200 px-4 py-6 text-center text-sm text-slate-500">
            No webhooks yet. Add one to forward task events to another system.
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {webhooks.map((webhook) => (
              <div key={webhook.id} className="rounded-xl border border-slate-200 px-4 py-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-semibold text-slate-800">{webhook.url}</span>
                      <StatusChip webhook={webhook} />
                    </div>
                    {webhook.description ? (
                      <div className="mt-0.5 text-xs text-slate-500">{webhook.description}</div>
                    ) : null}
                    <div className="mt-1 flex flex-wrap gap-1">
                      {(webhook.eventTypes?.length ? webhook.eventTypes : ['*']).map((type) => (
                        <span
                          key={type}
                          className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-600"
                        >
                          {type === '*' ? 'All events' : eventLabel(type)}
                        </span>
                      ))}
                    </div>
                    <div className="mt-1 text-[11px] text-slate-400">
                      Secret {webhook.secretPreview} · last delivery {relativeTime(webhook.lastDeliveryAt)}
                      {webhook.lastStatusCode ? ` · HTTP ${webhook.lastStatusCode}` : ''}
                    </div>
                    {webhook.lastError ? (
                      <div className="mt-1 text-[11px] text-rose-600">{webhook.lastError}</div>
                    ) : null}
                  </div>

                  <div className="flex shrink-0 flex-wrap gap-1.5">
                    <Button
                      variant="outline"
                      size="sm"
                      className="gap-1.5"
                      onClick={() => sendTest(webhook)}
                      disabled={testing === webhook.id}
                    >
                      {testing === webhook.id ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Send className="h-3.5 w-3.5" />
                      )}
                      Test
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => openEditor(webhook)}>
                      Edit
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => rotateSecret(webhook)}>
                      Rotate secret
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => toggleDeliveries(webhook.id)}>
                      {deliveries[webhook.id] ? 'Hide log' : 'Log'}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      className="text-rose-600"
                      onClick={() => removeWebhook(webhook)}
                      aria-label={`Delete webhook ${webhook.url}`}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>

                {testResult?.id === webhook.id ? (
                  <div
                    className={`mt-2 flex items-center gap-1.5 text-xs ${
                      testResult.delivered ? 'text-emerald-700' : 'text-rose-700'
                    }`}
                  >
                    {testResult.delivered ? (
                      <CheckCircle2 className="h-3.5 w-3.5" />
                    ) : (
                      <AlertCircle className="h-3.5 w-3.5" />
                    )}
                    {testResult.delivered
                      ? 'Test event delivered.'
                      : 'Test event was not accepted — see the log below.'}
                  </div>
                ) : null}

                {deliveries[webhook.id] ? (
                  <div className="mt-2 overflow-x-auto rounded-lg border border-slate-100">
                    {deliveries[webhook.id].length === 0 ? (
                      <div className="px-3 py-2 text-xs text-slate-500">No deliveries recorded yet.</div>
                    ) : (
                      <table className="w-full text-left text-[11px]">
                        <thead className="bg-slate-50 text-slate-500">
                          <tr>
                            <th className="px-3 py-1.5 font-medium">When</th>
                            <th className="px-3 py-1.5 font-medium">Event</th>
                            <th className="px-3 py-1.5 font-medium">Try</th>
                            <th className="px-3 py-1.5 font-medium">Result</th>
                          </tr>
                        </thead>
                        <tbody>
                          {deliveries[webhook.id].map((row) => (
                            <tr key={row.id} className="border-t border-slate-100">
                              <td className="px-3 py-1.5 text-slate-500">{relativeTime(row.createdAt)}</td>
                              <td className="px-3 py-1.5 text-slate-700">{row.eventType}</td>
                              <td className="px-3 py-1.5 text-slate-500">#{row.attempt}</td>
                              <td
                                className={`px-3 py-1.5 ${
                                  row.status === 'succeeded' ? 'text-emerald-700' : 'text-rose-700'
                                }`}
                              >
                                {row.statusCode ? `HTTP ${row.statusCode}` : row.status}
                                {row.error ? ` — ${row.error}` : ''}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
