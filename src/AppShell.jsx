import React, { useEffect, useState } from 'react';
import { AnimatePresence, MotionConfig, motion } from 'framer-motion';
import { getStoredAuth, installFetchAuthShim, logout } from '@/auth';
import { useHashRoute } from '@/lib/useHashRoute';
import { LazyBoundary, lazyComponent, preloadComponent } from '@/lib/lazyRoute';
import { AnalyticsSkeleton, RubricSkeleton, SettingsSkeleton } from '@/components/skeletons.jsx';
import { SkipToContent } from '@/components/PageHeader.jsx';
import LoginScreen from '@/LoginScreen.jsx';
import OSCEAiMarkerMockup from '@/OSCEAiMarkerMockup.jsx';
import { NotificationToast, useNotifications } from '@/notifications.jsx';

// The dashboard is what a logged-in user lands on, so it stays in the entry
// chunk. The other three routes are whole pages reached by a deliberate click:
// each is its own chunk, fetched on hover (see the mockup's nav handlers) and
// at the latest when the route changes.
//
// While a chunk loads, the route shows the same skeleton the page itself shows
// until its first response — the page's header for real, its content as
// placeholders in the shape the data will take — so a cold visit is one
// continuous wait rather than a spinner followed by a second "Loading…".
const CommunicationRubricPanel = lazyComponent(() => import('@/CommunicationRubricPanel.jsx'));
const AnalyticsPage = lazyComponent(() => import('@/AnalyticsPage.jsx'));
const SettingsPage = lazyComponent(() => import('@/SettingsPage.jsx'));

const ROUTE_CHUNKS = {
  rubric: CommunicationRubricPanel,
  analytics: AnalyticsPage,
  settings: SettingsPage,
};

installFetchAuthShim();

export default function AppShell() {
  const [authState, setAuthState] = useState(() => getStoredAuth());
  // The hash route is the source of truth for which page is shown, so a reload
  // (or a shared link) lands the user on the same page rather than the landing.
  const [route, navigate] = useHashRoute();
  // Global notification state: the toast renders on every page; the bell and
  // feed live inside the dashboard and receive this same state as props.
  // (Hook called unconditionally per hooks rules; the auth flag gates the
  // actual polling so nothing hits the API pre-login.)
  const notifications = useNotifications(Boolean(authState));

  useEffect(() => {
    function handleExpired() {
      setAuthState(null);
    }
    window.addEventListener('osce:auth:expired', handleExpired);
    return () => window.removeEventListener('osce:auth:expired', handleExpired);
  }, []);

  function handleLoggedIn(result) {
    // Keep the requested route (deep link) intact after logging in.
    setAuthState(getStoredAuth() || result);
  }

  // Called from the dashboard's nav on hover/focus: by the time the click
  // lands the route's chunk is usually already parsed, so the split is
  // invisible to the user.
  function preloadRoute(view) {
    preloadComponent(ROUTE_CHUNKS[view]);
  }

  function handleLogout() {
    logout();
    setAuthState(null);
  }

  // `reducedMotion="user"` makes every framer-motion transition in the app
  // honour the OS "reduce motion" setting: transforms are skipped, opacity
  // fades stay. Set once here so no page has to remember.
  if (!authState) {
    return (
      <MotionConfig reducedMotion="user">
        <LoginScreen onLoggedIn={handleLoggedIn} />
      </MotionConfig>
    );
  }

  return (
    <MotionConfig reducedMotion="user">
      <SkipToContent />
      <NotificationToast toast={notifications.toast} onDismiss={notifications.dismiss} />
      <AnimatePresence mode="wait">
      {route.view === 'rubric' ? (
        <motion.div
          key="rubric"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.25 }}
        >
          <LazyBoundary fallback={<RubricSkeleton onBack={() => navigate({ view: 'dashboard' })} />}>
            <CommunicationRubricPanel onBack={() => navigate({ view: 'dashboard' })} />
          </LazyBoundary>
        </motion.div>
      ) : route.view === 'analytics' ? (
        <motion.div
          key="analytics"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.25 }}
        >
          <LazyBoundary fallback={<AnalyticsSkeleton onBack={() => navigate({ view: 'dashboard' })} />}>
            <AnalyticsPage onBack={() => navigate({ view: 'dashboard' })} />
          </LazyBoundary>
        </motion.div>
      ) : route.view === 'settings' ? (
        <motion.div
          key="settings"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.25 }}
        >
          <LazyBoundary fallback={<SettingsSkeleton onBack={() => navigate({ view: 'dashboard' })} />}>
            <SettingsPage onBack={() => navigate({ view: 'dashboard' })} />
          </LazyBoundary>
        </motion.div>
      ) : (
        <motion.div
          key="dashboard"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.25 }}
        >
          <OSCEAiMarkerMockup
            authUsername={authState.username}
            routeSessionId={route.sessionId}
            onNavigateSession={(sessionId) => navigate({ view: 'dashboard', sessionId })}
            onOpenRubric={() => navigate({ view: 'rubric' })}
            onOpenAnalytics={() => navigate({ view: 'analytics' })}
            onOpenSettings={() => navigate({ view: 'settings' })}
            onPreloadRoute={preloadRoute}
            onLogout={handleLogout}
            notifications={notifications}
          />
        </motion.div>
      )}
      </AnimatePresence>
    </MotionConfig>
  );
}
