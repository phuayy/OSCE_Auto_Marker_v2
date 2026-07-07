import React, { useEffect, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { getStoredAuth, installFetchAuthShim, logout } from '@/auth';
import { useHashRoute } from '@/lib/useHashRoute';
import LoginScreen from '@/LoginScreen.jsx';
import CommunicationRubricPanel from '@/CommunicationRubricPanel.jsx';
import OSCEAiMarkerMockup from '@/OSCEAiMarkerMockup.jsx';

installFetchAuthShim();

export default function AppShell() {
  const [authState, setAuthState] = useState(() => getStoredAuth());
  // The hash route is the source of truth for which page is shown, so a reload
  // (or a shared link) lands the user on the same page rather than the landing.
  const [route, navigate] = useHashRoute();

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

  function handleLogout() {
    logout();
    setAuthState(null);
  }

  if (!authState) {
    return <LoginScreen onLoggedIn={handleLoggedIn} />;
  }

  return (
    <AnimatePresence mode="wait">
      {route.view === 'rubric' ? (
        <motion.div
          key="rubric"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.25 }}
        >
          <CommunicationRubricPanel onBack={() => navigate({ view: 'dashboard' })} />
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
            onLogout={handleLogout}
          />
        </motion.div>
      )}
    </AnimatePresence>
  );
}
