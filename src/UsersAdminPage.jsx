import React, { useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence } from 'framer-motion';
import {
  AlertCircle,
  Ban,
  CheckCircle2,
  Copy,
  KeyRound,
  Mail,
  RefreshCcw,
  Send,
  Trash2,
  UserCheck,
  UserPlus,
  Users,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Modal } from '@/components/ui/dialog';
import { PageHeader } from '@/components/PageHeader.jsx';
import { InviteFormSkeleton, LoadingRegion, UserRowsSkeleton } from '@/components/skeletons.jsx';
import { apiJson } from '@/lib/apiFetch';
import { UserRole } from '@/lib/enums';
import { roleLabel } from '@/lib/authz';
import { countActiveAdmins, describeDelivery, describeUserStatus, relativeTime, rowActions } from '@/lib/userAdmin';

// Account administration (#/users), administrators only.
//
// The API is the authority on every rule — who may be disabled, who is the
// last admin, whether a link may be shown — and answers each action with the
// row that changed, so this page never guesses: it renders the listing, offers
// only what lib/userAdmin.js says a row allows, and replaces one row from the
// response. The listing is not on the change stream; it is re-read after an
// action that may have moved more than one row (a role change can turn the
// "last admin" rule on or off for everyone).

const FIELD =
  'w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm placeholder:text-slate-400 focus:border-cyan-500 focus:outline-none focus:ring-2 focus:ring-cyan-500/30 disabled:opacity-60';

function ToneBanner({ tone = 'neutral', title, detail, children }) {
  const classes = {
    success: 'border-emerald-200 bg-emerald-50 text-emerald-800',
    warning: 'border-amber-300 bg-amber-50 text-amber-900',
    danger: 'border-rose-200 bg-rose-50 text-rose-800',
    neutral: 'border-slate-200 bg-slate-50 text-slate-700',
  }[tone];
  const Icon = tone === 'success' ? CheckCircle2 : AlertCircle;
  return (
    <div role="status" className={`rounded-xl border px-4 py-3 text-sm ${classes}`}>
      <div className="flex items-start gap-2">
        <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="font-semibold">{title}</div>
          {detail ? <p className="mt-0.5 text-xs leading-relaxed opacity-90">{detail}</p> : null}
          {children}
        </div>
      </div>
    </div>
  );
}

/**
 * The one-time link panel: shown when the deployment has no mail relay and the
 * server handed the invitation (or reset) link back to be passed on by hand.
 */
function LinkReveal({ link, onDismiss }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard is permission-gated; the value is selectable as a fallback.
    }
  }

  return (
    <div className="mt-2 flex items-center gap-2">
      <code className="min-w-0 flex-1 overflow-x-auto rounded-lg border border-amber-200 bg-white px-2 py-1.5 text-[11px] text-slate-800">
        {link}
      </code>
      <Button variant="outline" size="sm" className="shrink-0 gap-1.5" onClick={copy}>
        <Copy className="h-3.5 w-3.5" aria-hidden="true" />
        {copied ? 'Copied' : 'Copy'}
      </Button>
      <Button variant="ghost" size="sm" className="shrink-0" onClick={onDismiss}>
        Done
      </Button>
    </div>
  );
}

function InviteCard({ mail, busy, onInvite }) {
  const [email, setEmail] = useState('');
  const [role, setRole] = useState(UserRole.MARKER);
  const [displayName, setDisplayName] = useState('');
  const [error, setError] = useState('');
  const emailRef = useRef(null);

  async function handleSubmit(event) {
    event.preventDefault();
    const address = email.trim();
    if (!address) {
      setError('Enter an email address.');
      return;
    }
    setError('');
    const ok = await onInvite({ email: address, role, displayName: displayName.trim() });
    if (ok) {
      setEmail('');
      setDisplayName('');
      setRole(UserRole.MARKER);
      emailRef.current?.focus();
    }
  }

  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <UserPlus className="h-5 w-5 text-cyan-700" aria-hidden="true" />
          Invite a marker
        </CardTitle>
        <CardDescription>
          They receive a link to choose their own password; nobody else ever sees it. A marker can do everything in
          the app except manage accounts.
          {mail && mail.configured === false ? (
            <span className="mt-1 block text-amber-800">
              No mail server is configured (EMAIL_BACKEND={mail.backend}). The invitation link will be shown here to
              copy instead of being emailed.
            </span>
          ) : null}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="grid grid-cols-1 gap-3 md:grid-cols-[2fr_1fr_1fr_auto] md:items-end" onSubmit={handleSubmit}>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="invite-email" className="text-xs font-semibold text-slate-600">
              Email
            </label>
            <input
              ref={emailRef}
              id="invite-email"
              type="email"
              autoComplete="off"
              spellCheck={false}
              className={FIELD}
              placeholder="colleague@example.edu"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              disabled={busy}
              required
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="invite-name" className="text-xs font-semibold text-slate-600">
              Name <span className="font-normal text-slate-400">(optional)</span>
            </label>
            <input
              id="invite-name"
              type="text"
              className={FIELD}
              placeholder="Dr Marker"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              disabled={busy}
              maxLength={120}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="invite-role" className="text-xs font-semibold text-slate-600">
              Role
            </label>
            <select id="invite-role" className={FIELD} value={role} onChange={(event) => setRole(event.target.value)} disabled={busy}>
              <option value={UserRole.MARKER}>Marker</option>
              <option value={UserRole.ADMIN}>Admin</option>
            </select>
          </div>
          <Button type="submit" className="gap-2" disabled={busy}>
            <Send className="h-4 w-4" aria-hidden="true" />
            Send invitation
          </Button>
        </form>
        {error ? (
          <p role="alert" className="mt-3 text-xs text-rose-700">
            {error}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function UserRow({ user, actions, busy, onAction }) {
  const status = describeUserStatus(user);
  const name = user.displayName || user.username;
  const roleSelectId = `role-${user.id}`;
  return (
    <li className="rounded-xl border border-slate-200 px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-sm font-semibold text-slate-900">{name}</span>
            {actions.isSelf ? <Badge variant="info">You</Badge> : null}
            <Badge variant={status.tone}>{status.label}</Badge>
            <label htmlFor={roleSelectId} className="sr-only">
              Role for {name}
            </label>
            {actions.canChangeRole ? (
              <select
                id={roleSelectId}
                className="rounded-md border border-slate-300 bg-white px-2 py-0.5 text-xs text-slate-700 shadow-sm focus:border-cyan-500 focus:outline-none focus:ring-2 focus:ring-cyan-500/30 disabled:opacity-60"
                value={user.role}
                disabled={busy}
                onChange={(event) => onAction('role', user, event.target.value)}
              >
                <option value={UserRole.MARKER}>Marker</option>
                <option value={UserRole.ADMIN}>Admin</option>
              </select>
            ) : (
              <Badge variant={user.role === UserRole.ADMIN ? 'accent' : 'neutral'} title={actions.lockedReason}>
                {roleLabel(user.role)}
              </Badge>
            )}
          </div>
          <div className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-slate-500">
            {user.email && user.email !== name ? <span className="truncate">{user.email}</span> : null}
            <span>{status.detail}</span>
            {user.createdAt ? <span>Added {relativeTime(user.createdAt)}</span> : null}
          </div>
          {actions.lockedReason ? <p className="mt-1 text-[11px] text-slate-500">{actions.lockedReason}</p> : null}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          {actions.canResendInvite ? (
            <Button variant="outline" size="sm" className="gap-1.5" disabled={busy} onClick={() => onAction('resend', user)}>
              <RefreshCcw className="h-3.5 w-3.5" aria-hidden="true" />
              Resend invite
            </Button>
          ) : null}
          {actions.canSendReset ? (
            <Button variant="outline" size="sm" className="gap-1.5" disabled={busy} onClick={() => onAction('reset', user)}>
              <KeyRound className="h-3.5 w-3.5" aria-hidden="true" />
              Send reset
            </Button>
          ) : null}
          {actions.canDisable ? (
            <Button variant="outline" size="sm" className="gap-1.5" disabled={busy} onClick={() => onAction('disable', user)}>
              <Ban className="h-3.5 w-3.5" aria-hidden="true" />
              Disable
            </Button>
          ) : null}
          {actions.canEnable ? (
            <Button variant="outline" size="sm" className="gap-1.5" disabled={busy} onClick={() => onAction('enable', user)}>
              <UserCheck className="h-3.5 w-3.5" aria-hidden="true" />
              Enable
            </Button>
          ) : null}
          {actions.canDelete ? (
            <Button
              variant="destructive"
              size="sm"
              className="gap-1.5"
              disabled={busy}
              onClick={() => onAction('delete', user)}
              aria-label={`Delete ${name}`}
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
              Delete
            </Button>
          ) : null}
        </div>
      </div>
    </li>
  );
}

export default function UsersAdminPage({ onBack }) {
  const [listing, setListing] = useState(null); // null = never loaded
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState(null); // {tone, title, detail, link}
  const [pendingDelete, setPendingDelete] = useState(null);
  const confirmDeleteRef = useRef(null);

  async function load() {
    setLoadError('');
    try {
      const body = await apiJson('/api/admin/users', { fallbackMessage: 'Failed to load users.' });
      setListing({
        users: Array.isArray(body.users) ? body.users : [],
        me: String(body.me || ''),
        mail: body.mail || {},
      });
    } catch (error) {
      setLoadError(error.message || 'Failed to load users.');
    }
  }

  useEffect(() => {
    load();
  }, []);

  const activeAdminCount = useMemo(() => countActiveAdmins(listing?.users), [listing]);

  function replaceRow(user) {
    setListing((current) => (current ? { ...current, users: current.users.map((row) => (row.id === user.id ? user : row)) } : current));
  }

  async function run(work) {
    setBusy(true);
    setActionError('');
    try {
      await work();
      return true;
    } catch (error) {
      setActionError(error.message || 'The action failed.');
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function handleInvite({ email, role, displayName }) {
    setBanner(null);
    return run(async () => {
      const body = await apiJson('/api/admin/users', {
        method: 'POST',
        json: { email, role, displayName },
        fallbackMessage: 'The invitation could not be created.',
      });
      setBanner(describeDelivery(body, listing?.mail, { kind: 'invitation', email: body.user?.email || email }));
      await load();
    });
  }

  async function handleAction(kind, user, value) {
    setBanner(null);
    if (kind === 'delete') {
      setPendingDelete(user);
      return;
    }
    const base = `/api/admin/users/${encodeURIComponent(user.id)}`;
    await run(async () => {
      if (kind === 'resend') {
        const body = await apiJson(`${base}/resend-invite`, { method: 'POST', fallbackMessage: 'The invitation could not be re-sent.' });
        setBanner(describeDelivery(body, listing?.mail, { kind: 'invitation', email: user.email }));
        if (body.user) replaceRow(body.user);
      } else if (kind === 'reset') {
        const body = await apiJson(`${base}/send-password-reset`, { method: 'POST', fallbackMessage: 'The reset link could not be sent.' });
        setBanner(describeDelivery(body, listing?.mail, { kind: 'password reset', email: user.email }));
      } else if (kind === 'disable' || kind === 'enable') {
        const body = await apiJson(`${base}/${kind}`, { method: 'POST', fallbackMessage: 'The account could not be updated.' });
        if (body.user) replaceRow(body.user);
        // Enabling or disabling an admin moves the last-admin line for everyone.
        await load();
      } else if (kind === 'role') {
        const body = await apiJson(base, { method: 'PATCH', json: { role: value }, fallbackMessage: 'The role could not be changed.' });
        if (body.user) replaceRow(body.user);
        await load();
      }
    });
  }

  async function confirmDelete() {
    const user = pendingDelete;
    if (!user) return;
    setPendingDelete(null);
    await run(async () => {
      await apiJson(`/api/admin/users/${encodeURIComponent(user.id)}`, { method: 'DELETE', fallbackMessage: 'The account could not be deleted.' });
      await load();
    });
  }

  const isFirstLoad = listing === null && !loadError;

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <PageHeader
        icon={<Users className="h-5 w-5" />}
        title="Users"
        subtitle="Who can sign in, and what they may do"
        onBack={onBack}
        backTitle="Back to dashboard"
      />

      <main id="main" className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-8">
        {loadError ? (
          <div className="flex items-center justify-between rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
            <span>{loadError}</span>
            <Button variant="outline" size="sm" onClick={load}>
              Retry
            </Button>
          </div>
        ) : null}

        {isFirstLoad ? (
          <>
            <Card className="border-slate-200 bg-white shadow-sm">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-lg">
                  <UserPlus className="h-5 w-5 text-cyan-700" aria-hidden="true" />
                  Invite a marker
                </CardTitle>
              </CardHeader>
              <CardContent>
                <LoadingRegion label="Loading the invitation form">
                  <InviteFormSkeleton />
                </LoadingRegion>
              </CardContent>
            </Card>
            <Card className="border-slate-200 bg-white shadow-sm">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-lg">
                  <Users className="h-5 w-5 text-slate-700" aria-hidden="true" />
                  Accounts
                </CardTitle>
              </CardHeader>
              <CardContent>
                <LoadingRegion label="Loading accounts">
                  <UserRowsSkeleton />
                </LoadingRegion>
              </CardContent>
            </Card>
          </>
        ) : listing ? (
          <>
            <InviteCard mail={listing.mail} busy={busy} onInvite={handleInvite} />

            {banner ? (
              <ToneBanner tone={banner.tone} title={banner.title} detail={banner.detail}>
                {banner.link ? <LinkReveal link={banner.link} onDismiss={() => setBanner(null)} /> : null}
              </ToneBanner>
            ) : null}

            {actionError ? (
              <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                {actionError}
              </div>
            ) : null}

            <Card className="border-slate-200 bg-white shadow-sm">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-lg">
                  <Users className="h-5 w-5 text-slate-700" aria-hidden="true" />
                  Accounts
                </CardTitle>
                <CardDescription>
                  {listing.users.length} account{listing.users.length === 1 ? '' : 's'} ·{' '}
                  {activeAdminCount} active administrator{activeAdminCount === 1 ? '' : 's'}. Disabling an account ends its
                  sessions immediately.
                </CardDescription>
              </CardHeader>
              <CardContent>
                {listing.users.length === 0 ? (
                  <p className="text-sm text-slate-500">No accounts yet.</p>
                ) : (
                  <ul className={`flex flex-col gap-2 transition-opacity ${busy ? 'opacity-60' : ''}`}>
                    {listing.users.map((user) => (
                      <UserRow
                        key={user.id}
                        user={user}
                        actions={rowActions(user, { meId: listing.me, activeAdminCount })}
                        busy={busy}
                        onAction={handleAction}
                      />
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>

            <p className="text-xs text-slate-500">
              <Mail className="mr-1 inline h-3.5 w-3.5 align-text-bottom" aria-hidden="true" />
              Invitations expire after a few days and password-reset links after a few minutes; both work once.
              {listing.mail?.configured ? ` Emails are sent through ${listing.mail.backend}.` : ' No mail server is configured.'}
            </p>
          </>
        ) : null}
      </main>

      <AnimatePresence>
        {pendingDelete ? (
          <Modal
            onClose={() => setPendingDelete(null)}
            labelledBy="confirm-delete-user-title"
            describedBy="confirm-delete-user-description"
            initialFocus={confirmDeleteRef}
            size="sm"
          >
            <Card className="border-slate-200 bg-white shadow-xl">
              <CardHeader>
                <CardTitle id="confirm-delete-user-title" className="flex items-center gap-2 text-lg">
                  <Trash2 className="h-5 w-5 text-rose-700" aria-hidden="true" />
                  Delete this account?
                </CardTitle>
                <CardDescription id="confirm-delete-user-description">
                  {pendingDelete.displayName || pendingDelete.username}
                  {pendingDelete.email && pendingDelete.email !== pendingDelete.username ? ` (${pendingDelete.email})` : ''} will
                  be signed out everywhere and can no longer sign in. Their past assessments are kept. This cannot be undone.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="flex justify-end gap-2">
                  <Button variant="outline" size="sm" onClick={() => setPendingDelete(null)}>
                    Keep account
                  </Button>
                  <Button ref={confirmDeleteRef} variant="destructive" size="sm" className="gap-1.5" onClick={confirmDelete}>
                    <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                    Delete
                  </Button>
                </div>
              </CardContent>
            </Card>
          </Modal>
        ) : null}
      </AnimatePresence>
    </div>
  );
}

