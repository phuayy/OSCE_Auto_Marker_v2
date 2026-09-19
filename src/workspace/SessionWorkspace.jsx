// The opened-session view: player, crop timeline, transcript, score tabs,
// clip assessments, cohort charts.
//
// Its own chunk, loaded when a session is actually opened. A session in flight
// is not enterable (see the session-list card), so reaching this view is always
// a deliberate second click — the same criterion that already puts Settings,
// Analytics and the Communication Rubric behind lazy routes. Keeping it out of
// the entry bundle means a login pays for the dashboard only.
//
// The dashboard still owns the session state, because the upload flow, the
// change-stream refresh and the clip handlers all write it. What moved here is
// everything only this view uses: the results model derived from the score
// sheets, the player and download handlers, and the three effects that are
// meaningless with no player mounted.
import React, { useEffect, useMemo, useState } from 'react';
import { motion } from 'framer-motion';
import { ensureStreamTicket, resolveMediaUrl } from '@/auth';
import { ApiError, ERROR_KIND, apiJson } from '@/lib/apiFetch';
import { SessionStatus } from '@/lib/enums';
import { CLIP_ASSESSMENTS_ANCHOR_ID } from '@/lib/anchors';
import { describeProcessingStage, formatProcessingStageLabel } from '@/lib/processingStage';
import { describeCreator } from '@/lib/provenance';
import { INTERMISSION_KIND } from '@/lib/manualTimeline.js';
import { formatMetricValue, formatRuntime, prettySpeaker } from '@/lib/format';
import { downloadBlob, rowsToCsv, toSafeDownloadName } from '@/lib/download';
import { contentCriteriaState, feedbackCsvRows } from '@/lib/scoreSheet';
import { panelCsvColumns, panelReport } from '@/lib/panelReport';
import {
  buildCommunicationCriteria,
  buildCommunicationSummary,
  buildContentCriteria,
  buildKeepStartStop,
  buildScoringSummary,
} from '@/lib/resultsModel';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { SessionStatusBadge } from '@/components/SessionStatusBadge.jsx';
import { AudioProfessionalismSkeleton, CohortSummarySkeleton, LoadingRegion } from '@/components/skeletons.jsx';
import {
  Brain,
  ClipboardCheck,
  Clock3,
  Download,
  Loader2,
  MessageSquare,
  PlayCircle,
  RotateCw,
  Scissors,
  Sparkles,
  Trash2,
} from 'lucide-react';
import LongVideoSummaryCharts from '@/LongVideoSummaryCharts.jsx';
import { PanelMarkingSummary, PanelVotes } from '@/PanelMarkingSummary.jsx';
import CommunicationScoresTab from '@/workspace/CommunicationScoresTab.jsx';
import ManualTimelineEditor from '@/workspace/ManualTimelineEditor.jsx';
import StudentClipSplitterCard from '@/workspace/StudentClipSplitterCard.jsx';
import {
  ContentSheetEmptyState,
  FeedbackBlock,
  StatusRow,
} from '@/workspace/primitives.jsx';

export default function SessionWorkspace({
  session,
  videoFile,
  caseStudyFile,
  transcript,
  scoreReport,
  communicationScores,
  audioProfessionalism,
  setAudioProfessionalism,
  audioProfLoadError,
  setAudioProfLoadError,
  localVideoUrl,
  isDemoFallback,
  runtimeSeconds,
  notice,
  isLoadingWorkspace,
  isClipAssessmentView,
  restoreParentSession,
  videoPlayerRef,
  videoPlayerSectionRef,
  timelineContainerRef,
  timelineSegmentRefs,
  activeSegmentId,
  setActiveSegmentId,
  videoDurationSeconds,
  setVideoDurationSeconds,
  setCurrentVideoTime,
  durationKnown,
  isLongRecording,
  showCropWorkflow,
  clipEditsLocked,
  isPlanExportRunning,
  manualTimeline,
  clipSplitterSharedProps,
  currentModeLabel,
  showAssessmentPanels,
  showClipAssessmentPanel,
  videoClips,
  clipAssessmentRuns,
  clipAssessmentIndex,
  selectedClipAssessmentIds,
  selectedRunnableClipIds,
  batchSelectableClipIds,
  allClipsSelected,
  isQueueingSelectedClips,
  toggleClipSelected,
  toggleSelectAllClips,
  runSelectedClipAssessments,
  runClipAssessment,
  rerunClipAssessment,
  deleteClipAssessment,
  openClipAssessmentView,
  deletingSessionId,
  sessionIndex,
  clipSummaries,
  isLoadingClipSummaries,
  demoLongVideoSummaries,
}) {
  // Who uploaded the recording (or queued this clip's assessment), as the
  // server snapshotted them; null for a session recorded before creators were.
  const creator = describeCreator(session);
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

    return null;
  }, [audioProfessionalism]);
  // The artefact is normally part of the workspace payload; this covers the
  // fetch the effect below makes when it was not (a legacy row, a 404 at open
  // time). Local, not lifted: only this card reads it, and the dashboard's
  // `isLoadingWorkspace` means a whole view is on its way, which this is not.
  const [isLoadingAudioProf, setIsLoadingAudioProf] = useState(false);

  const audioProfMetrics = audioProfPayload?.metrics || null;
  const audioProfFeatures = audioProfPayload?.audio_features || null;
  const audioProfWarnings = Array.isArray(audioProfPayload?.warnings) ? audioProfPayload.warnings : [];

  const aiCriteria = useMemo(() => buildContentCriteria(scoreReport), [scoreReport]);

  // How a panel marked this sheet, or null for a single-model sheet.
  const contentPanel = useMemo(() => panelReport(scoreReport), [scoreReport]);

  const communicationPayload = useMemo(() => {
    if (communicationScores && typeof communicationScores === 'object') {
      return communicationScores;
    }
    return null;
  }, [communicationScores]);

  const communicationCriteria = useMemo(
    () => buildCommunicationCriteria(communicationPayload),
    [communicationPayload],
  );

  const communicationSummary = useMemo(
    () => buildCommunicationSummary(communicationPayload, communicationCriteria),
    [communicationPayload, communicationCriteria],
  );

  const keepStartStop = useMemo(() => buildKeepStartStop(scoreReport), [scoreReport]);

  const scoringSummary = useMemo(
    () => buildScoringSummary(scoreReport, aiCriteria),
    [scoreReport, aiCriteria],
  );

  const canDownloadScoreSheet =
    aiCriteria.length > 0 || Boolean(keepStartStop) || communicationCriteria.length > 0;

  // Drives the Content Scores / Feedback empty states. A real session with no
  // sheet is 'empty' (or 'failed'), never a template preview.
  const contentState = contentCriteriaState({
    criteriaCount: aiCriteria.length,
    isDemoFallback,
    sessionStatus: session?.status || null,
  });

  const [mediaState, setMediaState] = useState({ error: null });
  const serverVideoUrl = session?.files?.video?.url;
  const videoPurgedAt = session?.files?.video?.purgedAt || null;
  useEffect(() => {
    if (!serverVideoUrl?.startsWith('/media/')) return;
    let active = true;
    ensureStreamTicket().then(
      () => { if (active) setMediaState({ error: null }); },
      (error) => { if (active) setMediaState({ error }); },
    );
    return () => { active = false; };
  }, [serverVideoUrl]);

  const currentVideoUrl = resolveMediaUrl(serverVideoUrl) || localVideoUrl;
  const currentSubtitleUrl = resolveMediaUrl(session?.outputs?.subtitleTrack?.url) || null;


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
      // The card shows the artefact's outline until this settles. Without the
      // flag it showed the empty state — "check that openSMILE is installed"
      // — for the length of a fetch that usually succeeds.
      setIsLoadingAudioProf(true);
      try {
        const body = await apiJson(`/api/sessions/${session.id}/audio-professionalism`, {
          fallbackMessage: 'Audio professionalism fetch failed.',
        });

        if (!cancelled) {
          setAudioProfessionalism(body.audioProfessionalism || null);
          setAudioProfLoadError('');
        }
      } catch (error) {
        if (!cancelled) {
          setAudioProfLoadError(error.message || 'Audio professionalism unavailable.');
        }
      } finally {
        if (!cancelled) setIsLoadingAudioProf(false);
      }
    };

    loadAudioProfessionalism();

    return () => {
      cancelled = true;
      // A session switch mid-fetch: the next session's own effect decides
      // whether it is waiting; this one must not leave the flag raised.
      setIsLoadingAudioProf(false);
    };
  }, [audioProfPayload, session?.id, session?.outputs?.audioProfessionalism?.fileName, session?.outputs?.audioProfessionalism?.url]);

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
        [
          'Criterion',
          'Result',
          'Critical',
          'Timestamp',
          'Reason',
          ...(contentPanel ? ['Panel votes', 'Decided by'] : []),
        ],
      ];

      if (aiCriteria.length) {
        aiCriteria.forEach((criterion) => {
          contentRows.push([
            criterion.key || '',
            criterion.value || '',
            criterion.isCritical ? 'Yes' : 'No',
            criterion.timestamp || '',
            criterion.reason || '',
            ...panelCsvColumns(contentPanel, criterion.index),
          ]);
        });
      } else {
        contentRows.push(['No content rubric criteria were returned.', '', '', '', '']);
      }

      if (contentPanel) {
        contentRows.push(
          [],
          ['Panel'],
          ['Markers', contentPanel.markers.map((marker) => `${marker.initial}: ${marker.label}`).join(' / ')],
          ['Adjudicator', contentPanel.adjudicator.label || 'none'],
          ['Agreement', contentPanel.agreementLabel || ''],
          ['Degraded', contentPanel.degraded ? contentPanel.degraded.reason : 'No'],
        );
      }

      contentRows.push(
        [],
        ['Keep / Start / Stop Feedback'],
        ['Type', 'Notes'],
        // A field the model left empty exports empty: an examiner's sheet must
        // never carry text the model did not write.
        ...feedbackCsvRows(keepStartStop),
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

  return (
          <section className="space-y-6">
            {notice && (
              <div className="rounded-xl border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
                {notice}
              </div>
            )}

            <Card className="border-slate-200 bg-white shadow-sm">
              <CardContent className="pt-5">
                <dl className="flex flex-wrap items-center gap-2 text-sm">
                  <div className="flex items-baseline gap-1.5 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-slate-700">
                    <dt className="text-xs font-semibold text-slate-500">Session</dt>
                    <dd className="font-medium text-slate-900" title={session?.id || ''}>
                      {session?.name || session?.id || 'Pending'}
                    </dd>
                  </div>
                  <div className="flex items-baseline gap-1.5 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-slate-700">
                    <dt className="text-xs font-semibold text-slate-500">Recording</dt>
                    <dd className="max-w-[16rem] truncate" title={session?.files?.video?.originalName || videoFile?.name || ''}>
                      {session?.files?.video?.originalName || videoFile?.name || 'Not set'}
                    </dd>
                  </div>
                  <div className="flex items-baseline gap-1.5 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-slate-700">
                    <dt className="text-xs font-semibold text-slate-500">Case study</dt>
                    <dd className="max-w-[16rem] truncate" title={session?.files?.caseStudy?.originalName || caseStudyFile?.name || ''}>
                      {session?.files?.caseStudy?.originalName || caseStudyFile?.name || 'Bundled demo'}
                    </dd>
                  </div>
                  {creator ? (
                    <div className="flex items-baseline gap-1.5 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-slate-700">
                      <dt className="text-xs font-semibold text-slate-500">{creator.verb}</dt>
                      <dd className="max-w-[16rem] truncate" title={session?.createdBy?.username || ''}>
                        {creator.name}
                      </dd>
                    </div>
                  ) : null}
                  <div className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-slate-700">
                    <dt className="text-xs font-semibold text-slate-500">Status</dt>
                    <dd>
                      <SessionStatusBadge status={session?.status || SessionStatus.UPLOADED} />
                    </dd>
                  </div>
                </dl>
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
              className="rounded-2xl border border-slate-200/90 bg-white p-4 shadow-sm ring-1 ring-slate-100/80"
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
                        ? 'border-0 bg-violet-600 px-3 py-1 text-xs font-semibold text-white shadow-sm'
                        : 'border-0 bg-slate-800 px-3 py-1 text-xs font-semibold text-slate-50 shadow-sm'
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
                  <CardHeader className="space-y-0 border-b border-slate-100 pb-5">
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
                                  ? 'border-0 bg-violet-600 px-3 py-1 text-xs font-semibold text-white shadow-sm'
                                  : 'border-0 bg-slate-800 px-3 py-1 text-xs font-semibold text-slate-50 shadow-sm'
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
                          onLoadedMetadata={(event) => {
                            setMediaState({ error: null });
                            handleVideoLoadedMetadata(event);
                          }}
                          onError={() => setMediaState({
                            error: new ApiError('Media unavailable. Reopen the session to try again.', { kind: ERROR_KIND.CLIENT }),
                          })}
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
                        {mediaState.error ? (
                          <p role="alert" className="text-sm text-rose-600">{mediaState.error.message}</p>
                        ) : null}

                        {showCropWorkflow ? (
                          <Tabs
                            defaultValue={clipEditsLocked ? 'auto' : 'manual'}
                            key={`crop-workflow-${session?.id || 'anon'}-${clipEditsLocked ? 'locked' : 'editable'}`}
                            className="w-full"
                          >
                            <TabsList
                              className={`grid h-auto w-full gap-1 rounded-2xl border border-slate-200/90 p-1.5 ${
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
                                <ManualTimelineEditor {...manualTimeline} />
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
                                seekToSeconds={seekToSeconds}
                                // Re-cutting one clip is durable work now (a
                                // queued export scoped to that clip), so an
                                // exported session no longer locks the crop
                                // editor for good — only while a cut is
                                // actually in flight.
                                lockEdits={isPlanExportRunning}
                                title="Auto-detected clips"
                                description={
                                  clipEditsLocked
                                    ? 'Clips exported. Adjust a clip’s crop to re-cut just that student.'
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
                    ) : videoPurgedAt ? (
                      <div className="flex aspect-video w-full flex-col items-center justify-center gap-1 rounded-2xl border border-dashed border-slate-300 bg-slate-100 px-4 text-center text-slate-500">
                        <span>Video removed under the data retention policy</span>
                        <span className="text-xs text-slate-400">
                          Deleted {new Date(videoPurgedAt).toLocaleDateString()} · transcript and scores are unaffected
                        </span>
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
                          Transcript timeline
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
                  <TabsList className="grid h-auto w-full grid-cols-2 gap-1 rounded-2xl border border-slate-200/90 p-1.5 sm:grid-cols-4">
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
                        <CardTitle className="text-base">Transcript segments</CardTitle>
                        <CardDescription>
                          One row per spoken segment: speaker, words and timestamps from the transcription engine.
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
                        <CardTitle className="text-base">Content scores</CardTitle>
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

                        {!scoringSummary && !isDemoFallback && session?.status === SessionStatus.COMPLETED && !aiCriteria.length ? (
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
                        ) : (
                          <ContentSheetEmptyState state={contentState} error={session?.error} subject="scores" />
                        )}

                        <PanelMarkingSummary
                          report={contentPanel}
                          artifacts={session?.outputs?.scores?.panelArtifacts || null}
                        />

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
                                  <PanelVotes report={contentPanel} index={criterion.index} />
                                </div>
                              );
                            })
                          : scoringSummary ? (
                              <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/90 p-4 text-center text-sm text-slate-500">
                                This sheet lists no criteria.
                              </div>
                            ) : null}

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
                            <FeedbackBlock title="Keep" lines={[keepStartStop.keep]} />

                            <FeedbackBlock title="Start" lines={[keepStartStop.start]} />

                            <FeedbackBlock title="Stop" lines={[keepStartStop.stop]} />
                          </>
                        ) : (
                          <ContentSheetEmptyState state={contentState} error={session?.error} subject="feedback" />
                        )}
                      </CardContent>
                    </Card>
                  </TabsContent>
                    </Tabs>
                  </>
                ) : (
                  <Card className="border-slate-200 bg-white shadow-sm">
                    <CardHeader>
                      <CardTitle className="text-base">Long recording workflow</CardTitle>
                      <CardDescription>Transcript, scores, and feedback appear per clip after export.</CardDescription>
                    </CardHeader>
                    <CardContent className="text-sm text-slate-600">
                      Export clips first, then run assessments per student to view transcript and scoring details.
                    </CardContent>
                  </Card>
                )}

                {/* First computation only: LongVideoSummaryCharts renders
                    nothing without data, so this stands in its slot at its
                    height and nothing below moves when the charts land. A
                    recomputation (another clip finishing) keeps the charts
                    that are up. */}
                {showClipAssessmentPanel && isLoadingClipSummaries && !clipSummaries && !demoLongVideoSummaries ? (
                  <LoadingRegion label="Building cohort summary charts">
                    <CohortSummarySkeleton />
                  </LoadingRegion>
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
                    <CardTitle className="text-base">Run status</CardTitle>
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
                            : session?.status === SessionStatus.COMPLETED
                              ? 'Not produced — check OpenRouter key'
                              : 'Runs after transcription'
                      }
                    />
                  </CardContent>
                </Card>

                {/* {showCropWorkflow ? (
                  <Card className="border-violet-200 bg-violet-50/40 shadow-sm">
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
                      <CardTitle className="text-base">Single student mode</CardTitle>
                      <CardDescription>Clip splitting is disabled for standard assessments.</CardDescription>
                    </CardHeader>
                    <CardContent className="text-sm text-slate-600">
                      Use the workflow tabs on the left to run transcription and scoring for the full recording.
                    </CardContent>
                  </Card>
                )} */}

                {showClipAssessmentPanel ? (
                  <Card id={CLIP_ASSESSMENTS_ANCHOR_ID} className="border-slate-200 bg-white shadow-sm">
                    <CardHeader>
                      <CardTitle className="text-base">Clip assessments</CardTitle>
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
                            isLoadingWorkspace || isQueueingSelectedClips || selectedRunnableClipIds.length === 0
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
                                  <div className="text-xs text-slate-500">
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
                        const isCompleted = runState.status === SessionStatus.COMPLETED && Boolean(runState.sessionId);
                        // Child session id backing a running clip, used to re-open its progress overlay.
                        const progressSessionId = runState.sessionId || clipAssessmentIndex[clip.id]?.sessionId || null;
                        const statusLabel =
                          runState.status === 'running'
                            ? 'Running'
                            : runState.status === SessionStatus.COMPLETED
                              ? 'Completed'
                              : runState.status === SessionStatus.FAILED
                                ? 'Failed'
                                : 'Ready';
                        const statusClass =
                          runState.status === SessionStatus.COMPLETED
                            ? 'bg-emerald-100 text-emerald-700'
                            : runState.status === SessionStatus.FAILED
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
                                  {/* The child scored a cut this clip no longer has —
                                      re-cropping replaced the MP4 after it ran. */}
                                  {clipAssessmentIndex[clip.id]?.stale && runState.status !== 'running' ? (
                                    <div className="mt-1 text-xs font-medium text-amber-700">
                                      Clip re-cut since this assessment — re-run to score the new crop.
                                    </div>
                                  ) : null}
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
                                    disabled={isLoadingWorkspace || deletingSessionId === progressSessionId}
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
                                    disabled={isLoadingWorkspace}
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
                                        disabled={isLoadingWorkspace}
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
                                if (runState.status === SessionStatus.FAILED && progressSessionId) {
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
                                    disabled={isLoadingWorkspace}
                                  >
                                    Run assessment
                                  </Button>
                                );
                              })()}
                              {runState.status === SessionStatus.FAILED && runState.error ? (
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
                    <CardTitle className="text-base">Audio professionalism</CardTitle>
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
                    ) : isLoadingAudioProf ? (
                      /* The same rows WorkspaceSkeleton drew for this card while
                         the session opened, so a late artefact does not turn
                         the card into an empty state on its way in. */
                      <LoadingRegion label="Loading audio professionalism">
                        <AudioProfessionalismSkeleton />
                      </LoadingRegion>
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
  );
}
