import React, { useEffect, useRef, useState } from 'react';
import { Mail, Send } from 'lucide-react';
import { requestPasswordReset } from '@/auth';
import { AuthAlert, AuthField, AuthLink, AuthShell, AuthSubmitButton } from '@/components/AuthShell.jsx';

// #/forgot-password. The server answers the same way whether or not the
// identifier names an account, and so does this screen: the confirmation
// text never says "found" or "not found".
export default function ForgotPasswordScreen({ onGoToLogin }) {
  const [identifier, setIdentifier] = useState('');
  const [error, setError] = useState('');
  const [sent, setSent] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const inputRef = useRef(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  async function handleSubmit(event) {
    event.preventDefault();
    if (isSubmitting) return;
    setError('');
    setIsSubmitting(true);
    try {
      const body = await requestPasswordReset(identifier.trim());
      setSent(body?.message || 'If that account exists, an email with a reset link is on its way.');
    } catch (submitError) {
      setError(submitError.message || 'The reset link could not be requested.');
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <AuthShell
      title="Forgot your password?"
      subtitle="Enter your username or email and we'll send a link to choose a new one."
      footer={<AuthLink onClick={onGoToLogin}>Back to sign in</AuthLink>}
    >
      {sent ? (
        <div className="space-y-4">
          <AuthAlert tone="success">{sent}</AuthAlert>
          <p className="text-xs text-slate-400">
            The link works once and expires soon. If nothing arrives, check your spam folder or ask an administrator to
            send one.
          </p>
        </div>
      ) : (
        <form className="space-y-5" onSubmit={handleSubmit}>
          <AuthField
            ref={inputRef}
            id="reset-identifier"
            name="username"
            label="Username or email"
            icon={Mail}
            accent="cyan"
            autoComplete="username"
            spellCheck={false}
            type="text"
            value={identifier}
            onChange={(event) => setIdentifier(event.target.value)}
            disabled={isSubmitting}
            placeholder="you@example.edu"
            required
          />
          <AuthAlert tone="error">{error}</AuthAlert>
          <AuthSubmitButton busy={isSubmitting} busyLabel="Sending…" icon={Send} disabled={isSubmitting || !identifier.trim()}>
            Send reset link
          </AuthSubmitButton>
        </form>
      )}
    </AuthShell>
  );
}
