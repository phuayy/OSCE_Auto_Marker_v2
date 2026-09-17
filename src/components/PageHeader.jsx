import React from 'react';
import { ArrowLeft } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ThemeToggle } from '@/components/ThemeToggle.jsx';
import { cn } from '@/lib/utils';

// The chrome every page shares: a sticky bar with the app mark, the page's
// title as its h1, and a slot on the right for that page's actions.
//
// One component rather than four copies so the pages cannot drift — they
// had, into four icon gradients and a title that was a div. It also lets the
// route skeletons draw the real header (the same markup, so nothing moves
// when the chunk mounts) without copying it a third time.
//
// The mark is always the primary gradient: it identifies the app, not the
// page. The page is identified by its icon and title.
//
// The theme toggle is part of this chrome rather than a child each page
// passes: a page cannot forget it, and the route skeletons — which render
// this same header while a chunk loads — offer it too.
export function PageHeader({ icon, title, subtitle, onBack, backLabel = 'Back', backDisabled = false, backTitle, children, className }) {
  return (
    <header className={cn('sticky top-0 z-40 border-b border-slate-200 bg-white/95 backdrop-blur', className)}>
      <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-6 py-4">
        <div className="flex min-w-0 items-center gap-3">
          {onBack ? (
            <Button variant="outline" size="sm" className="gap-2" onClick={onBack} disabled={backDisabled} title={backTitle}>
              <ArrowLeft className="h-4 w-4" aria-hidden="true" />
              {backLabel}
            </Button>
          ) : null}
          <div
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-cyan-600 to-blue-700 text-white shadow-sm"
            aria-hidden="true"
          >
            {icon}
          </div>
          <div className="min-w-0">
            <h1 className="truncate text-lg font-bold leading-tight text-slate-900">{title}</h1>
            {subtitle ? <p className="text-xs text-slate-500">{subtitle}</p> : null}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {children}
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}

// Lets keyboard and screen-reader users jump past the header's controls.
// Invisible until it has focus; the target is each page's <main id="main">.
export function SkipToContent() {
  return (
    <a
      href="#main"
      className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[80] focus:rounded-lg focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:font-semibold focus:text-slate-900 focus:shadow-lg focus:ring-2 focus:ring-cyan-500"
    >
      Skip to content
    </a>
  );
}
