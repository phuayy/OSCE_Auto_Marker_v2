import { ClipExportScope, ClipExportStatus, SegmentationMethod, SessionStatus, Workflow } from '@/lib/enums';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { ensureStreamTicket } from '@/auth';
import { useChangeStream } from '@/changeStream';
import { ApiError, ERROR_KIND, apiJson } from '@/lib/apiFetch';
import { WORKSPACE_LAYOUT, loadSessionWorkspace, workspaceLayoutFor } from '@/lib/sessionWorkspace';
import { uploadFileToResumableSession } from '@/lib/resumableUpload';
import { DEFAULT_PART_CONCURRENCY, recordedPartNumbers, uploadParts } from '@/lib/partUpload';
import { CONNECTION_STATUS, useConnectionStatus } from '@/lib/connectionStatus';
import { CLIP_ASSESSMENTS_ANCHOR_ID } from '@/lib/anchors';
import { coalesceAsync } from '@/lib/coalesce';
import { clampNumber, formatRuntime } from '@/lib/format';
import { LazyBoundary, lazyComponent, preloadComponent } from '@/lib/lazyRoute';
import { LoadingRegion, SessionRowsSkeleton, WorkspaceSkeleton } from '@/components/skeletons.jsx';
import { ConnectionBadge, ConnectionNotice } from '@/components/ConnectionStatus';
import {
  IN_FLIGHT_STATUSES,
  describeProcessingStage,
  formatProcessingStageLabel,
} from '@/lib/processingStage';
import { UPLOAD_PHASE, useUploadTracker } from '@/lib/uploadTracking';
import {
  BarChart3,
  BellRing,
  Brain,
  ClipboardCheck,
  Clock3,
  FileSpreadsheet,
  FileText,
  Film,
  Loader2,
  LogOut,
  Play,
  PlayCircle,
  RotateCw,
  Scissors,
  Settings,
  Trash2,
  UploadCloud,
  User,
  Users,
  Video,
  Wand2,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Modal } from '@/components/ui/dialog';
import { Progress } from '@/components/ui/progress';
import { PageHeader } from '@/components/PageHeader.jsx';
import { SessionStatusBadge } from '@/components/SessionStatusBadge.jsx';
import RegionFocusPreview from '@/components/RegionFocusPreview.jsx';
import OccupancyPresetGlyph from '@/components/OccupancyPresetGlyph.jsx';
import CorporaManager from './CorporaManager.jsx';
import { NotificationBell, NotificationFeed } from '@/notifications.jsx';
import { describeClipExportOutcome } from '@/lib/clipExportOutcome';
import { indexClipAssessments } from '@/lib/clipAssessments';
import { describeRerunAction } from '@/lib/rerunAction';
import { describeStartAction } from '@/lib/sessionStartAction';
import {
  buildRegionFocusOptions,
  clampRegionRatio,
  defaultRegionFocus,
  describeRegionFocus,
  isRegionFocusLocked,
  toggleRegionSide,
} from '@/lib/regionFocus';
import {
  INTERMISSION_KIND,
  MIN_BOUNDARY_GAP_SECONDS,
  ensureKinds,
  hitTestTimeline,
  insertSeparator,
  normalizeLabels,
  removeSeparator,
  timeAtOffset,
  toggleSegmentKind,
} from './lib/manualTimeline.js';
import {
  areAllSelected,
  planClipDispatch,
  selectableClipIds,
  toggleSelection,
} from './lib/clipSelection.js';

/** Recordings at or above this length use bell/hybrid auto-split in the pipeline and show the Auto-split UI tab. */
const LONG_VIDEO_THRESHOLD_SECONDS = 300;

// Opening a session is a deliberate second click — an in-flight session is not
// enterable at all — so the whole workspace (player, crop timeline, score tabs,
// cohort charts) is its own chunk, warmed the moment a load starts rather than
// when it renders. Same contract AppShell uses for Settings/Analytics/Rubric.
const SessionWorkspace = lazyComponent(() => import('@/workspace/SessionWorkspace.jsx'));

// How often the open workspace re-reads a session while its clip export runs.
// Faster than the in-flight heartbeat below because the user is watching this one
// list fill in; each tick is a single lightweight session read.
const CLIP_EXPORT_POLL_MS = 3000;

// Floor under the change stream, active only while a session has work in
// flight.
//
// The list is normally push-driven: the backend announces a write and the app
// refetches. But a long step can run for minutes without writing anything —
// auto-crop's person detection is the extreme case — so a refresh that failed
// during that silence had nothing to trigger the retry that would have healed
// it, and the stale card sat there behind an error. This interval guarantees
// a recovery attempt regardless of what the backend has to say, and costs
// almost nothing: the session-index endpoint is served from a change-token
// cache, so a tick that finds no change is a dictionary lookup.
const IN_FLIGHT_HEARTBEAT_MS = 12000;

function debugPipeline(message) {
  if (import.meta.env.DEV) {
    console.debug(message);
  }
}

export default function OSCEAiMarkerMockup({
  authUsername = '',
  routeSessionId = null,
  onNavigateSession = null,
  onOpenRubric = null,
  onOpenAnalytics = null,
  onOpenSettings = null,
  onPreloadRoute = null,
  onLogout = null,
  notifications = null,
} = {}) {
  const [videoFile, setVideoFile] = useState(null);
  const [communicationScores, setCommunicationScores] = useState(null);
  const [caseStudyFile, setCaseStudyFile] = useState(null);
  const [showWorkspace, setShowWorkspace] = useState(false);
  const [uploadFlow, setUploadFlow] = useState(Workflow.STANDARD);
  // Long-workflow auto-crop method: 'bells' (audio bell detection) or
  // 'person' (RT-DETR human detection). Sent with the upload; the backend
  // worker reads it from the session when the auto_crop job runs.
  const [segmentationMethod, setSegmentationMethod] = useState(SegmentationMethod.BELLS);
  // Occupancy rule for human detection: which camera scenario this recording
  // is. The catalogue (labels + the numbers each preset stands for) comes from
  // the backend, so retuning a preset never needs a frontend release; only the
  // *selection* lives here. 'custom' unlocks the three numbers directly.
  const [segmentationPresets, setSegmentationPresets] = useState([]);
  const [segmentationPreset, setSegmentationPreset] = useState('pair');
  const [customOccupancy, setCustomOccupancy] = useState({
    minPeople: 2,
    minBoxHeightRatio: 0.4,
    minSessionSeconds: 120,
  });
  // Horizontal region-of-interest: which side(s) of the frame the detector
  // looks in. The backend treats this as independent of the occupancy rule
  // above, but a predefined preset can pick a people count the enabled
  // zone(s) can never satisfy (e.g. "pair" needs 2 people while one side
  // alone only ever shows one) — that failure is silent: zero clips found
  // degrades straight to bell detection with no error surfaced. The form
  // sidesteps it entirely by only offering this panel under "Custom", where
  // the operator is already reasoning about the numbers directly.
  const [regionFocus, setRegionFocus] = useState(defaultRegionFocus());
  // True whenever region focus is not the operator's to set — every preset
  // except "Custom". Gates both whether the panel renders at all (below) and
  // whether `regionFocus` gets reset to the full frame (the effect right
  // after this), so the two can never disagree about what "locked" means.
  // Kept in sync here rather than duplicated at each place `segmentationPreset`
  // can change (the radio click, and the catalogue's own fallback when a
  // stored preset id no longer exists).
  const regionFocusLocked = isRegionFocusLocked(segmentationPreset);
  useEffect(() => {
    if (regionFocusLocked) {
      setRegionFocus(defaultRegionFocus());
    }
  }, [regionFocusLocked]);
  // User-chosen name for the session about to be created, and the pre-flight
  // confirmation overlay shown before processing starts.
  const [sessionNameInput, setSessionNameInput] = useState('');
  const [showConfirmStart, setShowConfirmStart] = useState(false);
  // Transcription corpora (DB-backed term lists that bias WhisperX). The
  // selection ('' = None) is snapshotted into the session at upload and
  // automatically applies to every clip assessed within that session.
  const [corpora, setCorpora] = useState([]);
  const [selectedCorpusId, setSelectedCorpusId] = useState('');
  // CRUD lives in CorporaManager (shared with the Settings page); this modal
  // flag just shows it next to the pre-flight corpus picker.
  const [showCorpusManager, setShowCorpusManager] = useState(false);

  const [sessionIndex, setSessionIndex] = useState([]);
  const [sessionIndexLoading, setSessionIndexLoading] = useState(false);
  // False until the index has answered once. `sessionIndex` starts empty, and
  // "no sessions yet" and "not asked yet" must not look the same: the first
  // fetch draws rows in outline, and the empty state waits for a real answer.
  const [hasLoadedSessionIndex, setHasLoadedSessionIndex] = useState(false);
  // Reserved for failures of an action the user took (open, rename, delete, a
  // manual refresh). Background refreshes report to `connection` instead — see
  // refreshSessionIndex.
  const [sessionIndexError, setSessionIndexError] = useState('');
  // Shared reachability state, fed by every API call and by the change stream.
  const connection = useConnectionStatus();
  const [sessionNameDrafts, setSessionNameDrafts] = useState({});
  const [renamingSessionId, setRenamingSessionId] = useState(null);
  const [deletingSessionId, setDeletingSessionId] = useState(null);
  const [rerunningSessionId, setRerunningSessionId] = useState(null);
  const [startingSessionId, setStartingSessionId] = useState(null);

  const [session, setSession] = useState(null);
  const [transcript, setTranscript] = useState({ segments: [] });
  const [scoreReport, setScoreReport] = useState(null);
  const [audioProfessionalism, setAudioProfessionalism] = useState(null);
  const [audioProfLoadError, setAudioProfLoadError] = useState('');

  const [isUploading, setIsUploading] = useState(false);
  // A workspace fetch is in flight. Deliberately NOT "a pipeline is running":
  // this flag used to be called `isProcessing` and drove a modal titled
  // "Starting Job" with a milestone checklist, so opening a finished session
  // popped a fake pipeline dialog. Nothing here runs a pipeline — the run is a
  // queued job whose progress the session card gauges.
  const [isLoadingWorkspace, setIsLoadingWorkspace] = useState(false);
  // Per-session upload phase, owned by this tab (see lib/uploadTracking.js).
  // It outlives the progress overlay: dismissing that card hides a view, the
  // transfer keeps running and keeps reporting here, where the session card
  // reads it.
  const uploadTracker = useUploadTracker();
  // Overlay visibility only. Deliberately separate from `isUploading`, which
  // means "a transfer is in flight" and still gates the upload form.
  const [uploadOverlayDismissed, setUploadOverlayDismissed] = useState(false);
  // The confirm dialog hands focus to the name field; the field is what the
  // user came to fill in, and the dialog's Escape/Tab handling needs a ref.
  const sessionNameInputRef = useRef(null);
  // Session whose transfer this tab is currently driving, so the failure path
  // can mark the right track without threading the id through every throw.
  const activeUploadSessionIdRef = useRef(null);
  const [isDemoFallback, setIsDemoFallback] = useState(false);
  // The completed run's wall-clock, read off the session payload for the
  // workspace's Runtime row. Never a live clock: a run in progress is not
  // enterable, and an upload's own elapsed time comes from its track.
  const [runtimeSeconds, setRuntimeSeconds] = useState(0);
  // The workspace this tab is navigating *to*, while its payload is fetched:
  // `{ label, layout }`, or null when no navigation is pending. Non-null
  // replaces the current screen with a WorkspaceSkeleton in that layout — the
  // outline the view will have once the payload lands, chosen from what the
  // session list already knows (`workspaceLayoutFor`). Distinct from
  // `isLoadingWorkspace` on purpose: that flag also covers a fetch that stays
  // on the current screen (the demo re-run), which must not swap the view
  // for a placeholder. Deep links and the route restore effect set this too,
  // through openExistingSession.
  const [workspaceLoad, setWorkspaceLoad] = useState(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const [localVideoUrl, setLocalVideoUrl] = useState(null);
  const [activeSegmentId, setActiveSegmentId] = useState(null);
  const [selectedClipId, setSelectedClipId] = useState(null);
  const [videoDurationSeconds, setVideoDurationSeconds] = useState(0);
  const [isRecropping, setIsRecropping] = useState(false);
  const [cropDraft, setCropDraft] = useState({ start: 0, end: 0 });
  const [manualSegmentCount, setManualSegmentCount] = useState(0);
  const [manualBoundaries, setManualBoundaries] = useState([]);
  const [manualLabels, setManualLabels] = useState([]);
  // Per-segment kind ('session' | 'intermission'), parallel to manualLabels.
  const [manualSegmentKinds, setManualSegmentKinds] = useState([]);
  // Right-click context menu on the manual timeline: null | { x, y, hit }.
  const [timelineMenu, setTimelineMenu] = useState(null);
  const [draggingBoundaryIndex, setDraggingBoundaryIndex] = useState(null);
  const [isSavingManualSegments, setIsSavingManualSegments] = useState(false);
  const [currentVideoTime, setCurrentVideoTime] = useState(0);
  const [renamingClipId, setRenamingClipId] = useState(null);
  const [parentSessionSnapshot, setParentSessionSnapshot] = useState(null);
  const [clipAssessmentRuns, setClipAssessmentRuns] = useState({});
  // Batch-run selection: ids of clips checked in the Clip Assessments panel.
  const [selectedClipAssessmentIds, setSelectedClipAssessmentIds] = useState(() => new Set());
  const [isQueueingSelectedClips, setIsQueueingSelectedClips] = useState(false);
  const [clipSummaries, setClipSummaries] = useState(null);
  const [isLoadingClipSummaries, setIsLoadingClipSummaries] = useState(false);
  // Demo-only override: the long-video demo can pre-load summaries directly
  // without going through the server, since there is no parent session on disk.
  const [demoLongVideoSummaries, setDemoLongVideoSummaries] = useState(null);

  // The session whose clip export we requested, held from the request until the
  // job settles. The export runs in the queue, so the editor has to keep
  // reading the session; without this the watcher would depend entirely on the
  // clipExport record being present in the response that started it. Holding an
  // id rather than a flag means switching sessions cannot inherit the watch.
  const [awaitingClipExportFor, setAwaitingClipExportFor] = useState(null);
  const [clipExportJustFinished, setClipExportJustFinished] = useState(false);

  const videoInputRef = useRef(null);
  const caseStudyInputRef = useRef(null);
  const clipExportWasWatchedRef = useRef(false);
  // Issue number of the newest GET /api/sessions; see refreshSessionIndex.
  const sessionIndexRequestSeqRef = useRef(0);
  const videoPlayerRef = useRef(null);
  const videoPlayerSectionRef = useRef(null);
  const timelineContainerRef = useRef(null);
  const manualTimelineRef = useRef(null);
  // Latest separators, readable synchronously from the drag handlers: a
  // mousemove burst can queue several updates before React re-renders.
  const manualBoundariesRef = useRef([]);
  manualBoundariesRef.current = manualBoundaries;
  const timelineMenuRef = useRef(null);
  const timelineSegmentRefs = useRef(new Map());
  const workspaceLoadRef = useRef(null);

  useEffect(() => () => workspaceLoadRef.current?.abort(), []);

  const videoClips = useMemo(() => {
    return Array.isArray(session?.outputs?.videoClips) ? session.outputs.videoClips : [];
  }, [session]);

  const visibleSessions = useMemo(
    () => sessionIndex.filter((entry) => !entry.parentSessionId),
    [sessionIndex]
  );

  // Which child session assesses which clip, whether it is running, and
  // whether it scored a cut the clip no longer has. Pure — see
  // lib/clipAssessments.js, which also keeps the newest child of a clip rather
  // than whichever the index happened to list last.
  const clipAssessmentIndex = useMemo(
    () => indexClipAssessments(sessionIndex, session?.id, videoClips),
    [sessionIndex, session?.id, videoClips],
  );

  const hasClipFiles = useMemo(() => videoClips.some((clip) => Boolean(clip?.url)), [videoClips]);
  const hasDraftClips = videoClips.length > 0 && !hasClipFiles;
  // Progress of the durable clip-export job. Exporting cuts one MP4 per student
  // in the job queue rather than inside the POST, so the clips arrive over time
  // and this record — not the response body — is what says when they are all in.
  const clipExport = session?.clipExport || null;
  const clipExportStatus = String(clipExport?.status || '');
  const isClipExportRunning = [ClipExportStatus.QUEUED, ClipExportStatus.RUNNING].includes(clipExportStatus);
  // A recrop is an export scoped to one clip: it drives the same record and
  // the same watchers, but only the crop editor is busy — the split controls
  // and the rest of the timeline stay usable.
  const isRecropRunning = isClipExportRunning && clipExport?.scope === ClipExportScope.CLIP;
  const isPlanExportRunning = isClipExportRunning && !isRecropRunning;
  // Watch a bit wider than "the record says running": an export we just
  // requested counts too, so a response that never carried the clipExport
  // record still leaves the editor watching for the clips instead of sitting
  // on drafts until the user reloads the page.
  const awaitingClipExport = Boolean(session?.id) && awaitingClipExportFor === session?.id;
  const shouldWatchClipExport = isClipExportRunning || awaitingClipExport;
  // Batch-run selection derives from live run state: a clip that starts
  // running drops out of the selectable set (and the selected count) instead
  // of needing effect-based pruning when clips or statuses change.
  const batchSelectableClipIds = useMemo(
    () => selectableClipIds(videoClips, clipAssessmentRuns),
    [videoClips, clipAssessmentRuns]
  );
  const selectedRunnableClipIds = useMemo(
    () => batchSelectableClipIds.filter((id) => selectedClipAssessmentIds.has(id)),
    [batchSelectableClipIds, selectedClipAssessmentIds]
  );
  const allClipsSelected = areAllSelected(selectedClipAssessmentIds, batchSelectableClipIds);
  // Session clips are the assessable student clips; intermissions are greyed
  // timeline markers (empty room / lone person between stations).
  const sessionClipCount = useMemo(
    () => videoClips.filter((clip) => clip?.kind !== INTERMISSION_KIND).length,
    [videoClips]
  );
  const intermissionClipCount = videoClips.length - sessionClipCount;
  // The session/intermission distinction exists only for human-detection
  // segmentation. Bell detection splits AT bells (its constraint) — every
  // segment is a student clip, so the toggle is not offered there.
  const isPersonSegmentedSession = useMemo(
    () =>
      Boolean(
        session?.segmentation === SegmentationMethod.PERSON ||
          videoClips.some(
            (clip) =>
              clip?.kind === INTERMISSION_KIND || clip?.source?.type === 'person_detection_rtdetr'
          )
      ),
    [session?.segmentation, videoClips]
  );
  // While viewing a session, the session's own workflow (or the presence of
  // detected clips) is the source of truth — NOT the transient upload-form tab,
  // which is only correct right after picking it. This guarantees a long session
  // always shows the clip/auto-crop workflow (never the single-student panels),
  // regardless of how it was opened (reload, "View progress", deep link, etc.).
  const sessionIsLong = Boolean(session?.workflow === Workflow.LONG || videoClips.length > 0);
  const isLongWorkflow = showWorkspace ? sessionIsLong : uploadFlow === Workflow.LONG;
  // A clip assessment view is any child session (durable parentSessionId), or —
  // for the in-memory demo path where children may lack it — a snapshot whose id
  // differs from the current session. Deriving from parentSessionId means the
  // "Back to clip list" affordance survives reloads, deep links and browser
  // forward/back navigation, not just the in-app click that created a snapshot.
  const isClipAssessmentView = Boolean(
    (session?.id && session?.parentSessionId) ||
      (parentSessionSnapshot?.session?.id && session?.id && parentSessionSnapshot.session.id !== session.id)
  );
  const allowCropping = isLongWorkflow && !isClipAssessmentView;

  const selectedClip = useMemo(() => {
    if (!selectedClipId) return null;
    return videoClips.find((clip) => String(clip.id) === String(selectedClipId)) || null;
  }, [selectedClipId, videoClips]);

  useEffect(() => {
    if (!selectedClip) {
      return;
    }
    setCropDraft({
      start: Number(selectedClip.start || 0),
      end: Number(selectedClip.end || 0),
    });
  }, [selectedClipId, selectedClip]);

  useEffect(() => {
    if (!Number.isFinite(videoDurationSeconds) || videoDurationSeconds <= 0) {
      return;
    }

    if (videoClips.length > 1) {
      const sortedClips = [...videoClips].sort((a, b) => Number(a.start || 0) - Number(b.start || 0));
      const nextBoundaries = [];
      for (let index = 0; index < sortedClips.length - 1; index += 1) {
        const boundary = Number(sortedClips[index].end || 0);
        if (boundary > 0 && boundary < videoDurationSeconds) {
          nextBoundaries.push(boundary);
        }
      }

      // The person detector emits a full timeline partition — seed each
      // segment's kind so intermissions render greyed from the first paint.
      const nextKinds = sortedClips.map((clip) =>
        clip.kind === INTERMISSION_KIND ? INTERMISSION_KIND : 'session'
      );
      setManualSegmentCount(sortedClips.length);
      setManualBoundaries(nextBoundaries);
      setManualSegmentKinds(nextKinds);
      setManualLabels(normalizeLabels(sortedClips.map((clip) => String(clip.label || '')), nextKinds));
      return;
    }

    if (manualSegmentCount < 2) {
      setManualSegmentCount(2);
      setManualBoundaries([videoDurationSeconds / 2]);
      setManualSegmentKinds(['session', 'session']);
      setManualLabels(['Student 1', 'Student 2']);
    }
  }, [videoClips, videoDurationSeconds]);

  useEffect(() => {
    if (!session?.id || isClipAssessmentView || videoClips.length === 0) {
      return;
    }

    setClipAssessmentRuns((previous) => {
      const next = { ...previous };
      videoClips.forEach((clip) => {
        const mapped = clipAssessmentIndex[clip.id];
        if (!mapped) {
          return;
        }

        const existing = next[clip.id];
        // Keep an optimistic 'running' until the backend reports a terminal
        // state — but DO let it advance to completed/failed (e.g. when the run
        // finished via the "View progress" overlay, which doesn't set this map).
        if (existing?.status === 'running' && mapped.status !== SessionStatus.COMPLETED && mapped.status !== SessionStatus.FAILED) {
          return;
        }

        if (existing?.status === SessionStatus.COMPLETED && existing.sessionId) {
          return;
        }

        next[clip.id] = mapped;
      });
      return next;
    });
  }, [clipAssessmentIndex, isClipAssessmentView, session?.id, videoClips]);

  // Track which clips have *finished* assessment so we can fetch aggregate
  // summary statistics from the backend. We only fire the request once at least
  // two children are in the completed state to avoid spinning the server on
  // every single-student run.
  const completedClipSessionIdsKey = useMemo(() => {
    if (!session?.id || isClipAssessmentView) {
      return '';
    }
    const completedIds = Object.values(clipAssessmentRuns || {})
      .filter((entry) => entry?.status === SessionStatus.COMPLETED && entry?.sessionId)
      .map((entry) => String(entry.sessionId))
      .sort();
    return `${session.id}::${completedIds.join(',')}`;
  }, [clipAssessmentRuns, isClipAssessmentView, session?.id]);

  useEffect(() => {
    if (demoLongVideoSummaries) {
      // Demo mode already injected a synthetic summary payload.
      return undefined;
    }
    if (!session?.id || isClipAssessmentView) {
      return undefined;
    }
    const completedIds = completedClipSessionIdsKey.split('::')[1] || '';
    const completedCount = completedIds ? completedIds.split(',').filter(Boolean).length : 0;
    if (completedCount < 2) {
      // Need at least two completed students before charts are meaningful.
      setClipSummaries(null);
      return undefined;
    }

    let cancelled = false;
    setIsLoadingClipSummaries(true);
    apiJson(`/api/sessions/${session.id}/clip-summaries`)
      .then((body) => {
        if (cancelled) return;
        setClipSummaries(body || null);
      })
      .catch((_error) => {
        if (cancelled) return;
        setClipSummaries(null);
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoadingClipSummaries(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [completedClipSessionIdsKey, demoLongVideoSummaries, isClipAssessmentView, session?.id]);

  // Something this tab is driving is in flight: a transfer, or a workspace fetch.
  const isPipelineActive = isUploading || isLoadingWorkspace;
  const currentModeLabel = String(session?.pipeline?.mode || 'gpu').toUpperCase();

  // Clip export runs as a queued job, so the open workspace has to poll for its
  // clips. Deliberately not the session-list poll: the user stays inside this
  // session while it runs (the export does not move session.status), and the
  // list projection carries counters, not the clip rows this view renders.
  useEffect(() => {
    if (!showWorkspace || !session?.id || !shouldWatchClipExport) {
      return undefined;
    }

    let cancelled = false;
    const sessionId = session.id;

    async function pollClipExport() {
      const fresh = await refreshOpenSession(sessionId);
      if (cancelled || !fresh) {
        return;
      }
      if (String(fresh?.clipExport?.status) === SessionStatus.FAILED) {
        setError(fresh.clipExport.error || 'Clip export failed.');
      }
    }

    const intervalId = window.setInterval(pollClipExport, CLIP_EXPORT_POLL_MS);
    pollClipExport();

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [showWorkspace, session?.id, shouldWatchClipExport]);

  // Push, alongside the interval above: every clip the export job cuts is a
  // session write, so the change stream reports it the moment it lands and the
  // clip rows fill in without waiting for the next tick. Gated on an export
  // being watched — refreshing the session outside one would re-seed the
  // timeline from the server and discard separators the user is dragging.
  useChangeStream(() => {
    if (!showWorkspace || !session?.id || !shouldWatchClipExport) {
      return;
    }
    refreshOpenSession(session.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, ['sessions', 'jobs']);

  // The export finishing is what used to need a manual reload. The job writes
  // the last clip and the "completed" record separately, so once the watch
  // ends, read the session once more, then hand the user the clip list.
  useEffect(() => {
    if (!showWorkspace || !session?.id) {
      return undefined;
    }
    if (shouldWatchClipExport) {
      clipExportWasWatchedRef.current = true;
      return undefined;
    }
    if (!clipExportWasWatchedRef.current) {
      return undefined;
    }
    clipExportWasWatchedRef.current = false;

    let cancelled = false;
    const sessionId = session.id;

    (async () => {
      const fresh = (await refreshOpenSession(sessionId)) || session;
      if (cancelled) {
        return;
      }
      // What the editor owes the user depends on what the job cut: a whole
      // split hands over a new clip list, a recrop hands back the one clip
      // they were already looking at. The decision itself is pure — see
      // lib/clipExportOutcome.js.
      const outcome = describeClipExportOutcome({
        clipExport: fresh?.clipExport,
        clips: fresh?.outputs?.videoClips,
        selectedClipId,
      });
      if (!outcome.notice) {
        return;
      }
      setSelectedClipId(outcome.selectedClipId);
      setNotice(outcome.notice);
      if (outcome.scrollToAssessments) {
        setClipExportJustFinished(true);
      }
    })();

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showWorkspace, session?.id, shouldWatchClipExport]);

  // Drop the requested-export watch once the job records a terminal state, or
  // as soon as the workspace closes.
  useEffect(() => {
    if (!awaitingClipExportFor) {
      return;
    }
    if (!showWorkspace || clipExportStatus === SessionStatus.COMPLETED || clipExportStatus === SessionStatus.FAILED) {
      setAwaitingClipExportFor(null);
    }
  }, [awaitingClipExportFor, showWorkspace, clipExportStatus]);

  // Scroll only once the clip panel is actually mounted — it renders off the
  // refreshed session, one render after the export finishes.
  useEffect(() => {
    if (!clipExportJustFinished || !hasClipFiles) {
      return;
    }
    document
      .getElementById(CLIP_ASSESSMENTS_ANCHOR_ID)
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setClipExportJustFinished(false);
  }, [clipExportJustFinished, hasClipFiles]);

  useEffect(() => {
    if (!videoFile) {
      setLocalVideoUrl(null);
      return undefined;
    }

    const nextUrl = URL.createObjectURL(videoFile);
    setLocalVideoUrl(nextUrl);

    return () => {
      URL.revokeObjectURL(nextUrl);
    };
  }, [videoFile]);

  useEffect(() => {
    if (!isPipelineActive) {
      return undefined;
    }

    const intervalId = window.setInterval(() => {
      setRuntimeSeconds((previous) => previous + 1);
    }, 1000);

    return () => {
      window.clearInterval(intervalId);
    };
  }, [isPipelineActive]);

  useEffect(() => {
    refreshSessionIndex();
    refreshCorpora();
    refreshSegmentationPresets();
    // Warm a stream ticket on mount so media tags use the short-lived ticket
    // rather than the long-lived bearer token in their URLs.
    ensureStreamTicket();
  }, []);

  // --- Keep the address bar in sync with the open session -------------------
  // Lets a reload (or a shared link / browser back-forward) restore the same
  // page instead of dropping the user back on the landing screen.
  const didMountRouteRef = useRef(false);

  // State -> URL: reflect the open session in the hash. Skipped on the very
  // first render so it can't clobber a deep-linked URL before it is restored.
  useEffect(() => {
    if (!didMountRouteRef.current) {
      didMountRouteRef.current = true;
      return;
    }
    if (typeof onNavigateSession === 'function') {
      onNavigateSession(showWorkspace ? session?.id ?? null : null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showWorkspace, session?.id]);

  // URL -> State: restore on load and react to back/forward + deep links. The
  // navigate() no-op guard prevents this from looping with the sync effect.
  useEffect(() => {
    const target = routeSessionId || null;
    const current = showWorkspace ? session?.id ?? null : null;
    if (target === current) {
      return;
    }
    if (target) {
      openExistingSession(target);
    } else if (showWorkspace) {
      goHome();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeSessionId]);

  useEffect(() => {
    if (!showWorkspace) {
      refreshSessionIndex();
    }
  }, [showWorkspace]);

  // Refresh the session list when the backend says it changed, instead of
  // polling for it. Drives the card stage gauges on the main page AND the
  // per-clip run states inside a long-session workspace (both derive from this
  // index). `jobs` is included because a job status change is what moves a card
  // between queued/processing/completed.
  //
  // A `ready` event means the stream just (re)connected, so anything could have
  // happened while it was down — refetch unconditionally in that case.
  //
  // Both this and the heartbeat below go through one single-flight wrapper:
  // a run writes the session many times a minute (progress readings, step
  // transitions, the job mirror — two branches at once under
  // PARALLEL_SCORING), and on PostgreSQL every one of those is its own event.
  // Refetching per event cost a GET per write and let a slow, older response
  // land after a newer one. Coalesced, a burst is one request plus one
  // catch-up, and responses never overlap. The ref keeps the wrapper stable
  // across renders without freezing the first render's closure.
  const refreshSessionIndexRef = useRef(refreshSessionIndex);
  refreshSessionIndexRef.current = refreshSessionIndex;
  const refreshSessionIndexInBackground = useMemo(
    () => coalesceAsync(() => refreshSessionIndexRef.current({ silent: true })),
    [],
  );

  useChangeStream(() => {
    refreshSessionIndexInBackground();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, ['sessions', 'jobs']);

  // Heartbeat floor for in-flight work. See IN_FLIGHT_HEARTBEAT_MS: the change
  // stream is the primary signal, but it can only announce writes, and a
  // session can legitimately go minutes without one (auto-crop's person
  // detection, a long transcription). Recovery must not depend on the same
  // channel that just went quiet.
  const hasInFlightSessions = useMemo(
    () => sessionIndex.some((entry) => IN_FLIGHT_STATUSES.has(String(entry?.status))),
    [sessionIndex],
  );

  useEffect(() => {
    if (!hasInFlightSessions) {
      return undefined;
    }

    let cancelled = false;
    const isHidden = () =>
      typeof document !== 'undefined' && document.visibilityState === 'hidden';

    function tick() {
      // A background tab has nobody to show the result to, and browsers throttle
      // its timers anyway. Skip the work and catch up on the way back.
      if (cancelled || isHidden()) {
        return;
      }
      refreshSessionIndexInBackground();
    }

    const intervalId = window.setInterval(tick, IN_FLIGHT_HEARTBEAT_MS);
    const onVisibilityChange = () => {
      if (!isHidden()) {
        tick();
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasInFlightSessions]);

  async function refreshSegmentationPresets() {
    try {
      const body = await apiJson('/api/settings/segmentation-presets', {
        fallbackMessage: 'Failed to load segmentation presets.',
      });
      const presets = Array.isArray(body.presets) ? body.presets : [];
      setSegmentationPresets(presets);
      // Seed the custom form from the backend's own custom defaults so the
      // numbers a user starts editing are the ones the server would have used.
      const custom = presets.find((preset) => preset.id === (body.customPreset || 'custom'));
      if (custom) {
        setCustomOccupancy({
          minPeople: custom.minPeople,
          minBoxHeightRatio: custom.minBoxHeightRatio,
          minSessionSeconds: custom.minSessionSeconds,
        });
      }
      if (body.defaultPreset) {
        setSegmentationPreset((previous) =>
          presets.some((preset) => preset.id === previous) ? previous : body.defaultPreset
        );
      }
      // Region focus is a separate block on the same response (preset-
      // independent), so retuning its bounds is also a backend-only change.
      if (body.regionFocus?.defaults) {
        setRegionFocus(body.regionFocus.defaults);
      }
    } catch (presetLoadError) {
      // Non-fatal: with no catalogue the picker is hidden and the backend
      // applies its own default preset, which is the pre-preset behaviour.
      console.warn('Could not load segmentation presets:', presetLoadError);
    }
  }

  // The options object sent with an upload — null unless human detection is
  // the chosen method, in which case the backend also validates it.
  function buildSegmentationOptions() {
    if (uploadFlow !== Workflow.LONG || segmentationMethod !== SegmentationMethod.PERSON) {
      return null;
    }
    if (segmentationPreset !== 'custom') {
      return { preset: segmentationPreset };
    }
    return {
      preset: 'custom',
      minPeople: Number(customOccupancy.minPeople),
      minBoxHeightRatio: Number(customOccupancy.minBoxHeightRatio),
      minSessionSeconds: Number(customOccupancy.minSessionSeconds),
    };
  }

  // The region-of-interest object sent with an upload — null unless human
  // detection is the chosen method, same gating as buildSegmentationOptions
  // but a separate, preset-independent field.
  function buildRegionFocusOptionsForUpload() {
    return buildRegionFocusOptions(regionFocus, {
      workflow: uploadFlow,
      segmentationMethod,
    });
  }

  async function refreshCorpora() {
    try {
      const body = await apiJson('/api/corpora', { fallbackMessage: 'Failed to load corpora.' });
      setCorpora(Array.isArray(body.corpora) ? body.corpora : []);
    } catch (corpusLoadError) {
      // Non-fatal: the picker simply offers "None" and transcription still runs.
      console.warn('Could not load transcription corpora:', corpusLoadError);
    }
  }

  // Keep the pre-flight picker in sync with edits made in the corpora manager
  // (modal here or the Settings page): adopt the fresh list and drop a
  // selection whose corpus was deleted.
  function handleCorporaChanged(list) {
    setCorpora(list);
    setSelectedCorpusId((previous) => (list.some((corpus) => corpus.id === previous) ? previous : ''));
  }

  // `silent` refreshes are driven by the change stream or the in-flight
  // heartbeat rather than by the user, so they must neither flash the list's
  // loading state on every backend write nor claim its error slot.
  async function refreshSessionIndex({ silent = false } = {}) {
    if (!silent) {
      setSessionIndexLoading(true);
    }
    // Background refreshes are single-flight, but a user-triggered one can
    // still overlap them. Whichever request was *issued* last is the truth;
    // a response for an earlier request is dropped rather than allowed to
    // roll the cards back to a stage the run has already left.
    const requestSeq = (sessionIndexRequestSeqRef.current += 1);
    try {
      const body = await apiJson('/api/sessions', {
        fallbackMessage: 'Failed to load sessions.',
      });
      if (requestSeq !== sessionIndexRequestSeqRef.current) {
        return null;
      }

      const sessions = Array.isArray(body.sessions) ? body.sessions : [];
      setSessionIndex(sessions);
      setHasLoadedSessionIndex(true);
      setSessionNameDrafts((previous) => {
        const next = { ...previous };
        sessions.forEach((sessionEntry) => {
          if (!next[sessionEntry.id]) {
            next[sessionEntry.id] = sessionEntry.name || '';
          }
        });
        return next;
      });
      // Cleared on success rather than on entry: clearing up front made the
      // banner flicker on every background write, and left a real error
      // looking resolved for as long as the next request took.
      setSessionIndexError('');
      return sessions;
    } catch (error) {
      // A background refresh nobody asked for must not commandeer the page's
      // error slot. If it failed because the backend is unreachable, that is a
      // connectivity fact — already recorded by apiJson, and rendered by the
      // connection indicator, which knows the difference between a blip and an
      // outage. Only a refresh the user actually triggered writes the banner.
      if (!silent) {
        setSessionIndexError(error.message || 'Failed to load sessions.');
      }
      return null;
    } finally {
      // Only the refresh that raised the flag may clear it, so a stream-driven
      // refresh landing mid-flight cannot cancel a user-initiated spinner.
      if (!silent) {
        setSessionIndexLoading(false);
      }
    }
  }

  // Re-read the open session and adopt it, ignoring a response that arrived
  // after the user moved on. Returns the fresh session so a caller can act on
  // what it says; null on any failure, which the callers treat as "try again".
  async function refreshOpenSession(sessionId) {
    try {
      const body = await apiJson(`/api/sessions/${sessionId}`);
      const fresh = body?.session;
      if (!fresh || String(fresh.id) !== String(sessionId)) {
        return null;
      }
      setSession((previous) => (previous?.id === sessionId ? fresh : previous));
      return fresh;
    } catch {
      // Transient network failures are ignored; the next tick or change event retries.
      return null;
    }
  }

  function applyWorkspace(payload) {
    setSession(payload.session);
    setTranscript(payload.transcript || { segments: [] });
    setScoreReport(payload.scores || null);
    setAudioProfessionalism(payload.audioProfessionalism || null);
    setCommunicationScores(payload.communicationScores || null);
    setRuntimeSeconds(Math.round(payload.session?.pipeline?.runtimeSeconds || 0));
  }

  function beginWorkspaceLoad() {
    // The chunk and the session data are independent fetches; starting the
    // chunk here means it is normally parsed before the payload arrives, so
    // the split adds nothing to the time the user waits.
    preloadComponent(SessionWorkspace);
    workspaceLoadRef.current?.abort();
    const controller = new AbortController();
    workspaceLoadRef.current = controller;
    return controller;
  }

  async function openExistingSession(sessionId) {
    const controller = beginWorkspaceLoad();
    setError('');
    setNotice('');
    setIsDemoFallback(false);
    setIsLoadingWorkspace(true);
    // The skeleton takes the outline of the session about to open. The list
    // projection knows the workflow and whether clips exist (and, for a child
    // reached by deep link, its parent); a session the list has not loaded
    // yet — a deep link on a cold start — gets the standard layout.
    setWorkspaceLoad({
      label: 'Loading saved session',
      layout: workspaceLayoutFor(sessionIndex.find((entry) => String(entry.id) === String(sessionId))),
    });
    setParentSessionSnapshot(null);
    setClipAssessmentRuns({});
    setSelectedClipAssessmentIds(new Set());

    try {
      const payload = await loadSessionWorkspace(sessionId, { signal: controller.signal });
      if (controller.signal.aborted) return;

      // Gate: an in-flight session (assembling/queued/processing) cannot be
      // entered — its results are empty/partial until processing finishes.
      // Bounce back to the list, where the session card shows the live stage.
      // This also covers deep links / reloads that hit the URL→state restore
      // effect, so blocking the list button alone is not enough.
      const loaded = payload.session;
      if (IN_FLIGHT_STATUSES.has(String(loaded?.status))) {
        setShowWorkspace(false);
        // Hand the screen back to the list now, with the notice, rather than
        // holding the placeholder for the index refresh below.
        setWorkspaceLoad(null);
        setNotice(
          'This session is still processing. Track its stage on the session card — it unlocks when finished.',
        );
        if (typeof onNavigateSession === 'function') {
          onNavigateSession(null);
        }
        await refreshSessionIndex();
        return;
      }

      applyWorkspace(payload);
      setVideoFile(null);
      setCaseStudyFile(null);

      const nextClips = Array.isArray(payload.session?.outputs?.videoClips)
        ? payload.session.outputs.videoClips
        : [];
      // Select the first SESSION clip — intermissions are greyed markers.
      setSelectedClipId(nextClips.find((clip) => clip.kind !== INTERMISSION_KIND)?.id || null);
      setUploadFlow(nextClips.length > 0 ? Workflow.LONG : Workflow.STANDARD);
      setShowWorkspace(true);
    } catch (error) {
      if (!controller.signal.aborted) setSessionIndexError(error.message || 'Failed to open session.');
    } finally {
      // Only the newest load may clear these: an older one settling late
      // must not take the placeholder away from the load that replaced it.
      if (workspaceLoadRef.current === controller) {
        setIsLoadingWorkspace(false);
        setWorkspaceLoad(null);
      }
    }
  }

  // Renders the action control for a saved-session row. In-flight sessions
  // (assembling/queued/processing) are deliberately NOT enterable — opening a
  // half-processed session would show empty/partial results. The card itself
  // gauges the current stage (see describeProcessingStage); this button
  // unlocks once processing reaches a terminal state.
  function renderSessionAction(sessionEntry) {
    // An upload still in flight in this tab is as un-enterable as a running
    // pipeline — the session has no video on disk yet — but the server still
    // reports it as `waiting_for_upload`, so the status alone cannot say so.
    const uploading = uploadTracker.isActive(sessionEntry.id);
    const inFlight = uploading || IN_FLIGHT_STATUSES.has(sessionEntry.status);

    if (inFlight) {
      return (
        <Button
          size="sm"
          variant="outline"
          disabled
          className="gap-1"
          title={
            uploading
              ? 'Available once the upload finishes. Progress is shown on this card.'
              : 'Available when processing completes. Progress is shown on this card.'
          }
        >
          <Loader2 className="h-3 w-3 animate-spin" />
          {uploading ? 'Uploading…' : 'Processing…'}
        </Button>
      );
    }

    // Sources committed, run never queued (an upload completed with
    // autoProcess off). The only thing to do with such a session is start it,
    // so the card offers exactly that — the endpoint behind this button had no
    // caller in the browser at all, which is what made `uploaded` a dead end.
    const start = describeStartAction(sessionEntry);
    if (start) {
      return (
        <>
          <Button
            size="sm"
            onClick={() => startSession(sessionEntry)}
            disabled={startingSessionId === sessionEntry.id}
            className="gap-1"
            title={start.title}
          >
            {startingSessionId === sessionEntry.id ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Play className="h-3 w-3" />
            )}
            {start.label}
          </Button>
          <OpenSessionButton onOpen={() => openExistingSession(sessionEntry.id)} />
        </>
      );
    }

    if (sessionEntry.status === SessionStatus.FAILED) {
      // A failed session opens (its partial artefacts are worth seeing), and it
      // can be re-run in place: same id, fresh job. The reason it failed is
      // rendered on the card itself — see the session list. What the re-run
      // *is* depends on the workflow (a long recording re-runs segmentation,
      // not the pipeline), so the button says which: lib/rerunAction.js.
      const rerun = describeRerunAction(sessionEntry);
      return (
        <>
          <Button
            size="sm"
            variant="outline"
            onClick={() => rerunSession(sessionEntry)}
            disabled={rerunningSessionId === sessionEntry.id}
            className="gap-1"
            title={rerun.title}
          >
            {rerunningSessionId === sessionEntry.id ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <RotateCw className="h-3 w-3" />
            )}
            {rerun.label}
          </Button>
          <OpenSessionButton onOpen={() => openExistingSession(sessionEntry.id)} />
        </>
      );
    }

    return <OpenSessionButton onOpen={() => openExistingSession(sessionEntry.id)} />;
  }

  // Queue the first run of a session whose files are already on the server.
  // Deliberately NOT the re-run endpoint: re-run deletes the previous run's
  // artefacts and assessment rows, and this session has none to delete — while
  // a resumable artefact left by an interrupted attempt is exactly what
  // /process is designed to pick up.
  async function startSession(sessionEntry) {
    const start = describeStartAction(sessionEntry);
    if (!start) return;
    setStartingSessionId(sessionEntry.id);
    try {
      await apiJson(start.endpoint, { method: 'POST', fallbackMessage: start.failureMessage });
      setNotice(start.notice);
      await refreshSessionIndex({ silent: true });
    } catch (error) {
      setSessionIndexError(error.message || start.failureMessage);
    } finally {
      setStartingSessionId(null);
    }
  }

  // Queue a fresh run of a failed session. The server picks the job from the
  // session itself (segmentation for a long recording, the pipeline for
  // everything else) and resets only what that run owns; the card takes over
  // from there through the list poll.
  async function rerunSession(sessionEntry) {
    const sessionId = sessionEntry?.id;
    if (!sessionId) return;
    setRerunningSessionId(sessionId);
    try {
      await apiJson(`/api/sessions/${sessionId}/rerun`, {
        method: 'POST',
        fallbackMessage: 'The session could not be re-run.',
      });
      setNotice(describeRerunAction(sessionEntry).notice);
      await refreshSessionIndex({ silent: true });
    } catch (error) {
      setSessionIndexError(error.message || 'The session could not be re-run.');
    } finally {
      setRerunningSessionId(null);
    }
  }

  function goHome() {
    workspaceLoadRef.current?.abort();
    setShowWorkspace(false);
    setWorkspaceLoad(null);
    setSession(null);
    setTranscript({ segments: [] });
    setScoreReport(null);
    setAudioProfessionalism(null);
    setCommunicationScores(null);
    setVideoFile(null);
    setCaseStudyFile(null);
    setSelectedClipId(null);
    setParentSessionSnapshot(null);
    setClipAssessmentRuns({});
    setSelectedClipAssessmentIds(new Set());
    setClipSummaries(null);
    setDemoLongVideoSummaries(null);
    setIsLoadingClipSummaries(false);
    setUploadFlow(Workflow.STANDARD);
    setError('');
    setNotice('');
    setIsDemoFallback(false);
    setIsUploading(false);
    setIsLoadingWorkspace(false);
    setRuntimeSeconds(0);
    refreshSessionIndex();
  }

  async function renameSessionName(sessionId, nextName) {
    const trimmed = String(nextName || '').trim();
    if (!trimmed) {
      return;
    }

    setRenamingSessionId(sessionId);
    setSessionIndexError('');
    try {
      const body = await apiJson(`/api/sessions/${sessionId}/name`, {
        method: 'PATCH',
        json: { name: trimmed },
        fallbackMessage: 'Failed to rename session.',
      });

      if (body?.session?.name) {
        setSessionIndex((previous) =>
          previous.map((entry) =>
            entry.id === sessionId ? { ...entry, name: body.session.name } : entry
          )
        );
        setSessionNameDrafts((previous) => ({ ...previous, [sessionId]: body.session.name }));
        if (session?.id === sessionId) {
          setSession((previousSession) => ({ ...previousSession, name: body.session.name }));
        }
      }
    } catch (error) {
      setSessionIndexError(error.message || 'Failed to rename session.');
    } finally {
      setRenamingSessionId(null);
    }
  }

  async function deleteSession(sessionId, { childLabel } = {}) {
    if (!sessionId) {
      return false;
    }
    const isChild = Boolean(childLabel);
    const confirmMessage = isChild
      ? `Delete the assessment for "${childLabel}"? This permanently removes its scores and results. This cannot be undone.`
      : 'Delete this session and every student assessment under it? This permanently removes all scores, results and files. This cannot be undone.';
    if (!window.confirm(confirmMessage)) {
      return false;
    }

    setDeletingSessionId(sessionId);
    setSessionIndexError('');
    try {
      const body = await apiJson(`/api/sessions/${sessionId}`, {
        method: 'DELETE', fallbackMessage: 'Failed to delete session.',
      });
      const removed = new Set(
        Array.isArray(body.deletedSessionIds) ? body.deletedSessionIds.map(String) : [String(sessionId)]
      );
      // If the open workspace (or its parent) was deleted, bail back to the
      // dashboard; refreshSessionIndex runs inside goHome.
      if (session?.id && removed.has(String(session.id))) {
        goHome();
      } else {
        setSessionIndex((previous) => previous.filter((entry) => !removed.has(String(entry.id))));
        await refreshSessionIndex();
      }
      return true;
    } catch (error) {
      setSessionIndexError(error.message || 'Failed to delete session.');
      return false;
    } finally {
      setDeletingSessionId(null);
    }
  }

  async function openDemoWorkspace(reason) {
    setError('');
    setIsUploading(false);
    setIsLoadingWorkspace(true);
    setWorkspaceLoad({ label: 'Loading bundled demo workspace', layout: WORKSPACE_LAYOUT.STANDARD });
    setActiveSegmentId(null);

    try {
      const { loadBundledDemoResources } = await import('@/lib/demoSessions');
      const demoBundle = await loadBundledDemoResources();
      setIsDemoFallback(true);
      setShowWorkspace(true);
      setUploadFlow(Workflow.STANDARD);
      setSession(demoBundle.session);
      setTranscript(demoBundle.transcript);
      setScoreReport(demoBundle.scores);
      setAudioProfessionalism(demoBundle.audioProfessionalism);
      setCommunicationScores(demoBundle.communicationScores);
      setClipSummaries(null);
      setDemoLongVideoSummaries(null);
      setClipAssessmentRuns({});
      setSelectedClipAssessmentIds(new Set());
      setRuntimeSeconds(Math.round(demoBundle.session?.pipeline?.runtimeSeconds || 0));
      setNotice(reason);
    } catch (loadError) {
      setError(`Demo workspace failed to load: ${loadError.message || 'unknown error'}`);
      setNotice('');
    } finally {
      setIsUploading(false);
      setIsLoadingWorkspace(false);
      setWorkspaceLoad(null);
    }
  }

  async function openManualDemoMode() {
    setError('');
    setRuntimeSeconds(0);
    await openDemoWorkspace('Manual demo mode enabled. Model execution skipped.');
  }

  // ---------------------------------------------------------------------------
  // Long-video demo
  // ---------------------------------------------------------------------------
  // The long demo replicates session 946f0f67... in a 'cropped + all clips
  // already assessed' state. Clip MP4s ship with the bundle and each child
  // session ships its own scores/communication-scores/audio-professionalism/
  // transcript so the user can drill into any student without a backend.
  async function openLongVideoDemoWorkspace() {
    setError('');
    setIsUploading(false);
    setIsLoadingWorkspace(true);
    setWorkspaceLoad({ label: 'Loading bundled long-video demo', layout: WORKSPACE_LAYOUT.LONG });
    setActiveSegmentId(null);

    try {
      const { LONG_DEMO_CHILD_IDS, buildLongDemoSummaries, loadBundledLongDemoResources } =
        await import('@/lib/demoSessions');
      const bundle = await loadBundledLongDemoResources();

      setIsDemoFallback(true);
      setShowWorkspace(true);
      setUploadFlow(Workflow.LONG);
      // Stash the demo bundle on the parent session itself so other handlers
      // (openClipAssessmentView, runClipAssessment) can locate the demo data
      // without a network round-trip.
      setSession({
        ...bundle.parentSession,
        _demoChildren: bundle.childById,
      });
      setTranscript({ segments: [] });
      setScoreReport(null);
      setAudioProfessionalism(null);
      setCommunicationScores(null);
      setVideoDurationSeconds(Number(bundle.remappedClips?.[bundle.remappedClips.length - 1]?.end || 0));
      setRuntimeSeconds(Math.round(bundle.parentSession?.pipeline?.runtimeSeconds || 0));

      // Mark every clip as completed so the panel renders "View / Re-run"
      // controls and the cohort charts get the trigger they need.
      const runs = {};
      LONG_DEMO_CHILD_IDS.forEach((childId) => {
        const child = bundle.childById[childId];
        if (!child) return;
        const clipId = child.session?.clipSource?.clipId;
        if (clipId) {
          runs[clipId] = { status: SessionStatus.COMPLETED, sessionId: childId };
        }
      });
      setClipAssessmentRuns(runs);

      setDemoLongVideoSummaries(buildLongDemoSummaries(bundle.childById));
      setNotice(
        'Long-video demo mode: clips already exported, every student assessed. Export disabled.',
      );
    } catch (loadError) {
      setError(`Long-video demo failed to load: ${loadError.message || 'unknown error'}`);
      setNotice('');
    } finally {
      setIsUploading(false);
      setIsLoadingWorkspace(false);
      setWorkspaceLoad(null);
    }
  }

  async function uploadFileParts(file, fileUpload, onProgress, { completedParts } = {}) {
    // The backend picks the transport at initiate: parts relayed through this
    // API on a local deployment, straight at the bucket on a cloud one.
    if (fileUpload.strategy === 'gcs_resumable') {
      return uploadFileToResumableSession(file, fileUpload, onProgress);
    }
    if (fileUpload.strategy !== 'local_multipart') {
      throw new Error(`Unsupported upload strategy: ${fileUpload.strategy || 'unknown'}.`);
    }
    if (!fileUpload.partUrlTemplate) {
      throw new Error('Upload plan did not include a part URL template.');
    }
    const partSize = Number(fileUpload.partSizeBytes || 0);
    if (!Number.isFinite(partSize) || partSize <= 0) {
      throw new Error('Upload plan did not include a valid part size.');
    }

    // Parts go through the same door as every other API call: a 502 from the
    // proxy or a dropped socket is retried with jitter instead of ending the
    // transfer, and storing a part is idempotent on the server, so a retry of
    // a part whose response was lost is safe. Several parts are in flight at
    // once — the server serialises them per upload, so none can be lost.
    return uploadParts({
      file,
      partSize,
      completedParts,
      concurrency: DEFAULT_PART_CONCURRENCY,
      putPart: (partNumber, chunk) =>
        apiJson(fileUpload.partUrlTemplate.replace('{partNumber}', String(partNumber)), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/octet-stream' },
          body: chunk,
          idempotent: true,
          fallbackMessage: `Upload part ${partNumber} failed.`,
        }),
      onProgress: (uploadedBytes) => onProgress?.(uploadedBytes, file.size, fileUpload),
    });
  }

  // One transfer of one file, resumed once from the server's own ledger if it
  // breaks. `GET /uploads/{id}` says exactly which parts arrived, so an outage
  // in the middle of a 2 GB video costs the parts in flight, not the file.
  // A 4xx is the server refusing the request itself and is not retried.
  async function uploadFileWithResume(uploadId, file, fileUpload, onProgress, sessionId) {
    try {
      return await uploadFileParts(file, fileUpload, onProgress);
    } catch (error) {
      const refused = error instanceof ApiError && (error.kind === ERROR_KIND.CLIENT || error.kind === ERROR_KIND.ABORT);
      if (refused || fileUpload.strategy !== 'local_multipart') {
        throw error;
      }
      // Recorded on the track, not in overlay state: the user may well have
      // dismissed the overlay, and the note has to reach the session card too.
      uploadTracker.note(sessionId, 'Connection interrupted — resuming from the last saved part.');
      const status = await apiJson(`/api/uploads/${uploadId}`, {
        fallbackMessage: 'Could not read the upload status to resume.',
      });
      return uploadFileParts(file, fileUpload, onProgress, {
        completedParts: recordedPartNumbers(status, fileUpload.fileId),
      });
    }
  }

  async function runAsyncUploadAssessment() {
    const initiateBody = await apiJson('/api/uploads/initiate', {
      method: 'POST',
      fallbackMessage: 'Async upload initiation failed.',
      json: {
        workflow: uploadFlow,
        autoProcess: true,
        sessionName: sessionNameInput.trim() || null,
        segmentation: uploadFlow === Workflow.LONG ? segmentationMethod : null,
        segmentationOptions: buildSegmentationOptions(),
        regionFocusOptions: buildRegionFocusOptionsForUpload(),
        corpusId: selectedCorpusId || null,
        files: [
          {
            kind: 'video',
            originalName: videoFile.name,
            mimeType: videoFile.type || 'video/mp4',
            sizeBytes: videoFile.size,
          },
          {
            kind: 'caseStudy',
            originalName: caseStudyFile.name,
            mimeType: caseStudyFile.type || 'application/pdf',
            sizeBytes: caseStudyFile.size,
          },
        ],
      },
    });

    const sessionId = initiateBody.session?.id;
    if (!sessionId) {
      throw new Error('Async upload initiation did not return a session.');
    }
    setSession(initiateBody.session);

    const filePlans = initiateBody.fileUploads || [];
    const videoPlan = filePlans.find((item) => item.kind === 'video');
    const caseStudyPlan = filePlans.find((item) => item.kind === 'caseStudy');
    if (!videoPlan || !caseStudyPlan) {
      throw new Error('Async upload initiation did not return both file upload plans.');
    }

    // The session now exists server-side (status `waiting_for_upload`), so it
    // can carry the transfer's progress on its own card. Open the track before
    // the first byte and pull the session into the list right away, so the card
    // is already there if the user dismisses the overlay a second later.
    activeUploadSessionIdRef.current = sessionId;
    uploadTracker.begin(sessionId, videoFile.size + caseStudyFile.size);
    refreshSessionIndex();

    // Both files are one transfer as far as the user is concerned, so progress
    // is reported against their combined size rather than restarting at 0% for
    // the second file.
    let bytesFromCompletedFiles = 0;
    const updateUploadProgress = (uploadedBytes, totalBytes, plan) => {
      const label = plan.kind === 'caseStudy' ? 'case study' : 'video';
      uploadTracker.reportProgress(sessionId, bytesFromCompletedFiles + uploadedBytes, label);
    };

    await uploadFileWithResume(initiateBody.uploadId, videoFile, videoPlan, updateUploadProgress, sessionId);
    bytesFromCompletedFiles += videoFile.size;
    await uploadFileWithResume(
      initiateBody.uploadId,
      caseStudyFile,
      caseStudyPlan,
      updateUploadProgress,
      sessionId,
    );

    uploadTracker.setPhase(sessionId, UPLOAD_PHASE.FINALIZING);
    // `complete` is idempotent on the server (a repeat returns the same
    // assembling/committed record), so a lost response is safe to retry.
    await apiJson(`/api/uploads/${initiateBody.uploadId}/complete`, {
      method: 'POST',
      json: { autoProcess: true },
      idempotent: true,
      fallbackMessage: 'Upload finalization failed.',
    });
    // Upload is committed and the job is queued. Return the user to the main
    // page: the session card shows the live stage, other sessions stay fully
    // browsable, and this session unlocks when processing completes.
    //
    // The server's status now describes the session better than this tab can,
    // so the client-side track stands down and `describeProcessingStage` takes
    // the card back over.
    uploadTracker.setPhase(sessionId, UPLOAD_PHASE.DONE);
    uploadTracker.forget(sessionId);
    activeUploadSessionIdRef.current = null;
    setSession(null);
    setIsUploading(false);
    setSessionNameInput('');
    setNotice('Assessment started. Track its stage on the session card — it unlocks when finished.');
    await refreshSessionIndex();
    setShowWorkspace(false);
  }

  // Opens the pre-flight confirmation overlay (after validating the inputs).
  // The actual upload/processing only begins once the user confirms.
  function requestStartAssessment() {
    if (!videoFile) {
      setError('Upload a station video first.');
      return;
    }
    if (!caseStudyFile) {
      setError('Upload the case study PDF first.');
      return;
    }
    setError('');
    setShowConfirmStart(true);
  }

  // Hides the overlay only. The transfer carries on, and the session card
  // keeps its clock and percentage (see the overlay's comment).
  function dismissUploadOverlay() {
    setUploadOverlayDismissed(true);
    setNotice('Upload still running. Track its time and progress on the session card below.');
  }

  function cancelStartAssessment() {
    setShowConfirmStart(false);
  }

  async function confirmStartAssessment() {
    setShowConfirmStart(false);
    await startAssessment();
  }

  async function startAssessment() {
    if (!videoFile) {
      setError('Upload a station video first.');
      return;
    }

    if (!caseStudyFile) {
      setError('Upload the case study PDF first.');
      return;
    }

    setError('');
    setNotice('');
    setIsDemoFallback(false);
    setScoreReport(null);
    setAudioProfessionalism(null);
    setCommunicationScores(null);
    setParentSessionSnapshot(null);
    setClipSummaries(null);
    setDemoLongVideoSummaries(null);
    setRuntimeSeconds(0);
    debugPipeline('[pipeline] started');
    // A new run always opens with the overlay visible, whatever the user did
    // with the previous one.
    setUploadOverlayDismissed(false);
    activeUploadSessionIdRef.current = null;
    setIsUploading(true);

    try {
      await runAsyncUploadAssessment();
    } catch (requestError) {
      const message = requestError.message || 'Unknown error. Check that the backend is running.';
      // The overlay may well be dismissed by now, so the failure has to be
      // legible from the session card too: mark the track rather than only
      // raising a banner the user might not be looking at.
      uploadTracker.fail(activeUploadSessionIdRef.current, message);
      setError(`Upload failed: ${message}`);
      setShowWorkspace(false);
    } finally {
      activeUploadSessionIdRef.current = null;
      setIsUploading(false);
    }
  }

  async function recropSelectedClip() {
    if (!session?.id || !selectedClip) {
      return;
    }

    setIsRecropping(true);
    setError('');
    try {
      const payload = {
        start: Number(cropDraft.start),
        end: Number(cropDraft.end),
      };

      // 202: the clip comes back as a draft with its new range and the MP4 is
      // re-cut by the same export job a full split uses. Start the export
      // watch here rather than waiting for the response to carry a running
      // record — it is queued, not running, at this point.
      const body = await apiJson(`/api/sessions/${session.id}/clips/${selectedClip.id}/recrop`, {
        method: 'POST',
        json: payload,
        fallbackMessage: 'Recrop failed.',
      });

      if (body?.session) {
        setSession(body.session);
      }
      setAwaitingClipExportFor(session.id);
      setNotice(`Re-cutting ${selectedClip.label || 'clip'} — its row updates when the new crop lands.`);
    } catch (error) {
      setError(error.message || 'Recrop failed.');
    } finally {
      setIsRecropping(false);
    }
  }

  function generateManualBoundariesFromCount() {
    const segmentCount = clampNumber(Number(manualSegmentCount || 0), 2, 24);
    if (!Number.isFinite(videoDurationSeconds) || videoDurationSeconds <= 0) {
      setError('Load a video first so timeline duration is available.');
      return;
    }

    const boundaries = [];
    for (let index = 1; index < segmentCount; index += 1) {
      boundaries.push((videoDurationSeconds * index) / segmentCount);
    }
    const kinds = Array(segmentCount).fill('session');
    setManualSegmentCount(segmentCount);
    setManualBoundaries(boundaries);
    setManualSegmentKinds(kinds);
    setManualLabels((previous) => normalizeLabels(previous.slice(0, segmentCount), kinds));
  }

  function seekVideoPreview(seconds) {
    if (!videoPlayerRef.current) {
      return;
    }
    const safeSeconds = Number(seconds || 0);
    if (!Number.isFinite(safeSeconds) || safeSeconds < 0) {
      return;
    }
    videoPlayerRef.current.currentTime = Math.min(
      Math.max(0, safeSeconds),
      Number(videoDurationSeconds || safeSeconds)
    );
  }

  function updateBoundaryAt(index, nextValueSeconds, { seekVideo = false } = {}) {
    const current = manualBoundariesRef.current;
    if (index < 0 || index >= current.length) {
      return;
    }
    const leftLimit = index === 0 ? 0 : Number(current[index - 1] || 0) + MIN_BOUNDARY_GAP_SECONDS;
    const rightLimit =
      index === current.length - 1
        ? Number(videoDurationSeconds || 0)
        : Number(current[index + 1] || videoDurationSeconds) - MIN_BOUNDARY_GAP_SECONDS;
    const nextBoundary = clampNumber(Number(nextValueSeconds || 0), leftLimit, Math.max(leftLimit, rightLimit));
    const updated = [...current];
    updated[index] = nextBoundary;
    manualBoundariesRef.current = updated;
    setManualBoundaries(updated);

    if (seekVideo) {
      seekVideoPreview(nextBoundary);
    }
  }

  function updateBoundaryFromClientX(index, clientX, { seekVideo = false } = {}) {
    const container = manualTimelineRef.current;
    if (!container || !Number.isFinite(videoDurationSeconds) || videoDurationSeconds <= 0) {
      return;
    }
    const rect = container.getBoundingClientRect();
    const ratio = clampNumber((clientX - rect.left) / Math.max(1, rect.width), 0, 1);
    updateBoundaryAt(index, ratio * videoDurationSeconds, { seekVideo });
  }

  function updateManualLabelAt(index, nextLabel) {
    setManualLabels((previous) => {
      const updated = [...previous];
      while (updated.length <= index) {
        updated.push(`Student ${updated.length + 1}`);
      }
      updated[index] = String(nextLabel || '');
      return updated;
    });
  }

  function handleManualSegmentClick(segmentStart) {
    seekVideoPreview(Number(segmentStart || 0));
  }

  // Clicking anywhere on the timeline moves the playhead to the exact clicked
  // second (clicking a separator seeks to its second via its own mousedown).
  function handleTimelineClick(event) {
    const container = manualTimelineRef.current;
    if (!container || !Number.isFinite(videoDurationSeconds) || videoDurationSeconds <= 0) {
      return;
    }
    const rect = container.getBoundingClientRect();
    seekVideoPreview(timeAtOffset(event.clientX - rect.left, rect.width, videoDurationSeconds));
  }

  function handleTimelineContextMenu(event) {
    const container = manualTimelineRef.current;
    if (!container || !Number.isFinite(videoDurationSeconds) || videoDurationSeconds <= 0) {
      return;
    }
    event.preventDefault();
    const rect = container.getBoundingClientRect();
    const hit = hitTestTimeline({
      offsetX: event.clientX - rect.left,
      width: rect.width,
      duration: videoDurationSeconds,
      boundaries: manualBoundaries,
      playheadSeconds: currentVideoTime,
    });
    setTimelineMenu({ x: event.clientX, y: event.clientY, hit });
  }

  function applyTimelineMenuAction(action) {
    const hit = timelineMenu?.hit;
    setTimelineMenu(null);
    if (!hit) {
      return;
    }
    const segmentCount = manualBoundaries.length + 1;

    if (action === 'add') {
      const next = insertSeparator({
        boundaries: manualBoundaries,
        labels: manualLabels,
        kinds: manualSegmentKinds,
        timeSeconds: hit.timeSeconds,
        duration: videoDurationSeconds,
      });
      if (!next) {
        setNotice('Separator not added — too close to an existing separator or the video edge.');
        return;
      }
      setManualBoundaries(next.boundaries);
      setManualLabels(next.labels);
      setManualSegmentKinds(next.kinds);
      setManualSegmentCount(next.boundaries.length + 1);
      seekVideoPreview(hit.timeSeconds);
      return;
    }

    if (action === 'delete' && hit.separatorIndex !== null) {
      const next = removeSeparator({
        boundaries: manualBoundaries,
        labels: manualLabels,
        kinds: manualSegmentKinds,
        separatorIndex: hit.separatorIndex,
      });
      if (!next) {
        return;
      }
      setManualBoundaries(next.boundaries);
      setManualLabels(next.labels);
      setManualSegmentKinds(next.kinds);
      setManualSegmentCount(next.boundaries.length + 1);
      return;
    }

    if (action === 'toggle' && hit.segmentIndex !== null) {
      if (!isPersonSegmentedSession) {
        // Bell-segmented sessions split at bells only — no intermission concept.
        return;
      }
      const next = toggleSegmentKind({
        labels: manualLabels,
        kinds: manualSegmentKinds,
        segmentIndex: hit.segmentIndex,
        segmentCount,
      });
      if (!next) {
        return;
      }
      setManualLabels(next.labels);
      setManualSegmentKinds(next.kinds);
    }
  }

  // Close the timeline context menu on outside click / Escape.
  useEffect(() => {
    if (!timelineMenu) {
      return undefined;
    }
    const handlePointerDown = (event) => {
      if (!timelineMenuRef.current?.contains(event.target)) {
        setTimelineMenu(null);
      }
    };
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        setTimelineMenu(null);
      }
    };
    window.addEventListener('mousedown', handlePointerDown);
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('mousedown', handlePointerDown);
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [timelineMenu]);

  async function renameClipLabel(clipId, nextLabel) {
    if (!session?.id || !clipId) {
      return;
    }
    const trimmed = String(nextLabel || '').trim();
    if (!trimmed) {
      return;
    }
    setRenamingClipId(clipId);
    setError('');
    try {
      const body = await apiJson(`/api/sessions/${session.id}/clips/${clipId}`, {
        method: 'PATCH',
        json: { label: trimmed },
        fallbackMessage: 'Failed to rename clip.',
      });
      if (body?.session) {
        setSession(body.session);
      }
    } catch (renameError) {
      setError(renameError.message || 'Failed to rename clip.');
    } finally {
      setRenamingClipId(null);
    }
  }

  useEffect(() => {
    if (draggingBoundaryIndex === null) {
      return undefined;
    }

    if (videoPlayerRef.current && !videoPlayerRef.current.paused) {
      videoPlayerRef.current.pause();
    }

    const handleMouseMove = (event) => {
      updateBoundaryFromClientX(draggingBoundaryIndex, event.clientX, { seekVideo: true });
    };
    const handleMouseUp = () => {
      setDraggingBoundaryIndex(null);
    };

    window.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);
    return () => {
      window.removeEventListener('mousemove', handleMouseMove);
      window.removeEventListener('mouseup', handleMouseUp);
    };
  }, [draggingBoundaryIndex, videoDurationSeconds]);

  async function saveManualSegments() {
    if (!session?.id) {
      return;
    }
    if (!Number.isFinite(videoDurationSeconds) || videoDurationSeconds <= 0) {
      setError('Video duration is unavailable. Reload the video first.');
      return;
    }

    setIsSavingManualSegments(true);
    setError('');
    try {
      const sortedBoundaries = [...manualBoundaries].sort((a, b) => a - b).map((value) => Number(value));
      const expectedSegmentCount = sortedBoundaries.length + 1;
      const kinds = ensureKinds(manualSegmentKinds, expectedSegmentCount);
      const payload = {
        boundaries: sortedBoundaries,
        labels: normalizeLabels(manualLabels, kinds),
        // Kinds are a human-detection concept: intermission segments export as
        // greyed markers — no MP4 is cut, no downstream compute is spent. Bell
        // splits have no intermissions, so the field is omitted (all sessions).
        ...(isPersonSegmentedSession ? { kinds } : {}),
      };
      const body = await apiJson(`/api/sessions/${session.id}/clips/manual`, {
        method: 'POST',
        json: payload,
        fallbackMessage: 'Failed to save manual segments.',
      });
      // 202: the backend persisted the segmentation and queued an export job.
      // The draft clips come back immediately so the timeline re-renders, and
      // the MP4s land one at a time — watched by the clipExport poll below.
      // Watch from here, not from the returned clipExport record: the job is
      // queued either way, and the editor must reach the finished clips even if
      // this response says nothing about the export.
      setAwaitingClipExportFor(session.id);
      if (body?.session) {
        setSession(body.session);
        const nextClips = Array.isArray(body.session?.outputs?.videoClips) ? body.session.outputs.videoClips : [];
        // Select the first SESSION clip — intermissions are greyed markers.
        setSelectedClipId(nextClips.find((clip) => clip.kind !== INTERMISSION_KIND)?.id || null);
      }
    } catch (saveError) {
      setError(saveError.message || 'Failed to save manual segments.');
    } finally {
      setIsSavingManualSegments(false);
    }
  }

  // Returns true when the clip's assessment was queued (or demo-completed),
  // false otherwise — the batch runner uses this to keep failed clips checked.
  async function runClipAssessment(clip) {
    if (!session?.id || !clip?.id) {
      return false;
    }

    // Guard against double-submission: if this clip already has an assessment
    // queued/running, do not create a second child session for it.
    if (clipAssessmentRuns[clip.id]?.status === 'running') {
      return false;
    }

    // A dispatched clip leaves the batch selection so the checkboxes always
    // mirror what "Run Selected Assessments" would actually queue.
    unselectClipForBatch(clip.id);

    // Demo path: simulate the re-run by briefly switching to a "running" state,
    // then re-loading the pre-bundled child assessment from disk.
    const demoChildren = session?._demoChildren;
    const demoChildId = Object.keys(demoChildren || {}).find((id) => {
      const child = demoChildren[id];
      return String(child?.session?.clipSource?.clipId || '') === String(clip.id);
    });
    if (demoChildren && demoChildId) {
      setError('');
      setNotice('');
      setClipAssessmentRuns((previous) => ({
        ...previous,
        [clip.id]: { status: 'running' },
      }));
      // Not a navigation — the user stays on the clip list, so this sets the
      // busy flag (batch controls disable) but no `workspaceLoad`: the row's
      // own running state is the feedback, as it is for a real re-run.
      setIsLoadingWorkspace(true);
      // Brief simulated runtime so the row's running state is visible.
      await new Promise((resolve) => setTimeout(resolve, 900));
      setClipAssessmentRuns((previous) => ({
        ...previous,
        [clip.id]: { status: SessionStatus.COMPLETED, sessionId: demoChildId },
      }));
      setIsLoadingWorkspace(false);
      setNotice(
        `Demo mode: a real re-run would call the NVIDIA assessor again. Showing the cached score for "${clip.label || ''}".`,
      );
      return true;
    }

    // Non-blocking: queue the child assessment and STAY on the parent clip
    // list. The clip row shows the live stage (driven by the change stream,
    // with IN_FLIGHT_HEARTBEAT_MS as its floor) and flips to "View" when the
    // child session completes. The
    // child is not enterable while in flight (same rule as the session list).
    setError('');
    setClipAssessmentRuns((previous) => ({
      ...previous,
      [clip.id]: { status: 'running' },
    }));

    try {
      // 202: the child session is created and its job queued; nothing is
      // scored inside the request.
      const body = await apiJson(`/api/sessions/${session.id}/clips/${clip.id}/assess`, {
        method: 'POST',
        fallbackMessage: 'Clip assessment could not be queued.',
      });

      const clipSession = body.session;
      if (!clipSession?.id) {
        throw new Error('Clip assessment did not return a session ID.');
      }

      // Record the child session id so the row can show its stage from the
      // session index without waiting for the next poll to discover it.
      setClipAssessmentRuns((previous) => ({
        ...previous,
        [clip.id]: { status: 'running', sessionId: clipSession.id },
      }));
      setNotice(
        body.reused
          ? `Assessment for "${clip.label || 'clip'}" is already under way — its row updates as it progresses.`
          : `Assessment for "${clip.label || 'clip'}" queued — its row updates as it progresses.`,
      );
      refreshSessionIndex();
      return true;
    } catch (assessmentError) {
      setClipAssessmentRuns((previous) => ({
        ...previous,
        [clip.id]: { status: SessionStatus.FAILED, error: assessmentError.message || 'Clip assessment failed.' },
      }));
      setError(assessmentError.message || 'Clip assessment failed.');
      return false;
    }
  }

  // Re-running a clip's assessment is the same request as running it: the
  // server keeps one child session per clip, so `POST /assess` re-runs the
  // existing one in place — against the clip's *current* MP4, which is what a
  // re-cut clip needs. The separate `/rerun` call this used to make could not
  // do that: it re-scored whatever footage the child still pointed at.
  async function rerunClipAssessment(clip, _childSessionId) {
    return runClipAssessment(clip);
  }

  function unselectClipForBatch(clipId) {
    setSelectedClipAssessmentIds((previous) => {
      if (!previous.has(clipId)) {
        return previous;
      }
      const next = new Set(previous);
      next.delete(clipId);
      return next;
    });
  }

  function toggleClipSelected(clipId) {
    setSelectedClipAssessmentIds((previous) => toggleSelection(previous, clipId));
  }

  function toggleSelectAllClips() {
    setSelectedClipAssessmentIds(allClipsSelected ? new Set() : new Set(batchSelectableClipIds));
  }

  async function runSelectedClipAssessments() {
    if (isQueueingSelectedClips || selectedRunnableClipIds.length === 0) {
      return;
    }

    const clipsById = new Map(videoClips.map((clip) => [clip.id, clip]));
    // Snapshot each clip's dispatch route up front — run state advances as the
    // loop queues clips, and decisions should reflect the pre-batch statuses.
    const plans = selectedRunnableClipIds.map((clipId) => [
      clipId,
      planClipDispatch(clipAssessmentRuns[clipId], clipAssessmentIndex[clipId]),
    ]);

    // Re-scoring a completed clip costs real assessor calls and replaces its
    // scores — make that explicit instead of silently re-running.
    const completedCount = plans.filter(([, plan]) => plan.status === SessionStatus.COMPLETED).length;
    if (
      completedCount > 0 &&
      !window.confirm(
        completedCount === 1
          ? '1 selected clip already has a completed assessment; running it again will re-score it. Continue?'
          : `${completedCount} selected clips already have completed assessments; running them again will re-score them. Continue?`
      )
    ) {
      return;
    }

    setIsQueueingSelectedClips(true);
    const failedToQueue = new Set();
    let queuedCount = 0;
    try {
      // Sequential on purpose: one POST at a time keeps queue order matching
      // the clip order and avoids bursting the backend with parallel requests.
      for (const [clipId, plan] of plans) {
        const clip = clipsById.get(clipId);
        const queued =
          plan.mode === 'rerun'
            ? await rerunClipAssessment(clip, plan.childSessionId)
            : await runClipAssessment(clip);
        if (queued) {
          queuedCount += 1;
        } else {
          failedToQueue.add(clipId);
        }
      }
    } finally {
      // Queued clips already unchecked themselves; re-check the failures so
      // the user can fix the issue and retry the batch in one click.
      if (failedToQueue.size > 0) {
        setSelectedClipAssessmentIds((previous) => new Set([...previous, ...failedToQueue]));
      }
      setIsQueueingSelectedClips(false);
    }

    if (queuedCount > 0) {
      setNotice(
        `Queued ${queuedCount} clip assessment${queuedCount === 1 ? '' : 's'} — rows update as they progress.`
      );
    }
  }

  async function deleteClipAssessment(clip, childSessionId) {
    if (!childSessionId) {
      return;
    }
    const removed = await deleteSession(childSessionId, { childLabel: clip?.label || 'this clip' });
    if (removed) {
      // Drop the run state so the clip row returns to "Run assessment".
      setClipAssessmentRuns((previous) => {
        const next = { ...previous };
        delete next[clip.id];
        return next;
      });
    }
  }

  async function openClipAssessmentView(clip, runState) {
    const clipSessionId = runState?.sessionId;
    if (!clipSessionId) {
      return;
    }
    const controller = beginWorkspaceLoad();

    if (!parentSessionSnapshot) {
      setParentSessionSnapshot({
        session,
        transcript,
        scoreReport,
        audioProfessionalism,
        communicationScores,
        runtimeSeconds,
        notice,
      });
    }

    setError('');
    setNotice('');
    setIsLoadingWorkspace(true);
    // A child session opens in the single-student layout with a "Back to clip
    // list" control, so the placeholder is drawn that way; the parent view
    // stands down until the child's payload arrives.
    setWorkspaceLoad({ label: `Loading ${clip.label || 'clip'}`, layout: WORKSPACE_LAYOUT.CLIP });

    try {
      // Demo path: pull the child session straight out of the long-demo bundle.
      const demoChildren = session?._demoChildren;
      if (demoChildren && demoChildren[clipSessionId]) {
        const childBundle = demoChildren[clipSessionId];
        applyWorkspace(childBundle);
        setNotice(`Demo mode: showing pre-assessed clip "${clip.label || childBundle.session?.name || ''}"`);
        return;
      }

      const loaded = await loadSessionWorkspace(clipSessionId, { signal: controller.signal });
      if (controller.signal.aborted) return;
      applyWorkspace(loaded);
      setNotice('');
    } catch (error) {
      if (!controller.signal.aborted) setError(error.message || 'Failed to load clip assessment.');
    } finally {
      if (workspaceLoadRef.current === controller) {
        setIsLoadingWorkspace(false);
        setWorkspaceLoad(null);
      }
    }
  }

  function restoreParentSession() {
    workspaceLoadRef.current?.abort();
    // The aborted load's own cleanup lands when its request rejects; take the
    // placeholder down now so the parent is visible the moment it is restored.
    setWorkspaceLoad(null);
    setIsLoadingWorkspace(false);
    // Fast path: restore the parent from the in-memory snapshot captured when
    // the user drilled into a clip (no refetch, preserves prior UI state).
    if (parentSessionSnapshot?.session) {
      setSession(parentSessionSnapshot.session);
      setTranscript(parentSessionSnapshot.transcript || { segments: [] });
      setScoreReport(parentSessionSnapshot.scoreReport || null);
      setAudioProfessionalism(parentSessionSnapshot.audioProfessionalism || null);
      setCommunicationScores(parentSessionSnapshot.communicationScores || null);
      setRuntimeSeconds(Math.round(parentSessionSnapshot.runtimeSeconds || 0));
      setNotice(parentSessionSnapshot.notice || '');
      setParentSessionSnapshot(null);
      return;
    }

    // Fallback: no snapshot (reached the clip via reload / deep link / browser
    // forward). Navigate to the parent session by id through the router so the
    // address bar and history stay consistent.
    const parentId = session?.parentSessionId;
    if (!parentId) {
      return;
    }
    setParentSessionSnapshot(null);
    if (typeof onNavigateSession === 'function') {
      onNavigateSession(parentId);
    } else {
      openExistingSession(parentId);
    }
  }

  const durationKnown = Number.isFinite(videoDurationSeconds) && videoDurationSeconds > 0;
  const isLongRecording = durationKnown && videoDurationSeconds >= LONG_VIDEO_THRESHOLD_SECONDS;
  const showCropWorkflow = allowCropping;
  const showClipAssessmentPanel = allowCropping && hasClipFiles;
  const showAssessmentPanels = !isLongWorkflow || isClipAssessmentView;
  // Editing locks once the export has finished. While clips are still being
  // cut, or after a failed export, the manual editor stays: it carries the
  // progress / failure banners and the re-export button.
  const clipEditsLocked =
    allowCropping &&
    hasClipFiles &&
    !shouldWatchClipExport &&
    clipExportStatus !== SessionStatus.FAILED;

  // Everything the manual crop timeline reads, as one bundle. The separator
  // state stays here because the drag handlers, the boundary ref and
  // saveManualSegments all touch it together; only the markup moved.
  const manualTimelineProps = {
    applyTimelineMenuAction,
    clipExport,
    currentVideoTime,
    durationKnown,
    generateManualBoundariesFromCount,
    handleManualSegmentClick,
    handleTimelineClick,
    handleTimelineContextMenu,
    hasDraftClips,
    intermissionClipCount,
    isClipExportRunning,
    isPersonSegmentedSession,
    isSavingManualSegments,
    manualBoundaries,
    manualLabels,
    manualSegmentCount,
    manualSegmentKinds,
    manualTimelineRef,
    saveManualSegments,
    seekVideoPreview,
    session,
    sessionClipCount,
    setDraggingBoundaryIndex,
    setManualSegmentCount,
    timelineMenu,
    timelineMenuRef,
    updateManualLabelAt,
    videoDurationSeconds,
  };

  const clipSplitterSharedProps = {
    videoClips,
    selectedClipId,
    setSelectedClipId,
    renamingClipId,
    renameClipLabel,
    selectedClip,
    cropDraft,
    setCropDraft,
    videoDurationSeconds,
    // The crop editor is busy from the moment the request is sent until the
    // job's cut lands, not just while the POST is in flight — re-cutting is
    // queued work now, and the button has to say so for its whole life.
    isRecropping: isRecropping || isRecropRunning,
    recropSelectedClip,
  };

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      {/* The Back button is shown while a workspace is loading too, so the
          header has its final shape under the placeholder and does not shift
          when the view lands. Disabled until then, as before. */}
      <PageHeader
        icon={<Brain className="h-5 w-5" />}
        title="OSCE AI Marker"
        subtitle="Automated marking of OSCE station recordings"
        onBack={showWorkspace || workspaceLoad ? goHome : undefined}
        backDisabled={isUploading || isLoadingWorkspace}
        backTitle="Return to upload / saved sessions"
      >
        <ConnectionBadge status={connection.status} />
        {isDemoFallback ? <Badge variant="warning">Demo workspace</Badge> : null}
        {notifications ? (
          <NotificationBell
            items={notifications.items}
            unreadCount={notifications.unreadCount}
            hasLoaded={notifications.hasLoaded}
            onDismiss={notifications.dismiss}
            onDismissAll={notifications.dismissAll}
          />
        ) : null}
        <nav aria-label="Primary" className="flex flex-wrap items-center gap-2">
          {onOpenAnalytics ? (
            <Button
              variant="outline"
              size="sm"
              className="gap-2"
              onClick={onOpenAnalytics}
              onMouseEnter={() => onPreloadRoute?.('analytics')}
              onFocus={() => onPreloadRoute?.('analytics')}
            >
              <BarChart3 className="h-4 w-4" aria-hidden="true" />
              Analytics
            </Button>
          ) : null}
          {onOpenRubric ? (
            <Button
              variant="outline"
              size="sm"
              className="gap-2"
              onClick={onOpenRubric}
              onMouseEnter={() => onPreloadRoute?.('rubric')}
              onFocus={() => onPreloadRoute?.('rubric')}
            >
              <FileText className="h-4 w-4" aria-hidden="true" />
              Communication Rubric
            </Button>
          ) : null}
          {onOpenSettings ? (
            <Button
              variant="outline"
              size="sm"
              className="gap-2"
              onClick={onOpenSettings}
              onMouseEnter={() => onPreloadRoute?.('settings')}
              onFocus={() => onPreloadRoute?.('settings')}
            >
              <Settings className="h-4 w-4" aria-hidden="true" />
              Settings
            </Button>
          ) : null}
        </nav>
        {authUsername ? (
          <div className="flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">
            <User className="h-3.5 w-3.5 text-slate-500" aria-hidden="true" />
            <span className="text-xs font-semibold text-slate-700">{authUsername}</span>
          </div>
        ) : null}
        {onLogout ? (
          <Button variant="ghost" size="sm" className="gap-2 text-slate-600 hover:text-rose-700" onClick={onLogout}>
            <LogOut className="h-4 w-4" aria-hidden="true" />
            Log out
          </Button>
        ) : null}
      </PageHeader>

      <main id="main" className="mx-auto max-w-7xl px-6 py-10">
        {/* Three states share this slot: the dashboard, the placeholder for a
            workspace being fetched, and the workspace. The placeholder is a
            screen of its own — the dashboard stands down while it shows —
            because that is what the click asked for: the next screen, in
            outline, until its data arrives. `showWorkspace` itself only flips
            once the payload is in, so the URL sync effects above see the
            same sequence they always did.

            Opening a session is a route change (#/session/<id>) even though
            it stays inside AppShell's single "dashboard" AnimatePresence
            entry, so the fade between these three states is the same one
            AppShell gives every other route: opacity only, 0.25s, one state
            in flight at a time (`mode="wait"`). */}
        <AnimatePresence mode="wait">
        {!showWorkspace && !workspaceLoad && (
          <motion.section
            key="dashboard-form"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.25 }}
            className="grid grid-cols-1 gap-6 lg:grid-cols-3">
            <Card className="lg:col-span-2 border-slate-200 bg-white shadow-sm">
              <CardHeader>
                <CardTitle className="text-3xl">Upload a station recording</CardTitle>
                <CardDescription>
                  The recording is transcribed with speaker labels, then marked against the rubric in
                  the case study. Everything stays on this machine.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-5">
                {/* One control for one decision. This used to be a tab bar
                    with a coloured banner under it restating the choice; the
                    same radio-card pattern the auto-split method uses below
                    says what each mode is for in the option itself. */}
                <div
                  className="grid grid-cols-1 gap-2 sm:grid-cols-2"
                  role="radiogroup"
                  aria-label="What is in the recording"
                >
                  <button
                    type="button"
                    role="radio"
                    aria-checked={uploadFlow === Workflow.STANDARD}
                    onClick={() => setUploadFlow(Workflow.STANDARD)}
                    className={`flex items-start gap-3 rounded-xl border p-3 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 focus-visible:ring-offset-2 ${
                      uploadFlow === Workflow.STANDARD
                        ? 'border-cyan-400 bg-cyan-50 ring-2 ring-cyan-200'
                        : 'border-slate-200 bg-white hover:border-slate-300'
                    }`}
                  >
                    <Video className="mt-0.5 h-4 w-4 shrink-0 text-cyan-700" aria-hidden="true" />
                    <span className="min-w-0">
                      <span className="block text-sm font-semibold text-slate-800">One student</span>
                      <span className="block text-xs text-slate-500">
                        A single station encounter. Marked as one session; no cropping.
                      </span>
                    </span>
                  </button>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={uploadFlow === Workflow.LONG}
                    onClick={() => setUploadFlow(Workflow.LONG)}
                    className={`flex items-start gap-3 rounded-xl border p-3 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500 focus-visible:ring-offset-2 ${
                      uploadFlow === Workflow.LONG
                        ? 'border-violet-400 bg-violet-50 ring-2 ring-violet-200'
                        : 'border-slate-200 bg-white hover:border-slate-300'
                    }`}
                  >
                    <Film className="mt-0.5 h-4 w-4 shrink-0 text-violet-600" aria-hidden="true" />
                    <span className="min-w-0">
                      <span className="block text-sm font-semibold text-slate-800">Several students, one recording</span>
                      <span className="block text-xs text-slate-500">
                        Split into one clip per student — automatically, then adjusted by hand — and
                        mark each clip on its own.
                      </span>
                    </span>
                  </button>
                </div>

                {uploadFlow === Workflow.LONG && (
                  <div className="rounded-xl border border-slate-200 bg-white p-3">
                    <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                      Auto-split method
                    </div>
                    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Auto-split method">
                      <button
                        type="button"
                        role="radio"
                        aria-checked={segmentationMethod === SegmentationMethod.BELLS}
                        onClick={() => setSegmentationMethod(SegmentationMethod.BELLS)}
                        className={`flex items-start gap-2 rounded-lg border p-3 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500 focus-visible:ring-offset-2 ${
                          segmentationMethod === SegmentationMethod.BELLS
                            ? 'border-violet-400 bg-violet-50 ring-2 ring-violet-200'
                            : 'border-slate-200 bg-white hover:border-slate-300'
                        }`}
                      >
                        <BellRing className="mt-0.5 h-4 w-4 shrink-0 text-violet-600" aria-hidden="true" />
                        <span className="min-w-0">
                          <span className="block text-sm font-medium text-slate-800">Bell detection</span>
                          <span className="block text-xs text-slate-500">
                            Splits at station bell sounds (audio). Best when bells are clearly audible.
                          </span>
                        </span>
                      </button>
                      <button
                        type="button"
                        role="radio"
                        aria-checked={segmentationMethod === SegmentationMethod.PERSON}
                        onClick={() => setSegmentationMethod(SegmentationMethod.PERSON)}
                        className={`flex items-start gap-2 rounded-lg border p-3 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500 focus-visible:ring-offset-2 ${
                          segmentationMethod === SegmentationMethod.PERSON
                            ? 'border-violet-400 bg-violet-50 ring-2 ring-violet-200'
                            : 'border-slate-200 bg-white hover:border-slate-300'
                        }`}
                      >
                        <Users className="mt-0.5 h-4 w-4 shrink-0 text-violet-600" aria-hidden="true" />
                        <span className="min-w-0">
                          <span className="block text-sm font-medium text-slate-800">Human detection</span>
                          <span className="block text-xs text-slate-500">
                            AI vision (RT-DETR) splits when a student leaves the frame. Best when bells are unreliable.
                          </span>
                        </span>
                      </button>
                    </div>
                    {segmentationMethod === SegmentationMethod.PERSON && segmentationPresets.length > 0 && (
                      <div className="mt-3 border-t border-slate-100 pt-3">
                        <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                          Who is on screen during a station
                        </div>
                        <p className="mb-2 text-[11px] text-slate-500">
                          Pick the rule that matches this camera angle. A hand or shoulder at the edge
                          of the frame is a person to the detector, so a one-student angle needs a
                          different rule from a wide two-person shot.
                        </p>
                        <p className="mb-2 flex items-center gap-1.5 text-[11px] text-slate-400">
                          <User className="h-3 w-3 shrink-0 text-violet-600" aria-hidden="true" />
                          Solid figure counts
                          <User className="h-3 w-3 shrink-0 text-slate-300" aria-hidden="true" />
                          faint one at the edge is seen but ignored
                        </p>
                        <div className="grid grid-cols-1 gap-2" role="radiogroup" aria-label="Occupancy rule">
                          {segmentationPresets.map((preset) => (
                            <button
                              key={preset.id}
                              type="button"
                              role="radio"
                              aria-checked={segmentationPreset === preset.id}
                              onClick={() => setSegmentationPreset(preset.id)}
                              className={`flex items-center gap-3 rounded-lg border p-2.5 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500 focus-visible:ring-offset-2 ${
                                segmentationPreset === preset.id
                                  ? 'border-violet-400 bg-violet-50 ring-2 ring-violet-200'
                                  : 'border-slate-200 bg-white hover:border-slate-300'
                              }`}
                            >
                              <OccupancyPresetGlyph presetId={preset.id} />
                              <span className="min-w-0 flex-1">
                                <span className="flex items-center justify-between gap-2">
                                  <span className="text-sm font-medium text-slate-800">{preset.label}</span>
                                  {preset.id !== 'custom' && (
                                    <span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-500">
                                      {preset.minPeople}+ on screen
                                    </span>
                                  )}
                                </span>
                                <span className="mt-0.5 block text-xs text-slate-500">{preset.description}</span>
                              </span>
                            </button>
                          ))}
                        </div>

                        {segmentationPreset === 'custom' && (
                          <div className="mt-2 grid grid-cols-1 gap-2 rounded-lg border border-slate-200 bg-slate-50 p-3 sm:grid-cols-3">
                            <label className="block">
                              <span className="block text-[11px] font-medium text-slate-600">People on screen</span>
                              <input
                                type="number"
                                min={1}
                                max={10}
                                step={1}
                                value={customOccupancy.minPeople}
                                onChange={(event) =>
                                  setCustomOccupancy((previous) => ({
                                    ...previous,
                                    minPeople: event.target.value,
                                  }))
                                }
                                className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
                              />
                              <span className="mt-1 block text-[11px] text-slate-500">
                                Minimum for a station to count as running.
                              </span>
                            </label>
                            <label className="block">
                              <span className="block text-[11px] font-medium text-slate-600">Min person height</span>
                              <input
                                type="number"
                                min={0}
                                max={0.95}
                                step={0.05}
                                value={customOccupancy.minBoxHeightRatio}
                                onChange={(event) =>
                                  setCustomOccupancy((previous) => ({
                                    ...previous,
                                    minBoxHeightRatio: event.target.value,
                                  }))
                                }
                                className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
                              />
                              <span className="mt-1 block text-[11px] text-slate-500">
                                Fraction of frame height. 0 counts every detection, limbs included.
                              </span>
                            </label>
                            <label className="block">
                              <span className="block text-[11px] font-medium text-slate-600">Min station length</span>
                              <input
                                type="number"
                                min={0}
                                max={3600}
                                step={10}
                                value={customOccupancy.minSessionSeconds}
                                onChange={(event) =>
                                  setCustomOccupancy((previous) => ({
                                    ...previous,
                                    minSessionSeconds: event.target.value,
                                  }))
                                }
                                className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1 text-sm"
                              />
                              <span className="mt-1 block text-[11px] text-slate-500">
                                Seconds. Shorter detections are discarded as false starts.
                              </span>
                            </label>
                          </div>
                        )}
                      </div>
                    )}
                    {segmentationMethod === SegmentationMethod.PERSON && !regionFocusLocked && (
                      <div className="mt-3 border-t border-slate-100 pt-3">
                        <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                          Where to look in the frame
                        </div>
                        <p className="mb-2 text-[11px] text-slate-500">
                          Restrict detection to one side of the frame when a third party — another
                          examiner, a doorway — is half cut off at the other edge and would otherwise
                          get counted as an occupant. Leave both at 100% to use the whole frame.
                        </p>
                        <RegionFocusPreview videoUrl={localVideoUrl} regionFocus={regionFocus} />
                        <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
                          {[
                            { side: 'left', label: 'Left side', enabledKey: 'leftEnabled', ratioKey: 'leftRatio' },
                            { side: 'right', label: 'Right side', enabledKey: 'rightEnabled', ratioKey: 'rightRatio' },
                          ].map(({ side, label, enabledKey, ratioKey }) => (
                            <div
                              key={side}
                              className={`rounded-lg border p-2.5 transition ${
                                regionFocus[enabledKey] ? 'border-violet-200 bg-violet-50/50' : 'border-slate-200 bg-white'
                              }`}
                            >
                              <label className="flex items-center gap-2 text-sm font-medium text-slate-800">
                                <input
                                  type="checkbox"
                                  checked={Boolean(regionFocus[enabledKey])}
                                  onChange={() => setRegionFocus((previous) => toggleRegionSide(previous, side))}
                                  className="h-4 w-4 rounded border-slate-300 text-violet-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500"
                                />
                                {label}
                              </label>
                              <label className="mt-2 block">
                                <span className="block text-[11px] text-slate-500">
                                  Width of frame counted, measured from the {side} edge
                                </span>
                                <div className="mt-1 flex items-center gap-1">
                                  <input
                                    type="number"
                                    min={5}
                                    max={100}
                                    step={5}
                                    disabled={!regionFocus[enabledKey]}
                                    value={Math.round(clampRegionRatio(regionFocus[ratioKey]) * 100)}
                                    onChange={(event) =>
                                      setRegionFocus((previous) => ({
                                        ...previous,
                                        [ratioKey]: Number(event.target.value) / 100,
                                      }))
                                    }
                                    className="w-20 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-400"
                                  />
                                  <span className="text-xs text-slate-500">%</span>
                                </div>
                              </label>
                            </div>
                          ))}
                        </div>
                        <p className="mt-2 text-[11px] text-slate-500">
                          At least one side must stay on — the last enabled checkbox cannot be turned off.
                        </p>
                      </div>
                    )}
                    {segmentationMethod === SegmentationMethod.PERSON && (
                      <p className="mt-2 text-[11px] text-slate-500">
                        Falls back to bell detection automatically if the vision model is unavailable on the worker.
                      </p>
                    )}
                  </div>
                )}

                <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                  <UploadCard
                    icon={<Video className="h-5 w-5" />}
                    title={uploadFlow === Workflow.LONG ? 'Long station recording' : 'Station recording'}
                    subtitle={uploadFlow === Workflow.LONG ? 'MP4, MOV or MKV — usually 5 minutes or longer' : 'MP4, MOV or MKV'}
                    fileName={videoFile?.name || null}
                    onPick={() => videoInputRef.current?.click()}
                  />

                  <UploadCard
                    icon={<FileSpreadsheet className="h-5 w-5" />}
                    title="Case study"
                    subtitle="PDF. The rubric is read from its closing checklist section"
                    fileName={caseStudyFile?.name || null}
                    onPick={() => caseStudyInputRef.current?.click()}
                  />
                </div>

                <input
                  ref={videoInputRef}
                  type="file"
                  className="hidden"
                  accept="video/*"
                  onChange={(event) => setVideoFile(event.target.files?.[0] || null)}
                />

                <input
                  ref={caseStudyInputRef}
                  type="file"
                  className="hidden"
                  accept=".pdf,application/pdf"
                  onChange={(event) => setCaseStudyFile(event.target.files?.[0] || null)}
                />

                <div className="flex flex-wrap items-center gap-3 pt-2">
                  <Button
                    size="lg"
                    className="gap-2"
                    onClick={requestStartAssessment}
                    disabled={!videoFile || !caseStudyFile || isUploading || isLoadingWorkspace}
                  >
                    <Wand2 className="h-4 w-4" aria-hidden="true" />
                    {uploadFlow === Workflow.LONG ? 'Upload and split into clips' : 'Upload and start assessment'}
                  </Button>
                  <span className="text-sm text-slate-500" role="status">
                    {videoFile && caseStudyFile
                      ? 'Both files chosen. Marking runs on this machine.'
                      : 'Choose a recording and a case study PDF to start.'}
                  </span>
                </div>

                {/* The demos are for a first look, not for marking, so they
                    sit under the real action at a lower weight. */}
                <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 pt-4 text-sm text-slate-500">
                  <span>No recording to hand?</span>
                  <Button
                    size="sm"
                    variant="outline"
                    className="gap-2"
                    onClick={openManualDemoMode}
                    disabled={isUploading || isLoadingWorkspace}
                  >
                    <PlayCircle className="h-4 w-4" aria-hidden="true" />
                    Open the one-student demo
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="gap-2 border-violet-300 text-violet-700 hover:bg-violet-50"
                    onClick={openLongVideoDemoWorkspace}
                    disabled={isUploading || isLoadingWorkspace}
                  >
                    <Scissors className="h-4 w-4" aria-hidden="true" />
                    Open the multi-student demo
                  </Button>
                </div>

                {error && (
                  <div role="alert" className="rounded-xl border border-rose-300 bg-rose-50 p-3 text-sm text-rose-700">
                    {error}
                  </div>
                )}

                {notice && !showWorkspace && (
                  <div role="status" className="rounded-xl border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
                    {notice}
                  </div>
                )}
              </CardContent>
            </Card>

            <div className="space-y-6">
              <Card className="border-slate-200 bg-white shadow-sm">
                <CardHeader>
                  <CardTitle>Saved sessions</CardTitle>
                  <CardDescription>Open, rename or delete any session on this machine.</CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  {/* First load only: rows in outline until the index answers.
                      A later refresh keeps the rows it has and dims the list
                      (below); the coalesced background refreshes are silent
                      and never raise the flag at all. */}
                  {sessionIndexLoading && !hasLoadedSessionIndex ? (
                    <LoadingRegion label="Loading saved sessions">
                      <SessionRowsSkeleton />
                    </LoadingRegion>
                  ) : null}

                  {/* Stale data needs an explanation where the stale data is.
                      The header chip says the connection is down; this says
                      what that means for this list, and offers the one useful
                      action. */}
                  <ConnectionNotice
                    status={connection.status}
                    message={connection.message}
                    onRetry={() => refreshSessionIndex()}
                    retrying={sessionIndexLoading}
                  />

                  {/* Suppressed while offline: the notice above already
                      explains the same failure, and better. */}
                  {sessionIndexError && connection.status === CONNECTION_STATUS.ONLINE ? (
                    <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-xs text-rose-700">
                      {sessionIndexError}
                    </div>
                  ) : null}

                  {/* An empty state is an answer, so it waits for one: after a
                      failed first fetch the error above is the whole story. */}
                  {hasLoadedSessionIndex && !sessionIndexLoading && visibleSessions.length === 0 ? (
                    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-sm text-slate-500">
                      No saved sessions yet. Upload a video to create one.
                    </div>
                  ) : null}

                  <ul
                    className={`max-h-[32rem] space-y-2 overflow-y-auto pr-1 transition-opacity ${
                      sessionIndexLoading && hasLoadedSessionIndex ? 'opacity-60' : ''
                    }`}
                    aria-busy={sessionIndexLoading && hasLoadedSessionIndex ? 'true' : undefined}
                    aria-label="Saved sessions"
                  >
                    {visibleSessions.map((sessionEntry) => (
                      <li
                        key={sessionEntry.id}
                        className="rounded-xl border border-slate-200 bg-slate-50 p-3"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0 flex-1">
                            <input
                              type="text"
                              value={sessionNameDrafts[sessionEntry.id] ?? sessionEntry.name ?? ''}
                              onChange={(event) =>
                                setSessionNameDrafts((previous) => ({
                                  ...previous,
                                  [sessionEntry.id]: event.target.value,
                                }))
                              }
                              onBlur={(event) => {
                                const nextValue = event.target.value.trim();
                                const currentValue = String(sessionEntry.name || '').trim();
                                if (nextValue && nextValue !== currentValue) {
                                  renameSessionName(sessionEntry.id, nextValue);
                                }
                              }}
                              onKeyDown={(event) => {
                                if (event.key === 'Enter') {
                                  event.preventDefault();
                                  event.currentTarget.blur();
                                }
                              }}
                              disabled={renamingSessionId === sessionEntry.id}
                              aria-label="Session name"
                              className="w-full rounded-lg border border-transparent bg-white px-2 py-1.5 text-sm font-semibold text-slate-800 shadow-sm focus:border-cyan-400 focus:outline-none focus:ring-2 focus:ring-cyan-400/30"
                            />
                            <div className="mt-1 truncate text-[11px] text-slate-500" title={sessionEntry.id}>
                              {sessionEntry.id}
                            </div>
                            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                              {/* The status as a word, in its tone, from one
                                  table (lib/sessionStatus.js). An upload still
                                  leaving this tab is busy whatever the server
                                  calls the row. */}
                              <SessionStatusBadge
                                status={sessionEntry.status}
                                busy={uploadTracker.isActive(sessionEntry.id) || undefined}
                              />
                              {sessionEntry.hasVideoClips || sessionEntry.status === SessionStatus.CROPPED ? (
                                <Badge variant="accent">Several students</Badge>
                              ) : null}
                            </div>
                            {(() => {
                              // Stage gauge for in-flight sessions: the row is
                              // not enterable, so the card is where the user
                              // tracks how far along the run is.
                              //
                              // Two sources, one gauge. While this tab is still
                              // sending bytes the server knows nothing but
                              // "waiting_for_upload", so the client-side track
                              // answers; once the upload is committed it stands
                              // down and the server-derived stage takes over.
                              const uploadStage = uploadTracker.describe(sessionEntry.id);
                              const stage = uploadStage || describeProcessingStage(sessionEntry);
                              if (!stage) return null;
                              const failed = Boolean(stage.failed);
                              return (
                                <div className="mt-2">
                                  <div className="flex items-center justify-between gap-2 text-[11px] text-slate-600">
                                    {/* The stage's own percentage when the step
                                        streams one (upload bytes, WhisperX),
                                        next to the elapsed clock and the
                                        completion on the right. */}
                                    <span className={failed ? 'text-rose-600' : undefined}>
                                      {formatProcessingStageLabel(stage)}
                                      {failed ? '' : '…'}
                                    </span>
                                    <span className="flex shrink-0 items-center gap-1">
                                      {stage.elapsedSeconds === undefined ? null : (
                                        <>
                                          <Clock3 className="h-3 w-3 text-slate-500" aria-hidden="true" />
                                          <span className="tabular-nums">
                                            {formatRuntime(stage.elapsedSeconds)}
                                          </span>
                                          <span className="text-slate-300" aria-hidden="true">·</span>
                                        </>
                                      )}
                                      <span className="tabular-nums">{Math.round(stage.fraction * 100)}%</span>
                                    </span>
                                  </div>
                                  <Progress
                                    value={stage.fraction * 100}
                                    className="mt-1 h-1.5"
                                    label={`${formatProcessingStageLabel(stage)} progress`}
                                  />
                                  {failed && uploadTracker.tracks[sessionEntry.id]?.error ? (
                                    <div className="mt-1 text-[11px] text-rose-600">
                                      {uploadTracker.tracks[sessionEntry.id].error}
                                    </div>
                                  ) : null}
                                </div>
                              );
                            })()}
                            {sessionEntry.status === SessionStatus.FAILED && sessionEntry.error ? (
                              // The list projection carries the failure reason so a
                              // session the user cannot usefully open still says why —
                              // including "interrupted by a server restart", which the
                              // startup reconciliation writes for jobs that died.
                              <div className="mt-2 rounded border border-rose-100 bg-rose-50 px-2 py-1 text-[11px] leading-snug text-rose-700">
                                {sessionEntry.error}
                              </div>
                            ) : null}
                          </div>
                          <div className="flex shrink-0 items-center gap-1">
                            {renderSessionAction(sessionEntry)}
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => deleteSession(sessionEntry.id)}
                              disabled={deletingSessionId === sessionEntry.id}
                              aria-label={`Delete session ${sessionEntry.name || sessionEntry.id}`}
                              title="Delete this session and all student assessments under it"
                              className="text-rose-600 hover:bg-rose-50 hover:text-rose-700 focus-visible:ring-rose-500"
                            >
                              {deletingSessionId === sessionEntry.id ? (
                                <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                              ) : (
                                <Trash2 className="h-4 w-4" aria-hidden="true" />
                              )}
                            </Button>
                          </div>
                        </div>
                      </li>
                    ))}
                  </ul>
                </CardContent>
              </Card>

              {notifications ? (
                <NotificationFeed
                  items={notifications.items}
                  unreadCount={notifications.unreadCount}
                  hasLoaded={notifications.hasLoaded}
                  onDismiss={notifications.dismiss}
                />
              ) : null}
            </div>
          </motion.section>
        )}

        {workspaceLoad && (
          <motion.div
            key="workspace-loading"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.25 }}
          >
            <WorkspaceSkeleton layout={workspaceLoad.layout} label={workspaceLoad.label} />
          </motion.div>
        )}

        {/* The chunk fallback is the same skeleton, in the layout the loaded
            session actually has: if the chunk arrives after the payload — a
            slow network beats the preload — the placeholder simply stays. */}
        {showWorkspace && !workspaceLoad && (
          <motion.div
            key="workspace"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.25 }}
          >
          <LazyBoundary fallback={<WorkspaceSkeleton layout={workspaceLayoutFor(session)} />}>
            <SessionWorkspace
              session={session}
              videoFile={videoFile}
              caseStudyFile={caseStudyFile}
              transcript={transcript}
              scoreReport={scoreReport}
              communicationScores={communicationScores}
              audioProfessionalism={audioProfessionalism}
              setAudioProfessionalism={setAudioProfessionalism}
              audioProfLoadError={audioProfLoadError}
              setAudioProfLoadError={setAudioProfLoadError}
              localVideoUrl={localVideoUrl}
              isDemoFallback={isDemoFallback}
              runtimeSeconds={runtimeSeconds}
              notice={notice}
              isLoadingWorkspace={isLoadingWorkspace}
              isClipAssessmentView={isClipAssessmentView}
              restoreParentSession={restoreParentSession}
              videoPlayerRef={videoPlayerRef}
              videoPlayerSectionRef={videoPlayerSectionRef}
              timelineContainerRef={timelineContainerRef}
              timelineSegmentRefs={timelineSegmentRefs}
              activeSegmentId={activeSegmentId}
              setActiveSegmentId={setActiveSegmentId}
              videoDurationSeconds={videoDurationSeconds}
              setVideoDurationSeconds={setVideoDurationSeconds}
              setCurrentVideoTime={setCurrentVideoTime}
              durationKnown={durationKnown}
              isLongRecording={isLongRecording}
              showCropWorkflow={showCropWorkflow}
              clipEditsLocked={clipEditsLocked}
              isPlanExportRunning={isPlanExportRunning}
              currentModeLabel={currentModeLabel}
              manualTimeline={manualTimelineProps}
              clipSplitterSharedProps={clipSplitterSharedProps}
              showAssessmentPanels={showAssessmentPanels}
              showClipAssessmentPanel={showClipAssessmentPanel}
              videoClips={videoClips}
              clipAssessmentRuns={clipAssessmentRuns}
              clipAssessmentIndex={clipAssessmentIndex}
              selectedClipAssessmentIds={selectedClipAssessmentIds}
              selectedRunnableClipIds={selectedRunnableClipIds}
              batchSelectableClipIds={batchSelectableClipIds}
              allClipsSelected={allClipsSelected}
              isQueueingSelectedClips={isQueueingSelectedClips}
              toggleClipSelected={toggleClipSelected}
              toggleSelectAllClips={toggleSelectAllClips}
              runSelectedClipAssessments={runSelectedClipAssessments}
              runClipAssessment={runClipAssessment}
              rerunClipAssessment={rerunClipAssessment}
              deleteClipAssessment={deleteClipAssessment}
              openClipAssessmentView={openClipAssessmentView}
              deletingSessionId={deletingSessionId}
              sessionIndex={sessionIndex}
              clipSummaries={clipSummaries}
              isLoadingClipSummaries={isLoadingClipSummaries}
              demoLongVideoSummaries={demoLongVideoSummaries}
            />
          </LazyBoundary>
          </motion.div>
        )}
        </AnimatePresence>
      </main>

      <AnimatePresence>
        {showConfirmStart && (
          <Modal
            onClose={cancelStartAssessment}
            labelledBy="confirm-start-title"
            describedBy="confirm-start-description"
            initialFocus={sessionNameInputRef}
          >
              <Card className="border-slate-200 bg-white shadow-xl">
                <CardHeader>
                  <CardTitle id="confirm-start-title" className="flex items-center gap-2 text-lg">
                    <ClipboardCheck className="h-5 w-5 text-cyan-700" aria-hidden="true" />
                    Confirm assessment
                  </CardTitle>
                  <CardDescription id="confirm-start-description">
                    Check the files and name this session before it starts.
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="space-y-2">
                    <div className="flex items-start gap-2 rounded-xl border border-slate-200 bg-slate-50 p-3">
                      <Video className="mt-0.5 h-4 w-4 shrink-0 text-slate-500" />
                      <div className="min-w-0">
                        <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                          Station video
                        </div>
                        <div className="truncate text-sm text-slate-800" title={videoFile?.name || ''}>
                          {videoFile?.name || 'No file selected'}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-start gap-2 rounded-xl border border-slate-200 bg-slate-50 p-3">
                      <FileSpreadsheet className="mt-0.5 h-4 w-4 shrink-0 text-slate-500" />
                      <div className="min-w-0">
                        <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                          Case study (rubric)
                        </div>
                        <div className="truncate text-sm text-slate-800" title={caseStudyFile?.name || ''}>
                          {caseStudyFile?.name || 'No file selected'}
                        </div>
                      </div>
                    </div>
                    {uploadFlow === Workflow.LONG && (
                      <div className="flex items-start gap-2 rounded-xl border border-slate-200 bg-slate-50 p-3">
                        {segmentationMethod === SegmentationMethod.PERSON ? (
                          <Users className="mt-0.5 h-4 w-4 shrink-0 text-slate-500" />
                        ) : (
                          <BellRing className="mt-0.5 h-4 w-4 shrink-0 text-slate-500" />
                        )}
                        <div className="min-w-0">
                          <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                            Auto-split method
                          </div>
                          <div className="text-sm text-slate-800">
                            {segmentationMethod === SegmentationMethod.PERSON
                              ? 'Human detection (AI vision, RT-DETR)'
                              : 'Bell detection (audio)'}
                          </div>
                          {segmentationMethod === SegmentationMethod.PERSON && (
                            <div className="text-xs text-slate-500">
                              {segmentationPreset === 'custom'
                                ? `Custom rule — ${customOccupancy.minPeople}+ on screen, min height `
                                  + `${customOccupancy.minBoxHeightRatio}, min ${customOccupancy.minSessionSeconds}s`
                                : segmentationPresets.find((preset) => preset.id === segmentationPreset)?.label
                                  || segmentationPreset}
                            </div>
                          )}
                          {segmentationMethod === SegmentationMethod.PERSON && describeRegionFocus(regionFocus) && (
                            <div className="text-xs text-slate-500">
                              Region focus: {describeRegionFocus(regionFocus)}
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>

                  <div>
                    <div className="flex items-center justify-between">
                      <label htmlFor="corpus-select" className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                        Transcription corpus
                      </label>
                      <button
                        type="button"
                        onClick={() => setShowCorpusManager(true)}
                        className="text-[11px] font-semibold text-cyan-700 underline-offset-2 hover:underline"
                      >
                        Manage corpora
                      </button>
                    </div>
                    <select
                      id="corpus-select"
                      value={selectedCorpusId}
                      onChange={(event) => setSelectedCorpusId(event.target.value)}
                      className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 outline-none focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100"
                    >
                      <option value="">None — plain transcription</option>
                      {corpora.map((corpus) => (
                        <option key={corpus.id} value={corpus.id}>
                          {corpus.name} ({(corpus.terms || []).length} terms)
                        </option>
                      ))}
                    </select>
                    <p className="mt-1 text-[11px] text-slate-500">
                      Case-specific terms (e.g. nasal block, paracetamol) bias transcription and apply
                      to every clip marked in this session.
                    </p>
                  </div>

                  <div>
                    <label htmlFor="session-name-input" className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                      Session name
                    </label>
                    <input
                      ref={sessionNameInputRef}
                      id="session-name-input"
                      type="text"
                      value={sessionNameInput}
                      onChange={(event) => setSessionNameInput(event.target.value)}
                      maxLength={80}
                      placeholder="Leave blank to auto-generate"
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          confirmStartAssessment();
                        }
                      }}
                      className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 outline-none focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100"
                    />
                    <p className="mt-1 text-[11px] text-slate-500">
                      A unique number is appended automatically if the name already exists.
                    </p>
                  </div>

                  <div className="flex items-center justify-end gap-2 pt-1">
                    <Button variant="outline" size="sm" onClick={cancelStartAssessment}>
                      Cancel
                    </Button>
                    <Button size="sm" className="gap-2" onClick={confirmStartAssessment}>
                      <Wand2 className="h-4 w-4" aria-hidden="true" />
                      {uploadFlow === Workflow.LONG ? 'Upload and split into clips' : 'Upload and start assessment'}
                    </Button>
                  </div>
                </CardContent>
              </Card>
          </Modal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {showCorpusManager && (
          <Modal
            onClose={() => setShowCorpusManager(false)}
            labelledBy="corpora-manager-title"
            size="lg"
            backdropClassName="z-[60]"
          >
            <CorporaManager
              titleId="corpora-manager-title"
              onClose={() => setShowCorpusManager(false)}
              onChanged={handleCorporaChanged}
              onCreated={(corpus) => {
                // Creating a corpus from the picker usually means "use it now".
                setSelectedCorpusId(corpus.id);
              }}
            />
          </Modal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {/* The upload overlay. It shows the one thing this tab genuinely knows
            about that the server does not: how far the bytes have got. It used
            to also render a "started -> mp3 -> transcript -> scored" milestone
            checklist and a live console line, both fed by the per-session SSE
            stream the app stopped consuming — so the rows never ticked and the
            console line never moved. Worse, the same flag was set while *any*
            workspace loaded, so opening a finished session popped a modal
            titled "Starting Job" listing a pipeline that was not running.

            Everything here now comes from the upload track
            (`lib/uploadTracking.js`), which is also what the session card
            reads. One source, so the overlay and the card can never disagree,
            and dismissing this hides a view without stopping a transfer. */}
        {isUploading && !uploadOverlayDismissed && (
          <Modal
            onClose={dismissUploadOverlay}
            labelledBy="upload-overlay-title"
            describedBy="upload-overlay-description"
          >
              {(() => {
                // Null in the moment between the transfer being committed and
                // `isUploading` clearing; the label below covers it.
                const stage = uploadTracker.describeActive();
                return (
                  <Card className="border-slate-200 bg-white shadow-xl">
                    <CardHeader>
                      <CardTitle id="upload-overlay-title" className="flex items-center gap-2 text-lg">
                        <Loader2 className="h-5 w-5 animate-spin text-cyan-700 motion-reduce:animate-none" aria-hidden="true" />
                        Uploading files
                      </CardTitle>
                      <CardDescription id="upload-overlay-description" aria-live="polite">
                        {stage ? formatProcessingStageLabel(stage) : 'Handing the upload to the server…'}
                      </CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-3">
                      <div className="rounded-xl border border-cyan-200 bg-cyan-50 p-4 text-center">
                        <div className="flex items-center justify-center gap-2 text-xs font-semibold uppercase tracking-wide text-cyan-700">
                          <Clock3 className="h-3.5 w-3.5" aria-hidden="true" /> Elapsed
                        </div>
                        {/* Derived from the track's own timestamps, so it keeps
                            time across a dismiss and a re-open. */}
                        <div className="mt-1 text-3xl font-bold text-cyan-900">
                          {formatRuntime(stage?.elapsedSeconds || 0)}
                        </div>
                      </div>

                      <div>
                        <div className="flex items-center justify-between text-[11px] text-slate-600">
                          <span>{stage?.label || 'Finalizing upload'}</span>
                          <span className="tabular-nums">{Math.round((stage?.fraction || 0) * 100)}%</span>
                        </div>
                        <Progress
                          value={(stage?.fraction || 0) * 100}
                          className="mt-1 h-1.5"
                          label="Upload progress"
                        />
                        {stage?.detail ? (
                          <div className="mt-1 text-xs text-amber-700">{stage.detail}</div>
                        ) : null}
                      </div>

                      <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs text-slate-500">
                        Marking starts on the server once the files land. The session card
                        gauges every step and unlocks when the run finishes.
                      </div>

                      {/* Hides the card only. Previously this also cleared the
                          in-flight flags, which stopped the runtime clock and left
                          the session card with a bare "waiting_for_upload" — while
                          the transfer it appeared to cancel carried on regardless.
                          The run now keeps its clock and its percentage on the
                          Saved Sessions card. */}
                      <Button
                        variant="ghost"
                        size="sm"
                        className="w-full text-slate-500 hover:text-slate-700"
                        onClick={dismissUploadOverlay}
                        title="Hide this card — the upload keeps running"
                      >
                        Hide, keep uploading
                      </Button>
                    </CardContent>
                  </Card>
                );
              })()}
          </Modal>
        )}
      </AnimatePresence>

      {/* Fetching a saved session's artefacts has no overlay. It used to dim
          the page behind a "Loading session…" card; the wait is now the
          WorkspaceSkeleton in the main slot, drawn in the outline of the view
          about to open, so nothing pops up and nothing is dimmed. */}
    </div>
  );
}

// Opening a session is what needs the workspace chunk, so hovering or tabbing
// to the button that does it starts the fetch. By the time the click lands the
// chunk is normally parsed — the same preload-on-intent the dashboard's nav
// buttons use for Settings, Analytics and the Rubric.
function OpenSessionButton({ onOpen }) {
  const warm = () => preloadComponent(SessionWorkspace);
  return (
    <Button size="sm" variant="outline" onClick={onOpen} onMouseEnter={warm} onFocus={warm}>
      Open
    </Button>
  );
}

function UploadCard({ icon, title, subtitle, fileName, onPick }) {
  return (
    <div className="flex h-full flex-col rounded-2xl border border-slate-200 bg-slate-50 p-4">
      <div className="mb-3 flex items-start gap-3">
        <div className="rounded-xl bg-gradient-to-br from-cyan-600 to-blue-700 p-2.5 text-white" aria-hidden="true">{icon}</div>
        <div className="min-h-[2.5rem]">
          <div className="text-sm font-semibold text-slate-900">{title}</div>
          <div className="text-xs text-slate-500">{subtitle}</div>
        </div>
      </div>

      <button
        type="button"
        onClick={onPick}
        aria-label={`Choose ${title.toLowerCase()} file`}
        className="flex min-h-28 w-full flex-1 flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-slate-300 bg-white text-slate-600 transition hover:border-cyan-500 hover:bg-cyan-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 focus-visible:ring-offset-2"
      >
        <UploadCloud className="h-6 w-6" aria-hidden="true" />
        <span className="text-sm font-medium">{fileName ? 'Choose a different file' : 'Choose file'}</span>
      </button>

      <div className="mt-3 truncate text-xs font-medium text-slate-700" aria-live="polite">
        {fileName || 'No file chosen'}
      </div>
    </div>
  );
}
