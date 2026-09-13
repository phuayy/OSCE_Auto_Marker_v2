import React, { Suspense, lazy } from 'react';
import { Loader2, RotateCw } from 'lucide-react';
import { Button } from '@/components/ui/button';

// Code splitting plumbing, in one place so every split point behaves the same.
//
// Three things a bare React.lazy() does not give us and a deployed app needs:
//
//   1. Preloading. A chunk fetched when the user *hovers* the button that opens
//      it is usually already there when they click, so the split costs nothing
//      perceptible. lazyComponent() memoises the import promise and exposes it
//      as `.preload()`.
//   2. A shared fallback, so a loading route looks like the rest of the app
//      rather than like a blank page.
//   3. An error boundary. A lazy import rejects when the browser holds an old
//      index.html and asks for a chunk this deploy no longer has — without a
//      boundary that unmounts the whole tree and the user sees a white screen.
//      Reloading is the fix, so the boundary says so and offers the button.

/**
 * React.lazy with a memoised loader, so the chunk can be warmed before render.
 *
 * @param {() => Promise<{ default: React.ComponentType }>} loader dynamic import
 * @returns {React.LazyExoticComponent & { preload: () => Promise<unknown> }}
 */
export function lazyComponent(loader) {
  let pending = null;
  const preload = () => {
    if (!pending) pending = loader();
    return pending;
  };
  const Component = lazy(preload);
  Component.preload = preload;
  return Component;
}

/**
 * Warm a lazy component's chunk. Safe to call on hover/focus and repeatedly:
 * the loader is memoised, and a failed preload is swallowed so the real render
 * is the thing that reports the error (through ChunkErrorBoundary).
 */
export function preloadComponent(Component) {
  if (typeof Component?.preload === 'function') {
    Component.preload().catch(() => {});
  }
}

export function RouteFallback({ label = 'Loading…' }) {
  return (
    <div className="flex min-h-[60vh] items-center justify-center px-6 py-16 text-slate-500">
      <div className="flex items-center gap-3 text-sm">
        <Loader2 className="h-5 w-5 animate-spin" />
        {label}
      </div>
    </div>
  );
}

export function PanelFallback({ label = 'Loading…' }) {
  return (
    <div className="flex items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white p-6 text-sm text-slate-500">
      <Loader2 className="h-4 w-4 animate-spin" />
      {label}
    </div>
  );
}

export class ChunkErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error) {
    // Left visible in the console on purpose: a chunk that fails for a reason
    // other than a stale deploy (an offline browser, a blocked CDN) is worth
    // seeing in full rather than only as the summary below.
    console.error('[chunk]', error);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="mx-auto flex max-w-lg flex-col items-center gap-3 rounded-xl border border-amber-300 bg-amber-50 p-6 text-center text-sm text-amber-900">
        <p className="font-medium">This part of the app could not be loaded.</p>
        <p className="text-amber-800">
          It is usually a page left open across an update. Reloading fetches the current version.
        </p>
        <Button variant="outline" className="gap-2" onClick={() => window.location.reload()}>
          <RotateCw className="h-4 w-4" />
          Reload
        </Button>
      </div>
    );
  }
}

/**
 * Suspense + chunk error boundary as one wrapper, so no split point can forget
 * either half.
 */
export function LazyBoundary({ fallback, children }) {
  return (
    <ChunkErrorBoundary>
      <Suspense fallback={fallback ?? <RouteFallback />}>{children}</Suspense>
    </ChunkErrorBoundary>
  );
}
