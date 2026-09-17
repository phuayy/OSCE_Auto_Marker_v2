import React from 'react';
import { BarChart3, FileText, KeyRound, RefreshCcw, Settings as SettingsIcon, Users } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { PageHeader } from '@/components/PageHeader.jsx';
import { AuthShell } from '@/components/AuthShell.jsx';
import { WORKSPACE_LAYOUT } from '@/lib/sessionWorkspace';

// Placeholders shaped like the content that replaces them.
//
// Every route in this app is a lazy chunk, and every page fetches on mount, so
// a cold visit used to show two different waits in a row: a centred spinner
// while the chunk loaded, then a "Loading…" line inside each card while the
// data arrived. A skeleton is one placeholder for both — the route's Suspense
// fallback and the page's own first-load branch render the *same* component,
// so the hand-off from chunk to page is invisible and nothing reflows when the
// data lands.
//
// Two rules the file is built on:
//
//   * A skeleton is for a first load only — `data === null`, never `isLoading`
//     on its own. A refresh keeps what is on screen and dims it (the Analytics
//     page's pattern); a background refresh must never flash bone.
//   * This module ships in the entry chunk (AppShell renders the route
//     fallbacks, the dashboard renders the workspace one), so it may import
//     nothing from a lazy-only module: src/workspace/, AnalyticsPage,
//     SettingsPage, CommunicationRubricPanel. test/bundleSplit.test.mjs is what
//     catches it if that changes. The cost is that page titles in the *route*
//     skeletons are bone rather than text — the text lives in the chunk.
//
// The page-frame headers below are the real pages' header — the same
// PageHeader component with the same title and subtitle — so nothing moves
// when the chunk mounts. test/skeletons.test.mjs pins the titles.

/**
 * Announces a wait once, for everything inside it. The bones themselves are
 * aria-hidden decoration; this is the element a screen reader hears.
 */
export function LoadingRegion({ label, className = '', children }) {
  return (
    <div role="status" aria-busy="true" className={className}>
      <span className="sr-only">{label}</span>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Small parts shared by several cards
// ---------------------------------------------------------------------------

function TextLines({ count = 2, className = '' }) {
  const widths = ['w-full', 'w-11/12', 'w-4/5', 'w-2/3'];
  return (
    <div className={`flex flex-col gap-2 ${className}`}>
      {Array.from({ length: count }, (_, index) => (
        <Skeleton key={index} className={`h-3.5 ${widths[index % widths.length]}`} />
      ))}
    </div>
  );
}

/** The "Changes apply to future runs." hint beside a Save button. */
function SaveRowSkeleton() {
  return (
    <div className="flex items-center justify-between gap-3">
      <Skeleton className="h-3 w-44" />
      <Skeleton className="h-8 w-28 rounded-lg" />
    </div>
  );
}

/** A settings card whose title, description and body are all still to come. */
export function SettingsCardSkeleton({ children, descriptionLines = 3 }) {
  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <div className="flex items-center gap-2">
          <Skeleton className="h-5 w-5" />
          <Skeleton className="h-5 w-44" />
        </div>
        <TextLines count={descriptionLines} className="pt-1" />
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

/** One provider + model choice — the shape of components/TargetPicker.jsx. */
export function TargetPickerSkeleton() {
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex flex-col gap-1.5">
          <Skeleton className="h-4 w-28" />
          <Skeleton className="h-3 w-48" />
        </div>
        <Skeleton className="h-8 w-16 rounded-lg" />
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        {[0, 1].map((index) => (
          <div key={index} className="flex flex-col gap-1">
            <Skeleton className="h-3 w-16" />
            <Skeleton className="h-8 w-full rounded-lg" />
          </div>
        ))}
      </div>
    </div>
  );
}

/** A row of `count` selectable option tiles (engine choice, marking mode). */
function OptionTilesSkeleton({ count = 2 }) {
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {Array.from({ length: count }, (_, index) => (
        <div key={index} className="flex flex-col gap-2 rounded-xl border border-slate-200 bg-white px-4 py-3">
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-3 w-1/4" />
          <Skeleton className="h-3 w-5/6" />
        </div>
      ))}
    </div>
  );
}

/** Rows of "title + subtitle, two actions on the right" — corpora, webhooks. */
export function ListRowsSkeleton({ rows = 2, className = '' }) {
  return (
    <div className={`flex flex-col gap-2 ${className}`}>
      {Array.from({ length: rows }, (_, index) => (
        <div
          key={index}
          className="flex items-center justify-between gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2"
        >
          <div className="flex min-w-0 flex-1 flex-col gap-1.5">
            <Skeleton className={`h-4 ${index % 2 ? 'w-2/5' : 'w-3/5'}`} />
            <Skeleton className="h-3 w-1/4" />
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <Skeleton className="h-8 w-14 rounded-lg" />
            <Skeleton className="h-8 w-9 rounded-lg" />
          </div>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Settings — one body per card, and the page that stacks them
// ---------------------------------------------------------------------------

/** Transcription engine: engine tiles, the option box, Save. */
export function EngineSettingsSkeleton() {
  return (
    <div className="flex flex-col gap-4">
      <OptionTilesSkeleton />
      <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-2">
        <div className="divide-y divide-slate-200">
          {[0, 1, 2].map((index) => (
            <div key={index} className="flex items-center justify-between gap-3 py-3">
              <div className="flex flex-col gap-1.5">
                <Skeleton className="h-3.5 w-32" />
                <Skeleton className="h-3 w-56" />
              </div>
              <Skeleton className="h-8 w-28 rounded-lg" />
            </div>
          ))}
        </div>
      </div>
      <SaveRowSkeleton />
    </div>
  );
}

/** Scoring model: primary and fallback pickers, Save. */
export function ScoringModelSkeleton() {
  return (
    <div className="flex flex-col gap-4">
      <TargetPickerSkeleton />
      <TargetPickerSkeleton />
      <SaveRowSkeleton />
    </div>
  );
}

/** Marking mode: the two mode tiles, the picker the default mode shows, Save. */
export function MarkingModeSkeleton() {
  return (
    <div className="flex flex-col gap-4">
      <OptionTilesSkeleton />
      <TargetPickerSkeleton />
      <SaveRowSkeleton />
    </div>
  );
}

/** Provider API keys: one row per platform — name, source chip, key field, Save/Test. */
export function ProviderKeysSkeleton({ rows = 3 }) {
  return (
    <div className="flex flex-col gap-4">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
          <div className="flex items-start justify-between gap-2">
            <div className="flex flex-col gap-1.5">
              <Skeleton className="h-4 w-28" />
              <Skeleton className="h-3 w-40" />
            </div>
            <Skeleton className="h-5 w-24 rounded-full" />
          </div>
          <Skeleton className="h-3 w-3/4" />
          <div className="flex flex-col gap-1">
            <Skeleton className="h-3 w-16" />
            <div className="flex flex-wrap items-center gap-2">
              <Skeleton className="h-8 min-w-[220px] flex-1 rounded-lg" />
              <Skeleton className="h-8 w-16 rounded-lg" />
              <Skeleton className="h-8 w-16 rounded-lg" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

/** Custom providers: the list (usually empty) and the "Add a provider" button. */
export function CustomProvidersSkeleton() {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-3">
        <Skeleton className="mt-0.5 h-3.5 w-3.5" />
        <TextLines count={2} className="flex-1" />
      </div>
      <Skeleton className="h-8 w-36 rounded-lg" />
    </div>
  );
}

/** A labelled on/off row with a switch — the transcript-preprocess toggle. */
export function ToggleRowSkeleton() {
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
      <div className="flex flex-col gap-1.5">
        <Skeleton className="h-4 w-20" />
        <Skeleton className="h-3 w-64 max-w-full" />
      </div>
      <Skeleton className="h-6 w-11 shrink-0 rounded-full" />
    </div>
  );
}

/** Webhooks: the "Add webhook" button above the subscription rows. */
export function WebhookRowsSkeleton({ rows = 2 }) {
  return (
    <div className="flex flex-col gap-3">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="rounded-xl border border-slate-200 px-4 py-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              <div className="flex items-center gap-2">
                <Skeleton className={`h-4 ${index % 2 ? 'w-1/2' : 'w-2/3'}`} />
                <Skeleton className="h-4 w-14 rounded-full" />
              </div>
              <div className="flex flex-wrap gap-1">
                <Skeleton className="h-4 w-20 rounded-full" />
                <Skeleton className="h-4 w-24 rounded-full" />
                <Skeleton className="h-4 w-16 rounded-full" />
              </div>
              <Skeleton className="h-3 w-1/2" />
            </div>
            <div className="flex shrink-0 flex-wrap gap-1.5">
              <Skeleton className="h-8 w-14 rounded-lg" />
              <Skeleton className="h-8 w-14 rounded-lg" />
              <Skeleton className="h-8 w-9 rounded-lg" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * The Settings route while its chunk loads: the real page header (static, so
 * the user can still leave) over the eight cards in the order the page renders
 * them, each with the body skeleton that card itself shows until its data
 * arrives. The chunk mounting on top of this changes nothing but the text.
 */
export function SettingsSkeleton({ onBack }) {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <PageHeader
        icon={<SettingsIcon className="h-5 w-5" />}
        title="Settings"
        subtitle="Global options applied to every assessment run"
        onBack={onBack}
        backTitle="Back to dashboard"
      />

      <LoadingRegion label="Loading settings" className="mx-auto flex max-w-3xl flex-col gap-6 px-6 py-8">
        <SettingsCardSkeleton>
          <EngineSettingsSkeleton />
        </SettingsCardSkeleton>
        <SettingsCardSkeleton>
          <ScoringModelSkeleton />
        </SettingsCardSkeleton>
        <SettingsCardSkeleton descriptionLines={4}>
          <MarkingModeSkeleton />
        </SettingsCardSkeleton>
        <SettingsCardSkeleton>
          <ProviderKeysSkeleton />
        </SettingsCardSkeleton>
        <SettingsCardSkeleton descriptionLines={4}>
          <CustomProvidersSkeleton />
        </SettingsCardSkeleton>
        <SettingsCardSkeleton descriptionLines={4}>
          <ToggleRowSkeleton />
        </SettingsCardSkeleton>
        <SettingsCardSkeleton descriptionLines={2}>
          <ListRowsSkeleton rows={2} />
        </SettingsCardSkeleton>
        <SettingsCardSkeleton descriptionLines={4}>
          <div className="flex flex-col gap-4">
            <Skeleton className="h-8 w-32 rounded-lg" />
            <WebhookRowsSkeleton />
          </div>
        </SettingsCardSkeleton>
      </LoadingRegion>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Users (account administration) and Account
// ---------------------------------------------------------------------------

/** The invite form: three fields and a button, in the layout the card renders. */
export function InviteFormSkeleton() {
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-[2fr_1fr_1fr_auto] md:items-end">
      {[0, 1, 2].map((index) => (
        <div key={index} className="flex flex-col gap-1.5">
          <Skeleton className="h-3 w-16" />
          <Skeleton className="h-9 w-full rounded-lg" />
        </div>
      ))}
      <Skeleton className="h-9 w-24 rounded-lg" />
    </div>
  );
}

/** Account rows: name + email, a role and a status chip, actions on the right. */
export function UserRowsSkeleton({ rows = 3 }) {
  return (
    <div className="flex flex-col gap-2">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="rounded-xl border border-slate-200 px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              <div className="flex items-center gap-2">
                <Skeleton className={`h-4 ${index % 2 ? 'w-1/3' : 'w-1/4'}`} />
                <Skeleton className="h-4 w-14 rounded-full" />
                <Skeleton className="h-4 w-16 rounded-full" />
              </div>
              <Skeleton className="h-3 w-1/2" />
            </div>
            <div className="flex shrink-0 flex-wrap gap-1.5">
              <Skeleton className="h-8 w-20 rounded-lg" />
              <Skeleton className="h-8 w-16 rounded-lg" />
              <Skeleton className="h-8 w-9 rounded-lg" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * The Users route while its chunk loads: the invite card over the account
 * list, the same bodies the page shows until its first response.
 */
export function UsersSkeleton({ onBack }) {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <PageHeader
        icon={<Users className="h-5 w-5" />}
        title="Users"
        subtitle="Who can sign in, and what they may do"
        onBack={onBack}
        backTitle="Back to dashboard"
      />
      <LoadingRegion label="Loading users" className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-8">
        <SettingsCardSkeleton descriptionLines={2}>
          <InviteFormSkeleton />
        </SettingsCardSkeleton>
        <SettingsCardSkeleton descriptionLines={1}>
          <UserRowsSkeleton />
        </SettingsCardSkeleton>
      </LoadingRegion>
    </div>
  );
}

/** The change-password form: three fields and Save. */
export function AccountFormSkeleton() {
  return (
    <div className="flex max-w-md flex-col gap-4">
      {[0, 1, 2].map((index) => (
        <div key={index} className="flex flex-col gap-1.5">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-9 w-full rounded-lg" />
        </div>
      ))}
      <Skeleton className="h-9 w-32 rounded-lg" />
    </div>
  );
}

/** The Account route while its chunk loads. */
export function AccountSkeleton({ onBack }) {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <PageHeader
        icon={<KeyRound className="h-5 w-5" />}
        title="Your account"
        subtitle="Sign-in details, password and appearance"
        onBack={onBack}
        backTitle="Back to dashboard"
      />
      <LoadingRegion label="Loading your account" className="mx-auto flex max-w-3xl flex-col gap-6 px-6 py-8">
        <SettingsCardSkeleton descriptionLines={1}>
          <TextLines count={3} />
        </SettingsCardSkeleton>
        <SettingsCardSkeleton descriptionLines={2}>
          <AccountFormSkeleton />
        </SettingsCardSkeleton>
        <SettingsCardSkeleton descriptionLines={1}>
          <div className="grid gap-2 sm:grid-cols-3">
            {[0, 1, 2].map((index) => (
              <Skeleton key={index} className="h-16 w-full rounded-xl" />
            ))}
          </div>
        </SettingsCardSkeleton>
      </LoadingRegion>
    </div>
  );
}

/**
 * A pre-login screen (invitation, forgot-password, reset) while its chunk
 * loads: the same dark frame the screen itself renders, with the card's
 * fields in outline. Bones are lighter here because the ground is dark.
 */
export function AuthScreenSkeleton({ title = 'One moment' }) {
  return (
    <AuthShell title={title}>
      <LoadingRegion label="Loading" className="space-y-4">
        <div aria-hidden="true" className="h-3.5 w-1/2 animate-pulse rounded bg-white/10 motion-reduce:animate-none" />
        <div aria-hidden="true" className="h-11 w-full animate-pulse rounded-xl bg-white/10 motion-reduce:animate-none" />
        <div aria-hidden="true" className="h-11 w-full animate-pulse rounded-xl bg-white/10 motion-reduce:animate-none" />
        <div aria-hidden="true" className="h-11 w-full animate-pulse rounded-xl bg-white/10 motion-reduce:animate-none" />
      </LoadingRegion>
    </AuthShell>
  );
}

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------

/**
 * What the analytics page shows before its first response: the filter card,
 * the KPI tiles, the two distribution charts and the per-student breakdown,
 * at the heights they render at. Nothing partial is ever shown.
 */
export function AnalyticsSkeletonBody() {
  return (
    <div className="flex flex-col gap-6">
      <Skeleton className="h-24 rounded-xl" />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {[0, 1, 2, 3].map((index) => (
          <Skeleton key={index} className="h-24 rounded-xl" />
        ))}
      </div>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Skeleton className="h-72 rounded-xl" />
        <Skeleton className="h-72 rounded-xl" />
      </div>
      <Skeleton className="h-56 rounded-xl" />
    </div>
  );
}

/** The Analytics route while its chunk loads: the real header over the body above. */
export function AnalyticsSkeleton({ onBack }) {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <PageHeader
        icon={<BarChart3 className="h-5 w-5" />}
        title="Score Analytics"
        subtitle="Assessment results stored in the database"
        onBack={onBack}
        backTitle="Back to dashboard"
      >
        <Button variant="outline" size="sm" className="gap-2" disabled>
          <RefreshCcw className="h-4 w-4" aria-hidden="true" />
          Refresh
        </Button>
      </PageHeader>
      <LoadingRegion label="Loading analytics" className="mx-auto flex max-w-7xl flex-col gap-6 px-6 py-8">
        <AnalyticsSkeletonBody />
      </LoadingRegion>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Communication rubric
// ---------------------------------------------------------------------------

/** The "Source file" tile's two lines while the PDF's metadata is unknown. */
export function RubricSourceSkeleton() {
  return (
    <div className="flex flex-col gap-2 pt-1">
      <Skeleton className="h-4 w-3/4" />
      <Skeleton className="h-3 w-1/2" />
    </div>
  );
}

/** Grouped, collapsed criterion rows — the criteria card's body. */
export function RubricCriteriaSkeleton({ groups = 2, rows = 3 }) {
  return (
    <div className="space-y-5">
      {Array.from({ length: groups }, (_, group) => (
        <section key={group} className="space-y-3">
          <div className="flex items-center gap-2">
            <Skeleton className="h-3.5 w-3.5" />
            <Skeleton className="h-3 w-40" />
          </div>
          <ul className="space-y-2">
            {Array.from({ length: rows }, (_, row) => (
              <li key={row} className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
                <div className="flex items-start gap-3 px-4 py-3">
                  <Skeleton className="mt-0.5 h-7 w-7 shrink-0 rounded-lg" />
                  <Skeleton className={`mt-1.5 h-4 ${row % 2 ? 'w-2/3' : 'w-4/5'}`} />
                  <Skeleton className="ml-auto mt-1.5 h-4 w-4 shrink-0" />
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

/** The Rubric route while its chunk loads: the real header, then both cards. */
export function RubricSkeleton({ onBack }) {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <PageHeader
        icon={<FileText className="h-5 w-5" />}
        title="Communication Rubric"
        subtitle="The rubric every communication score is marked against"
        onBack={onBack}
        backTitle="Back to dashboard"
      >
        <Badge variant="accent">Editable</Badge>
      </PageHeader>

      <LoadingRegion label="Loading rubric" className="mx-auto max-w-5xl space-y-6 px-6 py-8">
        <Card className="overflow-hidden border-slate-200 bg-white shadow-sm">
          <CardHeader className="border-b border-slate-100 bg-slate-50/60">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex flex-col gap-2">
                <div className="flex items-center gap-2">
                  <Skeleton className="h-5 w-5" />
                  <Skeleton className="h-5 w-56" />
                </div>
                <TextLines count={2} />
              </div>
              <div className="flex flex-col items-end gap-2">
                <Skeleton className="h-5 w-36 rounded-full" />
                <Skeleton className="h-3 w-20" />
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-4 pt-5">
            <div className="grid gap-3 sm:grid-cols-2">
              {[0, 1].map((index) => (
                <div key={index} className="rounded-xl border border-slate-200 bg-slate-50 p-3">
                  <Skeleton className="h-3 w-24" />
                  <RubricSourceSkeleton />
                </div>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Skeleton className="h-9 w-48 rounded-lg" />
              <Skeleton className="h-9 w-32 rounded-lg" />
              <Skeleton className="h-9 w-24 rounded-lg" />
            </div>
          </CardContent>
        </Card>

        <Card className="border-slate-200 bg-white shadow-sm">
          <CardHeader className="flex flex-row items-start justify-between gap-3 border-b border-slate-100">
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-2">
                <Skeleton className="h-4 w-4" />
                <Skeleton className="h-4 w-64" />
              </div>
              <TextLines count={2} />
            </div>
            <div className="flex shrink-0 gap-2">
              <Skeleton className="h-8 w-20 rounded-lg" />
              <Skeleton className="h-8 w-20 rounded-lg" />
            </div>
          </CardHeader>
          <CardContent className="space-y-5 pt-5">
            <RubricCriteriaSkeleton />
          </CardContent>
        </Card>
      </LoadingRegion>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

/**
 * Saved-session rows before the index's first response: the name field, the
 * id line and the status chip on the left, the action and delete controls on
 * the right — the card each entry renders as. First load only: a background
 * refresh keeps the rows it has.
 */
export function SessionRowsSkeleton({ rows = 3 }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="rounded-xl border border-slate-200 bg-slate-50 p-3">
          <div className="flex items-start justify-between gap-2">
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              <Skeleton className={`h-8 rounded-lg ${index % 2 ? 'w-3/5' : 'w-4/5'}`} />
              <Skeleton className="h-3 w-2/3" />
              <Skeleton className="h-5 w-20 rounded-full" />
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <Skeleton className="h-8 w-16 rounded-lg" />
              <Skeleton className="h-8 w-8 rounded-lg" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * Notification rows before the first poll answers — the bell's dropdown and
 * the dashboard feed both list the same row (notifications.jsx
 * NotificationRow): unread dot, title, body, time, dismiss.
 */
export function NotificationRowsSkeleton({ rows = 3 }) {
  return (
    <div>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="flex items-start gap-2 border-b border-slate-100 px-4 py-3 last:border-b-0">
          <Skeleton className="mt-1.5 h-2 w-2 shrink-0 rounded-full" />
          <div className="flex min-w-0 flex-1 flex-col gap-1.5">
            <Skeleton className={`h-3 ${index % 2 ? 'w-2/5' : 'w-1/2'}`} />
            <Skeleton className="h-3 w-11/12" />
            <Skeleton className="h-2.5 w-14" />
          </div>
          <Skeleton className="h-3 w-12 shrink-0" />
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Session workspace
// ---------------------------------------------------------------------------

/** A label/value box, the shape of workspace/primitives.jsx's StatusRow. */
function StatusRowSkeleton({ valueWidth = 'w-24' }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5">
      <Skeleton className="h-3.5 w-28" />
      <Skeleton className={`h-3.5 ${valueWidth}`} />
    </div>
  );
}

/**
 * The Audio Professionalism card's body: the six headline rows the extractor
 * reports, then the collapsed openSMILE details box. Shown by the workspace
 * while it fetches the artefact after mount, and by WorkspaceSkeleton for the
 * same card — one shape for both waits.
 */
export function AudioProfessionalismSkeleton() {
  return (
    <div className="space-y-3">
      {['w-20', 'w-12', 'w-14', 'w-12', 'w-8', 'w-8'].map((width, index) => (
        <StatusRowSkeleton key={index} valueWidth={width} />
      ))}
      <Skeleton className="h-10 w-full rounded-xl" />
    </div>
  );
}

/**
 * The Cohort Summary card while `/clip-summaries` is computed: header with
 * the pass-count chip, the per-student bar chart at the height it draws at,
 * and the criterion breakdown rows. Rendered in the slot LongVideoSummaryCharts
 * fills, so nothing below it moves when the data lands.
 */
export function CohortSummarySkeleton() {
  const bars = ['h-2/5', 'h-4/5', 'h-full', 'h-3/5', 'h-1/3', 'h-3/4', 'h-1/2', 'h-2/3'];
  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <Skeleton className="h-5 w-5" />
              <Skeleton className="h-5 w-36" />
            </div>
            <Skeleton className="h-3.5 w-72 max-w-full" />
          </div>
          <Skeleton className="h-9 w-64 max-w-full rounded-xl" />
        </div>
      </CardHeader>
      <CardContent className="space-y-8">
        <section className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 pb-2">
            <Skeleton className="h-4 w-48" />
            <div className="flex items-center gap-2">
              <Skeleton className="h-3 w-28" />
              <Skeleton className="h-3 w-36" />
              <Skeleton className="h-8 w-24 rounded-lg" />
            </div>
          </div>
          <div className="flex h-72 items-end gap-3 px-2">
            {bars.map((height, index) => (
              <Skeleton key={index} className={`flex-1 rounded-t-md ${height}`} />
            ))}
          </div>
        </section>
        <section className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 pb-2">
            <Skeleton className="h-4 w-64" />
            <Skeleton className="h-8 w-24 rounded-lg" />
          </div>
          <div className="space-y-2">
            {[0, 1, 2, 3].map((index) => (
              <div key={index} className="flex items-center gap-3">
                <Skeleton className="h-3.5 w-40 shrink-0" />
                <Skeleton className={`h-5 rounded ${index % 2 ? 'w-3/4' : 'w-full'}`} />
              </div>
            ))}
          </div>
        </section>
      </CardContent>
    </Card>
  );
}

function WorkspaceCardSkeleton({ children, className = '' }) {
  return (
    <Card className={`border-slate-200 bg-white shadow-sm ${className}`}>
      <CardHeader>
        <Skeleton className="h-5 w-48" />
        <Skeleton className="h-3.5 w-3/4" />
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

/** Transcript rows: speaker + time on one line, the utterance below. */
function TranscriptRowsSkeleton({ rows = 5 }) {
  const widths = ['w-full', 'w-5/6', 'w-11/12', 'w-3/4', 'w-full'];
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="rounded-xl border border-slate-200 bg-slate-50 p-3">
          <div className="mb-2 flex items-center justify-between">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="h-3 w-24" />
          </div>
          <Skeleton className={`h-3.5 ${widths[index % widths.length]}`} />
        </div>
      ))}
    </div>
  );
}

/**
 * The opened-session view while its payload (and, on a cold visit, its chunk)
 * is fetched, in the outline the real view will have: the file chips, the
 * duration strip, the player card over its tabs, and the right-hand status
 * cards. `layout` picks between the single-student column and the long-video
 * one — decided from what the session list already knows about the session
 * (lib/sessionWorkspace.js `workspaceLayoutFor`), so the placeholder does not
 * change shape when the payload lands.
 */
export function WorkspaceSkeleton({ layout = WORKSPACE_LAYOUT.STANDARD, label = 'Loading session' }) {
  const isLong = layout === WORKSPACE_LAYOUT.LONG;
  const isClip = layout === WORKSPACE_LAYOUT.CLIP;
  return (
    <LoadingRegion label={label} className="space-y-6">
      {/* File / session / status chips. */}
      <Card className="border-slate-200 bg-white shadow-sm">
        <CardContent className="pt-5">
          <div className="flex flex-wrap items-center gap-3">
            {['w-44', 'w-52', 'w-56', 'w-40', 'w-36'].map((width) => (
              <Skeleton key={width} className={`h-9 ${width} rounded-lg`} />
            ))}
          </div>
          {isClip ? <Skeleton className="mt-4 h-9 w-36 rounded-lg" /> : null}
        </CardContent>
      </Card>

      {/* Duration + mode strip. */}
      <div className="rounded-2xl border border-slate-200/90 bg-white p-4 shadow-sm ring-1 ring-slate-100/80">
        <div className="flex flex-wrap items-center gap-3">
          <Skeleton className="h-9 w-28 rounded-xl" />
          <Skeleton className="h-6 w-48 rounded-full" />
          <Skeleton className="h-3.5 min-w-[12rem] flex-1" />
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          {/* Station recording. */}
          <Card className="overflow-hidden border border-slate-200/90 bg-white shadow-md shadow-slate-200/50 ring-1 ring-slate-100">
            <CardHeader className="space-y-0 border-b border-slate-100 pb-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="flex flex-col gap-2">
                  <Skeleton className="h-5 w-40" />
                  <Skeleton className="h-3.5 w-72 max-w-full" />
                </div>
                <div className="flex items-center gap-2">
                  <Skeleton className="h-6 w-20 rounded-full" />
                  <Skeleton className="h-6 w-28 rounded-full" />
                </div>
              </div>
            </CardHeader>
            <CardContent className="pt-6">
              <Skeleton className="aspect-video w-full rounded-2xl" />
              {isLong ? (
                <div className="mt-5 space-y-4">
                  <div className="grid grid-cols-2 gap-2 rounded-2xl border border-slate-200/90 bg-slate-50 p-2">
                    <Skeleton className="h-9 rounded-xl" />
                    <Skeleton className="h-9 rounded-xl" />
                  </div>
                  <Skeleton className="h-16 w-full rounded-xl" />
                </div>
              ) : null}
            </CardContent>
          </Card>

          {isLong ? (
            <WorkspaceCardSkeleton>
              <TextLines count={2} />
            </WorkspaceCardSkeleton>
          ) : (
            <>
              <WorkspaceCardSkeleton>
                <TranscriptRowsSkeleton />
              </WorkspaceCardSkeleton>
              <div className="grid h-auto w-full grid-cols-2 gap-2 rounded-2xl border border-slate-200/90 bg-slate-50 p-2 sm:grid-cols-4">
                {[0, 1, 2, 3].map((index) => (
                  <Skeleton key={index} className="h-10 rounded-xl" />
                ))}
              </div>
              <WorkspaceCardSkeleton>
                <TextLines count={4} />
              </WorkspaceCardSkeleton>
            </>
          )}
        </div>

        <div className="space-y-6">
          {/* Run status. */}
          <WorkspaceCardSkeleton>
            <div className="space-y-2">
              <StatusRowSkeleton valueWidth="w-16" />
              <StatusRowSkeleton valueWidth="w-32" />
            </div>
          </WorkspaceCardSkeleton>

          {isLong ? (
            /* Clip assessments: the two batch buttons, then one row per clip. */
            <WorkspaceCardSkeleton>
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Skeleton className="h-8 w-24 rounded-lg" />
                  <Skeleton className="h-8 w-28 rounded-lg" />
                </div>
                {[0, 1, 2, 3].map((index) => (
                  <div key={index} className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 p-3">
                    <Skeleton className="h-4 w-4 shrink-0" />
                    <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                      <Skeleton className="h-3.5 w-1/2" />
                      <Skeleton className="h-3 w-1/3" />
                    </div>
                    <Skeleton className="h-8 w-16 shrink-0 rounded-lg" />
                  </div>
                ))}
              </div>
            </WorkspaceCardSkeleton>
          ) : null}

          {/* Audio professionalism. */}
          <WorkspaceCardSkeleton>
            <AudioProfessionalismSkeleton />
          </WorkspaceCardSkeleton>
        </div>
      </div>
    </LoadingRegion>
  );
}
