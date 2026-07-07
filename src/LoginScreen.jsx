import React, { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { Brain, Eye, EyeOff, Loader2, Lock, Shield, ShieldCheck, User } from 'lucide-react';
import { loginRequest } from '@/auth';
import { Button } from '@/components/ui/button';

export default function LoginScreen({ onLoggedIn }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [didJustLogin, setDidJustLogin] = useState(false);

  const usernameInputRef = useRef(null);

  useEffect(() => {
    usernameInputRef.current?.focus();
  }, []);

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

  return (
    <div className="relative min-h-screen overflow-hidden bg-slate-950 text-slate-100">
      {/* Layered animated background blobs (matches dashboard accent gradients). */}
      <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -top-32 -left-24 h-96 w-96 rounded-full bg-purple-500/40 blur-3xl animate-blob" />
        <div className="absolute top-20 -right-24 h-96 w-96 rounded-full bg-cyan-500/40 blur-3xl animate-blob animation-delay-2000" />
        <div className="absolute bottom-0 left-1/3 h-96 w-96 rounded-full bg-fuchsia-500/40 blur-3xl animate-blob animation-delay-4000" />
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_50%_-10%,rgba(168,85,247,0.18),transparent_60%)]" />
        <div className="absolute inset-0 bg-[linear-gradient(to_top,#020617_5%,transparent_50%)]" />
      </div>

      {/* Subtle grid overlay, similar to many AI dashboards. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 opacity-[0.08]"
        style={{
          backgroundImage:
            'linear-gradient(rgba(255,255,255,0.6) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.6) 1px, transparent 1px)',
          backgroundSize: '32px 32px',
          maskImage: 'radial-gradient(circle at center, black 0%, transparent 75%)',
        }}
      />

      <div className="relative z-10 flex min-h-screen items-center justify-center px-6 py-12">
        <motion.div
          initial={{ opacity: 0, y: 16, scale: 0.97 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          transition={{ duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
          className="w-full max-w-md"
        >
          <motion.div
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.1, duration: 0.4 }}
            className="mb-8 flex flex-col items-center text-center"
          >
            <div className="relative mb-4 rounded-2xl bg-gradient-to-br from-purple-600 to-cyan-500 p-3 shadow-[0_0_40px_-10px_rgba(168,85,247,0.7)]">
              <Brain className="h-8 w-8 text-white" />
              <motion.span
                aria-hidden="true"
                className="absolute -inset-1 rounded-2xl border border-white/30"
                initial={{ opacity: 0 }}
                animate={{ opacity: [0, 0.4, 0] }}
                transition={{ duration: 2, repeat: Infinity, ease: 'easeInOut' }}
              />
            </div>
            <h1 className="text-3xl font-bold tracking-tight">OSCE AI Marker</h1>
            <p className="mt-2 text-sm text-slate-400">
              Secure sign-in to the assessment dashboard
            </p>
          </motion.div>

          <div className="relative overflow-hidden rounded-3xl border border-white/10 bg-slate-900/60 p-8 shadow-2xl backdrop-blur-xl">
            <div className="pointer-events-none absolute inset-px rounded-[calc(theme(borderRadius.3xl)-1px)] border border-white/5" />

            <form className="relative space-y-5" onSubmit={handleSubmit}>
              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-wider text-slate-300" htmlFor="login-username">
                  Username
                </label>
                <div className="group relative">
                  <div className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-slate-400 transition group-focus-within:text-cyan-300">
                    <User className="h-4 w-4" />
                  </div>
                  <input
                    ref={usernameInputRef}
                    id="login-username"
                    name="username"
                    autoComplete="username"
                    spellCheck={false}
                    type="text"
                    value={username}
                    onChange={(event) => setUsername(event.target.value)}
                    disabled={isSubmitting || didJustLogin}
                    className="w-full rounded-xl border border-white/10 bg-slate-950/60 py-3 pl-10 pr-3 text-sm text-slate-100 placeholder:text-slate-500 transition focus:border-cyan-400 focus:outline-none focus:ring-2 focus:ring-cyan-400/30"
                    placeholder="admin"
                    required
                  />
                </div>
              </div>

              <div className="space-y-2">
                <label className="text-xs font-semibold uppercase tracking-wider text-slate-300" htmlFor="login-password">
                  Password
                </label>
                <div className="group relative">
                  <div className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-slate-400 transition group-focus-within:text-purple-300">
                    <Lock className="h-4 w-4" />
                  </div>
                  <input
                    id="login-password"
                    name="password"
                    autoComplete="current-password"
                    type={showPassword ? 'text' : 'password'}
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    disabled={isSubmitting || didJustLogin}
                    className="w-full rounded-xl border border-white/10 bg-slate-950/60 py-3 pl-10 pr-12 text-sm text-slate-100 placeholder:text-slate-500 transition focus:border-purple-400 focus:outline-none focus:ring-2 focus:ring-purple-400/30"
                    placeholder="••••••••"
                    required
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((previous) => !previous)}
                    disabled={isSubmitting || didJustLogin}
                    className="absolute inset-y-0 right-2 flex items-center rounded-lg px-2 text-slate-400 transition hover:text-slate-200 focus:outline-none focus:ring-2 focus:ring-purple-400/30 disabled:opacity-50"
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                  >
                    {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </button>
                </div>
              </div>

              <AnimatePresence>
                {error ? (
                  <motion.div
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -4 }}
                    className="rounded-xl border border-rose-400/40 bg-rose-500/15 p-3 text-sm text-rose-200"
                  >
                    {error}
                  </motion.div>
                ) : null}
              </AnimatePresence>

              <Button
                type="submit"
                disabled={isSubmitting || didJustLogin || !username.trim() || !password}
                className="relative h-11 w-full overflow-hidden border-0 bg-gradient-to-r from-purple-600 via-fuchsia-500 to-cyan-500 text-base font-semibold text-white shadow-[0_10px_30px_-12px_rgba(168,85,247,0.7)] hover:from-purple-500 hover:to-cyan-400 focus:ring-cyan-400/40 disabled:opacity-60"
              >
                <span className="relative z-10 flex items-center justify-center gap-2">
                  {didJustLogin ? (
                    <>
                      <ShieldCheck className="h-4 w-4" />
                      Welcome back
                    </>
                  ) : isSubmitting ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Authenticating...
                    </>
                  ) : (
                    <>
                      <Shield className="h-4 w-4" />
                      Sign in
                    </>
                  )}
                </span>
                <motion.span
                  aria-hidden="true"
                  className="absolute inset-0 -translate-x-full bg-gradient-to-r from-transparent via-white/30 to-transparent"
                  animate={isSubmitting ? { translateX: ['-100%', '100%'] } : { translateX: '-100%' }}
                  transition={{ duration: 1.6, ease: 'easeInOut', repeat: isSubmitting ? Infinity : 0 }}
                />
              </Button>
            </form>

            {/* <div className="relative mt-6 rounded-xl border border-white/5 bg-white/5 px-4 py-3 text-xs leading-relaxed text-slate-300">
              <div className="flex items-start gap-2">
                <Shield className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-cyan-300" />
                <span>
                  Credentials and the model API key are stored server-side at
                  <code className="mx-1 rounded bg-slate-950/60 px-1 py-0.5 text-[11px] text-cyan-200">
                    storage/auth/
                  </code>
                  (gitignored). Passwords are bcrypt-hashed; sessions use HMAC-signed tokens that
                  expire in 8 hours.
                </span>
              </div>
            </div> */}
          </div>

          {/* <p className="mt-6 text-center text-xs text-slate-500">
            Default sign-in: <span className="font-mono text-slate-300">admin</span> /{' '}
            <span className="font-mono text-slate-300">admin</span>
          </p> */}
        </motion.div>
      </div>
    </div>
  );
}
