// Bundled demo fixtures.
//
// The two demo buttons on the landing screen build a finished workspace out of
// JSON shipped under /demo-resources, so the app can be shown without a
// backend behind it. Nothing here is reachable until one of those buttons is
// clicked, which is why it is a module of its own: the callers import it
// dynamically, so a normal login never downloads or parses any of it.
import { SessionStatus } from '@/lib/enums';
import { apiFetch } from '@/lib/apiFetch';

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
export const LONG_DEMO_CHILD_IDS = [
  '90f6d699-9ad9-4108-b83d-958311a373cb',
  '3d19b1f1-959e-4950-8766-014e0964e4c7',
  'eb5cb880-c855-4869-90de-685344781e54',
  '09dab304-cb05-4458-aec1-b35aa2c60639',
  '63972504-c9c5-4dda-9688-047eec287518',
];

/** Recordings at or above this length use bell/hybrid auto-split in the pipeline and show the Auto-split UI tab. */

async function readBundledDemoJson(fileName, label) {
  const response = await apiFetch(`${DEMO_RESOURCE_BASE}/${fileName}`, { reportConnection: false });
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
    status: SessionStatus.COMPLETED,
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
    status: SessionStatus.COMPLETED,
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

export async function loadBundledDemoResources() {
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

export async function loadBundledLongDemoResources() {
  const parentResponse = await apiFetch(`${LONG_DEMO_RESOURCE_BASE}/parent-session.json`, { reportConnection: false });
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
    status: SessionStatus.CROPPED,
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
        apiFetch(`${base}/session.json`, { reportConnection: false }),
        apiFetch(`${base}/transcript.json`, { reportConnection: false }),
        apiFetch(`${base}/scores.json`, { reportConnection: false }),
        apiFetch(`${base}/communication-scores.json`, { reportConnection: false }),
        apiFetch(`${base}/audio-professionalism.json`, { reportConnection: false }),
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

export function buildLongDemoSummaries(childById) {
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
      status: SessionStatus.COMPLETED,
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
