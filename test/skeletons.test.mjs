// A first load shows the shape of what is coming, not a spinner.
//
// Every route is a lazy chunk and every page fetches on mount, so a cold visit
// used to be two waits in a row that looked nothing alike: a centred spinner
// while the chunk loaded, then a "Loading…" line inside each card while its
// data arrived — and opening a saved session dimmed the whole dashboard behind
// a modal. Now the route's Suspense fallback and the page's own first-load
// branch render the same skeleton (src/components/skeletons.jsx), and the
// workspace's placeholder takes the outline of the session about to open.
//
// These are structural tests, like apiClientCoverage: the browser suite has
// no DOM, so the invariants are checked in the source. Each one guards a way
// the pattern quietly regresses — a card that grows a `Loader2 … Loading…`
// branch again, a route fallback that goes back to the spinner, a skeleton
// rendered without the region that announces it, a page-frame title that
// drifts from the page it stands in for.
//
// Run with: npm run test:ui
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = fileURLToPath(new URL('../src/', import.meta.url));
const read = (name) => readFileSync(join(SRC, name), 'utf8');

const SKELETONS = read('components/skeletons.jsx');
const PRIMITIVE = read('components/ui/skeleton.jsx');
const APP_SHELL = read('AppShell.jsx');
const DASHBOARD = read('OSCEAiMarkerMockup.jsx');
const LAZY_ROUTE = read('lib/lazyRoute.jsx');

// Every first-load site, and the body skeleton it shows. The settings entries
// are also what SettingsSkeleton (the route fallback) has to compose, in this
// order, so that chunk-load and card-load draw the same page.
const SETTINGS_CARDS = [
  ['TranscriptionEngineSettings.jsx', 'EngineSettingsSkeleton'],
  ['LlmRoutingSettings.jsx', 'ScoringModelSkeleton'],
  ['MarkingModeSettings.jsx', 'MarkingModeSkeleton'],
  ['ProviderKeysSettings.jsx', 'ProviderKeysSkeleton'],
  ['CustomProvidersSettings.jsx', 'CustomProvidersSkeleton'],
  ['SettingsPage.jsx', 'ToggleRowSkeleton'],
  ['CorporaManager.jsx', 'ListRowsSkeleton'],
  ['WebhooksManager.jsx', 'WebhookRowsSkeleton'],
];
const OTHER_SITES = [
  ['AnalyticsPage.jsx', 'AnalyticsSkeletonBody'],
  ['CommunicationRubricPanel.jsx', 'RubricCriteriaSkeleton'],
  ['CommunicationRubricPanel.jsx', 'RubricSourceSkeleton'],
  ['OSCEAiMarkerMockup.jsx', 'SessionRowsSkeleton'],
  ['notifications.jsx', 'NotificationRowsSkeleton'],
  ['workspace/SessionWorkspace.jsx', 'AudioProfessionalismSkeleton'],
  ['workspace/SessionWorkspace.jsx', 'CohortSummarySkeleton'],
];

// A file that had the copied first-load spinner. The tell is the spinner icon
// followed by a literal "Loading…" (any suffix), on one line or the next.
const FIRST_LOAD_SPINNER = /<Loader2[^>]*\/>\s*(\{' '\}\s*)?Loading[^<]*…/;

test('the bone is decoration that respects reduced motion', () => {
  assert.match(PRIMITIVE, /aria-hidden="true"/);
  assert.match(PRIMITIVE, /animate-pulse/);
  assert.match(PRIMITIVE, /motion-reduce:animate-none/);
});

test('a loading region is announced once, for everything inside it', () => {
  const region = SKELETONS.match(/export function LoadingRegion[\s\S]*?\n\}/)?.[0];
  assert.ok(region, 'LoadingRegion should be exported from components/skeletons.jsx');
  assert.match(region, /role="status"/);
  assert.match(region, /aria-busy="true"/);
  assert.match(region, /className="sr-only">\{label\}/);
});

test('every body skeleton a page renders sits inside a LoadingRegion', () => {
  const offenders = [];
  for (const [file, name] of [...SETTINGS_CARDS, ...OTHER_SITES]) {
    const lines = read(file).split('\n');
    const importLine = lines.find((line) => line.includes("from '@/components/skeletons.jsx'"));
    assert.ok(importLine, `${file} should import from components/skeletons.jsx`);
    assert.ok(importLine.includes(name), `${file} should import ${name}`);
    assert.ok(importLine.includes('LoadingRegion'), `${file} should import LoadingRegion`);

    lines.forEach((line, index) => {
      if (!new RegExp(`<${name}\\b`).test(line)) return;
      const above = lines.slice(Math.max(0, index - 3), index).join('\n');
      if (!/<LoadingRegion\b/.test(above)) offenders.push(`${file}:${index + 1}`);
    });
  }
  assert.deepEqual(offenders, [], `Wrap these in <LoadingRegion label="…">:\n  ${offenders.join('\n  ')}`);
});

test('no first-load site shows the spinner-plus-"Loading…" line any more', () => {
  const files = new Set([...SETTINGS_CARDS, ...OTHER_SITES].map(([file]) => file));
  const offenders = [...files].filter((file) => FIRST_LOAD_SPINNER.test(read(file)));
  assert.deepEqual(offenders, [], `These fell back to a spinner for a first load: ${offenders.join(', ')}`);
});

test('the settings route fallback composes the same card skeletons the cards show', () => {
  const page = SKELETONS.match(/export function SettingsSkeleton[\s\S]*?\n\}/)?.[0];
  assert.ok(page, 'SettingsSkeleton should be exported');
  // Same bodies, same order as SettingsPage renders its cards — so the chunk
  // mounting on top of the fallback changes text, not layout.
  let cursor = 0;
  for (const [file, name] of SETTINGS_CARDS) {
    const at = page.indexOf(`<${name}`, cursor);
    assert.ok(at >= 0, `SettingsSkeleton should render <${name}> (the body ${file} shows) after the previous card`);
    cursor = at;
  }
});

test('each route falls back to its page skeleton, not the generic spinner', () => {
  const fallbacks = [...APP_SHELL.matchAll(/<LazyBoundary fallback=\{<(\w+)/g)].map((match) => match[1]);
  assert.deepEqual(fallbacks, ['RubricSkeleton', 'AnalyticsSkeleton', 'SettingsSkeleton']);
  assert.equal(/RouteFallback/.test(APP_SHELL), false, 'AppShell should not reach for RouteFallback');
  // Each fallback keeps the page's Back button working while the chunk loads.
  assert.equal((APP_SHELL.match(/Skeleton onBack=\{/g) || []).length, 3);
});

test('the analytics page and its route share one skeleton', () => {
  const analytics = read('AnalyticsPage.jsx');
  assert.equal(/function SkeletonBlock/.test(analytics), false, 'the page-private SkeletonBlock is replaced by the shared one');
  assert.match(analytics, /isFirstLoad \? \([\s\S]*?<AnalyticsSkeletonBody \/>/);
  assert.match(SKELETONS, /export function AnalyticsSkeleton\(\{ onBack \}\)[\s\S]*?<AnalyticsSkeletonBody \/>/);
});

test('the rubric page treats the moment before its first response as a wait, not an empty state', () => {
  const rubric = read('CommunicationRubricPanel.jsx');
  // isLoading starts true: the mount effect always fetches, so the first paint
  // must not say "No rubric loaded" / "No PDF available".
  assert.match(rubric, /const \[isLoading, setIsLoading\] = useState\(true\)/);
  assert.match(rubric, /const isFirstLoad = isLoading && !rubric/);
  assert.match(rubric, /\{isFirstLoad \? \([\s\S]*?<RubricCriteriaSkeleton \/>/);
  assert.match(rubric, /\{isFirstLoad \? \([\s\S]*?<RubricSourceSkeleton \/>/);
});

test('page-frame skeletons carry the titles of the pages they stand in for', () => {
  // The frames copy the pages' headers so nothing moves when the chunk mounts.
  // The copy is deliberate; this keeps it honest.
  for (const [file, strings] of [
    ['SettingsPage.jsx', ['Settings', 'Global options applied to every assessment run']],
    ['AnalyticsPage.jsx', ['Score Analytics', 'Assessment results stored in the database']],
    ['CommunicationRubricPanel.jsx', ['Communication Rubric', 'Editable']],
  ]) {
    const page = read(file);
    for (const text of strings) {
      assert.ok(page.includes(text), `${file} should still render "${text}"`);
      assert.ok(SKELETONS.includes(text), `components/skeletons.jsx should render "${text}" for ${file}`);
    }
  }
});

test('opening a session draws the workspace in outline instead of dimming the dashboard', () => {
  // The modal is gone: nothing fixed-position is gated on the workspace fetch.
  assert.equal(/isLoadingWorkspace && !isUploading && \(/.test(DASHBOARD), false);
  assert.equal(/workspaceLoadLabel/.test(DASHBOARD), false);
  // A pending navigation is its own state, rendered in the main slot …
  assert.match(DASHBOARD, /const \[workspaceLoad, setWorkspaceLoad\] = useState\(null\)/);
  assert.match(DASHBOARD, /\{workspaceLoad \? \(\s*<WorkspaceSkeleton layout=\{workspaceLoad\.layout\} label=\{workspaceLoad\.label\} \/>/);
  // … and the dashboard stands down while it shows.
  assert.match(DASHBOARD, /\{!showWorkspace && !workspaceLoad && \(/);
  // The chunk fallback is the same skeleton, in the loaded session's layout.
  assert.match(DASHBOARD, /<LazyBoundary fallback=\{<WorkspaceSkeleton layout=\{workspaceLayoutFor\(session\)\} \/>\}>/);
  // Every navigation into a workspace declares its outline: a saved session
  // (from the list projection), a clip child, and the two demo bundles.
  const layouts = [...DASHBOARD.matchAll(/setWorkspaceLoad\(\{[\s\S]*?layout: ([^,}\n]+)/g)].map((match) => match[1].trim());
  assert.deepEqual(layouts, [
    'workspaceLayoutFor(sessionIndex.find((entry) => String(entry.id) === String(sessionId)))',
    'WORKSPACE_LAYOUT.STANDARD',
    'WORKSPACE_LAYOUT.LONG',
    'WORKSPACE_LAYOUT.CLIP',
  ]);
  // A pending load is cleared where the fetch settles, and by "Back".
  assert.ok((DASHBOARD.match(/setWorkspaceLoad\(null\)/g) || []).length >= 6);
  // The busy flag still exists on its own: it also covers the demo re-run,
  // which stays on the clip list and must not swap the view for a placeholder.
  assert.match(DASHBOARD, /const \[isLoadingWorkspace, setIsLoadingWorkspace\]/);
});

test('the workspace skeleton takes the layout the session will open in', () => {
  const workspace = SKELETONS.match(/export function WorkspaceSkeleton[\s\S]*$/)?.[0];
  assert.ok(workspace);
  assert.match(workspace, /layout = WORKSPACE_LAYOUT\.STANDARD/);
  assert.match(workspace, /const isLong = layout === WORKSPACE_LAYOUT\.LONG/);
  assert.match(workspace, /const isClip = layout === WORKSPACE_LAYOUT\.CLIP/);
  assert.match(workspace, /<LoadingRegion label=\{label\}/);
  // Long and standard differ where the real view differs: the crop tabs under
  // the player and the clip-assessment card on the right, versus the
  // transcript timeline and the four result tabs.
  assert.match(workspace, /\{isLong \? \([\s\S]*?grid-cols-2[\s\S]*?\) : null\}/);
  assert.match(workspace, /sm:grid-cols-4/);
});

test('the saved-session list draws rows in outline on its first load, and dims on a refresh', () => {
  // "No sessions yet" and "not asked yet" are different answers; the index
  // starts as [] so a flag has to tell them apart.
  assert.match(DASHBOARD, /const \[hasLoadedSessionIndex, setHasLoadedSessionIndex\] = useState\(false\)/);
  assert.match(DASHBOARD, /setSessionIndex\(sessions\);\s*setHasLoadedSessionIndex\(true\)/);
  // Skeleton on the first fetch only …
  assert.match(DASHBOARD, /\{sessionIndexLoading && !hasLoadedSessionIndex \? \(\s*<LoadingRegion label="Loading saved sessions">/);
  // … the empty state waits for a real answer …
  assert.match(DASHBOARD, /\{hasLoadedSessionIndex && !sessionIndexLoading && visibleSessions\.length === 0 \? \(/);
  // … and a refresh with rows on screen keeps them, dimmed, still clickable.
  assert.match(DASHBOARD, /sessionIndexLoading && hasLoadedSessionIndex \? 'opacity-60' : ''/);
  assert.match(DASHBOARD, /aria-busy=\{sessionIndexLoading && hasLoadedSessionIndex \? 'true' : undefined\}/);
  assert.equal(/Loading sessions\.\.\./.test(DASHBOARD), false, 'the plain "Loading sessions..." box is gone');
});

test('the notification bell and feed wait for the first poll before saying there is nothing', () => {
  const notifications = read('notifications.jsx');
  // The hook owns the flag: true once the first poll settles either way, reset
  // on logout so the next login seeds fresh.
  assert.match(notifications, /const \[hasLoaded, setHasLoaded\] = useState\(false\)/);
  assert.match(notifications, /\} finally \{[\s\S]*?setHasLoaded\(true\);\s*\}\s*\}, \[\]\);/);
  assert.match(notifications, /if \(!enabled\) \{[\s\S]*?setHasLoaded\(false\);/);
  assert.match(notifications, /return \{ items, unreadCount, hasLoaded, toast, dismiss, dismissAll \}/);
  // Both consumers default to today's behaviour for a caller that passes
  // nothing, and only an empty list before the first answer is bone — a row
  // pushed over the change stream is shown.
  assert.match(notifications, /export function NotificationBell\(\{ items, unreadCount, hasLoaded = true,/);
  assert.match(notifications, /export function NotificationFeed\(\{ items, unreadCount, hasLoaded = true,/);
  assert.equal((notifications.match(/\{!hasLoaded && items\.length === 0 \? \(/g) || []).length, 2);
  // The dashboard is the caller that knows, and passes it to both.
  assert.equal((DASHBOARD.match(/hasLoaded=\{notifications\.hasLoaded\}/g) || []).length, 2);
});

test('the workspace shows the audio artefact and the cohort charts in outline while they are fetched', () => {
  const workspace = read('workspace/SessionWorkspace.jsx');
  // The late audio-professionalism fetch raises a flag for its own card only,
  // and lowers it however the fetch ends — including a session switch mid-way.
  assert.match(workspace, /const \[isLoadingAudioProf, setIsLoadingAudioProf\] = useState\(false\)/);
  assert.match(workspace, /setIsLoadingAudioProf\(true\);\s*try \{\s*const body = await apiJson\(`\/api\/sessions\/\$\{session\.id\}\/audio-professionalism`/);
  assert.match(workspace, /\} finally \{\s*if \(!cancelled\) setIsLoadingAudioProf\(false\);/);
  assert.match(workspace, /cancelled = true;[\s\S]*?setIsLoadingAudioProf\(false\);\s*\};/);
  // The empty state ("check that openSMILE is installed") is reached only
  // when there is neither a payload nor a fetch in flight.
  assert.match(workspace, /\) : isLoadingAudioProf \? \([\s\S]*?<AudioProfessionalismSkeleton \/>[\s\S]*?\) : \(\s*<div className="space-y-2">/);
  // The cohort placeholder stands in the charts' own slot, first computation only.
  assert.match(workspace, /isLoadingClipSummaries && !clipSummaries && !demoLongVideoSummaries \? \(\s*<LoadingRegion label="Building cohort summary charts">\s*<CohortSummarySkeleton \/>/);
  assert.equal(/Building cohort summary charts\.\.\./.test(workspace), false, 'the spinner card is gone');
  // One shape for both waits: the workspace placeholder draws the same card.
  assert.match(SKELETONS, /export function WorkspaceSkeleton[\s\S]*?<AudioProfessionalismSkeleton \/>/);
});

test('the generic route spinner survives only as LazyBoundary\'s last resort', () => {
  assert.match(LAZY_ROUTE, /export function RouteFallback/);
  assert.match(LAZY_ROUTE, /<Suspense fallback=\{fallback \?\? <RouteFallback \/>\}>/);
  // PanelFallback had one caller — the workspace slot — and that is a skeleton now.
  assert.equal(/PanelFallback/.test(LAZY_ROUTE), false);
  assert.equal(/PanelFallback/.test(DASHBOARD), false);
});
