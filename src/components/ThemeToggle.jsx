import React from 'react';
import { Moon, Sun } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { describeThemeToggle } from '@/lib/theme';
import { useTheme } from '@/lib/useTheme';

// One click between light and dark, in every page's header. The icon shows
// the theme a click *gives* and the label says so — the pattern of a
// Mute / Unmute control, whose name changes rather than carrying a pressed
// state. The three-way choice (System / Light / Dark) is on the Account page;
// this button sets an explicit theme, so a user on "System" who clicks gets
// the opposite of what they see, and keeps it.
export function ThemeToggle({ className }) {
  const { theme, toggle } = useTheme();
  const dark = theme === 'dark';
  const label = describeThemeToggle(theme);
  const Icon = dark ? Sun : Moon;
  return (
    <Button variant="outline" size="icon-sm" onClick={toggle} aria-label={label} title={label} className={className}>
      <Icon className="h-4 w-4" aria-hidden="true" />
    </Button>
  );
}
