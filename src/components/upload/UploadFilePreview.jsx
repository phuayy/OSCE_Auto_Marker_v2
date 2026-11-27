// What the chosen files look like to the browser and to the server, before a
// byte is uploaded: a player on the existing object URL, the facts a person
// checks (length, picture size, file size), a verdict chip, and plain-language
// warnings. The rules live in src/lib/uploadPreview.js; this only gathers what
// the <video> element and the file's own bytes say and renders the answer.
//
// It blocks nothing itself. When a file is one the server will certainly refuse
// (a rejected recording, a case study that is not a PDF, empty or over its
// limit), `onBlockingReason` reports why so the form can disable Start.
import React, { useEffect, useMemo, useRef, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { apiJson } from '@/lib/apiFetch';
import { formatRuntime } from '@/lib/format';
import { readIsoBmffInfo } from '@/lib/isoBmff';
import {
  Severity,
  Verdict,
  assessPdfFile,
  assessVideoFile,
  canPlayTypeFor,
  describeCodecs,
  describeVerdict,
  PREVIEW_UNAVAILABLE_TEXT,
  fetchUploadLimits,
  formatFileSize,
  previewSurface,
  readPdfHeaderCheck,
  startBlockingReason,
  shouldReadContainer,
} from '@/lib/uploadPreview';

// A browser that neither loads nor refuses a file (some stall on formats they
// do not know) must not leave the chip on "Checking" for ever.
const CHECK_TIMEOUT_MS = 8000;

const VERDICT_TONE = {
  [Verdict.PLAYS]: 'success',
  [Verdict.WILL_CONVERT]: 'info',
  [Verdict.REJECTED]: 'danger',
  [Verdict.UNKNOWN]: 'neutral',
};

const SEVERITY_CLASS = {
  [Severity.ERROR]: 'text-rose-700',
  [Severity.WARNING]: 'text-amber-800',
  [Severity.INFO]: 'text-slate-600',
};

function fileFacts(file) {
  return file ? { name: file.name, type: file.type, size: file.size } : null;
}

export default function UploadFilePreview({ videoFile, videoUrl, caseStudyFile, onBlockingReason }) {
  const [limits, setLimits] = useState(null);
  useEffect(() => {
    let live = true;
    fetchUploadLimits(apiJson).then((value) => {
      if (live) setLimits(value);
    });
    return () => {
      live = false;
    };
  }, []);

  // Each async answer is stored with what it answered about, and only used
  // while that is still the chosen file: pick A then B quickly and A's late
  // answer is simply never read.
  const [containerState, setContainerState] = useState(null);
  const [timedOutFile, setTimedOutFile] = useState(null);
  useEffect(() => {
    if (!videoFile) return undefined;
    let live = true;
    if (shouldReadContainer(fileFacts(videoFile))) {
      readIsoBmffInfo(videoFile).then((info) => {
        if (live) setContainerState({ file: videoFile, info });
      });
    }
    const timer = setTimeout(() => setTimedOutFile(videoFile), CHECK_TIMEOUT_MS);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [videoFile]);

  // The element's own answers belong to the URL it was given. The dashboard
  // sets that URL one render after the file, so an event from the previous
  // file's (revoked) URL must not be read as this file's failure.
  const [playerState, setPlayerState] = useState(null);
  const latestUrl = useRef(videoUrl);
  latestUrl.current = videoUrl;
  function onPlayerEvent(event, next) {
    if (event.currentTarget.src !== latestUrl.current) return;
    setPlayerState({ url: event.currentTarget.src, ...next });
  }

  const needsContainer = Boolean(videoFile) && shouldReadContainer(fileFacts(videoFile));
  const containerRead = containerState?.file === videoFile;
  const container = containerRead ? containerState.info : null;
  const player = playerState?.url === videoUrl ? playerState : null;

  const canPlay = useMemo(() => {
    if (!videoFile || typeof document === 'undefined') return '';
    const type = canPlayTypeFor(fileFacts(videoFile), container, limits);
    return type ? document.createElement('video').canPlayType(type) : '';
  }, [videoFile, container, limits]);

  const assessment = useMemo(() => {
    if (!videoFile) return null;
    return assessVideoFile({
      file: fileFacts(videoFile),
      limits,
      container,
      browser: {
        canPlay,
        metadataLoaded: Boolean(player?.loaded),
        decodeError: Boolean(player?.failed),
        durationSeconds: player?.durationSeconds,
        videoWidth: player?.videoWidth,
        videoHeight: player?.videoHeight,
      },
    });
  }, [videoFile, limits, container, canPlay, player]);

  const verdict = assessment?.verdict ?? null;

  const settled = Boolean(player?.failed)
    || (Boolean(player?.loaded) && (!needsContainer || containerRead))
    || timedOutFile === videoFile;

  const [pdfState, setPdfState] = useState(null);
  useEffect(() => {
    if (!caseStudyFile) return undefined;
    let live = true;
    readPdfHeaderCheck(caseStudyFile).then((ok) => {
      if (live) setPdfState({ file: caseStudyFile, ok });
    });
    return () => {
      live = false;
    };
  }, [caseStudyFile]);
  const pdfAssessment = caseStudyFile
    ? assessPdfFile({
      file: fileFacts(caseStudyFile),
      limits,
      headerOk: pdfState?.file === caseStudyFile ? pdfState.ok : null,
    })
    : null;

  const blockingText = startBlockingReason(assessment, pdfAssessment);
  const onBlockingReasonRef = useRef(onBlockingReason);
  onBlockingReasonRef.current = onBlockingReason;
  useEffect(() => {
    onBlockingReasonRef.current?.(blockingText);
  }, [blockingText]);
  useEffect(() => () => onBlockingReasonRef.current?.(null), []);

  if (!videoFile && !caseStudyFile) return null;

  const facts = assessment?.facts;
  const checking = assessment && verdict !== Verdict.REJECTED && !settled;
  const showPlayer = videoFile && videoUrl && !player?.failed;
  const surface = videoFile ? previewSurface({
    verdict,
    loaded: player?.loaded,
    failed: player?.failed,
    videoWidth: player?.videoWidth,
  }) : 'player';
  const placeholder = surface === 'placeholder';
  const details = facts ? describeCodecs(facts) : '';
  const summary = facts && [
    facts.durationSeconds ? formatRuntime(facts.durationSeconds) : null,
    facts.width && facts.height ? `${facts.width} x ${facts.height}` : null,
    formatFileSize(videoFile.size),
  ].filter(Boolean).join(' · ');

  return (
    <div className="space-y-3" data-testid="upload-file-preview">
      {videoFile ? (
        <figure className="m-0 space-y-2">
          {showPlayer ? (
            <video
              key={videoUrl}
              src={videoUrl}
              muted
              controls
              playsInline
              preload="metadata"
              aria-label={`Preview of ${videoFile.name}`}
              className={`${placeholder ? 'hidden' : 'block'} aspect-video w-full rounded-lg border border-slate-200 bg-black`}
              onLoadedMetadata={(event) => onPlayerEvent(event, {
                loaded: true,
                durationSeconds: event.currentTarget.duration,
                videoWidth: event.currentTarget.videoWidth,
                videoHeight: event.currentTarget.videoHeight,
              })}
              onError={(event) => onPlayerEvent(event, { failed: true })}
            />
          ) : null}
          {placeholder ? (
            <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-600" data-testid="preview-unavailable">
              <p className="m-0">{PREVIEW_UNAVAILABLE_TEXT}</p>
              {details ? <p className="m-0 mt-1 text-[11px] text-slate-500">{details}</p> : null}
            </div>
          ) : null}
          <figcaption className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <span role="status">
                <Badge variant={checking ? 'neutral' : VERDICT_TONE[verdict]}>
                  {checking ? 'Checking this file...' : describeVerdict(verdict)}
                </Badge>
              </span>
              <span className="text-xs text-slate-600">{summary}</span>
            </div>
            {details && !placeholder ? <p className="text-[11px] text-slate-500">{details}</p> : null}
            <WarningList warnings={assessment?.warnings || []} />
          </figcaption>
        </figure>
      ) : null}
      {pdfAssessment ? (
        pdfAssessment.ok ? (
          <p className="text-xs text-slate-600">
            {caseStudyFile.name}: PDF, {formatFileSize(caseStudyFile.size)}
          </p>
        ) : (
          <WarningList warnings={pdfAssessment.warnings} />
        )
      ) : null}
    </div>
  );
}

function WarningList({ warnings }) {
  if (!warnings.length) return null;
  return (
    <ul className="m-0 list-none space-y-1 p-0">
      {warnings.map((item) => (
        <li key={item.code} className={`text-xs ${SEVERITY_CLASS[item.severity]}`}>
          {item.message}
        </li>
      ))}
    </ul>
  );
}
