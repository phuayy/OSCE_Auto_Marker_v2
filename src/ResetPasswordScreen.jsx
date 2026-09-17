import React, { useEffect, useState } from 'react';
import { CheckCircle2, KeyRound, ShieldCheck } from 'lucide-react';
import { confirmPasswordReset, fetchPasswordReset } from '@/auth';
import { AuthAlert, AuthLink, AuthShell, AuthSubmitButton } from '@/components/AuthShell.jsx';
import { AuthNewPasswordFields } from '@/components/AuthNewPasswordFields.jsx';
import { passwordProblems } from '@/lib/userAdmin';

// #/reset-password/<token>: the same shape as accepting an invitation, for an
// account that already exists. Success ends every other session the account
// had and sends a "your password was changed" email; the person signs in
// again here.
export default function ResetPasswordScreen({ token, onReset, onGoToLogin }) {
  const [reset, setReset] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setReset(null);
    setLoadError('');
    if (!token) {
      setReset({ valid: false, message: 'This link is incomplete. Open the one in your email.' });
      return undefined;
    }
    fetchPasswordReset(token)
      .then((body) => {
        if (!cancelled) setReset(body);
      })
      .catch((requestError) => {
        if (!cancelled) setLoadError(requestError.message || 'The reset link could not be checked.');
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const identifiers = reset?.email ? [reset.email, reset.email.split('@')[0]] : [];
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
      const body = await confirmPasswordReset(token, password);
      setDone(true);
      onReset?.({ username: body?.username || reset?.email || '' });
    } catch (submitError) {
      setError(submitError.message || 'The password could not be reset.');
    } finally {
      setIsSubmitting(false);
    }
  }

  const footer = <AuthLink onClick={onGoToLogin}>Back to sign in</AuthLink>;

  if (done) {
    return (
      <AuthShell title="Password changed" subtitle="Every other session on this account has been signed out.">
        <div className="space-y-5 text-center">
          <CheckCircle2 className="mx-auto h-10 w-10 text-emerald-300" aria-hidden="true" />
          <AuthSubmitButton type="button" icon={ShieldCheck} onClick={onGoToLogin}>
            Sign in with the new password
          </AuthSubmitButton>
        </div>
      </AuthShell>
    );
  }

  if (reset === null && !loadError) {
    return (
      <AuthShell title="Checking your link" subtitle="One moment…" footer={footer}>
        <div role="status" aria-busy="true" className="space-y-3">
          <span className="sr-only">Checking the reset link</span>
          <div aria-hidden="true" className="h-11 w-full animate-pulse rounded-xl bg-white/10 motion-reduce:animate-none" />
          <div aria-hidden="true" className="h-11 w-full animate-pulse rounded-xl bg-white/10 motion-reduce:animate-none" />
        </div>
      </AuthShell>
    );
  }

  if (loadError || !reset?.valid) {
    return (
      <AuthShell title="This link can't be used" footer={footer}>
        <div className="space-y-4">
          <AuthAlert tone="error">{loadError || reset?.message || 'This link is not valid.'}</AuthAlert>
          <p className="text-xs text-slate-400">You can ask for a new one from the sign-in screen.</p>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Choose a new password" subtitle={`For ${reset.email}`} footer={footer}>
      <form className="space-y-5" onSubmit={handleSubmit}>
        <AuthNewPasswordFields
          password={password}
          confirm={confirm}
          onPasswordChange={setPassword}
          onConfirmChange={setConfirm}
          disabled={isSubmitting}
          autoFocus
        />
        <AuthAlert tone="error">{error}</AuthAlert>
        <AuthSubmitButton busy={isSubmitting} busyLabel="Saving…" icon={KeyRound} disabled={isSubmitting || !password || !confirm}>
          Set new password
        </AuthSubmitButton>
      </form>
    </AuthShell>
  );
}
