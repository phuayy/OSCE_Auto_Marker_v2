import React, { useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { ensureStreamTicket, resolveMediaUrl } from '@/auth';
import { useChangeStream } from '@/changeStream';
import {
  IN_FLIGHT_STATUSES,
  describeProcessingStage,
  formatProcessingStageLabel,
} from '@/lib/processingStage';
import {
  ArrowLeft,
  BarChart3,
  BellRing,
  Brain,
  ClipboardCheck,
  Clock3,
  Download,
  FileSpreadsheet,
  FileText,
  Loader2,
  LogOut,
  MessageSquare,
  Mic,
  PlayCircle,
  RotateCw,
  Scissors,
  Settings,
  Sparkles,
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
import { Progress } from '@/components/ui/progress';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import CorporaManager from './CorporaManager.jsx';
import LongVideoSummaryCharts from './LongVideoSummaryCharts.jsx';
import { NotificationBell, NotificationFeed } from '@/notifications.jsx';
import {
  INTERMISSION_KIND,
  contextMenuActions,
  ensureKinds,
  hitTestTimeline,
  insertSeparator,
  normalizeLabels,
  removeSeparator,
  sessionOrdinals,
  timeAtOffset,
  toggleSegmentKind,
} from './lib/manualTimeline.js';
import {
  areAllSelected,
  planClipDispatch,
  selectableClipIds,
  toggleSelection,
} from './lib/clipSelection.js';

const SCORE_TEMPLATE = [
  {
    key: 'Communication',
    weight: 0.35,
    score: 84,
    desc: 'Clarity, rapport, empathy, and patient-centered responses.',
  },
  {
    key: 'Clinical Reasoning',
    weight: 0.4,
    score: 79,
    desc: 'Appropriate questioning, recommendations, and follow-up safety checks.',
  },
  {
    key: 'Professionalism',
    weight: 0.15,
    score: 92,
    desc: 'Respectful conduct, structure, and confidence.',
  },
  {
    key: 'Time Management',
    weight: 0.1,
    score: 86,
    desc: 'Efficient progression through the counseling workflow.',
  },
];

const DEFAULT_KEEP_START_STOP = {
  keep: 'Continue the clear and patient-friendly approach shown in this consultation.',
  start: 'Start adding sharper evidence checks and teach-back prompts after key advice.',
  stop: 'Stop repeating medication directions without confirmation of understanding.',
};

const DEMO_SESSION_ID = '4d4afdd3-8aa2-41d1-96ba-bfc44f0b2456';
const DEMO_RESOURCE_BASE = `/demo-resources/${DEMO_SESSION_ID}`;
const DEMO_RESOURCE_FILES = {
  session: 'session.json',
  scores: 'scores.json',
  communicationScores: 'communication-scores.json',
  audioProfessionalism: 'audio-professionalism.json',
  transcript: 'transcript.json',
  video: 'video.mp4',
  caseStudy: 'case-study.pdf',
  audio: 'audio.mp3',
  whisperx: 'whisperx.json',
  subtitle: 'subtitles.srt',
  subtitleTrack: 'subtitles.vtt',
};

// Long-video demo bundle — pre-cropped + pre-assessed.
const LONG_DEMO_SESSION_ID = '946f0f67-88f4-40c8-bf6b-5e23ac1169f7';
const LONG_DEMO_RESOURCE_BASE = `/demo-resources/${LONG_DEMO_SESSION_ID}`;
const LONG_DEMO_CHILD_IDS = [
  '90f6d699-9ad9-4108-b83d-958311a373cb',
  '3d19b1f1-959e-4950-8766-014e0964e4c7',
  'eb5cb880-c855-4869-90de-685344781e54',
  '09dab304-cb05-4458-aec1-b35aa2c60639',
  '63972504-c9c5-4dda-9688-047eec287518',
];

/** Recordings at or above this length use bell/hybrid auto-split in the pipeline and show the Auto-split UI tab. */
const LONG_VIDEO_THRESHOLD_SECONDS = 300;

function downloadBlob(fileName, blob) {
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement('a');

  anchor.href = objectUrl;
  anchor.download = fileName;
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();

  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

function toSafeDownloadName(value, fallback) {
  return String(value || fallback)
    .replace(/[^a-zA-Z0-9-_]/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 64) || fallback;
}

function csvCell(value) {
  if (value === null || value === undefined) {
    return '';
  }

  let text = String(value).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  if (typeof value === 'string' && /^[=+\-@\t]/.test(text)) {
    text = `'${text}`;
  }

  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function rowsToCsv(rows) {
  return rows.map((row) => row.map(csvCell).join(',')).join('\r\n');
}

function debugPipeline(message) {
  if (import.meta.env.DEV) {
    console.debug(message);
  }
}

async function readBundledDemoJson(fileName, label) {
  const response = await fetch(`${DEMO_RESOURCE_BASE}/${fileName}`);
  if (!response.ok) {
    throw new Error(`Failed to load ${label}. HTTP ${response.status}`);
  }

  return response.json();
}

function buildBundledDemoSession(rawSession, scoresPayload, communicationScoresPayload, audioProfPayload) {
  const safeSession = rawSession && typeof rawSession === 'object' ? rawSession : {};
  const safeFiles = safeSession.files && typeof safeSession.files === 'object' ? safeSession.files : {};
  const safeOutputs = safeSession.outputs && typeof safeSession.outputs === 'object' ? safeSession.outputs : {};
  const nowIso = new Date().toISOString();

  return {
    ...safeSession,
    id: DEMO_SESSION_ID,
    status: 'completed',
    pipeline: {
      ...(safeSession.pipeline || {}),
      startedAt: safeSession.pipeline?.startedAt || nowIso,
      endedAt: safeSession.pipeline?.endedAt || nowIso,
      runtimeSeconds: Number(safeSession.pipeline?.runtimeSeconds || 0),
      mode: 'demo',
    },
    files: {
      ...safeFiles,
      video: {
        ...(safeFiles.video || {}),
        originalName: safeFiles.video?.originalName || 'tinea-cropped.mp4',
        fileName: safeFiles.video?.fileName || DEMO_RESOURCE_FILES.video,
        url: `${DEMO_RESOURCE_BASE}/${DEMO_RESOURCE_FILES.video}`,
      },
      caseStudy: {
        ...(safeFiles.caseStudy || {}),
        originalName: safeFiles.caseStudy?.originalName || 'case-study.pdf',
        fileName: safeFiles.caseStudy?.fileName || DEMO_RESOURCE_FILES.caseStudy,
        sizeBytes: Number(safeFiles.caseStudy?.sizeBytes || 0),
        url: `${DEMO_RESOURCE_BASE}/${DEMO_RESOURCE_FILES.caseStudy}`,
      },
    },
    outputs: {
      ...safeOutputs,
      audio: safeOutputs.audio
        ? {
            ...safeOutputs.audio,
            fileName: DEMO_RESOURCE_FILES.audio,
            url: `${DEMO_RESOURCE_BASE}/${DEMO_RESOURCE_FILES.audio}`,
          }
        : null,
      whisperxJson: safeOutputs.whisperxJson
        ? {
            ...safeOutputs.whisperxJson,
            fileName: DEMO_RESOURCE_FILES.whisperx,
            url: `${DEMO_RESOURCE_BASE}/${DEMO_RESOURCE_FILES.whisperx}`,
          }
        : null,
      transcript: {
        ...(safeOutputs.transcript || {}),
        fileName: DEMO_RESOURCE_FILES.transcript,
        url: `${DEMO_RESOURCE_BASE}/${DEMO_RESOURCE_FILES.transcript}`,
      },
      subtitle: {
        ...(safeOutputs.subtitle || {}),
        fileName: DEMO_RESOURCE_FILES.subtitle,
        url: `${DEMO_RESOURCE_BASE}/${DEMO_RESOURCE_FILES.subtitle}`,
      },
      subtitleTrack: {
        ...(safeOutputs.subtitleTrack || {}),
        fileName: DEMO_RESOURCE_FILES.subtitleTrack,
        url: `${DEMO_RESOURCE_BASE}/${DEMO_RESOURCE_FILES.subtitleTrack}`,
      },
      scores: {
        ...(safeOutputs.scores || {}),
        fileName: DEMO_RESOURCE_FILES.scores,
        url: `${DEMO_RESOURCE_BASE}/${DEMO_RESOURCE_FILES.scores}`,
        payload: scoresPayload || safeOutputs.scores?.payload || null,
      },
      communicationScores: {
        ...(safeOutputs.communicationScores || {}),
        fileName: DEMO_RESOURCE_FILES.communicationScores,
        url: `${DEMO_RESOURCE_BASE}/${DEMO_RESOURCE_FILES.communicationScores}`,
        payload: communicationScoresPayload || safeOutputs.communicationScores?.payload || null,
      },
      audioProfessionalism: {
        ...(safeOutputs.audioProfessionalism || {}),
        fileName: DEMO_RESOURCE_FILES.audioProfessionalism,
        url: `${DEMO_RESOURCE_BASE}/${DEMO_RESOURCE_FILES.audioProfessionalism}`,
        payload: audioProfPayload || safeOutputs.audioProfessionalism?.payload || null,
      },
    },
    error: null,
  };
}

// Re-shape a child (clip) session.json the same way buildBundledDemoSession
// does for the standard demo, but pointing every URL at the long-demo bundle's
// children/<id>/ folder. The result is a fully synthetic "completed" session
// that openClipAssessmentView can hand to loadSessionWorkspace in demo mode.
function buildLongDemoChildSession(rawSession, scoresPayload, communicationScoresPayload, audioProfPayload, parentClip) {
  const safeSession = rawSession && typeof rawSession === 'object' ? rawSession : {};
  const childId = String(safeSession.id || '').trim();
  const base = `${LONG_DEMO_RESOURCE_BASE}/children/${childId}`;
  const parentClipFile = parentClip?.fileName || `${LONG_DEMO_SESSION_ID}-clip.mp4`;

  return {
    ...safeSession,
    id: childId || LONG_DEMO_SESSION_ID,
    parentSessionId: LONG_DEMO_SESSION_ID,
    status: 'completed',
    pipeline: {
      ...(safeSession.pipeline || {}),
      mode: 'demo',
    },
    files: {
      ...(safeSession.files || {}),
      video: {
        ...(safeSession.files?.video || {}),
        originalName: safeSession.files?.video?.originalName || parentClipFile,
        fileName: parentClipFile,
        // Clip videos live at the parent bundle root.
        url: `${LONG_DEMO_RESOURCE_BASE}/${parentClip?.url?.split('/').pop() || parentClipFile}`,
      },
    },
    outputs: {
      ...(safeSession.outputs || {}),
      transcript: {
        ...(safeSession.outputs?.transcript || {}),
        url: `${base}/transcript.json`,
      },
      subtitle: safeSession.outputs?.subtitle
        ? {
            ...safeSession.outputs.subtitle,
            url: `${base}/subtitles.srt`,
          }
        : null,
      subtitleTrack: safeSession.outputs?.subtitleTrack
        ? {
            ...safeSession.outputs.subtitleTrack,
            url: `${base}/subtitles.vtt`,
          }
        : null,
      scores: {
        ...(safeSession.outputs?.scores || {}),
        url: `${base}/scores.json`,
        payload: scoresPayload || safeSession.outputs?.scores?.payload || null,
      },
      communicationScores: {
        ...(safeSession.outputs?.communicationScores || {}),
        url: `${base}/communication-scores.json`,
        payload: communicationScoresPayload || safeSession.outputs?.communicationScores?.payload || null,
      },
      audioProfessionalism: {
        ...(safeSession.outputs?.audioProfessionalism || {}),
        url: `${base}/audio-professionalism.json`,
        payload: audioProfPayload || safeSession.outputs?.audioProfessionalism?.payload || null,
      },
    },
    error: null,
  };
}

export default function OSCEAiMarkerMockup({
  authUsername = '',
  routeSessionId = null,
  onNavigateSession = null,
  onOpenRubric = null,
  onOpenAnalytics = null,
  onOpenSettings = null,
  onLogout = null,
  notifications = null,
} = {}) {
  const [videoFile, setVideoFile] = useState(null);
  const [communicationScores, setCommunicationScores] = useState(null);
  const [caseStudyFile, setCaseStudyFile] = useState(null);
  const [showWorkspace, setShowWorkspace] = useState(false);
  const [uploadFlow, setUploadFlow] = useState('standard');
  // Long-workflow auto-crop method: 'bells' (audio bell detection) or
  // 'person' (RT-DETR human detection). Sent with the upload; the backend
  // worker reads it from the session when the auto_crop job runs.
  const [segmentationMethod, setSegmentationMethod] = useState('bells');
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
  const [sessionIndexError, setSessionIndexError] = useState('');
  const [sessionNameDrafts, setSessionNameDrafts] = useState({});
  const [renamingSessionId, setRenamingSessionId] = useState(null);
  const [deletingSessionId, setDeletingSessionId] = useState(null);

  const [session, setSession] = useState(null);
  const [transcript, setTranscript] = useState({ segments: [] });
  const [scoreReport, setScoreReport] = useState(null);
  const [audioProfessionalism, setAudioProfessionalism] = useState(null);
  const [audioProfLoadError, setAudioProfLoadError] = useState('');

  const [isUploading, setIsUploading] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [processingStage, setProcessingStage] = useState('pipeline');
  const [isDemoFallback, setIsDemoFallback] = useState(false);
  const [runtimeSeconds, setRuntimeSeconds] = useState(0);
  const [processingMessage, setProcessingMessage] = useState('Preparing local pipeline...');
  const [liveLogLine, setLiveLogLine] = useState('Waiting to start...');
  const [pipelineMilestones, setPipelineMilestones] = useState({
    started: false,
    convertedToMp3: false,
    transcriptionComplete: false,
    scored: false,
  });
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

  const videoInputRef = useRef(null);
  const caseStudyInputRef = useRef(null);
  const videoPlayerRef = useRef(null);
  const videoPlayerSectionRef = useRef(null);
  const timelineContainerRef = useRef(null);
  const manualTimelineRef = useRef(null);
  const timelineMenuRef = useRef(null);
  const timelineSegmentRefs = useRef(new Map());

  const transcriptSegments = useMemo(() => {
    const segmentList = Array.isArray(transcript?.segments) ? transcript.segments : [];
    return segmentList
      .map((segment, index) => ({
        id: segment.id || index + 1,
        speaker: segment.speaker || 'SPEAKER_UNKNOWN',
        start: Number(segment.start ?? 0),
        end: Number(segment.end ?? segment.start ?? 0),
        text: String(segment.text || '').trim(),
        startLabel: segment.startLabel || formatRuntime(segment.start ?? 0),
        endLabel: segment.endLabel || formatRuntime(segment.end ?? 0),
      }))
      .filter((segment) => Boolean(segment.text));
  }, [transcript]);

  const audioProfPayload = useMemo(() => {
    if (audioProfessionalism && typeof audioProfessionalism === 'object') {
      return audioProfessionalism;
    }

    const payload = session?.outputs?.audioProfessionalism?.payload;
    if (payload && typeof payload === 'object') {
      return payload;
    }

    return null;
  }, [audioProfessionalism, session]);

  const audioProfMetrics = audioProfPayload?.metrics || null;
  const audioProfFeatures = audioProfPayload?.audio_features || null;
  const audioProfWarnings = Array.isArray(audioProfPayload?.warnings) ? audioProfPayload.warnings : [];

  const videoClips = useMemo(() => {
    return Array.isArray(session?.outputs?.videoClips) ? session.outputs.videoClips : [];
  }, [session]);

  const visibleSessions = useMemo(
    () => sessionIndex.filter((entry) => !entry.parentSessionId),
    [sessionIndex]
  );

  const clipAssessmentIndex = useMemo(() => {
    if (!session?.id) {
      return {};
    }

    const indexMap = {};
    sessionIndex.forEach((entry) => {
      if (!entry.parentSessionId) {
        return;
      }
      if (String(entry.parentSessionId) !== String(session.id)) {
        return;
      }

      const clipId = entry.clipSource?.clipId;
      if (!clipId) {
        return;
      }

      let status = 'idle';
      if (entry.status === 'completed') {
        status = 'completed';
      } else if (entry.status === 'failed') {
        status = 'failed';
      } else if (['queued', 'assembling', 'processing'].includes(entry.status)) {
        // A child clip session that is queued/assembling/processing is already
        // in flight — surface it as 'running' so the UI blocks re-queueing the
        // same clip (the button becomes "View progress", not "Run assessment").
        status = 'running';
      }

      indexMap[clipId] = { status, sessionId: entry.id };
    });

    return indexMap;
  }, [sessionIndex, session?.id]);

  const hasClipFiles = useMemo(() => videoClips.some((clip) => Boolean(clip?.url)), [videoClips]);
  const hasDraftClips = videoClips.length > 0 && !hasClipFiles;
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
        session?.segmentation === 'person' ||
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
  const sessionIsLong = Boolean(session?.workflow === 'long' || videoClips.length > 0);
  const isLongWorkflow = showWorkspace ? sessionIsLong : uploadFlow === 'long';
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
        if (existing?.status === 'running' && mapped.status !== 'completed' && mapped.status !== 'failed') {
          return;
        }

        if (existing?.status === 'completed' && existing.sessionId) {
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
      .filter((entry) => entry?.status === 'completed' && entry?.sessionId)
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
    fetch(`/api/sessions/${session.id}/clip-summaries`)
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`Failed to load clip summaries (${response.status}).`);
        }
        return response.json();
      })
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

  const aiCriteria = useMemo(() => {
    const criteriaList = Array.isArray(scoreReport?.criteria) ? scoreReport.criteria : [];

    return criteriaList.map((item, index) => {
      const normalizedValue = String(item?.value || '').trim().toLowerCase() === 'yes' ? 'Yes' : 'No';
      const normalizedCritical =
        item?.is_critical === true || String(item?.is_critical || '').trim().toLowerCase() === 'true';
      const rawTimestamp = String(item?.timestamp || item?.evidence_timestamp || '').trim();
      const timestampSeconds = parseEvidenceTimestamp(rawTimestamp);
      const timestampLabel = rawTimestamp || (Number.isFinite(timestampSeconds) ? formatRuntime(timestampSeconds) : '');

      return {
        key: String(item?.label || `Criterion ${index + 1}`).trim(),
        value: normalizedValue,
        isCritical: normalizedCritical,
        reason: String(item?.reason || '').trim(),
        timestamp: timestampLabel,
        timestampSeconds: Number.isFinite(timestampSeconds) ? timestampSeconds : null,
      };
    });
  }, [scoreReport]);

  const communicationPayload = useMemo(() => {
    if (communicationScores && typeof communicationScores === 'object') {
      return communicationScores;
    }
    const sessionPayload = session?.outputs?.communicationScores?.payload;
    if (sessionPayload && typeof sessionPayload === 'object') {
      return sessionPayload;
    }
    return null;
  }, [communicationScores, session]);

  const communicationCriteria = useMemo(() => {
    const list = Array.isArray(communicationPayload?.criteria) ? communicationPayload.criteria : [];
    return list.map((item, index) => {
      const rawTimestamp = String(item?.timestamp || '').trim();
      const timestampSeconds = parseEvidenceTimestamp(rawTimestamp);
      const scoreLabel = String(item?.score_label || '').trim();
      const points = Number.isFinite(Number(item?.points))
        ? Number(item.points)
        : { All: 3, Most: 2, Some: 1, None: 0 }[scoreLabel] ?? 0;
      return {
        id: item?.id || index + 1,
        label: String(item?.label || `Criterion ${index + 1}`).trim(),
        section: item?.section || null,
        scoreLabel: ['None', 'Some', 'Most', 'All'].includes(scoreLabel) ? scoreLabel : 'None',
        points,
        evidence: String(item?.evidence || '').trim(),
        indicatorsObserved: Array.isArray(item?.indicators_observed) ? item.indicators_observed : [],
        indicatorsMissing: Array.isArray(item?.indicators_missing) ? item.indicators_missing : [],
        indicatorsNotObservable: Array.isArray(item?.indicators_not_observable)
          ? item.indicators_not_observable
          : [],
        indicatorsTotal: Array.isArray(item?.indicators_total) ? item.indicators_total : [],
        timestamp: rawTimestamp || (Number.isFinite(timestampSeconds) ? formatRuntime(timestampSeconds) : ''),
        timestampSeconds: Number.isFinite(timestampSeconds) ? timestampSeconds : null,
      };
    });
  }, [communicationPayload]);

  const communicationSummary = useMemo(() => {
    const summary = communicationPayload?.scoring_summary;
    if (summary && typeof summary === 'object') {
      const maxScore = Number(summary.max_score || 0);
      const totalScore = Number(summary.total_score || 0);
      const passThreshold = Number(summary.pass_threshold || Math.ceil(maxScore / 2));
      const passFail = String(summary.pass_fail || (totalScore >= passThreshold ? 'Pass' : 'Fail'));
      return {
        totalScore,
        maxScore,
        passThreshold,
        passFail,
        decisionReason: String(summary.decision_reason || '').trim(),
        labelCounts: summary.label_counts || {},
        totalCriteria: Number(summary.total_criteria || communicationCriteria.length),
        averageScore: Number(summary.average_score || 0),
      };
    }

    if (!communicationCriteria.length) {
      return null;
    }

    const totalScore = communicationCriteria.reduce((sum, item) => sum + (Number(item.points) || 0), 0);
    const maxScore = communicationCriteria.length * 3;
    const passThreshold = communicationCriteria.length === 7 ? 11 : Math.ceil(maxScore / 2);

    return {
      totalScore,
      maxScore,
      passThreshold,
      passFail: totalScore >= passThreshold ? 'Pass' : 'Fail',
      decisionReason: '',
      labelCounts: {},
      totalCriteria: communicationCriteria.length,
      averageScore: communicationCriteria.length ? totalScore / communicationCriteria.length : 0,
    };
  }, [communicationPayload, communicationCriteria]);

  const keepStartStop = useMemo(() => {
    if (!scoreReport?.keep_start_stop || typeof scoreReport.keep_start_stop !== 'object') {
      return null;
    }

    return {
      keep: String(scoreReport.keep_start_stop.keep || '').trim(),
      start: String(scoreReport.keep_start_stop.start || '').trim(),
      stop: String(scoreReport.keep_start_stop.stop || '').trim(),
    };
  }, [scoreReport]);

  const fallbackScores = useMemo(() => {
    const adjustment = Math.min(8, Math.floor(transcriptSegments.length / 20));
    return SCORE_TEMPLATE.map((score) => ({
      ...score,
      score: Math.min(98, score.score + adjustment),
    }));
  }, [transcriptSegments.length]);

  const fallbackOverallScore = useMemo(() => {
    const weightedTotal = fallbackScores.reduce((sum, item) => sum + item.score * item.weight, 0);
    return Math.round(weightedTotal);
  }, [fallbackScores]);

  const scoringSummary = useMemo(() => {
    const summary = scoreReport?.scoring_summary;
    if (summary && typeof summary === 'object') {
      return {
        passFail: String(summary.pass_fail || '').trim() || 'Fail',
        totalCriteria: Number(summary.total_criteria || 0),
        yesCount: Number(summary.yes_count || 0),
        noCount: Number(summary.no_count || 0),
        criticalTotal: Number(summary.critical_total || 0),
        criticalYes: Number(summary.critical_yes || 0),
        criticalNo: Number(summary.critical_no || 0),
        decisionReason: String(summary.decision_reason || '').trim(),
      };
    }

    if (!aiCriteria.length) {
      return null;
    }

    const totalCriteria = aiCriteria.length;
    const yesCount = aiCriteria.filter((criterion) => criterion.value === 'Yes').length;
    const noCount = totalCriteria - yesCount;
    const criticalCriteria = aiCriteria.filter((criterion) => criterion.isCritical);
    const criticalTotal = criticalCriteria.length;
    const criticalYes = criticalCriteria.filter((criterion) => criterion.value === 'Yes').length;
    const criticalNo = criticalTotal - criticalYes;

    let passFail = 'Pass';
    let decisionReason = 'Pass: all critical criteria are Yes and at least half of all criteria are Yes.';

    if (criticalNo > 0) {
      passFail = 'Fail';
      decisionReason = 'Fail: at least one critical criterion is marked No.';
    } else if (yesCount * 2 < totalCriteria) {
      passFail = 'Fail';
      decisionReason = 'Fail: fewer than half of all criteria are marked Yes.';
    }

    return {
      passFail,
      totalCriteria,
      yesCount,
      noCount,
      criticalTotal,
      criticalYes,
      criticalNo,
      decisionReason,
    };
  }, [scoreReport, aiCriteria]);

  const canDownloadScoreSheet =
    aiCriteria.length > 0 || Boolean(keepStartStop) || communicationCriteria.length > 0;

  const currentVideoUrl = resolveMediaUrl(session?.files?.video?.url) || localVideoUrl;
  const currentSubtitleUrl = resolveMediaUrl(session?.outputs?.subtitleTrack?.url) || null;
  const isPipelineActive = isUploading || isProcessing;
  const currentModeLabel = String(session?.pipeline?.mode || 'gpu').toUpperCase();

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
    if (!activeSegmentId) {
      return;
    }

    const timelineContainer = timelineContainerRef.current;
    const activeTimelineItem = timelineSegmentRefs.current.get(activeSegmentId);
    if (!activeTimelineItem || !timelineContainer) {
      return;
    }

    const activeTop = activeTimelineItem.offsetTop;
    const activeBottom = activeTop + activeTimelineItem.offsetHeight;
    const currentViewTop = timelineContainer.scrollTop;
    const currentViewBottom = currentViewTop + timelineContainer.clientHeight;
    const inset = 24;

    const alreadyVisible = activeTop >= currentViewTop + inset && activeBottom <= currentViewBottom - inset;

    if (alreadyVisible) {
      return;
    }

    const targetTop = Math.max(0, activeTop - timelineContainer.clientHeight / 2 + activeTimelineItem.offsetHeight / 2);
    timelineContainer.scrollTo({
      top: targetTop,
      behavior: 'smooth',
    });
  }, [activeSegmentId]);

  useEffect(() => {
    if (!videoPlayerRef.current || !currentSubtitleUrl) {
      return;
    }

    // Force the first subtitle/caption track to showing mode after source updates.
    const player = videoPlayerRef.current;
    for (let index = 0; index < player.textTracks.length; index += 1) {
      player.textTracks[index].mode = index === 0 ? 'showing' : 'disabled';
    }
  }, [currentSubtitleUrl, session?.id]);

  useEffect(() => {
    if (!session?.id) {
      setAudioProfLoadError('');
      return;
    }

    if (audioProfPayload) {
      if (audioProfLoadError) {
        setAudioProfLoadError('');
      }
      return;
    }

    const audioProfMeta = session?.outputs?.audioProfessionalism;
    if (!audioProfMeta?.url && !audioProfMeta?.fileName) {
      return;
    }

    let cancelled = false;

    const loadAudioProfessionalism = async () => {
      try {
        const response = await fetch(`/api/sessions/${session.id}/audio-professionalism`);
        const body = await response.json().catch(() => ({}));

        if (!response.ok) {
          throw new Error(body.error || 'Audio professionalism fetch failed.');
        }

        if (!cancelled) {
          setAudioProfessionalism(
            body.audioProfessionalism || body?.session?.outputs?.audioProfessionalism?.payload || null
          );
          setAudioProfLoadError('');
        }
      } catch (error) {
        if (!cancelled) {
          setAudioProfLoadError(error.message || 'Audio professionalism unavailable.');
        }
      }
    };

    loadAudioProfessionalism();

    return () => {
      cancelled = true;
    };
  }, [audioProfPayload, session?.id, session?.outputs?.audioProfessionalism?.fileName, session?.outputs?.audioProfessionalism?.url]);

  useEffect(() => {
    refreshSessionIndex();
    refreshCorpora();
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
  useChangeStream(() => {
    refreshSessionIndex({ silent: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, ['sessions', 'jobs']);

  async function refreshCorpora() {
    try {
      const response = await fetch('/api/corpora');
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.error || 'Failed to load corpora.');
      }
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

  // `silent` refreshes are driven by the change stream rather than by the user,
  // so they must not flash the list's loading state on every backend write.
  async function refreshSessionIndex({ silent = false } = {}) {
    if (!silent) {
      setSessionIndexLoading(true);
    }
    setSessionIndexError('');
    try {
      const response = await fetch('/api/sessions');
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.error || 'Failed to load sessions.');
      }

      const sessions = Array.isArray(body.sessions) ? body.sessions : [];
      setSessionIndex(sessions);
      setSessionNameDrafts((previous) => {
        const next = { ...previous };
        sessions.forEach((sessionEntry) => {
          if (!next[sessionEntry.id]) {
            next[sessionEntry.id] = sessionEntry.name || '';
          }
        });
        return next;
      });
    } catch (error) {
      setSessionIndexError(error.message || 'Failed to load sessions.');
    } finally {
      // Only the refresh that raised the flag may clear it, so a stream-driven
      // refresh landing mid-flight cannot cancel a user-initiated spinner.
      if (!silent) {
        setSessionIndexLoading(false);
      }
    }
  }

  async function loadSessionWorkspace(sessionId) {
    const sessionResponse = await fetch(`/api/sessions/${sessionId}`);
    const sessionBody = await sessionResponse.json().catch(() => ({}));
    if (!sessionResponse.ok) {
      throw new Error(sessionBody.error || 'Session not found.');
    }

    const sessionPayload = sessionBody.session;
    let transcriptPayload = { segments: [] };

    if (sessionPayload?.outputs?.transcript?.fileName) {
      const transcriptResponse = await fetch(`/api/sessions/${sessionId}/transcript`);
      const transcriptBody = await transcriptResponse.json().catch(() => ({}));
      if (transcriptResponse.ok && transcriptBody?.transcript) {
        transcriptPayload = transcriptBody.transcript;
      }
    }

    let scoresPayload = sessionPayload?.outputs?.scores?.payload || null;
    if (!scoresPayload && sessionPayload?.outputs?.scores?.fileName) {
      const scoresResponse = await fetch(`/api/sessions/${sessionId}/scores`);
      const scoresBody = await scoresResponse.json().catch(() => ({}));
      if (scoresResponse.ok && scoresBody?.scores) {
        scoresPayload = scoresBody.scores;
      }
    }

    let audioProfPayload = sessionPayload?.outputs?.audioProfessionalism?.payload || null;
    if (!audioProfPayload && sessionPayload?.outputs?.audioProfessionalism?.fileName) {
      const audioProfResponse = await fetch(`/api/sessions/${sessionId}/audio-professionalism`);
      const audioProfBody = await audioProfResponse.json().catch(() => ({}));
      if (audioProfResponse.ok && audioProfBody?.audioProfessionalism) {
        audioProfPayload = audioProfBody.audioProfessionalism;
      }
    }

    let commScoresPayload = sessionPayload?.outputs?.communicationScores?.payload || null;
    if (!commScoresPayload && sessionPayload?.outputs?.communicationScores?.fileName) {
      const commResponse = await fetch(`/api/sessions/${sessionId}/communication-scores`);
      const commBody = await commResponse.json().catch(() => ({}));
      if (commResponse.ok && commBody?.communicationScores) {
        commScoresPayload = commBody.communicationScores;
      }
    }

    return {
      session: sessionPayload,
      transcript: transcriptPayload,
      scores: scoresPayload,
      audioProfessionalism: audioProfPayload,
      communicationScores: commScoresPayload,
    };
  }

  async function openExistingSession(sessionId) {
    setError('');
    setNotice('');
    setIsDemoFallback(false);
    setIsProcessing(true);
    setProcessingStage('pipeline');
    setProcessingMessage('Loading saved session...');
    setLiveLogLine('Loading session metadata...');
    setParentSessionSnapshot(null);
    setClipAssessmentRuns({});
    setSelectedClipAssessmentIds(new Set());

    try {
      const payload = await loadSessionWorkspace(sessionId);

      // Gate: an in-flight session (assembling/queued/processing) cannot be
      // entered — its results are empty/partial until processing finishes.
      // Bounce back to the list, where the session card shows the live stage.
      // This also covers deep links / reloads that hit the URL→state restore
      // effect, so blocking the list button alone is not enough.
      const loaded = payload.session;
      if (IN_FLIGHT_STATUSES.has(String(loaded?.status))) {
        setShowWorkspace(false);
        setNotice(
          'This session is still processing. Track its stage on the session card — it unlocks when finished.',
        );
        if (typeof onNavigateSession === 'function') {
          onNavigateSession(null);
        }
        await refreshSessionIndex();
        return;
      }

      setSession(payload.session);
      setTranscript(payload.transcript || { segments: [] });
      setScoreReport(payload.scores || payload?.session?.outputs?.scores?.payload || null);
      setAudioProfessionalism(
        payload.audioProfessionalism || payload?.session?.outputs?.audioProfessionalism?.payload || null
      );
      setCommunicationScores(
        payload.communicationScores || payload?.session?.outputs?.communicationScores?.payload || null
      );
      setRuntimeSeconds(Math.round(payload.session?.pipeline?.runtimeSeconds || 0));
      setVideoFile(null);
      setCaseStudyFile(null);

      const nextClips = Array.isArray(payload.session?.outputs?.videoClips)
        ? payload.session.outputs.videoClips
        : [];
      // Select the first SESSION clip — intermissions are greyed markers.
      setSelectedClipId(nextClips.find((clip) => clip.kind !== INTERMISSION_KIND)?.id || null);
      setUploadFlow(nextClips.length > 0 ? 'long' : 'standard');
      setShowWorkspace(true);
    } catch (error) {
      setSessionIndexError(error.message || 'Failed to open session.');
    } finally {
      setIsProcessing(false);
    }
  }

  // Renders the action control for a saved-session row. In-flight sessions
  // (assembling/queued/processing) are deliberately NOT enterable — opening a
  // half-processed session would show empty/partial results. The card itself
  // gauges the current stage (see describeProcessingStage); this button
  // unlocks once processing reaches a terminal state.
  function renderSessionAction(sessionEntry) {
    const inFlight = IN_FLIGHT_STATUSES.has(sessionEntry.status);

    if (inFlight) {
      return (
        <Button
          size="sm"
          variant="outline"
          disabled
          className="gap-1"
          title="Available when processing completes. Progress is shown on this card."
        >
          <Loader2 className="h-3 w-3 animate-spin" />
          Processing…
        </Button>
      );
    }

    return (
      <Button size="sm" variant="outline" onClick={() => openExistingSession(sessionEntry.id)}>
        Open
      </Button>
    );
  }

  function goHome() {
    setShowWorkspace(false);
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
    setUploadFlow('standard');
    setError('');
    setNotice('');
    setIsDemoFallback(false);
    setIsUploading(false);
    setIsProcessing(false);
    setRuntimeSeconds(0);
    setLiveLogLine('Waiting to start...');
    setProcessingMessage('Preparing local pipeline...');
    setPipelineMilestones({
      started: false,
      convertedToMp3: false,
      transcriptionComplete: false,
      scored: false,
    });
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
      const response = await fetch(`/api/sessions/${sessionId}/name`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: trimmed }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.error || 'Failed to rename session.');
      }

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
      const response = await fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.error || 'Failed to delete session.');
      }
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

  async function loadBundledDemoResources() {
    const [
      demoSessionRaw,
      demoTranscriptRaw,
      demoScoresRaw,
      demoCommunicationRaw,
      demoAudioProfRaw,
    ] = await Promise.all([
      readBundledDemoJson(DEMO_RESOURCE_FILES.session, 'demo session metadata'),
      readBundledDemoJson(DEMO_RESOURCE_FILES.transcript, 'demo transcript'),
      readBundledDemoJson(DEMO_RESOURCE_FILES.scores, 'demo content scores'),
      readBundledDemoJson(DEMO_RESOURCE_FILES.communicationScores, 'demo communication scores'),
      readBundledDemoJson(DEMO_RESOURCE_FILES.audioProfessionalism, 'demo audio professionalism'),
    ]);

    const normalizedTranscript =
      demoTranscriptRaw && Array.isArray(demoTranscriptRaw.segments) ? demoTranscriptRaw : { segments: [] };

    return {
      session: buildBundledDemoSession(
        demoSessionRaw,
        demoScoresRaw,
        demoCommunicationRaw,
        demoAudioProfRaw,
      ),
      transcript: normalizedTranscript,
      scores: demoScoresRaw,
      communicationScores: demoCommunicationRaw,
      audioProfessionalism: demoAudioProfRaw,
    };
  }

  async function openDemoWorkspace(reason) {
    setError('');
    setIsUploading(false);
    setIsProcessing(true);
    setProcessingMessage('Loading bundled demo workspace resources...');
    setLiveLogLine('Loading demo resources...');
    setActiveSegmentId(null);

    try {
      const demoBundle = await loadBundledDemoResources();
      setIsDemoFallback(true);
      setShowWorkspace(true);
      setUploadFlow('standard');
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
      setPipelineMilestones({
        started: true,
        convertedToMp3: true,
        transcriptionComplete: true,
        scored: true,
      });
      setNotice(reason);
      setLiveLogLine('Bundled demo session loaded successfully.');
    } catch (loadError) {
      setError(`Demo workspace failed to load: ${loadError.message || 'unknown error'}`);
      setNotice('');
    } finally {
      setIsUploading(false);
      setIsProcessing(false);
    }
  }

  async function openManualDemoMode() {
    setError('');
    setRuntimeSeconds(0);
    setLiveLogLine('Manual demo mode enabled. Model execution skipped.');
    setPipelineMilestones({
      started: true,
      convertedToMp3: true,
      transcriptionComplete: true,
      scored: true,
    });
    await openDemoWorkspace('Manual demo mode enabled. Model execution skipped.');
  }

  // ---------------------------------------------------------------------------
  // Long-video demo
  // ---------------------------------------------------------------------------
  // The long demo replicates session 946f0f67... in a 'cropped + all clips
  // already assessed' state. Clip MP4s ship with the bundle and each child
  // session ships its own scores/communication-scores/audio-professionalism/
  // transcript so the user can drill into any student without a backend.
  async function loadBundledLongDemoResources() {
    const parentResponse = await fetch(`${LONG_DEMO_RESOURCE_BASE}/parent-session.json`);
    if (!parentResponse.ok) {
      throw new Error(`Failed to load long demo parent session. HTTP ${parentResponse.status}`);
    }
    const parentSessionRaw = await parentResponse.json();

    const clips = Array.isArray(parentSessionRaw?.outputs?.videoClips)
      ? parentSessionRaw.outputs.videoClips
      : [];

    const remappedClips = clips.map((clip, index) => {
      const fileNameOnly = `clip-${index + 1}.mp4`;
      return {
        ...clip,
        fileName: fileNameOnly,
        url: `${LONG_DEMO_RESOURCE_BASE}/${fileNameOnly}`,
      };
    });

    const parentSession = {
      ...parentSessionRaw,
      id: LONG_DEMO_SESSION_ID,
      status: 'cropped',
      pipeline: {
        ...(parentSessionRaw.pipeline || {}),
        mode: 'demo',
      },
      files: {
        ...(parentSessionRaw.files || {}),
        video: {
          ...(parentSessionRaw.files?.video || {}),
          fileName: 'parent-clip.mp4',
          originalName: parentSessionRaw.files?.video?.originalName || 'parent-clip.mp4',
          url: `${LONG_DEMO_RESOURCE_BASE}/parent-clip.mp4`,
        },
        caseStudy: {
          ...(parentSessionRaw.files?.caseStudy || {}),
          url: `${LONG_DEMO_RESOURCE_BASE}/case-study.pdf`,
        },
      },
      outputs: {
        ...(parentSessionRaw.outputs || {}),
        videoClips: remappedClips,
      },
    };

    const childResults = await Promise.all(
      LONG_DEMO_CHILD_IDS.map(async (childId) => {
        const base = `${LONG_DEMO_RESOURCE_BASE}/children/${childId}`;
        const [sessionRes, transcriptRes, scoresRes, commRes, audioRes] = await Promise.all([
          fetch(`${base}/session.json`),
          fetch(`${base}/transcript.json`),
          fetch(`${base}/scores.json`),
          fetch(`${base}/communication-scores.json`),
          fetch(`${base}/audio-professionalism.json`),
        ]);
        if (!sessionRes.ok || !transcriptRes.ok || !scoresRes.ok || !commRes.ok) {
          throw new Error(`Failed to load long demo child ${childId}.`);
        }
        const [sessionRaw, transcript, scores, communicationScores, audioProfessionalism] = await Promise.all([
          sessionRes.json(),
          transcriptRes.json(),
          scoresRes.json(),
          commRes.json(),
          audioRes.ok ? audioRes.json() : null,
        ]);

        const parentClip = remappedClips.find(
          (clip) => String(clip?.id || '') === String(sessionRaw?.clipSource?.clipId || ''),
        );

        const enrichedSession = buildLongDemoChildSession(
          sessionRaw,
          scores,
          communicationScores,
          audioProfessionalism,
          parentClip,
        );

        return {
          id: childId,
          session: enrichedSession,
          transcript: Array.isArray(transcript?.segments) ? transcript : { segments: [] },
          scores,
          communicationScores,
          audioProfessionalism,
        };
      }),
    );

    const childById = Object.create(null);
    childResults.forEach((entry) => {
      childById[entry.id] = entry;
    });

    return {
      parentSession,
      remappedClips,
      childById,
    };
  }

  function buildLongDemoSummaries(childById) {
    const summaries = LONG_DEMO_CHILD_IDS.map((childId) => {
      const child = childById?.[childId];
      if (!child) return null;
      const contentSummary = child.scores?.scoring_summary || null;
      const contentTotalCriteria = Array.isArray(child.scores?.criteria)
        ? child.scores.criteria.length
        : Number(contentSummary?.total_criteria || 0);
      const contentYes = Number(contentSummary?.yes_count || 0);
      const percentYes = contentTotalCriteria > 0
        ? Math.round((contentYes / contentTotalCriteria) * 1000) / 10
        : 0;
      const communicationCriteria = Array.isArray(child.communicationScores?.criteria)
        ? child.communicationScores.criteria
        : [];
      const communicationSummary = child.communicationScores?.scoring_summary || null;
      return {
        sessionId: childId,
        sessionName: child.session?.name || null,
        clipLabel: child.session?.clipSource?.label || child.session?.name || `Clip ${childId.slice(0, 8)}`,
        clipId: child.session?.clipSource?.clipId || null,
        clipOrder: -1,
        status: 'completed',
        content: {
          totalCriteria: contentTotalCriteria,
          yesCount: contentYes,
          noCount: Number(contentSummary?.no_count || 0),
          criticalYes: Number(contentSummary?.critical_yes || 0),
          criticalNo: Number(contentSummary?.critical_no || 0),
          passFail: String(contentSummary?.pass_fail || ''),
          percentYes,
        },
        communication: {
          totalCriteria: Number(communicationSummary?.total_criteria || communicationCriteria.length),
          totalScore: Number(communicationSummary?.total_score || 0),
          maxScore: Number(communicationSummary?.max_score || communicationCriteria.length * 3),
          passThreshold: Number(communicationSummary?.pass_threshold || 0),
          passFail: String(communicationSummary?.pass_fail || ''),
          labelCounts: communicationSummary?.label_counts || {},
          perCriterionPoints: communicationCriteria.map((criterion, index) => ({
            id: criterion?.id ?? index + 1,
            label: String(criterion?.label || `Criterion ${index + 1}`),
            section: criterion?.section || null,
            scoreLabel: String(criterion?.score_label || 'None'),
            points: Number(criterion?.points ?? 0),
          })),
        },
      };
    }).filter(Boolean);

    return {
      parentSessionId: LONG_DEMO_SESSION_ID,
      clipsCount: summaries.length,
      totalChildSessions: summaries.length,
      assessedCount: summaries.length,
      summaries,
    };
  }

  async function openLongVideoDemoWorkspace() {
    setError('');
    setIsUploading(false);
    setIsProcessing(true);
    setProcessingMessage('Loading bundled long-video demo workspace...');
    setLiveLogLine('Loading long-video demo bundle...');
    setActiveSegmentId(null);

    try {
      const bundle = await loadBundledLongDemoResources();

      setIsDemoFallback(true);
      setShowWorkspace(true);
      setUploadFlow('long');
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
      setPipelineMilestones({
        started: true,
        convertedToMp3: true,
        transcriptionComplete: true,
        scored: true,
      });

      // Mark every clip as completed so the panel renders "View / Re-run"
      // controls and the cohort charts get the trigger they need.
      const runs = {};
      LONG_DEMO_CHILD_IDS.forEach((childId) => {
        const child = bundle.childById[childId];
        if (!child) return;
        const clipId = child.session?.clipSource?.clipId;
        if (clipId) {
          runs[clipId] = { status: 'completed', sessionId: childId };
        }
      });
      setClipAssessmentRuns(runs);

      setDemoLongVideoSummaries(buildLongDemoSummaries(bundle.childById));
      setNotice(
        'Long-video demo mode: clips already exported, every student assessed. Export disabled.',
      );
      setLiveLogLine('Long-video demo bundle loaded successfully.');
    } catch (loadError) {
      setError(`Long-video demo failed to load: ${loadError.message || 'unknown error'}`);
      setNotice('');
    } finally {
      setIsUploading(false);
      setIsProcessing(false);
    }
  }

  // Direct-to-bucket upload against a resumable session URI the backend minted
  // at initiate. The bytes never touch the API process, so restarting or
  // redeploying the API mid-upload no longer kills the transfer, and a chunk
  // that fails is retried against the offset the bucket reports rather than
  // restarting the whole file.
  async function uploadFileToResumableSession(file, fileUpload, onProgress) {
    if (!fileUpload.uploadUrl) {
      throw new Error('Upload plan did not include a resumable session URL.');
    }
    const chunkSize = Number(fileUpload.partSizeBytes || 0);
    if (!Number.isFinite(chunkSize) || chunkSize <= 0) {
      throw new Error('Upload plan did not include a valid chunk size.');
    }

    let offset = 0;
    while (offset < file.size) {
      const end = Math.min(offset + chunkSize, file.size);
      const chunk = file.slice(offset, end);
      const delays = [1000, 2000];
      let response;
      let lastError;

      for (let attempt = 0; attempt <= delays.length; attempt++) {
        try {
          response = await fetch(fileUpload.uploadUrl, {
            method: 'PUT',
            headers: {
              'Content-Range': `bytes ${offset}-${end - 1}/${file.size}`,
            },
            body: chunk,
          });
          lastError = null;
          break;
        } catch (networkErr) {
          lastError = networkErr;
          if (attempt < delays.length) {
            await new Promise((r) => setTimeout(r, delays[attempt]));
          }
        }
      }
      if (lastError) throw lastError;

      // 308 means "chunk stored, send more" and carries the byte range the
      // bucket actually holds. Trusting that Range header over a local counter
      // is what makes a partially-accepted chunk resume correctly.
      if (response.status === 308) {
        const range = response.headers.get('Range');
        const lastByte = range ? Number(range.split('-').pop()) : NaN;
        offset = Number.isFinite(lastByte) ? lastByte + 1 : end;
      } else if (response.ok) {
        offset = file.size;
      } else {
        throw new Error(`Upload failed at byte ${offset} (HTTP ${response.status}).`);
      }
      onProgress?.(offset, file.size, fileUpload);
    }
  }

  async function uploadFileParts(file, fileUpload, onProgress) {
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

    let uploadedBytes = 0;
    let partNumber = 1;
    for (let offset = 0; offset < file.size; offset += partSize) {
      const chunk = file.slice(offset, Math.min(offset + partSize, file.size));
      const partUrl = fileUpload.partUrlTemplate.replace('{partNumber}', String(partNumber));

      // Retry up to 3 attempts with exponential back-off for transient network errors.
      const delays = [1000, 2000];
      let lastError;
      let response;
      for (let attempt = 0; attempt <= delays.length; attempt++) {
        try {
          response = await fetch(partUrl, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/octet-stream' },
            body: chunk,
          });
          lastError = null;
          break;
        } catch (networkErr) {
          lastError = networkErr;
          if (attempt < delays.length) {
            await new Promise((r) => setTimeout(r, delays[attempt]));
          }
        }
      }
      if (lastError) throw lastError;

      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.error || `Upload part ${partNumber} failed.`);
      }
      uploadedBytes += chunk.size;
      onProgress?.(uploadedBytes, file.size, fileUpload);
      partNumber += 1;
    }
  }

  async function runAsyncUploadAssessment() {
    const initiateResponse = await fetch('/api/uploads/initiate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        workflow: uploadFlow,
        autoProcess: true,
        sessionName: sessionNameInput.trim() || null,
        segmentation: uploadFlow === 'long' ? segmentationMethod : null,
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
      }),
    });
    const initiateBody = await initiateResponse.json().catch(() => ({}));
    if (!initiateResponse.ok) {
      const error = new Error(initiateBody.error || 'Async upload initiation failed.');
      error.status = initiateResponse.status;
      throw error;
    }

    const sessionId = initiateBody.session?.id;
    if (!sessionId) {
      throw new Error('Async upload initiation did not return a session.');
    }
    setSession(initiateBody.session);
    setProcessingMessage('Uploading source files in resumable parts...');

    const filePlans = initiateBody.fileUploads || [];
    const videoPlan = filePlans.find((item) => item.kind === 'video');
    const caseStudyPlan = filePlans.find((item) => item.kind === 'caseStudy');
    if (!videoPlan || !caseStudyPlan) {
      throw new Error('Async upload initiation did not return both file upload plans.');
    }

    const updateUploadProgress = (uploadedBytes, totalBytes, plan) => {
      const percent = totalBytes > 0 ? Math.round((uploadedBytes / totalBytes) * 100) : 0;
      const label = plan.kind === 'caseStudy' ? 'case study' : 'video';
      setProcessingMessage(`Uploading ${label} (${percent}%)...`);
      setLiveLogLine(`Uploaded ${label}: ${percent}%`);
    };

    await uploadFileParts(videoFile, videoPlan, updateUploadProgress);
    await uploadFileParts(caseStudyFile, caseStudyPlan, updateUploadProgress);

    setProcessingMessage('Finalizing upload and verifying media...');
    const completeResponse = await fetch(`/api/uploads/${initiateBody.uploadId}/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ autoProcess: true }),
    });
    const completeBody = await completeResponse.json().catch(() => ({}));
    if (!completeResponse.ok) {
      throw new Error(completeBody.error || 'Upload finalization failed.');
    }
    // Upload is committed and the job is queued. Return the user to the main
    // page: the session card shows the live stage, other sessions stay fully
    // browsable, and this session unlocks when processing completes.
    setSession(null);
    setIsUploading(false);
    setIsProcessing(false);
    setSessionNameInput('');
    setNotice('Assessment started. Track its stage on the session card — it unlocks when finished.');
    await refreshSessionIndex();
    setShowWorkspace(false);
  }

  async function runLegacyUploadAssessment() {
    const formData = new FormData();
    formData.append('video', videoFile);
    formData.append('caseStudy', caseStudyFile);
    // The backend persists the workflow so a long session renders the clip
    // workflow (and is gated while cropping) even via this legacy path.
    formData.append('workflow', uploadFlow);
    if (sessionNameInput.trim()) {
      formData.append('sessionName', sessionNameInput.trim());
    }
    if (uploadFlow === 'long') {
      formData.append('segmentation', segmentationMethod);
    }
    if (selectedCorpusId) {
      formData.append('corpusId', selectedCorpusId);
    }

    const uploadResponse = await fetch('/api/upload', {
      method: 'POST',
      body: formData,
    });

    const uploadBody = await uploadResponse.json().catch(() => ({}));
    if (!uploadResponse.ok) {
      throw new Error(uploadBody.error || 'Upload failed.');
    }

    // The legacy backend runs processing synchronously inside the request, so
    // it is deliberately NOT awaited: kick it off, return the user to the main
    // page, and let the 8-second list poll drive the session card's stage
    // gauge. The card unlocks (button becomes "Open") on a terminal status.
    const sessionId = uploadBody.session?.id;
    if (!sessionId) {
      throw new Error('Upload did not return a session ID.');
    }
    const kickoff =
      uploadFlow === 'long'
        ? fetch(`/api/sessions/${sessionId}/auto-crop`, { method: 'POST' })
        : fetch(`/api/sessions/${sessionId}/process`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: 'gpu' }),
          });
    kickoff
      .catch(() => {})
      // Success or failure, the durable session status is the source of truth.
      .then(() => refreshSessionIndex());

    setSession(null);
    setIsUploading(false);
    setIsProcessing(false);
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
    setLiveLogLine('started');
    setPipelineMilestones({
      started: true,
      convertedToMp3: false,
      transcriptionComplete: false,
      scored: false,
    });
    setProcessingStage(uploadFlow === 'long' ? 'autocrop' : 'pipeline');
    debugPipeline('[pipeline] started');
    setIsUploading(true);
    setIsProcessing(true);
    setProcessingMessage('Preparing cloud-ready upload...');

    try {
      try {
        await runAsyncUploadAssessment();
      } catch (asyncError) {
        if (![404, 405, 501].includes(Number(asyncError.status || 0))) {
          throw asyncError;
        }
        setProcessingMessage('Async upload unavailable. Falling back to compatibility upload...');
        await runLegacyUploadAssessment();
      }
    } catch (requestError) {
      setError(`Upload failed: ${requestError.message || 'Unknown error. Check that the backend is running.'}`);
      setShowWorkspace(false);
    } finally {
      setIsUploading(false);
      setIsProcessing(false);
    }
  }

  function seekToSegment(segment) {
    if (!videoPlayerRef.current) {
      return;
    }

    setActiveSegmentId(segment.id);
    videoPlayerRef.current.currentTime = segment.start;
    videoPlayerRef.current.play();
  }

  function seekToSeconds(seconds) {
    if (!videoPlayerRef.current) {
      return;
    }
    const safeSeconds = Number(seconds || 0);
    if (!Number.isFinite(safeSeconds) || safeSeconds < 0) {
      return;
    }
    videoPlayerRef.current.currentTime = safeSeconds;
    videoPlayerRef.current.play();

    videoPlayerSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function handleVideoLoadedMetadata(event) {
    const duration = Number(event.currentTarget?.duration || 0);
    if (Number.isFinite(duration) && duration > 0) {
      setVideoDurationSeconds(duration);
    }
  }

  function handleVideoTimeUpdate(event) {
    const currentTime = Number(event.currentTarget.currentTime || 0);
    if (Number.isFinite(currentTime)) {
      setCurrentVideoTime(currentTime);
    }

    if (!transcriptSegments.length) {
      return;
    }

    const activeSegment = transcriptSegments.find((segment) => {
      const segmentEnd = segment.end > segment.start ? segment.end : segment.start + 0.2;
      return currentTime >= segment.start && currentTime <= segmentEnd;
    });

    if (activeSegment) {
      if (activeSegment.id !== activeSegmentId) {
        setActiveSegmentId(activeSegment.id);
      }
      return;
    }

    let latestStartedSegment = null;
    for (const segment of transcriptSegments) {
      if (segment.start <= currentTime) {
        latestStartedSegment = segment;
      } else {
        break;
      }
    }

    if (latestStartedSegment && latestStartedSegment.id !== activeSegmentId) {
      setActiveSegmentId(latestStartedSegment.id);
    }
  }

  function downloadTranscriptJson() {
    if (!transcriptSegments.length) {
      return;
    }

    const blob = new Blob([JSON.stringify(transcript, null, 2)], { type: 'application/json' });
    downloadBlob(`${toSafeDownloadName(session?.id, 'transcript')}.json`, blob);
  }

  function downloadScoreSheet() {
    if (!canDownloadScoreSheet) {
      return;
    }

    const safeSessionId = toSafeDownloadName(
      scoreReport?.session_id || communicationScores?.session_id || session?.id,
      'scores',
    );
    const fileName = `${safeSessionId}-rubric.csv`;
    const rows = [];

    const appendSection = (sectionRows) => {
      if (!sectionRows.length) {
        return;
      }
      if (rows.length) {
        rows.push([]);
      }
      rows.push(...sectionRows);
    };

    const buildContentRows = () => {
      const feedbackPayload = {
        keep: keepStartStop?.keep || DEFAULT_KEEP_START_STOP.keep,
        start: keepStartStop?.start || DEFAULT_KEEP_START_STOP.start,
        stop: keepStartStop?.stop || DEFAULT_KEEP_START_STOP.stop,
      };
      const contentRows = [
        ['OSCE Rubric Score Sheet - Content'],
        [],
        ['Student Name', ''],
        ['Student ID', ''],
        ['Session ID', scoreReport?.session_id || session?.id || ''],
        [],
        ['Content Scoring Summary'],
        ['Result', 'Yes', 'No', 'Critical Yes', 'Critical No'],
        [
          scoringSummary?.passFail || (aiCriteria.length ? 'Pending' : 'N/A'),
          scoringSummary?.yesCount ?? aiCriteria.filter((criterion) => criterion.value === 'Yes').length,
          scoringSummary?.noCount ?? aiCriteria.filter((criterion) => criterion.value === 'No').length,
          scoringSummary?.criticalYes ?? 0,
          scoringSummary?.criticalNo ?? 0,
        ],
        [],
        ['Criteria'],
        ['Criterion', 'Result', 'Critical', 'Timestamp', 'Reason'],
      ];

      if (aiCriteria.length) {
        aiCriteria.forEach((criterion) => {
          contentRows.push([
            criterion.key || '',
            criterion.value || '',
            criterion.isCritical ? 'Yes' : 'No',
            criterion.timestamp || '',
            criterion.reason || '',
          ]);
        });
      } else {
        contentRows.push(['No content rubric criteria were returned.', '', '', '', '']);
      }

      contentRows.push(
        [],
        ['Keep / Start / Stop Feedback'],
        ['Type', 'Notes'],
        ['Keep', feedbackPayload.keep],
        ['Start', feedbackPayload.start],
        ['Stop', feedbackPayload.stop],
      );

      if (scoreReport?.overall_summary) {
        contentRows.push([], ['Overall Summary'], ['Summary', scoreReport.overall_summary]);
      }

      return contentRows;
    };

    const buildCommunicationRows = () => {
      const labelCounts = communicationSummary?.labelCounts || {};
      const labelDistribution = `${labelCounts.All ?? 0} / ${labelCounts.Most ?? 0} / ${labelCounts.Some ?? 0} / ${labelCounts.None ?? 0}`;
      const labelToPoints = { All: 3, Most: 2, Some: 1, None: 0 };
      const joinIndicators = (items) => (
        Array.isArray(items) && items.length ? `- ${items.join('\n- ')}` : ''
      );
      const communicationRows = [
        ['OSCE Rubric Score Sheet - Communication'],
        [],
        ['Student Name', ''],
        ['Student ID', ''],
        ['Session ID', communicationScores?.session_id || session?.id || ''],
        [],
        ['Scoring Scale'],
        ['Label', 'Marks', 'Meaning'],
        ['All', 3, 'Student consistently demonstrated EVERY observable indicator'],
        ['Most', 2, 'Student consistently demonstrated MOST observable indicators'],
        ['Some', 1, 'Student demonstrated ONLY SOME observable indicators'],
        ['None', 0, 'Student demonstrated NONE of the observable indicators'],
        [],
        ['Communication Scoring Summary'],
        ['Result', 'Total', 'Max', 'Pass threshold', 'All / Most / Some / None'],
        [
          communicationSummary?.passFail || (communicationCriteria.length ? 'Pending' : 'N/A'),
          communicationSummary?.totalScore ?? 0,
          communicationSummary?.maxScore ?? communicationCriteria.length * 3,
          communicationSummary?.passThreshold
            ?? (communicationCriteria.length === 7 ? 11 : Math.ceil((communicationCriteria.length * 3) / 2)),
          labelDistribution,
        ],
        [],
        ['Communication Criteria'],
        ['ID', 'Criterion', 'Section', 'Score Label', 'Marks', 'Timestamp'],
      ];

      communicationCriteria.forEach((criterion) => {
        communicationRows.push([
          criterion.id || '',
          criterion.label || '',
          criterion.section || '',
          criterion.scoreLabel || 'None',
          labelToPoints[criterion.scoreLabel] ?? 0,
          criterion.timestamp || '',
        ]);
      });

      communicationRows.push(
        [],
        ['Evidence & Indicators'],
        ['ID', 'Criterion', 'Evidence', 'Indicators observed', 'Indicators missing', 'Not observable'],
      );

      communicationCriteria.forEach((criterion) => {
        communicationRows.push([
          criterion.id || '',
          criterion.label || '',
          criterion.evidence || '',
          joinIndicators(criterion.indicatorsObserved),
          joinIndicators(criterion.indicatorsMissing),
          joinIndicators(criterion.indicatorsNotObservable),
        ]);
      });

      if (communicationPayload?.overall_summary) {
        communicationRows.push([], ['Overall Communication Summary'], ['Summary', communicationPayload.overall_summary]);
      }

      return communicationRows;
    };

    if (aiCriteria.length > 0 || keepStartStop) {
      appendSection(buildContentRows());
    }
    if (communicationCriteria.length > 0) {
      appendSection(buildCommunicationRows());
    }

    const blob = new Blob([`\uFEFF${rowsToCsv(rows)}\r\n`], { type: 'text/csv;charset=utf-8' });
    downloadBlob(fileName, blob);
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

      const res = await fetch(`/api/sessions/${session.id}/clips/${selectedClip.id}/recrop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.error || 'Recrop failed.');
      }

      if (body?.session) {
        setSession(body.session);
      }
    } catch (error) {
      setError(error.message || 'Recrop failed.');
    } finally {
      setIsRecropping(false);
    }
  }

  function syncManualLabels(segmentCount, preferExisting = true) {
    setManualLabels((previous) => {
      const next = [];
      for (let index = 0; index < segmentCount; index += 1) {
        const existing = preferExisting ? String(previous[index] || '').trim() : '';
        next.push(existing || `Student ${index + 1}`);
      }
      return next;
    });
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
    setManualSegmentCount(segmentCount);
    setManualBoundaries(boundaries);
    setManualSegmentKinds(Array(segmentCount).fill('session'));
    syncManualLabels(segmentCount, true);
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
    let appliedValue = null;
    setManualBoundaries((previous) => {
      const updated = [...previous];
      const leftLimit = index === 0 ? 0 : Number(updated[index - 1] || 0) + 0.2;
      const rightLimit =
        index === updated.length - 1
          ? Number(videoDurationSeconds || 0)
          : Number(updated[index + 1] || videoDurationSeconds) - 0.2;
      const nextBoundary = clampNumber(Number(nextValueSeconds || 0), leftLimit, rightLimit);
      updated[index] = nextBoundary;
      appliedValue = nextBoundary;
      return updated;
    });

    if (seekVideo && appliedValue != null) {
      seekVideoPreview(appliedValue);
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
      const response = await fetch(`/api/sessions/${session.id}/clips/${clipId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: trimmed }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.error || 'Failed to rename clip.');
      }
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
      const response = await fetch(`/api/sessions/${session.id}/clips/manual`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.error || 'Failed to save manual segments.');
      }
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
      setLiveLogLine(`Demo re-run for ${clip.label || 'clip'}...`);
      setProcessingStage('pipeline');
      setProcessingMessage(`Re-running ${clip.label || 'clip'} (demo)...`);
      setIsProcessing(true);
      // Brief simulated runtime so the spinner is visible.
      await new Promise((resolve) => setTimeout(resolve, 900));
      setClipAssessmentRuns((previous) => ({
        ...previous,
        [clip.id]: { status: 'completed', sessionId: demoChildId },
      }));
      setIsProcessing(false);
      setProcessingMessage('');
      setLiveLogLine('Demo re-run finished; scores unchanged.');
      setNotice(
        `Demo mode: a real re-run would call the NVIDIA assessor again. Showing the cached score for "${clip.label || ''}".`,
      );
      return true;
    }

    // Non-blocking: queue the child assessment and STAY on the parent clip
    // list. The clip row shows the live stage (driven by the 8-second session
    // index poll) and flips to "View" when the child session completes. The
    // child is not enterable while in flight (same rule as the session list).
    setError('');
    setClipAssessmentRuns((previous) => ({
      ...previous,
      [clip.id]: { status: 'running' },
    }));

    try {
      const response = await fetch(`/api/sessions/${session.id}/clips/${clip.id}/assess?defer=1`, {
        method: 'POST',
      });

      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.error || 'Clip assessment failed.');
      }

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
      setNotice(`Assessment for "${clip.label || 'clip'}" queued — its row updates as it progresses.`);
      refreshSessionIndex();
      return true;
    } catch (assessmentError) {
      setClipAssessmentRuns((previous) => ({
        ...previous,
        [clip.id]: { status: 'failed', error: assessmentError.message || 'Clip assessment failed.' },
      }));
      setError(assessmentError.message || 'Clip assessment failed.');
      return false;
    }
  }

  async function rerunClipAssessment(clip, childSessionId) {
    if (!clip?.id) {
      return false;
    }
    // No existing child (or demo bundle) → fall back to a first run, which
    // handles the demo path and creates the child session.
    if (!childSessionId || session?._demoChildren) {
      return runClipAssessment(clip);
    }
    if (clipAssessmentRuns[clip.id]?.status === 'running') {
      return false;
    }

    unselectClipForBatch(clip.id);
    setError('');
    setClipAssessmentRuns((previous) => ({
      ...previous,
      [clip.id]: { status: 'running', sessionId: childSessionId },
    }));
    try {
      const response = await fetch(`/api/sessions/${childSessionId}/rerun`, { method: 'POST' });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.error || 'Re-run failed.');
      }
      setNotice(`Re-running assessment for "${clip.label || 'clip'}" — its row updates as it progresses.`);
      refreshSessionIndex();
      return true;
    } catch (rerunError) {
      setClipAssessmentRuns((previous) => ({
        ...previous,
        [clip.id]: { status: 'failed', sessionId: childSessionId, error: rerunError.message || 'Re-run failed.' },
      }));
      setError(rerunError.message || 'Re-run failed.');
      return false;
    }
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
    const completedCount = plans.filter(([, plan]) => plan.status === 'completed').length;
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
    setProcessingStage('pipeline');
    setProcessingMessage(`Loading ${clip.label || 'clip'}...`);
    setLiveLogLine('Loading saved clip assessment...');
    setIsProcessing(true);

    try {
      // Demo path: pull the child session straight out of the long-demo bundle.
      const demoChildren = session?._demoChildren;
      if (demoChildren && demoChildren[clipSessionId]) {
        const childBundle = demoChildren[clipSessionId];
        setSession(childBundle.session);
        setTranscript(childBundle.transcript || { segments: [] });
        setScoreReport(childBundle.scores || null);
        setAudioProfessionalism(childBundle.audioProfessionalism || null);
        setCommunicationScores(childBundle.communicationScores || null);
        setRuntimeSeconds(Math.round(childBundle.session?.pipeline?.runtimeSeconds || 0));
        setNotice(`Demo mode: showing pre-assessed clip "${clip.label || childBundle.session?.name || ''}"`);
        return;
      }

      const loaded = await loadSessionWorkspace(clipSessionId);
      setSession(loaded.session);
      setTranscript(loaded.transcript || { segments: [] });
      setScoreReport(loaded.scores || loaded?.session?.outputs?.scores?.payload || null);
      setAudioProfessionalism(
        loaded.audioProfessionalism || loaded?.session?.outputs?.audioProfessionalism?.payload || null
      );
      setCommunicationScores(
        loaded.communicationScores || loaded?.session?.outputs?.communicationScores?.payload || null
      );
      setRuntimeSeconds(Math.round(loaded.session?.pipeline?.runtimeSeconds || 0));
      setNotice('');
    } catch (error) {
      setError(error.message || 'Failed to load clip assessment.');
    } finally {
      setIsProcessing(false);
    }
  }

  function restoreParentSession() {
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
  const clipEditsLocked = allowCropping && hasClipFiles;

  const manualCropControls = (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 shadow-inner">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs font-semibold uppercase tracking-wide text-slate-600">Manual crop</div>
        <div className="flex flex-wrap items-end gap-2">
          <label className="w-36 text-xs text-slate-600">
            Students / segments
            <input
              type="number"
              min={2}
              max={24}
              value={manualSegmentCount || ''}
              onChange={(event) => {
                const nextValue = Number(event.target.value || 0);
                setManualSegmentCount(Number.isFinite(nextValue) ? nextValue : 0);
              }}
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900"
            />
          </label>
          <Button
            variant="outline"
            size="sm"
            onClick={generateManualBoundariesFromCount}
            disabled={!videoDurationSeconds || isSavingManualSegments}
          >
            Generate
          </Button>
          <Button
            size="sm"
            className="bg-gradient-to-r from-cyan-600 to-blue-700 text-white hover:from-cyan-700 hover:to-blue-800"
            onClick={saveManualSegments}
            disabled={!session?.id || !videoDurationSeconds || isSavingManualSegments}
          >
            {isSavingManualSegments ? 'Saving…' : 'Export clips'}
          </Button>
        </div>
      </div>

      {hasDraftClips ? (
        <div className="mb-3 rounded-xl border border-violet-200 bg-violet-50 px-3 py-2 text-xs text-violet-900">
          Auto-detected <span className="font-semibold">{sessionClipCount}</span> session clip
          {sessionClipCount === 1 ? '' : 's'}
          {intermissionClipCount > 0 ? (
            <>
              {' '}
              and <span className="font-semibold">{intermissionClipCount}</span> intermission
              {intermissionClipCount === 1 ? '' : 's'} (greyed)
            </>
          ) : null}
          . Adjust the separators, rename students, then export clips to create individual videos.
        </div>
      ) : null}

      <div className="mb-2 flex items-center justify-between text-xs text-slate-600">
        <span>
          Playhead:{' '}
          <span className="font-semibold text-slate-800">{formatRuntime(currentVideoTime)}</span>
          {' / '}
          <span>{formatRuntime(videoDurationSeconds)}</span>
        </span>
        <span className="hidden text-slate-500 sm:inline">
          Click to seek · drag a separator to adjust · right-click for actions.
        </span>
      </div>

      <div
        ref={manualTimelineRef}
        onContextMenu={handleTimelineContextMenu}
        className="relative h-14 overflow-hidden rounded-xl border border-slate-300 bg-white shadow-sm"
      >
        {[0, ...manualBoundaries, videoDurationSeconds]
          .filter((value) => Number.isFinite(value))
          .map((value, index, arr) => {
            if (index === arr.length - 1) return null;
            const start = Number(arr[index] || 0);
            const end = Number(arr[index + 1] || 0);
            const left = (start / Math.max(videoDurationSeconds, 1)) * 100;
            const width = ((end - start) / Math.max(videoDurationSeconds, 1)) * 100;
            const palette = [
              'bg-cyan-200',
              'bg-violet-200',
              'bg-emerald-200',
              'bg-amber-200',
              'bg-rose-200',
              'bg-blue-200',
            ];
            const isIntermission = manualSegmentKinds[index] === INTERMISSION_KIND;
            const labelValue =
              String(manualLabels[index] || '').trim() ||
              (isIntermission ? 'Intermission' : `Student ${index + 1}`);
            // Intermissions render greyed + hatched, visually distinct from
            // the coloured session segments.
            const segmentClass = isIntermission
              ? 'bg-slate-200 text-slate-500 [background-image:repeating-linear-gradient(45deg,transparent,transparent_6px,rgba(148,163,184,0.25)_6px,rgba(148,163,184,0.25)_12px)]'
              : `text-slate-800 ${palette[index % palette.length]}`;
            return (
              <button
                key={`manual-segment-${index}-${start.toFixed(2)}`}
                type="button"
                onClick={handleTimelineClick}
                className={`absolute top-0 flex h-full items-center justify-center truncate px-2 text-[11px] font-semibold transition ${segmentClass} hover:brightness-95`}
                style={{ left: `${left}%`, width: `${Math.max(width, 0)}%` }}
                title={`${labelValue}: click to move the playhead to that exact second`}
              >
                <span className="truncate drop-shadow-sm">{labelValue}</span>
              </button>
            );
          })}

        {durationKnown ? (
          <div
            className="pointer-events-none absolute top-0 z-10 h-full w-0.5 bg-rose-600 shadow-[0_0_0_1px_rgba(0,0,0,0.12)]"
            style={{
              left: `${Math.min(100, Math.max(0, (currentVideoTime / Math.max(videoDurationSeconds, 1)) * 100))}%`,
            }}
          />
        ) : null}

        {manualBoundaries.map((boundary, index) => {
          const left = (Number(boundary || 0) / Math.max(videoDurationSeconds, 1)) * 100;
          return (
            <button
              key={`boundary-${index}`}
              type="button"
              className="absolute top-0 z-20 h-full w-1.5 -translate-x-1/2 cursor-ew-resize bg-slate-900/85 outline-none ring-2 ring-transparent hover:ring-cyan-400 focus:ring-cyan-500"
              style={{ left: `${left}%` }}
              onMouseDown={(event) => {
                event.preventDefault();
                event.stopPropagation();
                setDraggingBoundaryIndex(index);
                seekVideoPreview(Number(boundary || 0));
              }}
              title={`Separator ${index + 1} @ ${formatRuntime(boundary)}`}
            />
          );
        })}
      </div>

      {timelineMenu ? (
        <div
          ref={timelineMenuRef}
          role="menu"
          className="fixed z-[80] w-64 overflow-hidden rounded-xl border border-slate-200 bg-white py-1 shadow-xl"
          style={{
            left: Math.min(timelineMenu.x, (typeof window !== 'undefined' ? window.innerWidth : 0) - 272),
            top: Math.min(timelineMenu.y, (typeof window !== 'undefined' ? window.innerHeight : 0) - 132),
          }}
          onContextMenu={(event) => event.preventDefault()}
        >
          {(() => {
            const actions = contextMenuActions(timelineMenu.hit.target);
            // Session/intermission marking is a human-detection feature; bell
            // detection splits at bells only, so the toggle stays blocked.
            const toggleAllowed = actions.toggleEnabled && isPersonSegmentedSession;
            const toggleBlockedHint = !actions.toggleEnabled
              ? 'Right-click a clip area to switch its type.'
              : 'Available for human-detection sessions only (bell splits have no intermissions).';
            const segmentKind =
              timelineMenu.hit.segmentIndex !== null
                ? ensureKinds(manualSegmentKinds, manualBoundaries.length + 1)[timelineMenu.hit.segmentIndex]
                : null;
            const menuItemClass = (enabled) =>
              `flex w-full items-center gap-2 px-3 py-2 text-left text-sm ${
                enabled
                  ? 'text-slate-700 hover:bg-cyan-50 hover:text-cyan-900'
                  : 'cursor-not-allowed text-slate-300'
              }`;
            return (
              <>
                <button
                  type="button"
                  role="menuitem"
                  disabled={!actions.deleteEnabled}
                  onClick={() => applyTimelineMenuAction('delete')}
                  className={menuItemClass(actions.deleteEnabled)}
                  title={actions.deleteEnabled ? undefined : 'Right-click a separator to delete it.'}
                >
                  <Trash2 className="h-3.5 w-3.5 shrink-0" />
                  Delete separator
                </button>
                <button
                  type="button"
                  role="menuitem"
                  disabled={!actions.addEnabled}
                  onClick={() => applyTimelineMenuAction('add')}
                  className={menuItemClass(actions.addEnabled)}
                  title={actions.addEnabled ? undefined : 'Right-click a clip area or the playhead to add a separator.'}
                >
                  <Scissors className="h-3.5 w-3.5 shrink-0" />
                  Add separator at {formatRuntime(timelineMenu.hit.timeSeconds)}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  disabled={!toggleAllowed}
                  onClick={() => applyTimelineMenuAction('toggle')}
                  className={menuItemClass(toggleAllowed)}
                  title={toggleAllowed ? undefined : toggleBlockedHint}
                >
                  <RotateCw className="h-3.5 w-3.5 shrink-0" />
                  {segmentKind === INTERMISSION_KIND
                    ? 'Switch to session clip'
                    : 'Switch to intermission (break)'}
                </button>
              </>
            );
          })()}
        </div>
      ) : null}

      {manualBoundaries.length > 0 && manualLabels.length > 0 ? (
        <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {manualLabels.map((labelValue, segmentIndex) => {
            const segmentStart =
              segmentIndex === 0 ? 0 : Number(manualBoundaries[segmentIndex - 1] || 0);
            const segmentEnd =
              segmentIndex === manualBoundaries.length
                ? Number(videoDurationSeconds || 0)
                : Number(manualBoundaries[segmentIndex] || 0);
            const isIntermission = manualSegmentKinds[segmentIndex] === INTERMISSION_KIND;
            const ordinal = sessionOrdinals(
              ensureKinds(manualSegmentKinds, manualLabels.length)
            )[segmentIndex];
            return (
              <div
                key={`manual-label-${segmentIndex}`}
                className={`flex items-center gap-2 rounded-xl border px-2 py-2 shadow-sm ${
                  isIntermission ? 'border-slate-200 bg-slate-100 opacity-70' : 'border-slate-200 bg-white'
                }`}
              >
                <div className="flex w-8 shrink-0 items-center justify-center rounded-lg bg-slate-100 text-[11px] font-bold text-slate-700">
                  {isIntermission ? '—' : ordinal}
                </div>
                {isIntermission ? (
                  <span className="min-w-0 flex-1 truncate px-1 text-xs font-medium italic text-slate-500">
                    Intermission (break)
                  </span>
                ) : (
                  <input
                    type="text"
                    value={labelValue}
                    onChange={(event) => updateManualLabelAt(segmentIndex, event.target.value)}
                    placeholder={`Student ${ordinal || segmentIndex + 1}`}
                    className="min-w-0 flex-1 rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-800 focus:border-cyan-400 focus:outline-none focus:ring-2 focus:ring-cyan-400/30"
                  />
                )}
                <button
                  type="button"
                  onClick={() => handleManualSegmentClick(segmentStart)}
                  className="shrink-0 rounded-lg border border-slate-200 bg-slate-50 px-2 py-1.5 text-[11px] font-semibold text-slate-700 hover:border-cyan-300 hover:bg-cyan-50"
                  title={`Jump to ${formatRuntime(segmentStart)} – ${formatRuntime(segmentEnd)}`}
                >
                  {formatRuntime(segmentStart)}
                </button>
              </div>
            );
          })}
        </div>
      ) : null}

      <p className="mt-3 text-xs leading-relaxed text-slate-500">
        Generate equal splits, drag boundaries onto buzzers, rename segments, then export. Works for every recording
        length.
      </p>
    </div>
  );

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
    isRecropping,
    seekToSeconds,
    recropSelectedClip,
  };

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="sticky top-0 z-40 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-6 py-4">
          <div className="flex items-center gap-3">
            {showWorkspace ? (
              <Button
                variant="outline"
                size="sm"
                className="gap-2"
                onClick={goHome}
                disabled={isUploading || isProcessing}
                title="Return to upload / saved sessions"
              >
                <ArrowLeft className="h-4 w-4" />
                Back
              </Button>
            ) : null}
            <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-cyan-600 to-blue-700 text-white shadow-sm">
              <Brain className="h-5 w-5" />
            </div>
            <div>
              <div className="text-lg font-bold">OSCE AI Marker</div>
              <div className="text-xs text-slate-500">Local WhisperX Pipeline</div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <Badge className="bg-slate-100 text-slate-700">Local API</Badge>
            {isDemoFallback ? (
              <Badge className="bg-amber-100 text-amber-700">Demo Workspace</Badge>
            ) : (
              <Badge className="bg-emerald-100 text-emerald-700">{currentModeLabel} Ready</Badge>
            )}
            {notifications ? (
              <NotificationBell
                items={notifications.items}
                unreadCount={notifications.unreadCount}
                onDismiss={notifications.dismiss}
              />
            ) : null}
            {onOpenAnalytics ? (
              <Button variant="outline" size="sm" className="gap-2" onClick={onOpenAnalytics}>
                <BarChart3 className="h-4 w-4" />
                Analytics
              </Button>
            ) : null}
            {onOpenRubric ? (
              <Button variant="outline" size="sm" className="gap-2" onClick={onOpenRubric}>
                <FileText className="h-4 w-4" />
                Communication Rubric
              </Button>
            ) : null}
            {onOpenSettings ? (
              <Button variant="outline" size="sm" className="gap-2" onClick={onOpenSettings}>
                <Settings className="h-4 w-4" />
                Settings
              </Button>
            ) : null}
            {authUsername ? (
              <div className="flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">
                <User className="h-3.5 w-3.5 text-slate-500" />
                <span className="text-[11px] font-semibold text-slate-700">{authUsername}</span>
              </div>
            ) : null}
            {onLogout ? (
              <Button variant="ghost" size="sm" className="gap-2 text-slate-500 hover:text-rose-600" onClick={onLogout}>
                <LogOut className="h-4 w-4" />
                Logout
              </Button>
            ) : null}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-10">
        {!showWorkspace && (
          <section className="grid grid-cols-1 gap-6 lg:grid-cols-3">
            <Card className="lg:col-span-2 border-slate-200 bg-white shadow-sm">
              <CardHeader>
                <CardTitle className="text-3xl">Upload Station Video</CardTitle>
                <CardDescription>
                  Video is stored locally, converted to MP3, then transcribed using your local WhisperX setup.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-5">
                <Tabs defaultValue="standard" className="w-full">
                  <TabsList className="grid w-full grid-cols-2 border border-slate-200 bg-slate-50">
                    <TabsTrigger value="standard" onClick={() => setUploadFlow('standard')}>
                      Standard Upload
                    </TabsTrigger>
                    <TabsTrigger value="long" onClick={() => setUploadFlow('long')}>
                      Long Video Upload (5+ min)
                    </TabsTrigger>
                  </TabsList>
                </Tabs>

                <div
                  className={`rounded-xl border p-3 text-sm ${
                    uploadFlow === 'long'
                      ? 'border-violet-200 bg-violet-50 text-violet-900'
                      : 'border-cyan-200 bg-cyan-50 text-cyan-900'
                  }`}
                >
                  {uploadFlow === 'long'
                    ? 'Long-video mode selected. Upload multi-student recordings to auto-detect clip ranges, adjust them manually, then run assessments per student.'
                    : 'Standard mode selected. Best for single-student recordings; cropping tools are hidden.'}
                </div>

                {uploadFlow === 'long' && (
                  <div className="rounded-xl border border-slate-200 bg-white p-3">
                    <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                      Auto-split method
                    </div>
                    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Auto-split method">
                      <button
                        type="button"
                        role="radio"
                        aria-checked={segmentationMethod === 'bells'}
                        onClick={() => setSegmentationMethod('bells')}
                        className={`flex items-start gap-2 rounded-lg border p-3 text-left transition ${
                          segmentationMethod === 'bells'
                            ? 'border-violet-400 bg-violet-50 ring-2 ring-violet-200'
                            : 'border-slate-200 bg-white hover:border-slate-300'
                        }`}
                      >
                        <BellRing className="mt-0.5 h-4 w-4 shrink-0 text-violet-600" />
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
                        aria-checked={segmentationMethod === 'person'}
                        onClick={() => setSegmentationMethod('person')}
                        className={`flex items-start gap-2 rounded-lg border p-3 text-left transition ${
                          segmentationMethod === 'person'
                            ? 'border-violet-400 bg-violet-50 ring-2 ring-violet-200'
                            : 'border-slate-200 bg-white hover:border-slate-300'
                        }`}
                      >
                        <Users className="mt-0.5 h-4 w-4 shrink-0 text-violet-600" />
                        <span className="min-w-0">
                          <span className="block text-sm font-medium text-slate-800">Human detection</span>
                          <span className="block text-xs text-slate-500">
                            AI vision (RT-DETR) splits when a student leaves the frame. Best when bells are unreliable.
                          </span>
                        </span>
                      </button>
                    </div>
                    {segmentationMethod === 'person' && (
                      <p className="mt-2 text-[11px] text-slate-400">
                        Falls back to bell detection automatically if the vision model is unavailable on the worker.
                      </p>
                    )}
                  </div>
                )}

                <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                  <UploadCard
                    icon={<Video className="h-5 w-5" />}
                    title={uploadFlow === 'long' ? 'Long Station Video' : 'Station Video'}
                    subtitle={uploadFlow === 'long' ? '5+ min preferred (MP4 / MOV / MKV)' : 'MP4 / MOV / MKV'}
                    fileName={videoFile?.name || null}
                    onPick={() => videoInputRef.current?.click()}
                  />

                  <UploadCard
                    icon={<FileSpreadsheet className="h-5 w-5" />}
                    title="Case Study"
                    subtitle="PDF required (rubric is read from its ending checklist section)"
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
                    className="gap-2 bg-gradient-to-r from-cyan-600 to-blue-700 text-white hover:from-cyan-700 hover:to-blue-800"
                    onClick={requestStartAssessment}
                    disabled={!videoFile || !caseStudyFile || isUploading || isProcessing}
                  >
                    <Wand2 className="h-4 w-4" />
                    {uploadFlow === 'long' ? 'Start Long Video Assessment' : 'Start Assessment'}
                  </Button>
                  <Button
                    size="lg"
                    variant="outline"
                    className="gap-2"
                    onClick={openManualDemoMode}
                    disabled={isUploading || isProcessing}
                  >
                    <PlayCircle className="h-4 w-4" />
                    Open Standard Demo
                  </Button>
                  <Button
                    size="lg"
                    variant="outline"
                    className="gap-2 border-violet-300 text-violet-700 hover:bg-violet-50"
                    onClick={openLongVideoDemoWorkspace}
                    disabled={isUploading || isProcessing}
                  >
                    <Scissors className="h-4 w-4" />
                    Open Long-Video Demo
                  </Button>
                  <span className="text-sm text-slate-500">
                    {videoFile && caseStudyFile
                      ? 'Ready to process on local machine.'
                      : 'Select a video and case study PDF, or open a bundled demo workspace.'}
                  </span>
                </div>

                {error && (
                  <div className="rounded-xl border border-rose-300 bg-rose-50 p-3 text-sm text-rose-700">
                    {error}
                  </div>
                )}

                {notice && !showWorkspace && (
                  <div className="rounded-xl border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
                    {notice}
                  </div>
                )}
              </CardContent>
            </Card>

            <div className="space-y-6">
              <Card className="border-slate-200 bg-white shadow-sm">
                <CardHeader>
                  <CardTitle>Saved Sessions</CardTitle>
                  <CardDescription>Open or rename any previous session stored on disk.</CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  {sessionIndexLoading ? (
                    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-sm text-slate-500">
                      Loading sessions...
                    </div>
                  ) : null}

                  {sessionIndexError ? (
                    <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-xs text-rose-700">
                      {sessionIndexError}
                    </div>
                  ) : null}

                  {!sessionIndexLoading && visibleSessions.length === 0 ? (
                    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-sm text-slate-500">
                      No saved sessions yet. Upload a video to create one.
                    </div>
                  ) : null}

                  <div className="max-h-[32rem] space-y-2 overflow-y-auto pr-1">
                    {visibleSessions.map((sessionEntry) => (
                      <div
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
                              className="w-full rounded-lg border border-transparent bg-white px-2 py-1.5 text-sm font-semibold text-slate-800 shadow-sm focus:border-cyan-400 focus:outline-none focus:ring-2 focus:ring-cyan-400/30"
                            />
                            <div className="mt-1 text-[11px] text-slate-500" title={sessionEntry.id}>
                              {sessionEntry.id}
                            </div>
                            <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px]">
                              {(sessionEntry.status === 'assembling' || sessionEntry.status === 'queued' || sessionEntry.status === 'processing') && (
                                <Loader2 className="h-3 w-3 animate-spin text-cyan-600" />
                              )}
                              <span className={
                                sessionEntry.status === 'completed' || sessionEntry.status === 'succeeded'
                                  ? 'font-semibold text-emerald-600'
                                  : sessionEntry.status === 'failed'
                                  ? 'font-semibold text-rose-600'
                                  : sessionEntry.status === 'processing'
                                  ? 'font-semibold text-cyan-700'
                                  : sessionEntry.status === 'queued'
                                  ? 'font-semibold text-amber-600'
                                  : sessionEntry.status === 'assembling'
                                  ? 'font-semibold text-sky-600'
                                  : 'text-slate-500'
                              }>
                                {sessionEntry.status || 'unknown'}
                              </span>
                              {sessionEntry.hasVideoClips || sessionEntry.status === 'cropped' ? (
                                <Badge className="bg-violet-100 text-violet-700">
                                  Folder • Long upload
                                </Badge>
                              ) : null}
                            </div>
                            {(() => {
                              // Stage gauge for in-flight sessions: the row is
                              // not enterable, so the card is where the user
                              // tracks how far along processing is.
                              const stage = describeProcessingStage(sessionEntry);
                              if (!stage) return null;
                              return (
                                <div className="mt-2">
                                  <div className="flex items-center justify-between text-[11px] text-slate-600">
                                    {/* The stage's own percentage when the step
                                        streams one (WhisperX), next to the
                                        overall run completion on the right. */}
                                    <span>{formatProcessingStageLabel(stage)}…</span>
                                    <span>{Math.round(stage.fraction * 100)}%</span>
                                  </div>
                                  <Progress value={stage.fraction * 100} className="mt-1 h-1.5" />
                                </div>
                              );
                            })()}
                          </div>
                          <div className="flex shrink-0 items-center gap-1">
                            {renderSessionAction(sessionEntry)}
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => deleteSession(sessionEntry.id)}
                              disabled={deletingSessionId === sessionEntry.id}
                              title="Delete this session and all student assessments under it"
                              className="text-rose-600 hover:bg-rose-50 hover:text-rose-700"
                            >
                              {deletingSessionId === sessionEntry.id ? (
                                <Loader2 className="h-4 w-4 animate-spin" />
                              ) : (
                                <Trash2 className="h-4 w-4" />
                              )}
                            </Button>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </CardContent>
              </Card>

              {/* <Card className="border-slate-200 bg-white shadow-sm">
                <CardHeader>
                  <CardTitle>Pipeline Overview</CardTitle>
                  <CardDescription>Current local processing steps</CardDescription>
                </CardHeader>
                <CardContent className="space-y-3 text-sm text-slate-700">
                  <FeatureRow
                    icon={<UploadCloud className="h-4 w-4" />}
                    text="Upload video and case-study PDF to storage/input"
                  />
                  <FeatureRow icon={<Mic className="h-4 w-4" />} text="Extract MP3 into storage/output/audio" />
                  <FeatureRow icon={<Brain className="h-4 w-4" />} text="Run WhisperX locally (GPU/CPU configurable)" />
                  <FeatureRow icon={<MessageSquare className="h-4 w-4" />} text="Save normalized transcript JSON" />
                  <FeatureRow
                    icon={<FileSpreadsheet className="h-4 w-4" />}
                    text="Score transcript using rubric at end of case-study PDF"
                  />
                  <FeatureRow
                    icon={<PlayCircle className="h-4 w-4" />}
                    text="Review video + transcript in synced workspace"
                  />
                </CardContent>
              </Card> */}

              {notifications ? (
                <NotificationFeed
                  items={notifications.items}
                  unreadCount={notifications.unreadCount}
                  onDismiss={notifications.dismiss}
                />
              ) : null}
            </div>
          </section>
        )}

        {showWorkspace && (
          <section className="space-y-6">
            {notice && (
              <div className="rounded-xl border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
                {notice}
              </div>
            )}

            <Card className="border-slate-200 bg-white shadow-sm">
              <CardContent className="pt-5">
                <div className="flex flex-wrap items-center gap-3 text-sm">
                  <div className="rounded-lg border border-cyan-200 bg-cyan-50 px-3 py-2 text-cyan-800">
                    Video: {session?.files?.video?.originalName || videoFile?.name || 'Not set'}
                  </div>
                  <div className="rounded-lg border border-violet-200 bg-violet-50 px-3 py-2 text-violet-800">
                    Case Study: {session?.files?.caseStudy?.originalName || caseStudyFile?.name || 'Fallback'}
                  </div>
                  <div className="rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-indigo-800">
                    Rubric Source: End of case-study PDF
                  </div>
                  <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-slate-700">
                    Session: <span title={session?.id || ''}>{session?.name || session?.id || 'Pending'}</span>
                  </div>
                  <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-emerald-700">
                    Status: {session?.status || (isProcessing ? 'processing' : 'uploaded')}
                  </div>
                </div>
                {isClipAssessmentView ? (
                  <div className="mt-4">
                    <Button variant="outline" onClick={restoreParentSession}>
                      Back to clip list
                    </Button>
                  </div>
                ) : null}
              </CardContent>
            </Card>

            <motion.div
              initial={{ opacity: 0, y: -8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
              className="rounded-2xl border border-slate-200/90 bg-gradient-to-r from-white via-slate-50/90 to-cyan-50/40 p-4 shadow-sm ring-1 ring-slate-100/80"
            >
              <div className="flex flex-wrap items-center gap-3">
                <div className="flex items-center gap-2 rounded-xl bg-white px-3 py-2 shadow-sm ring-1 ring-slate-100">
                  <Clock3 className="h-4 w-4 shrink-0 text-cyan-600" />
                  <span className="text-sm font-semibold text-slate-800">
                    {durationKnown ? formatRuntime(videoDurationSeconds) : 'Duration —'}
                  </span>
                </div>
                {durationKnown ? (
                  <Badge
                    className={
                      showCropWorkflow
                        ? 'border-0 bg-gradient-to-r from-violet-600 to-indigo-600 px-3 py-1 text-xs font-semibold text-white shadow-sm'
                        : 'border-0 bg-slate-800 px-3 py-1 text-xs font-semibold text-white shadow-sm'
                    }
                  >
                    {showCropWorkflow
                      ? isLongRecording
                        ? 'Long video mode: auto-split + manual crop'
                        : 'Long video mode: manual crop (short recording)'
                      : 'Single student mode: no cropping'}
                  </Badge>
                ) : (
                  <Badge className="border-0 bg-slate-100 px-3 py-1 text-xs font-medium text-slate-500">
                    Waiting for video metadata…
                  </Badge>
                )}
                <p className="min-w-[12rem] flex-1 text-sm leading-snug text-slate-600">
                  {showCropWorkflow
                    ? isLongRecording
                      ? 'Open the tabs under the player: refine with Manual crop or review bell-based Auto-split clips.'
                      : 'Long video mode is enabled. Use the manual crop timeline to set student boundaries.'
                    : 'Single-student flow. Cropping controls are hidden to keep the workflow focused on assessment.'}
                </p>
              </div>
            </motion.div>

            <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
              <div className="space-y-6 lg:col-span-2">
                <div ref={videoPlayerSectionRef} className="scroll-mt-6">
                <Card className="overflow-hidden border border-slate-200/90 bg-white shadow-md shadow-slate-200/50 ring-1 ring-slate-100">
                  <CardHeader className="space-y-0 border-b border-slate-100 bg-gradient-to-br from-white via-slate-50/40 to-cyan-50/25 pb-5">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="space-y-1">
                        <CardTitle className="text-lg font-semibold tracking-tight text-slate-900">Station recording</CardTitle>
                        <CardDescription className="max-w-xl text-sm leading-relaxed">
                          WhisperX captions, then crop. Longer files unlock a separate Auto-split tab for bell-detected
                          clips.
                        </CardDescription>
                      </div>
                      <div className="flex flex-wrap items-center justify-end gap-2">
                        {durationKnown ? (
                          <>
                            <Badge className="border-0 bg-white px-3 py-1 text-xs font-semibold text-slate-700 shadow-sm ring-1 ring-slate-200/90">
                              <Clock3 className="mr-1.5 inline h-3.5 w-3.5 text-cyan-600" />
                              {formatRuntime(videoDurationSeconds)}
                            </Badge>
                            <Badge
                              className={
                                showCropWorkflow
                                  ? 'border-0 bg-gradient-to-r from-violet-600 to-indigo-600 px-3 py-1 text-xs font-semibold text-white shadow-md'
                                  : 'border-0 bg-slate-800 px-3 py-1 text-xs font-semibold text-white shadow-md'
                              }
                            >
                              {showCropWorkflow
                                ? isLongRecording
                                  ? 'Multi-student (5+ min)'
                                  : 'Short recording (long mode)'
                                : 'Single student'}
                            </Badge>
                          </>
                        ) : (
                          <Badge className="border-0 bg-slate-100 px-3 py-1 text-xs font-medium text-slate-500">
                            Duration loading…
                          </Badge>
                        )}
                      </div>
                    </div>
                  </CardHeader>
                  <CardContent className="pt-6">
                    {currentVideoUrl ? (
                      <div className="space-y-5">
                        <video
                          ref={videoPlayerRef}
                          controls
                          preload="metadata"
                          onLoadedMetadata={handleVideoLoadedMetadata}
                          onTimeUpdate={handleVideoTimeUpdate}
                          className="aspect-video w-full rounded-2xl border border-slate-200/90 bg-black shadow-inner"
                          src={currentVideoUrl}
                        >
                          {currentSubtitleUrl ? (
                            <track
                              key={currentSubtitleUrl}
                              kind="subtitles"
                              srcLang="en"
                              label="WhisperX Subtitles"
                              src={currentSubtitleUrl}
                              default
                            />
                          ) : null}
                        </video>

                        {showCropWorkflow ? (
                          <Tabs
                            defaultValue={clipEditsLocked ? 'auto' : 'manual'}
                            key={`crop-workflow-${session?.id || 'anon'}`}
                            className="w-full"
                          >
                            <TabsList
                              className={`grid h-auto w-full gap-2 rounded-2xl border border-slate-200/90 bg-slate-100/90 p-2 shadow-inner ${
                                clipEditsLocked ? 'grid-cols-1' : 'grid-cols-2'
                              }`}
                            >
                              {!clipEditsLocked ? (
                                <TabsTrigger
                                  value="manual"
                                  className="h-auto min-h-[2.75rem] justify-center gap-2 py-2.5 text-xs sm:text-sm"
                                >
                                  <Scissors className="h-4 w-4 shrink-0 opacity-90" />
                                  Manual crop
                                </TabsTrigger>
                              ) : null}
                              <TabsTrigger
                                value="auto"
                                className="h-auto min-h-[2.75rem] justify-center gap-2 py-2.5 text-xs sm:text-sm"
                              >
                                <Sparkles className="h-4 w-4 shrink-0 opacity-90" />
                                Auto-split
                              </TabsTrigger>
                            </TabsList>
                            {!clipEditsLocked ? (
                              <TabsContent value="manual" className="mt-4">
                                {manualCropControls}
                              </TabsContent>
                            ) : null}
                            <TabsContent value="auto" className="mt-4 space-y-4">
                              <div className="rounded-xl border border-violet-200/80 bg-gradient-to-br from-violet-50/80 to-white p-4 text-sm leading-relaxed text-violet-950 shadow-sm">
                                <span className="font-semibold text-violet-900">Auto-split</span>{' '}
                                {clipEditsLocked
                                  ? 'clips have been exported. Crops are locked; run assessments per clip below.'
                                  : 'runs immediately after upload to estimate student boundaries. Review the suggested ranges here, then switch to Manual crop to nudge boundaries before exporting clips.'}
                              </div>
                              <StudentClipSplitterCard
                                {...clipSplitterSharedProps}
                                lockEdits={clipEditsLocked}
                                title="Auto-detected clips"
                                description={
                                  clipEditsLocked
                                    ? 'Clips exported. Review labels and downloads (editing is locked).'
                                    : 'Detected boundaries from the bell / hybrid detector (clips are not generated yet)'
                                }
                              />
                            </TabsContent>
                          </Tabs>
                        ) : (
                          <div className="rounded-2xl border border-slate-200/90 bg-slate-50/90 p-4 text-sm text-slate-600 shadow-sm">
                            Single-student mode is active. Cropping tools are hidden to keep the assessment workflow
                            focused on transcript + scoring.
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="flex aspect-video w-full items-center justify-center rounded-2xl border border-dashed border-slate-300 bg-slate-100 text-slate-500">
                        No video loaded
                      </div>
                    )}
                  </CardContent>
                </Card>
                </div>

                {showAssessmentPanels ? (
                  <>
                    <Card className="border-slate-200 bg-white shadow-sm">
                      <CardHeader>
                        <CardTitle className="flex items-center gap-2 text-base">
                          Transcript Timeline
                          {session?.corpus?.name && (
                            <span
                              className="rounded-full bg-cyan-50 px-2 py-0.5 text-[11px] font-medium text-cyan-700"
                              title={`Transcription biased with the "${session.corpus.name}" corpus (${(session.corpus.terms || []).length} terms).`}
                            >
                              Corpus: {session.corpus.name}
                            </span>
                          )}
                        </CardTitle>
                        <CardDescription>
                          Click a line to jump video playback directly to that segment timestamp.
                        </CardDescription>
                      </CardHeader>
                      <CardContent>
                        {transcriptSegments.length === 0 ? (
                          <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm text-slate-500">
                            Transcript is not available yet. Start processing to populate this timeline.
                          </div>
                        ) : (
                          <div ref={timelineContainerRef} className="max-h-[65vh] space-y-2 overflow-y-auto pr-2">
                            {transcriptSegments.map((segment) => (
                              <button
                                type="button"
                                key={segment.id}
                                ref={(node) => {
                                  if (node) {
                                    timelineSegmentRefs.current.set(segment.id, node);
                                    return;
                                  }

                                  timelineSegmentRefs.current.delete(segment.id);
                                }}
                                onClick={() => seekToSegment(segment)}
                                className={`w-full rounded-xl border p-3 text-left transition ${
                                  activeSegmentId === segment.id
                                    ? 'border-cyan-400 bg-cyan-50'
                                    : 'border-slate-200 bg-slate-50 hover:border-cyan-300 hover:bg-cyan-50/50'
                                }`}
                              >
                                <div className="mb-1 flex items-center justify-between text-xs text-slate-500">
                                  <span className="font-semibold text-slate-700">{prettySpeaker(segment.speaker)}</span>
                                  <span>
                                    {segment.startLabel} - {segment.endLabel}
                                  </span>
                                </div>
                                <div className="text-sm text-slate-800">{segment.text}</div>
                              </button>
                            ))}
                          </div>
                        )}
                      </CardContent>
                    </Card>

                    <Tabs defaultValue="transcript" className="w-full">
                  <TabsList className="grid h-auto w-full grid-cols-2 gap-2 rounded-2xl border border-slate-200/90 bg-gradient-to-r from-slate-50 via-white to-slate-100 p-2 shadow-inner sm:grid-cols-4">
                    <TabsTrigger value="transcript" className="gap-2 py-2.5 text-xs sm:text-sm">
                      <MessageSquare className="h-4 w-4 shrink-0 opacity-80" />
                      Transcript
                    </TabsTrigger>
                    <TabsTrigger value="scores" className="gap-2 py-2.5 text-xs sm:text-sm">
                      <ClipboardCheck className="h-4 w-4 shrink-0 opacity-80" />
                      Content Scores
                    </TabsTrigger>
                    <TabsTrigger value="communication-scores" className="gap-2 py-2.5 text-xs sm:text-sm">
                      <Sparkles className="h-4 w-4 shrink-0 opacity-80" />
                      Communication Scores
                    </TabsTrigger>
                    <TabsTrigger value="feedback" className="gap-2 py-2.5 text-xs sm:text-sm">
                      <Brain className="h-4 w-4 shrink-0 opacity-80" />
                      Feedback
                    </TabsTrigger>
                  </TabsList>

                  <TabsContent value="transcript">
                    <Card className="border-slate-200 bg-white shadow-sm">
                      <CardHeader>
                        <CardTitle className="text-base">Transcript JSON View (Segment Level)</CardTitle>
                        <CardDescription>
                          Rendered from WhisperX JSON using segment speaker, text, and timestamps.
                        </CardDescription>
                      </CardHeader>
                      <CardContent>
                        <div className="mb-3 flex flex-wrap gap-2">
                          <Button
                            variant="outline"
                            className="gap-2"
                            onClick={downloadTranscriptJson}
                            disabled={!transcriptSegments.length}
                          >
                            <Download className="h-4 w-4" />
                            Download Transcript JSON
                          </Button>
                        </div>

                        {transcriptSegments.length === 0 ? (
                          <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm text-slate-500">
                            No transcript segments yet.
                          </div>
                        ) : (
                          <div className="max-h-[32rem] space-y-2 overflow-y-auto pr-2">
                            {transcriptSegments.map((segment) => (
                              <div key={segment.id} className="rounded-xl border border-slate-200 bg-slate-50 p-3">
                                <div className="mb-2 flex items-center justify-between text-xs">
                                  <span className="rounded-full bg-white px-2 py-0.5 font-semibold text-slate-700">
                                    {prettySpeaker(segment.speaker)}
                                  </span>
                                  <button
                                    type="button"
                                    onClick={() => seekToSegment(segment)}
                                    className="font-medium text-cyan-700 hover:text-cyan-900"
                                  >
                                    {segment.startLabel} - {segment.endLabel}
                                  </button>
                                </div>
                                <div className="text-sm leading-relaxed text-slate-800">{segment.text}</div>
                              </div>
                            ))}
                          </div>
                        )}
                      </CardContent>
                    </Card>
                  </TabsContent>

                  <TabsContent value="scores">
                    <Card className="border-slate-200 bg-white shadow-sm">
                      <CardHeader>
                        <CardTitle className="text-base">Content Scores (Rubric-Aligned)</CardTitle>
                        <CardDescription>
                          Clinical content rubric. Final result is determined by critical criteria and total Yes/No
                          performance.
                        </CardDescription>
                      </CardHeader>
                      <CardContent className="space-y-4">
                        <div className="flex flex-wrap gap-2">
                          <Button
                            variant="outline"
                            className="gap-2"
                            onClick={downloadScoreSheet}
                            disabled={!canDownloadScoreSheet}
                          >
                            <Download className="h-4 w-4" />
                            Download Score Sheet
                          </Button>
                        </div>

                        {!scoringSummary && !isDemoFallback && session?.status === 'completed' && !aiCriteria.length ? (
                          <div className="rounded-xl border border-amber-200 bg-gradient-to-br from-amber-50 to-white p-4 text-sm leading-relaxed text-amber-950 shadow-sm">
                            <p className="font-semibold text-amber-900">No rubric JSON attached to this session.</p>
                            <p className="mt-2 text-amber-900/85">
                              Confirm the API has scoring enabled (default on), set{' '}
                              <code className="rounded-md bg-white px-1.5 py-0.5 font-mono text-xs shadow-sm ring-1 ring-amber-100">
                                OPENROUTER_API_KEY
                              </code>{' '}
                              for the scorer, then re-run process or refresh scores.
                            </p>
                          </div>
                        ) : null}
                        {scoringSummary ? (
                          <div
                            className={`rounded-xl border p-4 ${
                              scoringSummary.passFail === 'Pass'
                                ? 'border-emerald-200 bg-emerald-50'
                                : 'border-rose-200 bg-rose-50'
                            }`}
                          >
                            <div className="text-sm text-slate-700">Overall Result</div>
                            <div
                              className={`text-3xl font-bold ${
                                scoringSummary.passFail === 'Pass' ? 'text-emerald-800' : 'text-rose-800'
                              }`}
                            >
                              {scoringSummary.passFail}
                            </div>
                            <div className="mt-2 text-sm text-slate-700">
                              Yes: {scoringSummary.yesCount} / {scoringSummary.totalCriteria} | Critical Yes:{' '}
                              {scoringSummary.criticalYes} / {scoringSummary.criticalTotal}
                            </div>
                            {scoringSummary.decisionReason ? (
                              <div className="mt-2 text-xs text-slate-600">{scoringSummary.decisionReason}</div>
                            ) : null}
                          </div>
                        ) : isDemoFallback ? (
                          <div className="rounded-xl border border-cyan-200 bg-cyan-50 p-4">
                            <div className="text-sm text-cyan-700">Demo weighted preview</div>
                            <div className="text-3xl font-bold text-cyan-900">{fallbackOverallScore} / 100</div>
                          </div>
                        ) : (
                          <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/90 p-6 text-center shadow-inner">
                            <p className="text-sm font-medium text-slate-700">
                              Rubric-aligned scores appear here after transcription finishes.
                            </p>
                            <p className="mt-2 text-xs text-slate-500">
                              Run the pipeline with a valid OpenRouter key to populate Pass/Fail and criteria.
                            </p>
                          </div>
                        )}

                        {aiCriteria.length > 0
                          ? aiCriteria.map((criterion) => {
                              const hasEvidence = Number.isFinite(criterion.timestampSeconds);
                              const evidenceLabel =
                                criterion.timestamp || (hasEvidence ? formatRuntime(criterion.timestampSeconds) : '');

                              return (
                                <div key={criterion.key} className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                                  <div className="mb-2 flex items-start justify-between gap-3">
                                    <div>
                                      <div className="font-semibold text-slate-900">{criterion.key}</div>
                                      <div className="mt-1 flex items-center gap-2 text-[11px] font-semibold uppercase">
                                        <span
                                          className={`rounded-full px-2 py-0.5 ${
                                            criterion.value === 'Yes'
                                              ? 'bg-emerald-100 text-emerald-700'
                                              : 'bg-rose-100 text-rose-700'
                                          }`}
                                        >
                                          {criterion.value}
                                        </span>
                                        <span
                                          className={`rounded-full px-2 py-0.5 ${
                                            criterion.isCritical
                                              ? 'bg-amber-100 text-amber-700'
                                              : 'bg-slate-200 text-slate-700'
                                          }`}
                                        >
                                          {criterion.isCritical ? 'Critical' : 'Non-Critical'}
                                        </span>
                                      </div>
                                    </div>
                                    <div className="flex flex-col items-end gap-2">
                                      <Button
                                        size="sm"
                                        variant="outline"
                                        onClick={() => seekToSeconds(criterion.timestampSeconds)}
                                        disabled={!hasEvidence}
                                      >
                                        View evidence
                                      </Button>
                                      <div className="text-[11px] text-slate-500">
                                        {hasEvidence ? `Timestamp: ${evidenceLabel}` : 'No timestamp'}
                                      </div>
                                    </div>
                                  </div>
                                  <div className="text-sm text-slate-700">
                                    {criterion.reason || 'No explicit evidence note provided by the model.'}
                                  </div>
                                </div>
                              );
                            })
                          : fallbackScores.map((criterion) => (
                              <div key={criterion.key} className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                                <div className="mb-2 flex items-start justify-between gap-3">
                                  <div>
                                    <div className="font-semibold text-slate-900">{criterion.key}</div>
                                    <div className="text-xs text-slate-500">{criterion.desc}</div>
                                  </div>
                                  <div className="text-right">
                                    <div className="text-xs text-slate-500">
                                      Weight {Math.round(criterion.weight * 100)}%
                                    </div>
                                    <div className="text-xl font-bold text-slate-900">{criterion.score}/100</div>
                                  </div>
                                </div>
                                <Progress value={criterion.score} className="h-2.5 bg-slate-200" />
                              </div>
                            ))}

                        {scoreReport?.overall_summary ? (
                          <div className="rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-700">
                            <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                              Model Summary
                            </div>
                            {scoreReport.overall_summary}
                          </div>
                        ) : null}
                      </CardContent>
                    </Card>
                  </TabsContent>

                  <TabsContent value="communication-scores">
                    <CommunicationScoresTab
                      criteria={communicationCriteria}
                      summary={communicationSummary}
                      overallSummary={communicationPayload?.overall_summary || ''}
                      sessionStatus={session?.status || null}
                      onSeek={seekToSeconds}
                    />
                  </TabsContent>

                  <TabsContent value="feedback">
                    <Card className="border-slate-200 bg-white shadow-sm">
                      <CardHeader>
                        <CardTitle className="text-base">Feedback</CardTitle>
                        <CardDescription>
                          Actionable guidance aligned with transcript behavior and rubric expectations.
                        </CardDescription>
                      </CardHeader>
                      <CardContent className="space-y-4 text-sm text-slate-800">
                        {keepStartStop ? (
                          <>
                            <FeedbackBlock
                              title="Keep"
                              lines={[
                                keepStartStop.keep || DEFAULT_KEEP_START_STOP.keep,
                              ]}
                            />

                            <FeedbackBlock
                              title="Start"
                              lines={[
                                keepStartStop.start || DEFAULT_KEEP_START_STOP.start,
                              ]}
                            />

                            <FeedbackBlock
                              title="Stop"
                              lines={[
                                keepStartStop.stop || DEFAULT_KEEP_START_STOP.stop,
                              ]}
                            />
                          </>
                        ) : (
                          <>
                            <FeedbackBlock
                              title="Strengths"
                              lines={[
                                'Maintains consistent patient-facing tone and polite transitions.',
                                'Uses structured explanations and confirms understanding.',
                                'Keeps interaction flowing with clear progression through questions.',
                              ]}
                            />

                            <FeedbackBlock
                              title="Areas to Improve"
                              lines={[
                                'Tighten phrasing in complex medication advice to reduce repetition.',
                                'Add explicit teach-back checkpoints after key counseling instructions.',
                                'Close with a concise summary of red flags and follow-up timing.',
                              ]}
                            />

                            <FeedbackBlock
                              title="Suggested Practice"
                              lines={[
                                'Practice 60-second medication summaries using plain language.',
                                'Use one verification question every 2-3 recommendation blocks.',
                                'End with a short recap: what to use, how to use, when to seek help.',
                              ]}
                            />
                          </>
                        )}
                      </CardContent>
                    </Card>
                  </TabsContent>
                    </Tabs>
                  </>
                ) : (
                  <Card className="border-slate-200 bg-white shadow-sm">
                    <CardHeader>
                      <CardTitle className="text-base">Long Video Workflow</CardTitle>
                      <CardDescription>Transcript, scores, and feedback appear per clip after export.</CardDescription>
                    </CardHeader>
                    <CardContent className="text-sm text-slate-600">
                      Export clips first, then run assessments per student to view transcript and scoring details.
                    </CardContent>
                  </Card>
                )}

                {showClipAssessmentPanel && isLoadingClipSummaries && !clipSummaries && !demoLongVideoSummaries ? (
                  <Card className="border-slate-200 bg-white shadow-sm">
                    <CardContent className="flex items-center gap-3 py-4 text-sm text-slate-500">
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Building cohort summary charts...
                    </CardContent>
                  </Card>
                ) : null}
                {showClipAssessmentPanel ? (
                  <LongVideoSummaryCharts
                    data={demoLongVideoSummaries || clipSummaries}
                  />
                ) : null}
              </div>

              <div className="space-y-6">
                <Card className="border-slate-200 bg-white shadow-sm">
                  <CardHeader>
                    <CardTitle className="text-base">Run Status</CardTitle>
                    <CardDescription>Local model execution details</CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-3 text-sm">
                    <StatusRow label="Runtime" value={`${formatRuntime(runtimeSeconds)}`} />
                    <StatusRow
                      label="Mode"
                      value={isDemoFallback ? 'Demo fallback (model skipped)' : `WhisperX ${currentModeLabel}`}
                    />
                    {/* <StatusRow
                      label="Audio Output"
                      value={session?.outputs?.audio?.fileName || 'Waiting...'}
                    />
                    <StatusRow
                      label="Case Study"
                      value={session?.files?.caseStudy?.fileName || 'Using fallback case study'}
                    />
                    <StatusRow
                      label="Subtitle SRT"
                      value={session?.outputs?.subtitle?.fileName || 'Waiting...'}
                    />
                    <StatusRow
                      label="Transcript JSON"
                      value={session?.outputs?.transcript?.fileName || 'Waiting...'}
                    />
                    <StatusRow
                      label="Audio Prof JSON"
                      value={session?.outputs?.audioProfessionalism?.fileName || 'Waiting...'}
                    />
                    <StatusRow label="Score JSON" value={session?.outputs?.scores?.fileName || 'Waiting...'} /> */}
                    <StatusRow
                      label="AI scoring"
                      value={
                        isDemoFallback && scoreReport
                          ? 'Demo bundle'
                          : session?.outputs?.scores?.fileName
                            ? 'Completed'
                            : session?.status === 'completed'
                              ? 'Not produced — check OpenRouter key'
                              : 'Runs after transcription'
                      }
                    />
                  </CardContent>
                </Card>

                {/* {showCropWorkflow ? (
                  <Card className="border-violet-100 bg-gradient-to-br from-violet-50/50 via-white to-indigo-50/30 shadow-sm ring-1 ring-violet-100/70">
                    <CardHeader className="pb-3">
                      <CardTitle className="flex items-center gap-2 text-base text-violet-950">
                        <Sparkles className="h-5 w-5 text-violet-600" />
                        Clip studio (sidebar)
                      </CardTitle>
                      <CardDescription className="text-violet-900/75">
                        Full clip list and trim controls are under the player in the{' '}
                        <span className="font-semibold text-violet-900">Auto-split</span> tab. Use this panel for a quick
                        reminder while you work.
                      </CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-2 pt-0 text-sm text-violet-950/85">
                      <p className="rounded-xl border border-violet-100 bg-white/70 p-3 leading-relaxed shadow-sm">
                        {videoClips.length > 0 ? (
                          <>
                            <span className="font-semibold">{videoClips.length}</span> clip
                            {videoClips.length === 1 ? '' : 's'} detected — open{' '}
                            <span className="font-medium">Auto-split</span> to review, then export clips when ready.
                          </>
                        ) : (
                          <>
                            After processing a long recording, clips appear here and in{' '}
                            <span className="font-medium">Auto-split</span>. Refine boundaries anytime in{' '}
                            <span className="font-medium">Manual crop</span>.
                          </>
                        )}
                      </p>
                    </CardContent>
                  </Card>
                ) : (
                  <Card className="border-slate-200 bg-white shadow-sm">
                    <CardHeader>
                      <CardTitle className="text-base">Single Student Mode</CardTitle>
                      <CardDescription>Clip splitting is disabled for standard assessments.</CardDescription>
                    </CardHeader>
                    <CardContent className="text-sm text-slate-600">
                      Use the workflow tabs on the left to run transcription and scoring for the full recording.
                    </CardContent>
                  </Card>
                )} */}

                {showClipAssessmentPanel ? (
                  <Card className="border-slate-200 bg-white shadow-sm">
                    <CardHeader>
                      <CardTitle className="text-base">Clip Assessments</CardTitle>
                      <CardDescription>
                        Run full scoring on each exported student clip, or tick clips to queue them together.
                      </CardDescription>
                      <div className="flex flex-wrap items-center gap-2 pt-2">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={toggleSelectAllClips}
                          disabled={isQueueingSelectedClips || batchSelectableClipIds.length === 0}
                          title={
                            allClipsSelected
                              ? 'Clear the current clip selection'
                              : 'Select every clip that can be queued'
                          }
                        >
                          {allClipsSelected ? 'Unselect all' : 'Select all'}
                        </Button>
                        <Button
                          size="sm"
                          onClick={runSelectedClipAssessments}
                          disabled={
                            isProcessing || isQueueingSelectedClips || selectedRunnableClipIds.length === 0
                          }
                          title={
                            selectedRunnableClipIds.length === 0
                              ? 'Tick at least one clip to queue its assessment.'
                              : `Queue scoring for ${selectedRunnableClipIds.length} selected clip${
                                  selectedRunnableClipIds.length === 1 ? '' : 's'
                                }`
                          }
                        >
                          {isQueueingSelectedClips ? (
                            <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                          ) : (
                            <PlayCircle className="mr-1 h-3.5 w-3.5" />
                          )}
                          Run Selected Assessments
                          {selectedRunnableClipIds.length > 0 ? ` (${selectedRunnableClipIds.length})` : ''}
                        </Button>
                      </div>
                    </CardHeader>
                    <CardContent className="space-y-3">
                      {videoClips.map((clip, index) => {
                        if (clip.kind === INTERMISSION_KIND) {
                          // Intermissions are timeline markers (empty room /
                          // lone person) — greyed out, never assessable.
                          const personNote =
                            clip.personCount === 0
                              ? 'No people detected'
                              : clip.personCount === 1
                                ? '1 person — not a session'
                                : 'Marked as intermission';
                          return (
                            <div
                              key={clip.id}
                              className="rounded-xl border border-dashed border-slate-300 bg-slate-100 p-3 opacity-70"
                              title="Intermission segments cannot be assessed."
                            >
                              <div className="flex items-center justify-between gap-2">
                                <div>
                                  <div className="text-sm font-semibold italic text-slate-500">
                                    {clip.label || 'Intermission'}
                                  </div>
                                  <div className="text-xs text-slate-400">
                                    {formatRuntime(clip.start)} - {formatRuntime(clip.end)}
                                  </div>
                                </div>
                                <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[11px] font-semibold text-slate-500">
                                  {personNote}
                                </span>
                              </div>
                            </div>
                          );
                        }
                        const runState = clipAssessmentRuns[clip.id] || { status: 'idle' };
                        const isCompleted = runState.status === 'completed' && Boolean(runState.sessionId);
                        // Child session id backing a running clip, used to re-open its progress overlay.
                        const progressSessionId = runState.sessionId || clipAssessmentIndex[clip.id]?.sessionId || null;
                        const statusLabel =
                          runState.status === 'running'
                            ? 'Running'
                            : runState.status === 'completed'
                              ? 'Completed'
                              : runState.status === 'failed'
                                ? 'Failed'
                                : 'Ready';
                        const statusClass =
                          runState.status === 'completed'
                            ? 'bg-emerald-100 text-emerald-700'
                            : runState.status === 'failed'
                              ? 'bg-rose-100 text-rose-700'
                              : runState.status === 'running'
                                ? 'bg-amber-100 text-amber-700'
                                : 'bg-slate-100 text-slate-700';

                        return (
                          <div key={clip.id} className="rounded-xl border border-slate-200 bg-slate-50 p-3">
                            <div className="flex items-center justify-between gap-2">
                              <div className="flex min-w-0 items-start gap-2.5">
                                <input
                                  type="checkbox"
                                  className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer accent-slate-900 disabled:cursor-not-allowed disabled:opacity-40"
                                  checked={selectedClipAssessmentIds.has(clip.id)}
                                  disabled={runState.status === 'running' || isQueueingSelectedClips}
                                  onChange={() => toggleClipSelected(clip.id)}
                                  aria-label={`Select ${clip.label || `Student ${index + 1}`} for batch assessment`}
                                  title={
                                    runState.status === 'running'
                                      ? 'Already queued — available again when this run finishes.'
                                      : 'Include this clip in "Run Selected Assessments"'
                                  }
                                />
                                <div className="min-w-0">
                                  <div className="text-sm font-semibold text-slate-900">
                                    {clip.label || `Student ${index + 1}`}
                                  </div>
                                  <div className="text-xs text-slate-500">
                                    {formatRuntime(clip.start)} - {formatRuntime(clip.end)}
                                  </div>
                                </div>
                              </div>
                              <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${statusClass}`}>
                                {statusLabel}
                              </span>
                            </div>
                            <div className="mt-2 flex flex-wrap items-center gap-2">
                              {(() => {
                                // Delete button shared by the completed/failed
                                // states — wipes the child session's scores and
                                // returns the clip row to "Run assessment".
                                const deleteButton = (
                                  <Button
                                    size="sm"
                                    variant="ghost"
                                    onClick={() => deleteClipAssessment(clip, progressSessionId)}
                                    disabled={isProcessing || deletingSessionId === progressSessionId}
                                    title="Delete this clip's assessment and scores"
                                    className="text-rose-600 hover:bg-rose-50 hover:text-rose-700"
                                  >
                                    {deletingSessionId === progressSessionId ? (
                                      <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                                    ) : (
                                      <Trash2 className="mr-1 h-3.5 w-3.5" />
                                    )}
                                    Delete
                                  </Button>
                                );
                                const rerunButton = (
                                  <Button
                                    size="sm"
                                    variant="ghost"
                                    onClick={() => rerunClipAssessment(clip, progressSessionId)}
                                    disabled={isProcessing}
                                    title="Re-run assessment for this clip (keeps the same record)"
                                    className="text-slate-700 hover:bg-slate-100"
                                  >
                                    <RotateCw className="mr-1 h-3.5 w-3.5" />
                                    Re-run
                                  </Button>
                                );

                                if (isCompleted) {
                                  return (
                                    <>
                                      <Button
                                        size="sm"
                                        variant="outline"
                                        onClick={() => openClipAssessmentView(clip, runState)}
                                        disabled={isProcessing}
                                      >
                                        View
                                      </Button>
                                      {rerunButton}
                                      {deleteButton}
                                    </>
                                  );
                                }
                                if (runState.status === 'running') {
                                  // Blocked while in flight (same rule as the
                                  // session list): show the live stage instead
                                  // of opening a half-processed child session.
                                  const childEntry = progressSessionId
                                    ? sessionIndex.find((entry) => String(entry.id) === String(progressSessionId))
                                    : null;
                                  const stage = childEntry ? describeProcessingStage(childEntry) : null;
                                  return (
                                    <Button
                                      size="sm"
                                      variant="outline"
                                      className="gap-1"
                                      disabled
                                      title="Available when this clip's assessment completes."
                                    >
                                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                      {stage ? `${formatProcessingStageLabel(stage)}…` : 'Starting…'}
                                    </Button>
                                  );
                                }
                                if (runState.status === 'failed' && progressSessionId) {
                                  // A failed child still exists as a record —
                                  // offer to re-run it in place or delete it.
                                  return (
                                    <>
                                      {rerunButton}
                                      {deleteButton}
                                    </>
                                  );
                                }
                                return (
                                  <Button
                                    size="sm"
                                    onClick={() => runClipAssessment(clip)}
                                    disabled={isProcessing}
                                  >
                                    Run assessment
                                  </Button>
                                );
                              })()}
                              {runState.status === 'failed' && runState.error ? (
                                <span className="text-xs text-rose-600">{runState.error}</span>
                              ) : null}
                            </div>
                          </div>
                        );
                      })}
                    </CardContent>
                  </Card>
                ) : null}

                {/* <Card className="border-slate-200 bg-white shadow-sm">
                  <CardHeader>
                    <CardTitle className="text-base">Transcript Stats</CardTitle>
                    <CardDescription>Segment-level quick counts</CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <Metric label="Segments" value={String(transcriptSegments.length)} />
                    <Metric
                      label="Distinct Speakers"
                      value={String(new Set(transcriptSegments.map((segment) => segment.speaker)).size)}
                    />
                    <Metric
                      label="Longest Segment"
                      value={`${Math.max(
                        0,
                        ...transcriptSegments.map((segment) => Math.round(segment.end - segment.start))
                      )}s`}
                    />
                  </CardContent>
                </Card> */}

                <Card className="border-slate-200 bg-white shadow-sm">
                  <CardHeader>
                    <CardTitle className="text-base">Audio Professionalism (openSMILE)</CardTitle>
                    <CardDescription>Student speech pacing, pauses, and voice features</CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-3 text-sm">
                    {audioProfPayload ? (
                      <>
                        <StatusRow
                          label="Student speaker"
                          value={audioProfPayload?.student_speaker?.id || 'Unknown'}
                        />
                        <StatusRow
                          label="Talk ratio"
                          value={formatMetricValue(audioProfMetrics?.student_talk_ratio, 2)}
                        />
                        <StatusRow
                          label="Words per min"
                          value={formatMetricValue(audioProfMetrics?.words_per_minute, 1)}
                        />
                        <StatusRow
                          label="Filler words / min"
                          value={formatMetricValue(audioProfMetrics?.filler_words_per_minute, 2)}
                        />
                        <StatusRow
                          label="Long pauses (2s+)"
                          value={formatMetricValue(audioProfMetrics?.long_student_pauses_over_2s, 0)}
                        />
                        <StatusRow
                          label="Interruptions"
                          value={formatMetricValue(audioProfMetrics?.interruptions_count, 0)}
                        />

                        <details className="rounded-xl border border-slate-200 bg-slate-50 p-3">
                          <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-slate-600">
                            OpenSMILE features
                          </summary>
                          <div className="mt-2 space-y-2">
                            <StatusRow
                              label="Pitch mean (semitone)"
                              value={formatMetricValue(audioProfFeatures?.pitch_mean_semitone, 2)}
                            />
                            <StatusRow
                              label="Pitch range (semitone)"
                              value={formatMetricValue(audioProfFeatures?.pitch_range_semitone, 2)}
                            />
                            <StatusRow
                              label="Loudness mean"
                              value={formatMetricValue(audioProfFeatures?.loudness_mean, 3)}
                            />
                            <StatusRow
                              label="Loudness std"
                              value={formatMetricValue(audioProfFeatures?.loudness_std, 3)}
                            />
                            <StatusRow
                              label="HNR (dB)"
                              value={formatMetricValue(audioProfFeatures?.hnr_db, 2)}
                            />
                          </div>
                        </details>

                        {audioProfWarnings.length > 0 ? (
                          <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
                            {audioProfWarnings.join(' ')}
                          </div>
                        ) : null}
                      </>
                    ) : (
                      <div className="space-y-2">
                        {audioProfLoadError ? (
                          <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-xs text-rose-800">
                            {audioProfLoadError}
                          </div>
                        ) : null}
                        <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/90 p-4 text-center text-sm text-slate-600">
                          Audio professionalism output appears after extraction finishes. If this stays empty,
                          check that openSMILE is installed and ENABLE_AUDIO_PROFESSIONALISM is true.
                        </div>
                      </div>
                    )}
                  </CardContent>
                </Card>
              </div>
            </div>
          </section>
        )}
      </main>

      <AnimatePresence>
        {showConfirmStart && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/45 px-4"
            onClick={cancelStartAssessment}
          >
            <motion.div
              initial={{ scale: 0.96, y: 14 }}
              animate={{ scale: 1, y: 0 }}
              exit={{ scale: 0.96, y: 14 }}
              className="w-full max-w-md"
              onClick={(event) => event.stopPropagation()}
            >
              <Card className="border-slate-200 bg-white shadow-xl">
                <CardHeader>
                  <CardTitle className="flex items-center gap-2 text-lg">
                    <ClipboardCheck className="h-5 w-5 text-cyan-700" />
                    Confirm assessment
                  </CardTitle>
                  <CardDescription>
                    Review the files and name this session before processing starts.
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
                    {uploadFlow === 'long' && (
                      <div className="flex items-start gap-2 rounded-xl border border-slate-200 bg-slate-50 p-3">
                        {segmentationMethod === 'person' ? (
                          <Users className="mt-0.5 h-4 w-4 shrink-0 text-slate-500" />
                        ) : (
                          <BellRing className="mt-0.5 h-4 w-4 shrink-0 text-slate-500" />
                        )}
                        <div className="min-w-0">
                          <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                            Auto-split method
                          </div>
                          <div className="text-sm text-slate-800">
                            {segmentationMethod === 'person'
                              ? 'Human detection (AI vision, RT-DETR)'
                              : 'Bell detection (audio)'}
                          </div>
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
                    <p className="mt-1 text-[11px] text-slate-400">
                      Case-specific terms (e.g. nasal block, paracetamol) bias transcription and apply
                      to every clip marked in this session.
                    </p>
                  </div>

                  <div>
                    <label htmlFor="session-name-input" className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                      Session name
                    </label>
                    <input
                      id="session-name-input"
                      type="text"
                      value={sessionNameInput}
                      onChange={(event) => setSessionNameInput(event.target.value)}
                      maxLength={80}
                      autoFocus
                      placeholder="Leave blank to auto-generate"
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          confirmStartAssessment();
                        }
                      }}
                      className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 outline-none focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100"
                    />
                    <p className="mt-1 text-[11px] text-slate-400">
                      A unique number is appended automatically if the name already exists.
                    </p>
                  </div>

                  <div className="flex items-center justify-end gap-2 pt-1">
                    <Button variant="outline" size="sm" onClick={cancelStartAssessment}>
                      Cancel
                    </Button>
                    <Button
                      size="sm"
                      className="gap-2 bg-gradient-to-r from-cyan-600 to-blue-700 text-white hover:from-cyan-700 hover:to-blue-800"
                      onClick={confirmStartAssessment}
                    >
                      <Wand2 className="h-4 w-4" />
                      Start assessment
                    </Button>
                  </div>
                </CardContent>
              </Card>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {showCorpusManager && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-[60] flex items-center justify-center bg-slate-900/45 px-4"
            onClick={() => setShowCorpusManager(false)}
          >
            <motion.div
              initial={{ scale: 0.96, y: 14 }}
              animate={{ scale: 1, y: 0 }}
              exit={{ scale: 0.96, y: 14 }}
              className="w-full max-w-lg"
              onClick={(event) => event.stopPropagation()}
            >
              <CorporaManager
                onClose={() => setShowCorpusManager(false)}
                onChanged={handleCorporaChanged}
                onCreated={(corpus) => {
                  // Creating a corpus from the picker usually means "use it now".
                  setSelectedCorpusId(corpus.id);
                }}
              />
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {(isUploading || isProcessing) && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/45 px-4"
          >
            <motion.div
              initial={{ scale: 0.96, y: 14 }}
              animate={{ scale: 1, y: 0 }}
              exit={{ scale: 0.96, y: 14 }}
              className="w-full max-w-md"
            >
              <Card className="border-slate-200 bg-white shadow-xl">
                <CardHeader>
                  <CardTitle className="flex items-center gap-2 text-lg">
                    <Loader2 className="h-5 w-5 animate-spin text-cyan-700" />
                    {isUploading ? 'Uploading Files' : 'Starting Job'}
                  </CardTitle>
                  <CardDescription>{processingMessage}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="rounded-xl border border-cyan-200 bg-cyan-50 p-4 text-center">
                    <div className="flex items-center justify-center gap-2 text-xs font-semibold uppercase tracking-wide text-cyan-700">
                      <Clock3 className="h-3.5 w-3.5" /> Runtime
                    </div>
                    <div className="mt-1 text-3xl font-bold text-cyan-900">{formatRuntime(runtimeSeconds)}</div>
                  </div>

                  <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
                    <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                      Live Console Line
                    </div>
                    <div className="mt-1 truncate font-mono text-xs text-slate-700">{liveLogLine}</div>
                  </div>

                  {processingStage === 'pipeline' ? (
                    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700">
                      <div className="mb-2 font-semibold uppercase tracking-wide text-slate-500">Milestones</div>
                      <div className="mb-2 text-[11px] text-slate-500">
                        started -&gt; converted to mp3 -&gt; transcript generated -&gt; scored
                      </div>
                      <MilestoneRow label="started" done={pipelineMilestones.started} />
                      <MilestoneRow label="converted to mp3" done={pipelineMilestones.convertedToMp3} />
                      <MilestoneRow label="transcript generated" done={pipelineMilestones.transcriptionComplete} />
                      <MilestoneRow label="scored" done={pipelineMilestones.scored} />
                    </div>
                  ) : (
                    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700">
                      <div className="mb-2 font-semibold uppercase tracking-wide text-slate-500">Auto-crop</div>
                      <div className="text-[11px] text-slate-500">
                        Detecting bell frequencies and estimating student clip boundaries.
                      </div>
                    </div>
                  )}

                  <Button
                    variant="ghost"
                    size="sm"
                    className="w-full text-slate-400 hover:text-slate-600"
                    onClick={() => {
                      setIsUploading(false);
                      setIsProcessing(false);
                    }}
                  >
                    Dismiss
                  </Button>
                </CardContent>
              </Card>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

const COMMUNICATION_LABEL_META = {
  All: {
    color: 'bg-emerald-100 text-emerald-700',
    barColor: 'bg-emerald-500',
    points: 3,
  },
  Most: {
    color: 'bg-cyan-100 text-cyan-700',
    barColor: 'bg-cyan-500',
    points: 2,
  },
  Some: {
    color: 'bg-amber-100 text-amber-700',
    barColor: 'bg-amber-500',
    points: 1,
  },
  None: {
    color: 'bg-rose-100 text-rose-700',
    barColor: 'bg-rose-500',
    points: 0,
  },
};

function CommunicationScoresTab({ criteria, summary, overallSummary, sessionStatus, onSeek }) {
  const hasCriteria = Array.isArray(criteria) && criteria.length > 0;

  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle className="text-base">Communication Scores</CardTitle>
        <CardDescription>
          Communication rubric scoring on a None / Some / Most / All scale. All=3, Most=2, Some=1, None=0.
          Pass threshold is {summary?.passThreshold ?? 11}/{summary?.maxScore ?? 21}.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {summary ? (
          <div
            className={`rounded-xl border p-4 ${
              summary.passFail === 'Pass'
                ? 'border-emerald-200 bg-emerald-50'
                : 'border-rose-200 bg-rose-50'
            }`}
          >
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div>
                <div className="text-sm text-slate-700">Overall Communication Result</div>
                <div
                  className={`text-3xl font-bold ${
                    summary.passFail === 'Pass' ? 'text-emerald-800' : 'text-rose-800'
                  }`}
                >
                  {summary.passFail}
                </div>
              </div>
              <div className="text-right">
                <div className="text-xs uppercase tracking-wide text-slate-500">Total</div>
                <div className="text-2xl font-bold text-slate-900">
                  {summary.totalScore} / {summary.maxScore}
                </div>
                <div className="text-[11px] text-slate-500">
                  Pass at {summary.passThreshold}/{summary.maxScore}
                </div>
              </div>
            </div>
            <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-slate-200">
              <div
                className={`h-full rounded-full ${
                  summary.passFail === 'Pass' ? 'bg-emerald-500' : 'bg-rose-500'
                }`}
                style={{
                  width: `${Math.max(0, Math.min(100, (summary.totalScore / Math.max(1, summary.maxScore)) * 100))}%`,
                }}
              />
            </div>
            {summary.decisionReason ? (
              <div className="mt-2 text-xs text-slate-600">{summary.decisionReason}</div>
            ) : null}
            {summary.labelCounts && Object.keys(summary.labelCounts).length ? (
              <div className="mt-3 flex flex-wrap gap-2 text-xs">
                {['All', 'Most', 'Some', 'None'].map((label) => {
                  const meta = COMMUNICATION_LABEL_META[label];
                  return (
                    <span
                      key={label}
                      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-semibold ${meta.color}`}
                    >
                      {label} · {Number(summary.labelCounts[label] || 0)}
                    </span>
                  );
                })}
              </div>
            ) : null}
          </div>
        ) : !hasCriteria ? (
          <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/90 p-6 text-center shadow-inner">
            <p className="text-sm font-medium text-slate-700">
              Communication scores appear here once the pipeline runs.
            </p>
            <p className="mt-2 text-xs text-slate-500">
              Make sure the communication scorer is enabled (set <code className="rounded bg-white px-1.5 py-0.5 text-[11px]">ENABLE_COMMUNICATION_SCORING=true</code>)
              and the rubric is loaded under Settings → Communication Rubric.
            </p>
            {sessionStatus ? (
              <p className="mt-2 text-[11px] text-slate-400">Current session status: {sessionStatus}</p>
            ) : null}
          </div>
        ) : null}

        {hasCriteria
          ? criteria.map((criterion) => {
              const meta = COMMUNICATION_LABEL_META[criterion.scoreLabel] || COMMUNICATION_LABEL_META.None;
              const hasEvidence = Number.isFinite(criterion.timestampSeconds);
              return (
                <div
                  key={criterion.id}
                  className="rounded-xl border border-slate-200 bg-slate-50 p-4 transition hover:border-purple-200 hover:bg-white"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="inline-flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-purple-500 to-cyan-500 text-xs font-bold text-white shadow">
                          {criterion.id}
                        </span>
                        <div className="font-semibold text-slate-900">{criterion.label}</div>
                      </div>
                      {criterion.section ? (
                        <div className="ml-9 text-[11px] uppercase tracking-wider text-slate-400">
                          {criterion.section}
                        </div>
                      ) : null}
                    </div>
                    <div className="flex flex-col items-end gap-1.5">
                      <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-bold uppercase ${meta.color}`}>
                        {criterion.scoreLabel}
                      </span>
                      <span className="text-[11px] text-slate-500">{criterion.points}/3 marks</span>
                    </div>
                  </div>

                  <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
                    <div className={`h-full rounded-full ${meta.barColor}`} style={{ width: `${(criterion.points / 3) * 100}%` }} />
                  </div>

                  {criterion.evidence ? (
                    <div className="mt-3 rounded-lg bg-white p-3 text-sm text-slate-700 ring-1 ring-slate-200">
                      {criterion.evidence}
                    </div>
                  ) : null}

                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => onSeek?.(criterion.timestampSeconds)}
                      disabled={!hasEvidence}
                    >
                      View evidence
                    </Button>
                    <span className="text-[11px] text-slate-500">
                      {hasEvidence ? `Timestamp: ${criterion.timestamp}` : 'No timestamp'}
                    </span>
                  </div>

                  {(criterion.indicatorsObserved.length
                    || criterion.indicatorsMissing.length
                    || criterion.indicatorsNotObservable.length) ? (
                    <details className="mt-3 rounded-lg border border-slate-200 bg-white p-3">
                      <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-slate-600">
                        Indicator breakdown
                      </summary>
                      <div className="mt-2 space-y-2 text-xs text-slate-700">
                        {criterion.indicatorsObserved.length ? (
                          <IndicatorList title="Observed" color="text-emerald-700" items={criterion.indicatorsObserved} />
                        ) : null}
                        {criterion.indicatorsMissing.length ? (
                          <IndicatorList title="Missing" color="text-rose-700" items={criterion.indicatorsMissing} />
                        ) : null}
                        {criterion.indicatorsNotObservable.length ? (
                          <IndicatorList
                            title="Not observable from audio/video"
                            color="text-slate-500"
                            items={criterion.indicatorsNotObservable}
                          />
                        ) : null}
                      </div>
                    </details>
                  ) : null}
                </div>
              );
            })
          : null}

        {overallSummary ? (
          <div className="rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-700">
            <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Model Summary
            </div>
            {overallSummary}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function IndicatorList({ title, items, color = 'text-slate-700' }) {
  if (!items?.length) {
    return null;
  }
  return (
    <div>
      <div className={`text-[11px] font-semibold uppercase tracking-wide ${color}`}>{title}</div>
      <ul className="mt-1 space-y-0.5 pl-3">
        {items.map((item, index) => (
          <li key={`${title}-${index}`} className="list-disc">
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}

function UploadCard({ icon, title, subtitle, fileName, onPick }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
      <div className="mb-3 flex items-center gap-3">
        <div className="rounded-xl bg-gradient-to-br from-cyan-600 to-blue-700 p-2.5 text-white">{icon}</div>
        <div>
          <div className="text-sm font-semibold text-slate-900">{title}</div>
          <div className="text-xs text-slate-500">{subtitle}</div>
        </div>
      </div>

      <button
        type="button"
        onClick={onPick}
        className="flex h-28 w-full flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-slate-300 bg-white text-slate-600 transition hover:border-cyan-500 hover:bg-cyan-50"
      >
        <UploadCloud className="h-6 w-6" />
        <span className="text-sm font-medium">Click to upload</span>
      </button>

      <div className="mt-3 truncate text-xs font-medium text-slate-700">
        {fileName || 'No file selected'}
      </div>
    </div>
  );
}

function FeatureRow({ icon, text }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
      <span className="text-cyan-700">{icon}</span>
      <span>{text}</span>
    </div>
  );
}

function FeedbackBlock({ title, lines }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
      <div className="mb-2 text-sm font-semibold text-slate-900">{title}</div>
      <div className="space-y-1.5 text-sm text-slate-800">
        {lines.map((line) => (
          <div key={line}>- {line}</div>
        ))}
      </div>
    </div>
  );
}

function StatusRow({ label, value }) {
  return (
    <div className="flex items-center justify-between rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
      <span className="text-slate-600">{label}</span>
      <span className="max-w-[60%] truncate font-medium text-slate-900" title={value}>
        {value}
      </span>
    </div>
  );
}

function Metric({ label, value }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-2xl font-bold text-slate-900">{value}</div>
    </div>
  );
}

function MilestoneRow({ label, done }) {
  return (
    <div className="flex items-center justify-between rounded-lg border border-slate-200 bg-white px-3 py-2">
      <span>{label}</span>
      <span className={`text-[11px] font-semibold uppercase ${done ? 'text-emerald-700' : 'text-slate-500'}`}>
        {done ? 'done' : 'pending'}
      </span>
    </div>
  );
}

function prettySpeaker(rawSpeaker) {
  if (!rawSpeaker) {
    return 'Speaker';
  }

  return rawSpeaker
    .toString()
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function clampNumber(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function formatRuntime(secondsInput) {
  const totalSeconds = Math.max(0, Math.floor(Number(secondsInput) || 0));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  }

  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function parseEvidenceTimestamp(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }

  const text = String(value || '').trim();
  if (!text) {
    return null;
  }

  const hmsMatch = text.match(/(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,3}))?/);
  if (hmsMatch) {
    const hours = Number(hmsMatch[1]);
    const minutes = Number(hmsMatch[2]);
    const seconds = Number(hmsMatch[3]);
    const millis = Number(hmsMatch[4] || 0);
    return hours * 3600 + minutes * 60 + seconds + millis / 1000;
  }

  const msMatch = text.match(/(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?/);
  if (msMatch) {
    const minutes = Number(msMatch[1]);
    const seconds = Number(msMatch[2]);
    const millis = Number(msMatch[3] || 0);
    return minutes * 60 + seconds + millis / 1000;
  }

  return null;
}

function formatMetricValue(value, digits = 2) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return '—';
  }

  const rounded = Number(numeric.toFixed(Math.max(0, digits)));
  return rounded.toString();
}

function StudentClipSplitterCard({
  videoClips,
  selectedClipId,
  setSelectedClipId,
  renamingClipId,
  renameClipLabel,
  selectedClip,
  cropDraft,
  setCropDraft,
  videoDurationSeconds,
  isRecropping,
  seekToSeconds,
  recropSelectedClip,
  lockEdits = false,
  title = 'Student clips',
  description = 'Bell-based auto-split plus per-clip trim',
}) {
  return (
    <Card className="border-slate-200 bg-white shadow-sm">
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {videoClips.length === 0 ? (
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm text-slate-500">
            No clips yet. Run processing (long recordings) or save manual segments.
          </div>
        ) : (
          <>
            <div className="max-h-60 space-y-2 overflow-y-auto pr-2">
              {videoClips.map((clip) => {
                if (clip.kind === 'intermission') {
                  // Greyed marker row — an intermission has no exported file
                  // and is not selectable/renamable/downloadable.
                  return (
                    <div
                      key={clip.id}
                      className="w-full rounded-xl border border-dashed border-slate-300 bg-slate-100 p-3 text-left opacity-70"
                      title="Intermission (break) — not a student clip."
                    >
                      <div className="mb-1 flex items-center justify-between gap-2 text-xs text-slate-400">
                        <span className="text-sm font-semibold italic text-slate-500">
                          {clip.label || 'Intermission'}
                        </span>
                        <span className="shrink-0 whitespace-nowrap">
                          {formatRuntime(clip.start)} - {formatRuntime(clip.end)}
                        </span>
                      </div>
                      <div className="text-xs text-slate-400">
                        {clip.personCount === 0
                          ? 'No people detected'
                          : clip.personCount === 1
                            ? '1 person — not a session'
                            : 'Marked as intermission'}
                      </div>
                    </div>
                  );
                }
                const isSelected = String(selectedClipId) === String(clip.id);
                const isRenaming = String(renamingClipId) === String(clip.id);
                return (
                  <div
                    key={clip.id}
                    onClick={() => setSelectedClipId(clip.id)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        setSelectedClipId(clip.id);
                      }
                    }}
                    className={`w-full cursor-pointer rounded-xl border p-3 text-left transition ${
                      isSelected
                        ? 'border-cyan-400 bg-cyan-50'
                        : 'border-slate-200 bg-slate-50 hover:border-cyan-300 hover:bg-cyan-50/50'
                    }`}
                  >
                    <div className="mb-2 flex items-center justify-between gap-2 text-xs text-slate-500">
                      <input
                        type="text"
                        defaultValue={clip.label || ''}
                        onClick={(event) => event.stopPropagation()}
                        onBlur={(event) => {
                          const nextLabel = event.target.value.trim();
                          if (nextLabel && nextLabel !== String(clip.label || '').trim()) {
                            renameClipLabel(clip.id, nextLabel);
                          }
                        }}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') {
                            event.preventDefault();
                            event.target.blur();
                          }
                        }}
                        placeholder={`Student ${videoClips.indexOf(clip) + 1}`}
                        disabled={isRenaming}
                        className="flex-1 rounded-md border border-transparent bg-transparent px-1 py-0.5 text-sm font-semibold text-slate-700 hover:border-slate-200 focus:border-cyan-400 focus:bg-white focus:outline-none focus:ring-1 focus:ring-cyan-400"
                      />
                      <span className="shrink-0 whitespace-nowrap">
                        {formatRuntime(clip.start)} - {formatRuntime(clip.end)}
                      </span>
                    </div>
                    <div className="flex items-center justify-between text-xs text-slate-600">
                      <span>{Math.max(0, clip.end - clip.start).toFixed(1)}s</span>
                      {clip.url ? (
                        <a
                          href={resolveMediaUrl(clip.url)}
                          download={clip.fileName}
                          onClick={(event) => event.stopPropagation()}
                          className="font-medium text-cyan-700 hover:text-cyan-900"
                        >
                          Download
                        </a>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>

            {selectedClip ? (
              <div className="space-y-3">
                {selectedClip.url ? (
                  <video
                    src={resolveMediaUrl(selectedClip.url)}
                    controls
                    className="w-full rounded-xl border border-slate-200 bg-black"
                  />
                ) : null}

                {lockEdits ? (
                  <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-sm text-slate-600">
                    Clips are finalized after export. Crop editing is locked for this session.
                  </div>
                ) : (
                  <>
                    <div className="text-sm font-semibold text-slate-800">
                      Edit crop for {selectedClip.label || 'clip'}
                    </div>

                    <div className="text-xs text-slate-600">
                      Draft: {formatRuntime(cropDraft.start)} - {formatRuntime(cropDraft.end)}
                    </div>

                    <div className="space-y-2">
                      <div className="text-xs text-slate-600">Start ({formatRuntime(cropDraft.start)})</div>
                      <input
                        type="range"
                        min={0}
                        max={Math.max(0, cropDraft.end - 0.1)}
                        step={0.1}
                        value={cropDraft.start}
                        onChange={(e) => {
                          const nextStart = Number(e.target.value);
                          setCropDraft((prev) => {
                            const safeEnd = Number(prev.end || 0);
                            const safeStart = clampNumber(nextStart, 0, safeEnd - 0.1);
                            return { ...prev, start: safeStart };
                          });
                        }}
                        disabled={!videoDurationSeconds || isRecropping}
                      />

                      <div className="text-xs text-slate-600">End ({formatRuntime(cropDraft.end)})</div>
                      <input
                        type="range"
                        min={Math.min(videoDurationSeconds, cropDraft.start + 0.1)}
                        max={Math.max(0, videoDurationSeconds)}
                        step={0.1}
                        value={cropDraft.end}
                        onChange={(e) => {
                          const nextEnd = Number(e.target.value);
                          setCropDraft((prev) => {
                            const safeStart = Number(prev.start || 0);
                            const safeEnd = clampNumber(nextEnd, safeStart + 0.1, videoDurationSeconds);
                            return { ...prev, end: safeEnd };
                          });
                        }}
                        disabled={!videoDurationSeconds || isRecropping}
                      />
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        variant="outline"
                        className="flex-1"
                        onClick={() => seekToSeconds(cropDraft.start)}
                        disabled={isRecropping || !videoDurationSeconds}
                      >
                        Preview start
                      </Button>

                      <Button
                        className="flex-1 bg-gradient-to-r from-cyan-600 to-blue-700 text-white hover:from-cyan-700 hover:to-blue-800"
                        onClick={recropSelectedClip}
                        disabled={isRecropping || !videoDurationSeconds}
                      >
                        {isRecropping ? 'Re-cropping...' : 'Save crop'}
                      </Button>
                    </div>
                  </>
                )}
              </div>
            ) : (
              <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-sm text-slate-500">
                Select a clip to edit.
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
