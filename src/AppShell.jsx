import React, { useEffect, useState } from 'react';
import { AnimatePresence, MotionConfig, motion } from 'framer-motion';
import { getStoredAuth, installFetchAuthShim, logout, refreshIdentity } from '@/auth';
import { useHashRoute } from '@/lib/useHashRoute';
import { isAdminView, isPublicView } from '@/lib/navigation';
import { canManageUsers } from '@/lib/authz';
import { LazyBoundary, lazyComponent, preloadComponent } from '@/lib/lazyRoute';
import {
  AccountSkeleton,
  AnalyticsSkeleton,
  AuthScreenSkeleton,
  RubricSkeleton,
  SettingsSkeleton,
  UsersSkeleton,
} from '@/components/skeletons.jsx';
import { SkipToContent } from '@/components/PageHeader.jsx';
import LoginScreen from '@/LoginScreen.jsx';
import OSCEAiMarkerMockup from '@/OSCEAiMarkerMockup.jsx';
import { NotificationToast, useNotifications } from '@/notifications.jsx';

// The dashboard is what a logged-in user lands on, so it stays in the entry
// chunk. The other routes are whole pages reached by a deliberate click —
// or, for the three pre-login screens, by a link in an email — so each is
// its own chunk, fetched on hover (see the mockup's nav handlers) and at the
// latest when the route changes.
//
// While a chunk loads, the route shows the same skeleton the page itself shows
// until its first response — the page's header for real, its content as
// placeholders in the shape the data will take — so a cold visit is one
// continuous wait rather than a spinner followed by a second "Loading…".
const CommunicationRubricPanel = lazyComponent(() => import('@/CommunicationRubricPanel.jsx'));
const AnalyticsPage = lazyComponent(() => import('@/AnalyticsPage.jsx'));
const SettingsPage = lazyComponent(() => import('@/SettingsPage.jsx'));
const UsersAdminPage = lazyComponent(() => import('@/UsersAdminPage.jsx'));
const AccountPage = lazyComponent(() => import('@/AccountPage.jsx'));
const AcceptInviteScreen = lazyComponent(() => import('@/AcceptInviteScreen.jsx'));
const ForgotPasswordScreen = lazyComponent(() => import('@/ForgotPasswordScreen.jsx'));
const ResetPasswordScreen = lazyComponent(() => import('@/ResetPasswordScreen.jsx'));

const ROUTE_CHUNKS = {
  rubric: CommunicationRubricPanel,
  analytics: AnalyticsPage,
  settings: SettingsPage,
  users: UsersAdminPage,
  account: AccountPage,
};

installFetchAuthShim();

export default function AppShell() {
  const [authState, setAuthState] = useState(() => getStoredAuth());
  // What the login screen shows after an invitation or reset was completed:
  // a one-line notice and the account it concerns, so the form only needs
  // the password typed once more.
  const [loginHandoff, setLoginHandoff] = useState({ notice: '', username: '' });
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
    window.addEventListener('osce:auth:required', handleExpired);
    return () => {
      window.removeEventListener('osce:auth:expired', handleExpired);
      window.removeEventListener('osce:auth:required', handleExpired);
    };
  }, []);

  // The role gates what the header offers, and an administrator can change it
  // while this tab is open. Re-reading it once per token keeps a reload
  // honest; the API checks the account row on every request regardless.
  const token = authState?.token || '';
  useEffect(() => {
    if (!token) return undefined;
    let cancelled = false;
    refreshIdentity().then((fresh) => {
      if (!cancelled && fresh) setAuthState(fresh);
    });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const isAdmin = canManageUsers(authState);

  // A non-administrator on an administrators-only route is sent to the
  // dashboard; the URL is corrected in place so Back does not bounce again.
  const gatedToDashboard = Boolean(authState) && isAdminView(route.view) && !isAdmin;
  useEffect(() => {
    if (gatedToDashboard) navigate({ view: 'dashboard' }, { replace: true });
  }, [gatedToDashboard, navigate]);

  function handleLoggedIn(result) {
    setLoginHandoff({ notice: '', username: '' });
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

  function goToLogin() {
    navigate({ view: 'dashboard' });
  }

  // `reducedMotion="user"` makes every framer-motion transition in the app
  // honour the OS "reduce motion" setting: transforms are skipped, opacity
  // fades stay. Set once here so no page has to remember.
  //
  // The three pre-login screens render for everyone: they are what an
  // emailed link opens, and a signed-in administrator following one to check
  // it should see the same thing the invitee will.
  if (isPublicView(route.view)) {
    return (
      <MotionConfig reducedMotion="user">
        {route.view === 'acceptInvite' ? (
          <LazyBoundary fallback={<AuthScreenSkeleton title="Checking your invitation" />}>
            <AcceptInviteScreen
              token={route.token}
              onAccepted={({ username }) => setLoginHandoff({ notice: 'Your account is ready. Sign in to get started.', username })}
              onGoToLogin={goToLogin}
            />
          </LazyBoundary>
        ) : route.view === 'resetPassword' ? (
          <LazyBoundary fallback={<AuthScreenSkeleton title="Checking your link" />}>
            <ResetPasswordScreen
              token={route.token}
              onReset={({ username }) => setLoginHandoff({ notice: 'Password changed. Sign in with the new one.', username })}
              onGoToLogin={goToLogin}
            />
          </LazyBoundary>
        ) : (
          <LazyBoundary fallback={<AuthScreenSkeleton title="Forgot your password?" />}>
            <ForgotPasswordScreen onGoToLogin={goToLogin} />
          </LazyBoundary>
        )}
      </MotionConfig>
    );
  }

  if (!authState) {
    return (
      <MotionConfig reducedMotion="user">
        <LoginScreen
          onLoggedIn={handleLoggedIn}
          onForgotPassword={() => navigate({ view: 'forgotPassword' })}
          notice={loginHandoff.notice}
          initialUsername={loginHandoff.username}
        />
      </MotionConfig>
    );
  }

  const view = gatedToDashboard ? 'dashboard' : route.view;

  return (
    <MotionConfig reducedMotion="user">
      <SkipToContent />
      <NotificationToast toast={notifications.toast} onDismiss={notifications.dismiss} />
      <AnimatePresence mode="wait">
      {view === 'rubric' ? (
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
      ) : view === 'analytics' ? (
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
      ) : view === 'settings' ? (
        <motion.div
          key="settings"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.25 }}
        >
          <LazyBoundary fallback={<SettingsSkeleton onBack={() => navigate({ view: 'dashboard' })} isAdmin={isAdmin} />}>
            <SettingsPage onBack={() => navigate({ view: 'dashboard' })} isAdmin={isAdmin} />
          </LazyBoundary>
        </motion.div>
      ) : view === 'users' ? (
        <motion.div
          key="users"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.25 }}
        >
          <LazyBoundary fallback={<UsersSkeleton onBack={() => navigate({ view: 'dashboard' })} />}>
            <UsersAdminPage onBack={() => navigate({ view: 'dashboard' })} />
          </LazyBoundary>
        </motion.div>
      ) : view === 'account' ? (
        <motion.div
          key="account"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.25 }}
        >
          <LazyBoundary fallback={<AccountSkeleton onBack={() => navigate({ view: 'dashboard' })} />}>
            <AccountPage
              identity={authState}
              onBack={() => navigate({ view: 'dashboard' })}
              onIdentityChanged={() => setAuthState(getStoredAuth())}
            />
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
            authUser={authState}
            routeSessionId={route.sessionId}
            onNavigateSession={(sessionId) => navigate({ view: 'dashboard', sessionId })}
            onOpenRubric={() => navigate({ view: 'rubric' })}
            onOpenAnalytics={() => navigate({ view: 'analytics' })}
            onOpenSettings={() => navigate({ view: 'settings' })}
            onOpenUsers={isAdmin ? () => navigate({ view: 'users' }) : null}
            onOpenAccount={() => navigate({ view: 'account' })}
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
