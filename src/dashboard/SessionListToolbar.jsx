import React, { useEffect, useRef, useState } from 'react';
import { Search, X } from 'lucide-react';
import {
  FILTER_CHIPS,
  SEARCH_DEBOUNCE_MS,
  describeResultCount,
  normalizeQuery,
} from '@/lib/sessionFilters';

// The search box, the status chips and the count above the Saved sessions
// list. The rules (what a chip asks the server, what text is sent) live in
// lib/sessionFilters.js; this only renders them and debounces the typing.
//
// The text box is the one place that holds text the filter does not yet:
// typing updates `text` at once and the filter after SEARCH_DEBOUNCE_MS (or
// on Enter), so a request is not issued per keystroke. Clear and Escape
// apply immediately.
export function SessionListToolbar({
  filter,
  onFilterChange,
  shownCount,
  hasMore,
  selectableCount,
  selectedCount,
  onToggleSelectAll,
}) {
  const [text, setText] = useState(filter.q);
  const onChangeRef = useRef(onFilterChange);
  onChangeRef.current = onFilterChange;
  const filterRef = useRef(filter);
  filterRef.current = filter;
  const selectAllRef = useRef(null);

  useEffect(() => {
    if (normalizeQuery(text) === filterRef.current.q) return undefined;
    const id = window.setTimeout(() => {
      onChangeRef.current({ ...filterRef.current, q: normalizeQuery(text) });
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(id);
  }, [text]);

  const allSelected = selectableCount > 0 && selectedCount >= selectableCount;
  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = selectedCount > 0 && !allSelected;
    }
  }, [selectedCount, allSelected]);

  function applyNow(nextText) {
    setText(nextText);
    onFilterChange({ ...filter, q: normalizeQuery(nextText) });
  }

  return (
    <div className="space-y-2">
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" aria-hidden="true" />
        <input
          type="search"
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape' && text) {
              event.preventDefault();
              applyNow('');
            } else if (event.key === 'Enter') {
              event.preventDefault();
              applyNow(text);
            }
          }}
          aria-label="Search sessions by name"
          placeholder="Search sessions by name"
          autoComplete="off"
          className="h-9 w-full rounded-md border border-slate-300 bg-white pl-9 pr-9 text-sm text-slate-800 placeholder:text-slate-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500"
        />
        {text ? (
          <button
            type="button"
            onClick={() => applyNow('')}
            aria-label="Clear search"
            className="absolute right-2 top-1/2 flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded text-slate-500 hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500"
          >
            <X className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        ) : null}
      </div>

      <div role="radiogroup" aria-label="Show sessions" className="flex flex-wrap gap-1.5">
        {FILTER_CHIPS.map((chip) => {
          const active = filter.chip === chip.id;
          return (
            <button
              key={chip.id}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => onFilterChange({ ...filter, chip: chip.id })}
              className={`rounded-full border px-3 py-1 text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 focus-visible:ring-offset-2 ${
                active
                  ? 'border-cyan-400 bg-cyan-50 text-cyan-800 ring-1 ring-cyan-200'
                  : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300'
              }`}
            >
              {chip.label}
            </button>
          );
        })}
      </div>

      <div className="flex items-center justify-between gap-2 text-xs text-slate-500">
        <label className="flex items-center gap-2">
          <input
            ref={selectAllRef}
            type="checkbox"
            checked={allSelected}
            disabled={selectableCount === 0}
            onChange={onToggleSelectAll}
            className="h-4 w-4 rounded border-slate-300 text-cyan-600 focus-visible:ring-2 focus-visible:ring-cyan-500"
          />
          Select all shown
        </label>
        <span role="status" aria-live="polite">
          {describeResultCount(shownCount, filter, hasMore)}
        </span>
      </div>
    </div>
  );
}

export default SessionListToolbar;
