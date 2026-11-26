// The cohort's communication criteria, aggregated for the criterion-by-criterion
// chart.
//
// A criterion's `id` is its position in the rubric, and every save of the rubric
// renumbers them, so id 3 under one revision can be a different criterion under
// the next. Clips of one recording that were assessed before and after an edit
// therefore must not be merged by id: the rows are grouped by the rubric revision
// each sheet was marked against, and merged by id only inside one revision. A
// sheet that records no revision (marked before revisions existed) forms its own
// group, so a cohort that never saw an edit is one group, exactly as before.
const LABELS = ['All', 'Most', 'Some', 'None'];

function sortId(row) {
  return Number(row.id) || 0;
}

/**
 * `[{ revision, versionNumber, studentCount, rows }]`, oldest rubric first.
 * Each row: `{ id, label, section, counts, totalPoints, studentCount }`, with the
 * label and section of that revision.
 */
export function groupCriteriaByRubricRevision(summaries) {
  const groups = new Map();

  (summaries || []).forEach((summary) => {
    const communication = summary?.communication;
    const list = communication?.perCriterionPoints;
    if (!Array.isArray(list) || list.length === 0) return;

    const revision = communication.rubricRevision || null;
    if (!groups.has(revision)) {
      groups.set(revision, {
        revision,
        versionNumber: Number.isFinite(Number(communication.rubricVersionNumber))
          ? Number(communication.rubricVersionNumber)
          : null,
        studentCount: 0,
        rowsByKey: new Map(),
      });
    }
    const group = groups.get(revision);
    group.studentCount += 1;

    list.forEach((criterion) => {
      const key = String(criterion.id ?? criterion.label);
      if (!group.rowsByKey.has(key)) {
        group.rowsByKey.set(key, {
          id: criterion.id,
          label: criterion.label,
          section: criterion.section,
          counts: { All: 0, Most: 0, Some: 0, None: 0 },
          totalPoints: 0,
          studentCount: 0,
        });
      }
      const row = group.rowsByKey.get(key);
      row.counts[LABELS.includes(criterion.scoreLabel) ? criterion.scoreLabel : 'None'] += 1;
      row.totalPoints += Number.isFinite(Number(criterion.points)) ? Number(criterion.points) : 0;
      row.studentCount += 1;
    });
  });

  return [...groups.values()]
    .map(({ rowsByKey, ...group }) => ({
      ...group,
      rows: [...rowsByKey.values()].sort((a, b) => sortId(a) - sortId(b)),
    }))
    .sort((a, b) => (a.versionNumber ?? -1) - (b.versionNumber ?? -1));
}
