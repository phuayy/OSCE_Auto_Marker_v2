import React, { useState } from 'react';
import { Eye, EyeOff, Lock } from 'lucide-react';
import { AuthField, AuthTrailingButton } from '@/components/AuthShell.jsx';
import { PASSWORD_MIN_LENGTH } from '@/lib/userAdmin';

// "Choose a password" twice, on the dark ground: what both the invitation and
// the reset screen ask for. One show/hide toggle governs both fields, since
// revealing one and not the other helps nobody.
export function AuthNewPasswordFields({ password, confirm, onPasswordChange, onConfirmChange, disabled = false, autoFocus = false }) {
  const [show, setShow] = useState(false);
  const type = show ? 'text' : 'password';
  const toggle = (
    <AuthTrailingButton onClick={() => setShow((previous) => !previous)} disabled={disabled} aria-label={show ? 'Hide passwords' : 'Show passwords'}>
      {show ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
    </AuthTrailingButton>
  );
  return (
    <>
      <AuthField
        id="new-password"
        name="new-password"
        label="New password"
        icon={Lock}
        accent="purple"
        autoComplete="new-password"
        type={type}
        value={password}
        onChange={(event) => onPasswordChange(event.target.value)}
        disabled={disabled}
        autoFocus={autoFocus}
        required
        minLength={PASSWORD_MIN_LENGTH}
        hint={`At least ${PASSWORD_MIN_LENGTH} characters. A long phrase beats a short puzzle.`}
        trailing={toggle}
      />
      <AuthField
        id="confirm-password"
        name="confirm-password"
        label="Confirm password"
        icon={Lock}
        accent="purple"
        autoComplete="new-password"
        type={type}
        value={confirm}
        onChange={(event) => onConfirmChange(event.target.value)}
        disabled={disabled}
        required
      />
    </>
  );
}
