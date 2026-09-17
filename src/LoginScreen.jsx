import React, { useEffect, useRef, useState } from 'react';
import { Eye, EyeOff, Lock, Shield, ShieldCheck, User } from 'lucide-react';
import { loginRequest } from '@/auth';
import { AuthAlert, AuthField, AuthLink, AuthShell, AuthSubmitButton, AuthTrailingButton } from '@/components/AuthShell.jsx';

// `notice` is a one-line message from the screen that sent the visitor here
// ("Your account is ready — sign in"), and `initialUsername` pre-fills the
// account it concerns, so activating an invitation lands on a form that only
// needs the password typed once more.
export default function LoginScreen({ onLoggedIn, onForgotPassword = null, notice = '', initialUsername = '' }) {
  const [username, setUsername] = useState(initialUsername);
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [didJustLogin, setDidJustLogin] = useState(false);

  const usernameInputRef = useRef(null);
  const passwordInputRef = useRef(null);

  useEffect(() => {
    (initialUsername ? passwordInputRef : usernameInputRef).current?.focus();
  }, [initialUsername]);

  async function handleSubmit(event) {
    event.preventDefault();
    if (isSubmitting) {
      return;
    }

    setError('');
    setIsSubmitting(true);

    try {
      const result = await loginRequest(username.trim(), password);
      setDidJustLogin(true);
      // Tiny success-flash before unmounting.
      setTimeout(() => {
        onLoggedIn?.(result);
      }, 450);
    } catch (loginError) {
      setError(loginError.message || 'Login failed. Please try again.');
    } finally {
      setIsSubmitting(false);
    }
  }

  const locked = isSubmitting || didJustLogin;

  return (
    <AuthShell title="OSCE AI Marker" subtitle="Secure sign-in to the assessment dashboard">
      <form className="space-y-5" onSubmit={handleSubmit}>
        <AuthAlert tone="success">{notice}</AuthAlert>

        <AuthField
          ref={usernameInputRef}
          id="login-username"
          name="username"
          label="Username or email"
          icon={User}
          accent="cyan"
          autoComplete="username"
          spellCheck={false}
          type="text"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          disabled={locked}
          placeholder="you@example.edu"
          required
        />

        <AuthField
          ref={passwordInputRef}
          id="login-password"
          name="password"
          label="Password"
          icon={Lock}
          accent="purple"
          autoComplete="current-password"
          type={showPassword ? 'text' : 'password'}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          disabled={locked}
          placeholder="••••••••"
          required
          trailing={
            <AuthTrailingButton
              onClick={() => setShowPassword((previous) => !previous)}
              disabled={locked}
              aria-label={showPassword ? 'Hide password' : 'Show password'}
            >
              {showPassword ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
            </AuthTrailingButton>
          }
        />

        <AuthAlert tone="error">{error}</AuthAlert>

        <AuthSubmitButton
          busy={isSubmitting}
          busyLabel="Authenticating..."
          icon={didJustLogin ? ShieldCheck : Shield}
          disabled={locked || !username.trim() || !password}
        >
          {didJustLogin ? 'Welcome back' : 'Sign in'}
        </AuthSubmitButton>

        {onForgotPassword ? (
          <div className="text-center">
            <AuthLink onClick={onForgotPassword} disabled={locked}>
              Forgot your password?
            </AuthLink>
          </div>
        ) : null}
      </form>
    </AuthShell>
  );
}
