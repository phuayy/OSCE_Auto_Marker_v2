import React, { useEffect, useState } from 'react';
import { CheckCircle2, KeyRound, Monitor, Moon, ShieldCheck, Sun, SunMoon, UserCircle2 } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { PageHeader } from '@/components/PageHeader.jsx';
import { AccountFormSkeleton, LoadingRegion } from '@/components/skeletons.jsx';
import { changePasswordRequest, refreshIdentity } from '@/auth';
import { identityLabel, roleLabel } from '@/lib/authz';
import { THEME_PREFERENCES, ThemePreference, describeThemePreference } from '@/lib/theme';
import { useTheme } from '@/lib/useTheme';
import { PASSWORD_MIN_LENGTH, passwordProblems } from '@/lib/userAdmin';

// The signed-in user's own account (#/account): who the server says they
// are, the one thing anyone may change about their own access — the
// password — and how the app looks on this device. A password change ends
// every other session on the account; the server answers with a fresh token
// so this tab stays signed in. The appearance choice is the browser's, not
// the account's (see src/lib/theme.js), which the card says in as many words.

const FIELD =
  'w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm placeholder:text-slate-400 focus:border-cyan-500 focus:outline-none focus:ring-2 focus:ring-cyan-500/30 disabled:opacity-60';

function Field({ id, label, hint, ...inputProps }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-xs font-semibold text-slate-600">
        {label}
      </label>
      <input id={id} className={FIELD} {...inputProps} />
      {hint ? <p className="text-[11px] text-slate-500">{hint}</p> : null}
    </div>
  );
}

export default function AccountPage({ identity: initialIdentity = null, onBack, onIdentityChanged = null }) {
  const [identity, setIdentity] = useState(initialIdentity);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  // The stored copy is what the header rendered; the server's answer is what
  // an administrator may have changed since (the role, the display name).
  useEffect(() => {
    let cancelled = false;
    refreshIdentity().then((fresh) => {
      if (!cancelled && fresh) {
        setIdentity(fresh);
        onIdentityChanged?.(fresh);
      }
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const identifiers = identity ? [identity.username, identity.email, String(identity.email || '').split('@')[0]] : [];
  const problems = passwordProblems({ password: newPassword, confirm, identifiers });

  async function handleSubmit(event) {
    event.preventDefault();
    if (saving) return;
    if (problems.length) {
      setError(problems[0]);
      return;
    }
    setError('');
    setSaved(false);
    setSaving(true);
    try {
      const body = await changePasswordRequest(currentPassword, newPassword);
      setSaved(true);
      setCurrentPassword('');
      setNewPassword('');
      setConfirm('');
      if (body?.username) {
        setIdentity((current) => ({ ...(current || {}), ...body }));
        onIdentityChanged?.(body);
      }
    } catch (submitError) {
      setError(submitError.message || 'The password could not be changed.');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <PageHeader
        icon={<KeyRound className="h-5 w-5" />}
        title="Your account"
        subtitle="Sign-in details, password and appearance"
        onBack={onBack}
        backTitle="Back to dashboard"
      />

      <main id="main" className="mx-auto flex max-w-3xl flex-col gap-6 px-6 py-8">
        <Card className="border-slate-200 bg-white shadow-sm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <UserCircle2 className="h-5 w-5 text-cyan-700" aria-hidden="true" />
              Signed in as
            </CardTitle>
            <CardDescription>What the server knows about this account.</CardDescription>
          </CardHeader>
          <CardContent>
            {identity ? (
              <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-[auto_1fr]">
                <dt className="text-slate-500">Name</dt>
                <dd className="font-medium text-slate-900">{identityLabel(identity)}</dd>
                <dt className="text-slate-500">Username</dt>
                <dd className="font-mono text-slate-800">{identity.username}</dd>
                <dt className="text-slate-500">Email</dt>
                <dd className="text-slate-800">{identity.email || <span className="text-slate-400">none on file</span>}</dd>
                <dt className="text-slate-500">Role</dt>
                <dd>
                  <Badge variant={identity.role === 'admin' ? 'accent' : 'neutral'}>{roleLabel(identity.role)}</Badge>
                </dd>
              </dl>
            ) : (
              <LoadingRegion label="Loading your account">
                <AccountFormSkeleton />
              </LoadingRegion>
            )}
          </CardContent>
        </Card>

        <Card className="border-slate-200 bg-white shadow-sm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <ShieldCheck className="h-5 w-5 text-cyan-700" aria-hidden="true" />
              Change password
            </CardTitle>
            <CardDescription>
              Every other session on this account is signed out when the password changes; this one stays signed in.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form className="flex max-w-md flex-col gap-4" onSubmit={handleSubmit}>
              <Field
                id="current-password"
                label="Current password"
                type="password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
                disabled={saving}
                required
              />
              <Field
                id="new-password"
                label="New password"
                type="password"
                autoComplete="new-password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                disabled={saving}
                required
                minLength={PASSWORD_MIN_LENGTH}
                hint={`At least ${PASSWORD_MIN_LENGTH} characters. A long phrase beats a short puzzle.`}
              />
              <Field
                id="confirm-new-password"
                label="Confirm new password"
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
                disabled={saving}
                required
              />
              {error ? (
                <p role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
                  {error}
                </p>
              ) : null}
              {saved ? (
                <p role="status" className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
                  <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                  Password changed. Other sessions have been signed out.
                </p>
              ) : null}
              <div>
                <Button type="submit" className="gap-2" disabled={saving || !currentPassword || !newPassword || !confirm}>
                  <KeyRound className="h-4 w-4" aria-hidden="true" />
                  {saving ? 'Saving…' : 'Change password'}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>

        <AppearanceCard />
      </main>
    </div>
  );
}

const PREFERENCE_ICONS = {
  [ThemePreference.SYSTEM]: Monitor,
  [ThemePreference.LIGHT]: Sun,
  [ThemePreference.DARK]: Moon,
};

// The three-way choice the header's one-click toggle stands in for. A
// radio-card group like every other "choose one" in the app; it applies on
// click and needs no save, because there is nothing to send anywhere.
function AppearanceCard() {
  const { preference, setPreference } = useTheme();
  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <SunMoon className="h-5 w-5 text-cyan-700" aria-hidden="true" />
          Appearance
        </CardTitle>
        <CardDescription>
          Light, dark, or whatever this device is set to. Remembered in this browser, not on your account.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div role="radiogroup" aria-label="Appearance" className="grid gap-2 sm:grid-cols-3">
          {THEME_PREFERENCES.map((value) => {
            const { label, description } = describeThemePreference(value);
            const Icon = PREFERENCE_ICONS[value];
            const selected = preference === value;
            return (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={selected}
                onClick={() => setPreference(value)}
                className={`flex items-start gap-3 rounded-xl border p-3 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 ${
                  selected ? 'border-cyan-400 bg-cyan-50 ring-2 ring-cyan-200' : 'border-slate-200 bg-slate-50 hover:border-slate-300'
                }`}
              >
                <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${selected ? 'text-cyan-700' : 'text-slate-500'}`} aria-hidden="true" />
                <span className="min-w-0">
                  <span className="block text-sm font-semibold text-slate-800">{label}</span>
                  <span className="block text-[11px] text-slate-500">{description}</span>
                </span>
              </button>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
