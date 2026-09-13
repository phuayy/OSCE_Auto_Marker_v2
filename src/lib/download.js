// Handing the browser a file, and the CSV encoding behind the score sheet.
//
// csvCell prefixes a leading =, +, - or @ with an apostrophe: a rubric reason
// starting with one of those is a formula to Excel, and an examiner opening a
// downloaded sheet must not run it.

export function downloadBlob(fileName, blob) {
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

export function toSafeDownloadName(value, fallback) {
  return String(value || fallback)
    .replace(/[^a-zA-Z0-9-_]/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 64) || fallback;
}

export function csvCell(value) {
  if (value === null || value === undefined) {
    return '';
  }

  let text = String(value).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  if (typeof value === 'string' && /^[=+\-@\t]/.test(text)) {
    text = `'${text}`;
  }

  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function rowsToCsv(rows) {
  return rows.map((row) => row.map(csvCell).join(',')).join('\r\n');
}
