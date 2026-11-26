import { useEffect, useState } from 'react';
import { AlertTriangle, Info } from 'lucide-react';
import { apiJson } from '@/lib/apiFetch';
import { describeUploadProbe, watchUploadProbe } from '@/lib/uploadProbe';

const TONES = {
  warning: { box: 'border-amber-200 bg-amber-50 text-amber-800', Icon: AlertTriangle },
  info: { box: 'border-slate-200 bg-slate-50 text-slate-700', Icon: Info },
};

/**
 * What the server saw in the first bytes of the video while it uploads. Renders
 * nothing unless there is a finding — it is advisory, and the common case is
 * that there is nothing to say.
 */
export default function UploadProbeNotice({ uploadId }) {
  const [probe, setProbe] = useState(null);

  useEffect(() => {
    setProbe(null);
    if (!uploadId) return undefined;
    const controller = new AbortController();
    watchUploadProbe({ apiJson, uploadId, onProbe: setProbe, signal: controller.signal });
    return () => controller.abort();
  }, [uploadId]);

  const notice = describeUploadProbe(probe);
  if (!notice) return null;
  const { box, Icon } = TONES[notice.tone];
  return (
    <div role="status" className={`flex gap-2 rounded-xl border p-3 text-xs ${box}`}>
      <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <div className="space-y-1">
        <p className="font-semibold">{notice.headline}</p>
        {notice.details.map((detail) => (
          <p key={detail}>{detail}</p>
        ))}
      </div>
    </div>
  );
}
