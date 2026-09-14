import React from 'react';
import { Loader2 } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { describeSessionStatus } from '@/lib/sessionStatus';

// A session's status as a word in the tone that word deserves. `busy`
// overrides the table's own reading: an upload still leaving this tab is
// busy however the server describes the row.
export function SessionStatusBadge({ status, busy, className }) {
  const described = describeSessionStatus(status);
  const spinning = busy ?? described.busy;
  return (
    <Badge variant={described.tone} className={className}>
      {spinning ? (
        <Loader2 className="mr-1 h-3 w-3 animate-spin motion-reduce:animate-none" aria-hidden="true" />
      ) : null}
      {described.label}
    </Badge>
  );
}
