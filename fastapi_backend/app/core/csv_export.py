"""CSV encoding for files a person opens in a spreadsheet.

Two rules decide what a cell looks like, and both live here so no export
re-derives them:

* **Formula injection.** A text cell that starts with ``=``, ``+``, ``-``,
  ``@`` or a tab is a formula to Excel and Sheets. A rubric label, a student's
  name or an examiner's reason is free text, so such a cell is prefixed with an
  apostrophe and the spreadsheet shows it as text. Only *strings* are touched:
  a number typed as a number (``-2.5``) is data, not an instruction. This is
  the same rule as ``csvCell`` in ``src/lib/download.js`` — the browser's own
  CSVs and the server's must agree on what a cell means.
* **Excel and UTF-8.** Excel reads a CSV with no byte-order mark as the
  machine's legacy code page, which turns ``Zoë`` into ``ZoÃ«``. Every file
  therefore starts with :data:`CSV_BOM`, once, ahead of the header.

Lines end in CRLF (RFC 4180); a newline *inside* a cell is normalised to
``\\n`` and the cell quoted.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

CSV_BOM = "﻿"
CSV_NEWLINE = "\r\n"

_FORMULA_LEADERS = ("=", "+", "-", "@", "\t")


def csv_cell(value: Any) -> str:
    """One cell, escaped. ``None`` is an empty cell; a whole-number float
    (``3.0``, as the REAL columns return it) reads as ``3``."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    else:
        text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, str) and text.startswith(_FORMULA_LEADERS):
        text = f"'{text}"
    if any(ch in text for ch in ('"', ",", "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text


def csv_line(cells: Iterable[Any]) -> str:
    """One record, terminated by CRLF."""
    return ",".join(csv_cell(cell) for cell in cells) + CSV_NEWLINE
