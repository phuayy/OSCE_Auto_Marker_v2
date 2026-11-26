// The results model: a score sheet on disk turned into what the workspace
// renders.
//
// Pure functions of the sheets, deliberately not hooks. The workspace memoises
// them; `downloadScoreSheet` reuses the very same values, so the CSV an
// examiner downloads can never disagree with the tab they downloaded it from.
import { feedbackLines } from './scoreSheet.js';
import { formatRuntime, parseEvidenceTimestamp } from './format.js';

// Content criteria, normalised: Yes/No, critical flag, and the evidence
// timestamp as both a label and seconds the player can seek to.
export function buildContentCriteria(scoreReport) {
  const criteriaList = Array.isArray(scoreReport?.criteria) ? scoreReport.criteria : [];

  return criteriaList.map((item, index) => {
    const normalizedValue = String(item?.value || '').trim().toLowerCase() === 'yes' ? 'Yes' : 'No';
    const normalizedCritical =
      item?.is_critical === true || String(item?.is_critical || '').trim().toLowerCase() === 'true';
    const rawTimestamp = String(item?.timestamp || item?.evidence_timestamp || '').trim();
    const timestampSeconds = parseEvidenceTimestamp(rawTimestamp);
    const timestampLabel = rawTimestamp || (Number.isFinite(timestampSeconds) ? formatRuntime(timestampSeconds) : '');

    return {
      index,
      key: String(item?.label || `Criterion ${index + 1}`).trim(),
      value: normalizedValue,
      isCritical: normalizedCritical,
      reason: String(item?.reason || '').trim(),
      timestamp: timestampLabel,
      timestampSeconds: Number.isFinite(timestampSeconds) ? timestampSeconds : null,
    };
  });
}

const DEFAULT_COMMUNICATION_SCALE = Object.freeze({ All: 3, Most: 2, Some: 1, None: 0 });

// The points each communication label is worth under this sheet's rubric:
// its own `scoring_scale` when it is four finite non-negative numbers, else
// the default 3/2/1/0 (a sheet from before editable scales carries none).
export function communicationScale(communicationPayload) {
  const raw = communicationPayload?.scoring_scale;
  if (!raw || typeof raw !== 'object') return { ...DEFAULT_COMMUNICATION_SCALE };
  const scale = {};
  for (const label of ['All', 'Most', 'Some', 'None']) {
    const value = raw[label];
    if (value === null || value === '' || typeof value === 'boolean') {
      return { ...DEFAULT_COMMUNICATION_SCALE };
    }
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) return { ...DEFAULT_COMMUNICATION_SCALE };
    scale[label] = number;
  }
  return scale;
}

export function buildCommunicationCriteria(communicationPayload) {
  const scale = communicationScale(communicationPayload);
  const list = Array.isArray(communicationPayload?.criteria) ? communicationPayload.criteria : [];
  return list.map((item, index) => {
    const rawTimestamp = String(item?.timestamp || '').trim();
    const timestampSeconds = parseEvidenceTimestamp(rawTimestamp);
    const scoreLabel = String(item?.score_label || '').trim();
    const points = Number.isFinite(Number(item?.points))
      ? Number(item.points)
      : scale[scoreLabel] ?? 0;
    return {
      id: item?.id || index + 1,
      label: String(item?.label || `Criterion ${index + 1}`).trim(),
      section: item?.section || null,
      scoreLabel: ['None', 'Some', 'Most', 'All'].includes(scoreLabel) ? scoreLabel : 'None',
      points,
      maxPoints: scale.All,
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
}

export function buildCommunicationSummary(communicationPayload, communicationCriteria) {
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
  const maxScore = communicationCriteria.length * communicationScale(communicationPayload).All;
  const passThreshold =
    communicationCriteria.length === 7 && maxScore === 21 ? 11 : Math.ceil(maxScore / 2);

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
}

const COMMUNICATION_LABEL_MEANINGS = Object.freeze({
  All: 'Student consistently demonstrated EVERY observable indicator',
  Most: 'Student consistently demonstrated MOST observable indicators',
  Some: 'Student demonstrated ONLY SOME observable indicators',
  None: 'Student demonstrated NONE of the observable indicators',
});

// The communication block of the CSV score sheet. It reads the marks from the
// same values the Communication tab renders — the criteria's `points` and the
// sheet's own scale — so an edited scale reads the same in both places.
export function buildCommunicationCsvRows({ sessionId, payload, criteria, summary }) {
  const scale = communicationScale(payload);
  const labelCounts = summary?.labelCounts || {};
  const labelDistribution = ['All', 'Most', 'Some', 'None']
    .map((label) => labelCounts[label] ?? 0)
    .join(' / ');
  const joinIndicators = (items) => (Array.isArray(items) && items.length ? `- ${items.join('\n- ')}` : '');

  const rows = [
    ['OSCE Rubric Score Sheet - Communication'],
    [],
    ['Student Name', ''],
    ['Student ID', ''],
    ['Session ID', sessionId || ''],
    [],
    ['Scoring Scale'],
    ['Label', 'Marks', 'Meaning'],
    ...['All', 'Most', 'Some', 'None'].map((label) => [
      label,
      scale[label],
      COMMUNICATION_LABEL_MEANINGS[label],
    ]),
    [],
    ['Communication Scoring Summary'],
    ['Result', 'Total', 'Max', 'Pass threshold', 'All / Most / Some / None'],
    [
      summary?.passFail || (criteria.length ? 'Pending' : 'N/A'),
      summary?.totalScore ?? 0,
      summary?.maxScore ?? 0,
      summary?.passThreshold ?? 0,
      labelDistribution,
    ],
    [],
    ['Communication Criteria'],
    ['ID', 'Criterion', 'Section', 'Score Label', 'Marks', 'Timestamp'],
  ];

  criteria.forEach((criterion) => {
    rows.push([
      criterion.id || '',
      criterion.label || '',
      criterion.section || '',
      criterion.scoreLabel || 'None',
      criterion.points,
      criterion.timestamp || '',
    ]);
  });

  rows.push(
    [],
    ['Evidence & Indicators'],
    ['ID', 'Criterion', 'Evidence', 'Indicators observed', 'Indicators missing', 'Not observable'],
  );

  criteria.forEach((criterion) => {
    rows.push([
      criterion.id || '',
      criterion.label || '',
      criterion.evidence || '',
      joinIndicators(criterion.indicatorsObserved),
      joinIndicators(criterion.indicatorsMissing),
      joinIndicators(criterion.indicatorsNotObservable),
    ]);
  });

  if (payload?.overall_summary) {
    rows.push([], ['Overall Communication Summary'], ['Summary', payload.overall_summary]);
  }

  return rows;
}

// The sheet's own summary when it has one; otherwise recomputed from the
// criteria under the rubric's rule — a critical No fails, and fewer than half
// Yes fails.
export function buildScoringSummary(scoreReport, aiCriteria) {
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
}

// '' marks a field the model left empty; the view says so instead of
// substituting a sentence of its own (see lib/scoreSheet.js). null still means
// the sheet has no block at all.
export function buildKeepStartStop(scoreReport) {
  if (!scoreReport?.keep_start_stop || typeof scoreReport.keep_start_stop !== 'object') {
    return null;
  }

  return feedbackLines(scoreReport.keep_start_stop);
}
