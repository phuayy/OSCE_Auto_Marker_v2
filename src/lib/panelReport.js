// Reading the `panel` block of a content sheet for the results view.
//
// A sheet marked by a panel carries every marker's vote, how each criterion
// was decided, and how alike the markers were. This module turns that block
// into what the score tab renders — nothing here fetches or formats for the
// DOM, so it is unit-testable (see test/panelReport.test.mjs). A sheet with no
// block (single-model marking, or one written before panels existed) yields
// null, and the view renders exactly as it always has.

export const RESOLUTION = Object.freeze({
  AGREED: 'agreed',
  ADJUDICATED: 'adjudicated',
  SOLE_MARKER: 'sole_marker',
  TIE_BREAK_PREFIX: 'tie_break:',
});

// A marker's display name: "gemini:gemini-2.5-pro", or the key when the sheet
// recorded no provenance.
export function markerLabel(marker) {
  if (!marker) return '';
  const provider = String(marker.provider_id || marker.providerId || '').trim();
  const model = String(marker.model || '').trim();
  if (provider && model) return `${provider}:${model}`;
  return provider || model || String(marker.key || '');
}

// Short letter for a marker position, matching how the adjudicator's record
// refers to them ("Examiner A") — but by *position*, since the results view
// always lists markers in configured order.
export function markerInitial(position) {
  return String.fromCharCode('A'.charCodeAt(0) + position);
}

// Human label and tone for a resolution string.
export function describeResolution(resolution) {
  const value = String(resolution || '');
  if (value === RESOLUTION.AGREED) return { label: 'Markers agreed', tone: 'neutral' };
  if (value === RESOLUTION.ADJUDICATED) return { label: 'Adjudicated', tone: 'attention' };
  if (value === RESOLUTION.SOLE_MARKER) return { label: 'Single marker', tone: 'warning' };
  if (value.startsWith(RESOLUTION.TIE_BREAK_PREFIX)) {
    const policy = value.slice(RESOLUTION.TIE_BREAK_PREFIX.length).replace(/_/g, ' ');
    return { label: `Tie-break (${policy})`, tone: 'warning' };
  }
  return { label: value || 'Unknown', tone: 'neutral' };
}

// "22/25 agreed · κ 0.71" — the one line that says how alike the markers were.
export function describeAgreement(agreement) {
  if (!agreement || typeof agreement !== 'object') return '';
  const total = Number(agreement.total || 0);
  const agreed = Number(agreement.agreed || 0);
  const parts = [];
  if (total) parts.push(`${agreed}/${total} agreed`);
  const kappa = agreement.cohen_kappa;
  if (typeof kappa === 'number' && Number.isFinite(kappa)) parts.push(`κ ${kappa.toFixed(2)}`);
  if (agreement.pass_fail_agreed === false) parts.push('markers split on pass/fail');
  return parts.join(' · ');
}

// The report the score tab renders, or null when the sheet is not a panel's.
export function panelReport(scoreReport) {
  const panel = scoreReport?.panel;
  if (!panel || typeof panel !== 'object') return null;

  const markers = (Array.isArray(panel.markers) ? panel.markers : []).map((marker, position) => ({
    key: String(marker?.key || ''),
    label: markerLabel(marker),
    initial: markerInitial(position),
    passFail: String(marker?.summary?.pass_fail || ''),
    yesCount: marker?.summary?.yes_count ?? null,
    warnings: Array.isArray(marker?.warnings) ? marker.warnings : [],
  }));

  const adjudicatorRaw = panel.adjudicator && typeof panel.adjudicator === 'object' ? panel.adjudicator : {};
  const adjudicator = {
    label: markerLabel(adjudicatorRaw),
    called: Boolean(adjudicatorRaw.called),
    ok: adjudicatorRaw.ok ?? null,
    feedbackSource: String(adjudicatorRaw.feedback_source || ''),
  };

  const criteria = new Map();
  (Array.isArray(panel.criteria) ? panel.criteria : []).forEach((item) => {
    if (!item || typeof item !== 'object') return;
    const index = Number(item.index);
    if (!Number.isInteger(index)) return;
    const resolution = String(item.resolution || '');
    criteria.set(index, {
      index,
      votes: Array.isArray(item.votes) ? item.votes.map((vote) => String(vote || '')) : [],
      reasons: Array.isArray(item.reasons) ? item.reasons.map((reason) => String(reason || '')) : [],
      timestamps: Array.isArray(item.timestamps) ? item.timestamps.map((ts) => String(ts || '')) : [],
      resolution,
      ...describeResolution(resolution),
      disputed: resolution !== RESOLUTION.AGREED && resolution !== RESOLUTION.SOLE_MARKER,
      sidedWith: Number.isInteger(item.sided_with) ? item.sided_with : null,
      confidence: typeof item.confidence === 'number' ? item.confidence : null,
      reason: String(item.reason || ''),
    });
  });

  const degraded = panel.degraded && typeof panel.degraded === 'object' ? panel.degraded : null;

  return {
    markers,
    adjudicator,
    agreement: panel.agreement && typeof panel.agreement === 'object' ? panel.agreement : null,
    agreementLabel: describeAgreement(panel.agreement),
    tieBreak: String(panel.tie_break || ''),
    degraded: degraded ? { reason: String(degraded.reason || ''), marker: String(degraded.marker || '') } : null,
    warnings: Array.isArray(panel.warnings) ? panel.warnings.map((warning) => String(warning)) : [],
    criteria,
    disputedCount: Array.from(criteria.values()).filter((item) => item.disputed).length,
  };
}

// The two extra columns a panel adds to the CSV score sheet, for one criterion.
export function panelCsvColumns(report, index) {
  if (!report) return [];
  const detail = report.criteria.get(index);
  if (!detail) return ['', ''];
  const votes = detail.votes.map((vote, position) => `${report.markers[position]?.initial || markerInitial(position)}: ${vote}`);
  return [votes.join(' / '), detail.label];
}
