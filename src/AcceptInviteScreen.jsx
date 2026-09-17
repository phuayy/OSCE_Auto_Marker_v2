import React, { useEffect, useState } from 'react';
import { CheckCircle2, UserCheck, UserPlus } from 'lucide-react';
import { acceptInvitationRequest, fetchInvitation } from '@/auth';
import { AuthAlert, AuthField, AuthLink, AuthShell, AuthSubmitButton } from '@/components/AuthShell.jsx';
import { AuthNewPasswordFields } from '@/components/AuthNewPasswordFields.jsx';
import { passwordProblems } from '@/lib/userAdmin';

// Where an invitation email lands: #/accept-invite/<token>. The screen first
// asks the server whose invitation this is and whether it still works, then
// takes a password. No session is issued on success — the person signs in
// next, which is also the moment they learn the password they chose works.
export default function AcceptInviteScreen({ token, onAccepted, onGoToLogin }) {
  const [invitation, setInvitation] = useState(null); // null = not asked yet
  const [loadError, setLoadError] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setInvitation(null);
    setLoadError('');
    if (!token) {
      setInvitation({ valid: false, message: 'This link is incomplete. Open the one in your email.' });
      return undefined;
    }
    fetchInvitation(token)
      .then((body) => {
        if (cancelled) return;
        setInvitation(body);
        setDisplayName(body?.displayName || '');
      })
      .catch((requestError) => {
        if (!cancelled) setLoadError(requestError.message || 'The invitation could not be checked.');
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const identifiers = invitation?.email ? [invitation.email, invitation.email.split('@')[0]] : [];
  const problems = passwordProblems({ password, confirm, identifiers });

  async function handleSubmit(event) {
    event.preventDefault();
    if (isSubmitting) return;
    if (problems.length) {
      setError(problems[0]);
      return;
    }
    setError('');
    setIsSubmitting(true);
    try {
      await acceptInvitationRequest(token, { password, displayName: displayName.trim() });
      setDone(true);
      onAccepted?.({ username: invitation?.email || '' });
    } catch (submitError) {
      setError(submitError.message || 'The account could not be activated.');
    } finally {
      setIsSubmitting(false);
    }
  }

  const footer = (
    <AuthLink onClick={onGoToLogin}>Already have an account? Sign in</AuthLink>
  );

  if (done) {
    return (
      <AuthShell title="You're all set" subtitle="Your account is active." footer={null}>
        <div className="space-y-5 text-center">
          <CheckCircle2 className="mx-auto h-10 w-10 text-emerald-300" aria-hidden="true" />
          <p className="text-sm text-slate-300">
            Sign in with <span className="font-semibold text-slate-100">{invitation?.email}</span> and the password you just chose.
          </p>
          <AuthSubmitButton type="button" icon={UserCheck} onClick={onGoToLogin}>
            Go to sign in
          </AuthSubmitButton>
        </div>
      </AuthShell>
    );
  }

  if (invitation === null && !loadError) {
    return (
      <AuthShell title="Checking your invitation" subtitle="One moment…" footer={footer}>
        <div role="status" aria-busy="true" className="space-y-3">
          <span className="sr-only">Checking the invitation</span>
          <div aria-hidden="true" className="h-4 w-2/3 animate-pulse rounded bg-white/10 motion-reduce:animate-none" />
          <div aria-hidden="true" className="h-11 w-full animate-pulse rounded-xl bg-white/10 motion-reduce:animate-none" />
          <div aria-hidden="true" className="h-11 w-full animate-pulse rounded-xl bg-white/10 motion-reduce:animate-none" />
        </div>
      </AuthShell>
    );
  }

  if (loadError || !invitation?.valid) {
    return (
      <AuthShell title="This invitation can't be used" footer={footer}>
        <AuthAlert tone="error">{loadError || invitation?.message || 'This link is not valid.'}</AuthAlert>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title="Welcome to OSCE AI Marker"
      subtitle={`Choose a password for ${invitation.email}`}
      footer={footer}
    >
      <form className="space-y-5" onSubmit={handleSubmit}>
        <AuthField
          id="display-name"
          name="name"
          label="Your name"
          icon={UserPlus}
          accent="cyan"
          autoComplete="name"
          type="text"
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
          disabled={isSubmitting}
          placeholder="How colleagues will see you"
          maxLength={120}
        />
        <AuthNewPasswordFields
          password={password}
          confirm={confirm}
          onPasswordChange={setPassword}
          onConfirmChange={setConfirm}
          disabled={isSubmitting}
          autoFocus
        />
        <AuthAlert tone="error">{error}</AuthAlert>
        <AuthSubmitButton busy={isSubmitting} busyLabel="Activating…" icon={UserCheck} disabled={isSubmitting || !password || !confirm}>
          Activate account
        </AuthSubmitButton>
      </form>
    </AuthShell>
  );
}
