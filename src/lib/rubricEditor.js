// The communication rubric editor's model. Pure: no React, no fetch.
//
// An administrator reviews a rubric (usually one an AI model just extracted
// from a PDF), fixes what the model got wrong, and saves it. This module owns
// the editable shape, every edit operation and the validation, mirroring the
// server's rules in fastapi_backend/app/domain/communication_rubric.py so the
// form can say what is wrong before the save round trip. The server still
// validates: this is a convenience, never the gate.

export const SCORE_LABELS = ['All', 'Most', 'Some', 'None'];
export const DEFAULT_SCORING_SCALE = Object.freeze({ All: 3, Most: 2, Some: 1, None: 0 });
export const MAX_LEVEL_POINTS = 100;
export const MIN_CRITERIA = 2;
export const MAX_CRITERIA = 60;
export const MAX_INDICATORS_PER_CRITERION = 30;
export const MAX_LABEL_CHARS = 300;
export const MAX_INDICATOR_CHARS = 500;
export const MAX_TITLE_CHARS = 200;
export const MAX_SECTION_CHARS = 200;
export const MAX_NOTES_CHARS = 5000;

// Editable shape (what the form holds):
// {
//   title: string, notes: string,
//   scoringScale: { All, Most, Some, None },   // values as typed (string|number)
//   passThreshold: string|number|null,         // null = automatic (default rule)
//   criteria: [{ key, section, label, indicators: [{ key, text }] }],
// }
// `key`s are stable, unique, generated here — React list keys, never sent.

let keyCounter = 0;
function nextKey(prefix) {
  keyCounter += 1;
  return `${prefix}${keyCounter}`;
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function str(value) {
  return typeof value === 'string' ? value : '';
}

// Whole number from a number or a trimmed numeric string; NaN otherwise.
function parseWhole(value) {
  if (typeof value === 'number') return Number.isInteger(value) ? value : NaN;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return /^-?\d+$/.test(trimmed) ? Number(trimmed) : NaN;
  }
  return NaN;
}

function looseNumber(value) {
  const n = parseWhole(value);
  if (Number.isFinite(n)) return n;
  const loose = typeof value === 'string' && value.trim() === '' ? NaN : Number(value);
  return Number.isFinite(loose) ? loose : 0;
}

function validPoints(value) {
  const n = parseWhole(value);
  return Number.isFinite(n) && n >= 0 && n <= MAX_LEVEL_POINTS;
}

function criteriaOf(editable) {
  return Array.isArray(editable?.criteria) ? editable.criteria : [];
}

/** Default pass mark: 11 for 7 criteria at 21 points, else ceil(max / 2). */
export function defaultPassThreshold(criterionCount, maxScore) {
  if (criterionCount === 7 && maxScore === 21) return 11;
  return Math.ceil(maxScore / 2);
}

function newIndicator(text = '') {
  return { key: nextKey('i'), text };
}

/** Server rubric JSON (communication-rubric-v1) -> editable. */
export function toEditableRubric(rubric) {
  const source = isPlainObject(rubric) ? rubric : {};
  const rawScale = source.scoring_scale ?? source.scoringScale;
  let scoringScale = { ...DEFAULT_SCORING_SCALE };
  if (isPlainObject(rawScale) && SCORE_LABELS.every((label) => validPoints(rawScale[label]))) {
    scoringScale = {};
    for (const label of SCORE_LABELS) scoringScale[label] = parseWhole(rawScale[label]);
  }

  let list = Array.isArray(source.criteria) ? source.criteria.filter(isPlainObject) : [];
  const ids = list.map((c) => Number(c.id));
  if (list.length > 1 && ids.every((n) => Number.isFinite(n))) {
    list = list
      .map((c, index) => ({ c, index, id: Number(c.id) }))
      .sort((a, b) => a.id - b.id || a.index - b.index)
      .map((entry) => entry.c);
  }
  const criteria = list.map((c) => ({
    key: nextKey('c'),
    section: str(c.section),
    label: str(c.label),
    indicators: (Array.isArray(c.indicators) ? c.indicators : [])
      .map((ind) => (typeof ind === 'string' ? ind : str(ind?.text)))
      .map((text) => newIndicator(text)),
  }));

  const storedRaw = source.pass_threshold ?? source.passThreshold;
  const stored = parseWhole(storedRaw);
  let passThreshold = null;
  if (Number.isFinite(stored)) {
    const auto = defaultPassThreshold(criteria.length, criteria.length * scoringScale.All);
    passThreshold = stored === auto ? null : stored;
  }

  return {
    title: str(source.title),
    notes: str(source.notes),
    scoringScale,
    passThreshold,
    criteria,
  };
}

/** Editable -> the `rubric` body of PUT /api/admin/communication-rubric. */
export function fromEditableRubric(editable) {
  const scale = isPlainObject(editable?.scoringScale) ? editable.scoringScale : {};
  const scoring_scale = {};
  for (const label of SCORE_LABELS) {
    scoring_scale[label] = looseNumber(scale[label] ?? DEFAULT_SCORING_SCALE[label]);
  }
  const raw = editable?.passThreshold;
  const pass_threshold =
    raw === null || raw === undefined || (typeof raw === 'string' && raw.trim() === '')
      ? null
      : looseNumber(raw);
  return {
    title: str(editable?.title).trim(),
    notes: str(editable?.notes).trim(),
    scoring_scale,
    pass_threshold,
    criteria: criteriaOf(editable).map((c) => {
      const section = str(c.section).trim();
      return {
        section: section || null,
        label: str(c.label).trim(),
        indicators: (Array.isArray(c.indicators) ? c.indicators : [])
          .map((ind) => str(ind?.text).trim())
          .filter(Boolean),
      };
    }),
  };
}

/** Totals derived from the editable state. */
export function deriveRubricTotals(editable) {
  const criteriaCount = criteriaOf(editable).length;
  const all = parseWhole(editable?.scoringScale?.All);
  const maxScore = Number.isFinite(all) && all >= 0 ? criteriaCount * all : 0;
  const defaultThreshold = defaultPassThreshold(criteriaCount, maxScore);
  const raw = editable?.passThreshold;
  const isAutomatic = raw === null || raw === undefined;
  const parsed = isAutomatic ? NaN : parseWhole(raw);
  return {
    criteriaCount,
    maxScore,
    defaultPassThreshold: defaultThreshold,
    passThreshold: isAutomatic ? defaultThreshold : Number.isFinite(parsed) ? parsed : null,
    isAutomatic,
  };
}

/** Every reason the editable cannot be saved. */
export function validateEditableRubric(editable) {
  const issues = [];
  const fieldErrors = { criteria: {} };
  const fail = (message) => issues.push(message);
  const criterionErrors = (key) => {
    if (!fieldErrors.criteria[key]) fieldErrors.criteria[key] = {};
    return fieldErrors.criteria[key];
  };

  const title = str(editable?.title).trim();
  if (!title) {
    fieldErrors.title = 'The rubric needs a title.';
    fail(fieldErrors.title);
  } else if (title.length > MAX_TITLE_CHARS) {
    fieldErrors.title = `The title must be at most ${MAX_TITLE_CHARS} characters.`;
    fail(fieldErrors.title);
  }
  if (str(editable?.notes).trim().length > MAX_NOTES_CHARS) {
    fieldErrors.notes = `The notes must be at most ${MAX_NOTES_CHARS} characters.`;
    fail(fieldErrors.notes);
  }

  // Scale
  const scale = isPlainObject(editable?.scoringScale) ? editable.scoringScale : {};
  const scaleProblems = [];
  for (const label of SCORE_LABELS) {
    if (!validPoints(scale[label])) {
      scaleProblems.push(`"${label}" points must be a whole number from 0 to ${MAX_LEVEL_POINTS}.`);
    }
  }
  if (!scaleProblems.length) {
    const [a, m, s, n] = SCORE_LABELS.map((label) => parseWhole(scale[label]));
    if (!(a >= m && m >= s && s >= n)) {
      scaleProblems.push('Points must not increase from All to Most to Some to None.');
    } else if (!(a > n)) {
      scaleProblems.push('"All" must score more than "None".');
    }
  }
  if (scaleProblems.length) {
    fieldErrors.scale = scaleProblems[0];
    scaleProblems.forEach(fail);
  }

  // Criteria
  const criteria = criteriaOf(editable);
  if (criteria.length < MIN_CRITERIA) {
    fail(`A rubric needs at least ${MIN_CRITERIA} criteria.`);
  } else if (criteria.length > MAX_CRITERIA) {
    fail(`A rubric can have at most ${MAX_CRITERIA} criteria.`);
  }
  const byLabel = new Map();
  criteria.forEach((c, index) => {
    const n = index + 1;
    const label = str(c.label).trim();
    if (!label) {
      criterionErrors(c.key).label = 'Needs a label.';
      fail(`Criterion ${n} needs a label.`);
    } else {
      if (label.length > MAX_LABEL_CHARS) {
        criterionErrors(c.key).label = `Label must be at most ${MAX_LABEL_CHARS} characters.`;
        fail(`Criterion ${n}'s label must be at most ${MAX_LABEL_CHARS} characters.`);
      }
      const id = label.toLowerCase();
      if (!byLabel.has(id)) byLabel.set(id, []);
      byLabel.get(id).push({ key: c.key, label });
    }
    if (str(c.section).trim().length > MAX_SECTION_CHARS) {
      criterionErrors(c.key).section = `Section must be at most ${MAX_SECTION_CHARS} characters.`;
      fail(`Criterion ${n}'s section must be at most ${MAX_SECTION_CHARS} characters.`);
    }
    const filled = (Array.isArray(c.indicators) ? c.indicators : []).filter(
      (ind) => str(ind?.text).trim(),
    );
    filled.forEach((ind, i) => {
      const length = str(ind.text).trim().length;
      if (length > MAX_INDICATOR_CHARS) {
        const errs = criterionErrors(c.key);
        errs.indicators = { ...(errs.indicators || {}) };
        errs.indicators[ind.key] = `At most ${MAX_INDICATOR_CHARS} characters.`;
        fail(`An indicator of criterion ${n} must be at most ${MAX_INDICATOR_CHARS} characters.`);
      }
      if (i === MAX_INDICATORS_PER_CRITERION) {
        const errs = criterionErrors(c.key);
        errs.indicators = { ...(errs.indicators || {}) };
        errs.indicators[ind.key] = `At most ${MAX_INDICATORS_PER_CRITERION} indicators.`;
        fail(`Criterion ${n} can have at most ${MAX_INDICATORS_PER_CRITERION} indicators.`);
      }
    });
  });
  for (const group of byLabel.values()) {
    if (group.length < 2) continue;
    for (const entry of group) criterionErrors(entry.key).label = 'Duplicate label.';
    fail(
      group.length === 2
        ? `Two criteria are both called "${group[0].label}".`
        : `${group.length} criteria are all called "${group[0].label}".`,
    );
  }

  // Threshold
  const raw = editable?.passThreshold;
  if (raw !== null && raw !== undefined) {
    const n = parseWhole(raw);
    if (!Number.isFinite(n)) {
      fieldErrors.passThreshold = 'The pass mark must be a whole number.';
      fail(fieldErrors.passThreshold);
    } else if (n < 0) {
      fieldErrors.passThreshold = 'The pass mark cannot be negative.';
      fail(fieldErrors.passThreshold);
    } else if (!fieldErrors.scale) {
      const { maxScore } = deriveRubricTotals(editable);
      if (n > maxScore) {
        fieldErrors.passThreshold = `The pass mark cannot be more than the maximum score (${maxScore}).`;
        fail(fieldErrors.passThreshold);
      }
    }
  }

  return { valid: issues.length === 0, issues, fieldErrors };
}

/** True when the request bodies the two would produce differ. */
export function isRubricDirty(original, current) {
  return (
    JSON.stringify(fromEditableRubric(original)) !== JSON.stringify(fromEditableRubric(current))
  );
}

export function setRubricField(editable, field, value) {
  if (field !== 'title' && field !== 'notes') return editable;
  return { ...editable, [field]: value };
}

export function setScalePoints(editable, label, value) {
  if (!SCORE_LABELS.includes(label)) return editable;
  return { ...editable, scoringScale: { ...editable.scoringScale, [label]: value } };
}

export function setPassThreshold(editable, value) {
  return { ...editable, passThreshold: value === undefined ? null : value };
}

export function addCriterion(editable, afterKey = null) {
  const list = criteriaOf(editable);
  let at = list.length;
  let section = list.length ? str(list[list.length - 1].section) : '';
  if (afterKey !== null && afterKey !== undefined) {
    const index = list.findIndex((c) => c.key === afterKey);
    if (index === -1) return editable;
    at = index + 1;
    section = str(list[index].section);
  }
  const created = { key: nextKey('c'), section, label: '', indicators: [] };
  return { ...editable, criteria: [...list.slice(0, at), created, ...list.slice(at)] };
}

export function removeCriterion(editable, key) {
  const list = criteriaOf(editable);
  if (!list.some((c) => c.key === key)) return editable;
  return { ...editable, criteria: list.filter((c) => c.key !== key) };
}

export function moveCriterion(editable, key, delta) {
  const list = criteriaOf(editable);
  const from = list.findIndex((c) => c.key === key);
  if (from === -1) return editable;
  const to = Math.min(list.length - 1, Math.max(0, from + (Number(delta) || 0)));
  if (to === from) return editable;
  const next = list.slice();
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return { ...editable, criteria: next };
}

function mapCriterion(editable, key, fn) {
  const list = criteriaOf(editable);
  if (!list.some((c) => c.key === key)) return editable;
  return { ...editable, criteria: list.map((c) => (c.key === key ? fn(c) : c)) };
}

export function updateCriterion(editable, key, patch) {
  return mapCriterion(editable, key, (c) => {
    const next = { ...c };
    if (patch && 'label' in patch) next.label = patch.label;
    if (patch && 'section' in patch) next.section = patch.section;
    return next;
  });
}

export function addIndicator(editable, criterionKey) {
  return mapCriterion(editable, criterionKey, (c) => ({
    ...c,
    indicators: [...c.indicators, newIndicator('')],
  }));
}

export function updateIndicator(editable, criterionKey, indicatorKey, text) {
  const criterion = criteriaOf(editable).find((c) => c.key === criterionKey);
  if (!criterion || !criterion.indicators.some((i) => i.key === indicatorKey)) return editable;
  return mapCriterion(editable, criterionKey, (c) => ({
    ...c,
    indicators: c.indicators.map((i) => (i.key === indicatorKey ? { ...i, text } : i)),
  }));
}

export function removeIndicator(editable, criterionKey, indicatorKey) {
  const criterion = criteriaOf(editable).find((c) => c.key === criterionKey);
  if (!criterion || !criterion.indicators.some((i) => i.key === indicatorKey)) return editable;
  return mapCriterion(editable, criterionKey, (c) => ({
    ...c,
    indicators: c.indicators.filter((i) => i.key !== indicatorKey),
  }));
}

/** Distinct non-blank sections in first-appearance order (for a datalist). */
export function sectionSuggestions(editable) {
  const seen = new Set();
  const out = [];
  for (const c of criteriaOf(editable)) {
    const section = str(c.section).trim();
    if (section && !seen.has(section)) {
      seen.add(section);
      out.push(section);
    }
  }
  return out;
}
