import React from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { Brain, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

// The one deliberately loud surface in the app: the dark, purple→cyan hero
// that every pre-login screen sits on. It used to be the login screen's own
// markup; the invitation, forgot-password and reset screens need the same
// frame, so it lives here once — the login screen composes it like the rest.
//
// Everything below paints its own colours (`variant="plain"` on the button)
// because the light design system's tokens are wrong on this ground — and
// the root carries `theme-fixed`, so the tonal palette (tailwind.palette.js)
// does not re-map them when the document is in the dark theme: this surface
// is dark by design, in both themes, and has no toggle.

export function AuthShell({ title, subtitle, children, footer }) {
  return (
    <div className="theme-fixed relative min-h-screen overflow-hidden bg-slate-950 text-slate-100">
      <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
        <div className="absolute -top-32 -left-24 h-96 w-96 rounded-full bg-purple-500/40 blur-3xl animate-blob" />
        <div className="absolute top-20 -right-24 h-96 w-96 rounded-full bg-cyan-500/40 blur-3xl animate-blob animation-delay-2000" />
        <div className="absolute bottom-0 left-1/3 h-96 w-96 rounded-full bg-fuchsia-500/40 blur-3xl animate-blob animation-delay-4000" />
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_50%_-10%,rgba(168,85,247,0.18),transparent_60%)]" />
        <div className="absolute inset-0 bg-[linear-gradient(to_top,#020617_5%,transparent_50%)]" />
      </div>

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
              <Brain className="h-8 w-8 text-white" aria-hidden="true" />
              <motion.span
                aria-hidden="true"
                className="absolute -inset-1 rounded-2xl border border-white/30"
                initial={{ opacity: 0 }}
                animate={{ opacity: [0, 0.4, 0] }}
                transition={{ duration: 2, repeat: Infinity, ease: 'easeInOut' }}
              />
            </div>
            <h1 className="text-3xl font-bold tracking-tight">{title}</h1>
            {subtitle ? <p className="mt-2 text-sm text-slate-400">{subtitle}</p> : null}
          </motion.div>

          <div className="relative overflow-hidden rounded-3xl border border-white/10 bg-slate-900/60 p-8 shadow-2xl backdrop-blur-xl">
            <div className="pointer-events-none absolute inset-px rounded-[calc(theme(borderRadius.3xl)-1px)] border border-white/5" />
            <div className="relative">{children}</div>
          </div>

          {footer ? <div className="mt-6 text-center text-xs text-slate-400">{footer}</div> : null}
        </motion.div>
      </div>
    </div>
  );
}

const ACCENTS = {
  cyan: {
    icon: 'group-focus-within:text-cyan-300',
    input: 'focus:border-cyan-400 focus:ring-cyan-400/30',
  },
  purple: {
    icon: 'group-focus-within:text-purple-300',
    input: 'focus:border-purple-400 focus:ring-purple-400/30',
  },
};

/**
 * A labelled input on the dark ground, with a leading icon and an optional
 * trailing control (the show/hide password toggle).
 */
export const AuthField = React.forwardRef(function AuthField(
  { id, label, icon: Icon, accent = 'cyan', trailing = null, hint = '', className, ...inputProps },
  ref,
) {
  const tones = ACCENTS[accent] || ACCENTS.cyan;
  return (
    <div className="space-y-2">
      <label className="text-xs font-semibold uppercase tracking-wider text-slate-300" htmlFor={id}>
        {label}
      </label>
      <div className="group relative">
        {Icon ? (
          <div className={cn('pointer-events-none absolute inset-y-0 left-3 flex items-center text-slate-400 transition', tones.icon)}>
            <Icon className="h-4 w-4" aria-hidden="true" />
          </div>
        ) : null}
        <input
          ref={ref}
          id={id}
          className={cn(
            'w-full rounded-xl border border-white/10 bg-slate-950/60 py-3 text-sm text-slate-100 placeholder:text-slate-500 transition focus:outline-none focus:ring-2',
            Icon ? 'pl-10' : 'pl-3',
            trailing ? 'pr-12' : 'pr-3',
            tones.input,
            className,
          )}
          {...inputProps}
        />
        {trailing}
      </div>
      {hint ? <p className="text-[11px] text-slate-400">{hint}</p> : null}
    </div>
  );
});

/** The password show/hide control, positioned inside an AuthField. */
export function AuthTrailingButton({ className, ...props }) {
  return (
    <button
      type="button"
      className={cn(
        'absolute inset-y-0 right-2 flex items-center rounded-lg px-2 text-slate-400 transition hover:text-slate-200 focus:outline-none focus:ring-2 focus:ring-purple-400/30 disabled:opacity-50',
        className,
      )}
      {...props}
    />
  );
}

const ALERT_TONES = {
  error: 'border-rose-400/40 bg-rose-500/15 text-rose-200',
  success: 'border-emerald-400/40 bg-emerald-500/15 text-emerald-100',
  info: 'border-cyan-400/40 bg-cyan-500/15 text-cyan-100',
};

/** A message inside the card. `error` is announced as an alert; the rest as status. */
export function AuthAlert({ tone = 'info', children }) {
  return (
    <AnimatePresence>
      {children ? (
        <motion.div
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          role={tone === 'error' ? 'alert' : 'status'}
          className={cn('rounded-xl border p-3 text-sm', ALERT_TONES[tone] || ALERT_TONES.info)}
        >
          {children}
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}

/** The gradient primary action, with the busy shimmer the login button had. */
export function AuthSubmitButton({ busy = false, busyLabel = 'Working…', icon: Icon = null, children, className, ...props }) {
  return (
    <Button
      type="submit"
      variant="plain"
      className={cn(
        'relative h-11 w-full overflow-hidden border-0 bg-gradient-to-r from-purple-600 via-fuchsia-500 to-cyan-500 text-base font-semibold text-white shadow-[0_10px_30px_-12px_rgba(168,85,247,0.7)] hover:from-purple-500 hover:to-cyan-400 focus-visible:ring-cyan-400/60 focus-visible:ring-offset-slate-950 disabled:opacity-60',
        className,
      )}
      {...props}
    >
      <span className="relative z-10 flex items-center justify-center gap-2">
        {busy ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            {busyLabel}
          </>
        ) : (
          <>
            {Icon ? <Icon className="h-4 w-4" aria-hidden="true" /> : null}
            {children}
          </>
        )}
      </span>
      <motion.span
        aria-hidden="true"
        className="absolute inset-0 -translate-x-full bg-gradient-to-r from-transparent via-white/30 to-transparent"
        animate={busy ? { translateX: ['-100%', '100%'] } : { translateX: '-100%' }}
        transition={{ duration: 1.6, ease: 'easeInOut', repeat: busy ? Infinity : 0 }}
      />
    </Button>
  );
}

/** A quiet text link on the dark ground — "Forgot password?", "Back to sign in". */
export function AuthLink({ className, ...props }) {
  return (
    <button
      type="button"
      className={cn(
        'rounded text-xs font-medium text-cyan-300 underline-offset-4 transition hover:text-cyan-200 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60',
        className,
      )}
      {...props}
    />
  );
}
