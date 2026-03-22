import { useCallback, useEffect, useRef, useState } from "react";

interface DatePreset {
  label: string;
  from: string;
  to: string;
}

function getDatePresets(): DatePreset[] {
  const today = new Date();
  const fmt = (d: Date) => d.toISOString().slice(0, 10);
  const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate());

  const todayStr = fmt(startOf(today));
  const yday = new Date(today);
  yday.setDate(today.getDate() - 1);
  const weekStart = new Date(today);
  weekStart.setDate(today.getDate() - today.getDay());
  const monthStart = new Date(today.getFullYear(), today.getMonth(), 1);
  const yearStart = new Date(today.getFullYear(), 0, 1);

  return [
    { label: "Today", from: todayStr, to: todayStr },
    { label: "Yesterday", from: fmt(startOf(yday)), to: fmt(startOf(yday)) },
    { label: "This week", from: fmt(weekStart), to: todayStr },
    { label: "This month", from: fmt(monthStart), to: todayStr },
    { label: "This year", from: fmt(yearStart), to: todayStr },
  ];
}

function formatDateChiclet(from: string, to: string): string {
  const parseLocal = (s: string) => new Date(s + "T00:00:00");
  const fmtShort = (d: Date) =>
    d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const fmtFull = (d: Date) =>
    d.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
    });

  if (from === to) return fmtFull(parseLocal(from));

  const df = parseLocal(from);
  const dt = parseLocal(to);
  if (df.getFullYear() === dt.getFullYear()) {
    if (df.getMonth() === dt.getMonth()) {
      return `${fmtShort(df)} – ${dt.getDate()}, ${df.getFullYear()}`;
    }
    return `${fmtShort(df)} – ${fmtShort(dt)}, ${df.getFullYear()}`;
  }
  return `${fmtFull(df)} – ${fmtFull(dt)}`;
}

interface FilterBarProps {
  q: string | null;
  tag: string | null;
  path: string | null;
  dateFrom: string | null;
  dateTo: string | null;
  onChangeQ: (q: string | null) => void;
  onChangeTag: (tag: string | null) => void;
  onChangePath: (path: string | null) => void;
  onChangeDateRange: (from: string | null, to: string | null) => void;
}

export function FilterBar({
  q,
  tag,
  path,
  dateFrom,
  dateTo,
  onChangeQ,
  onChangeTag,
  onChangePath,
  onChangeDateRange,
}: FilterBarProps) {
  const [inputValue, setInputValue] = useState(q ?? "");
  const [showDateRow, setShowDateRow] = useState(Boolean(dateFrom));
  const [customFrom, setCustomFrom] = useState(dateFrom ?? "");
  const [customTo, setCustomTo] = useState(dateTo ?? "");
  const [showCustom, setShowCustom] = useState(false);
  const customFromRef = useRef<HTMLInputElement>(null);

  // Keep input in sync when external q changes
  useEffect(() => {
    setInputValue(q ?? "");
  }, [q]);

  // Sync custom fields when external date changes
  useEffect(() => {
    setCustomFrom(dateFrom ?? "");
    setCustomTo(dateTo ?? "");
    if (dateFrom) setShowDateRow(true);
  }, [dateFrom, dateTo]);

  const applySearch = useCallback(
    (value: string) => {
      const trimmed = value.trim();
      onChangeQ(trimmed.length > 0 ? trimmed : null);
    },
    [onChangeQ],
  );

  useEffect(() => {
    const handle = window.setTimeout(() => {
      applySearch(inputValue);
    }, 500);
    return () => window.clearTimeout(handle);
  }, [inputValue, applySearch]);

  const handleKeyDown: React.KeyboardEventHandler<HTMLInputElement> = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      applySearch(inputValue);
    }
  };

  const handleClear = () => {
    setInputValue("");
    onChangeQ(null);
  };

  const applyPreset = (preset: DatePreset) => {
    setCustomFrom(preset.from);
    setCustomTo(preset.to);
    setShowCustom(false);
    onChangeDateRange(preset.from, preset.to);
  };

  const applyCustom = () => {
    if (customFrom && customTo) {
      onChangeDateRange(customFrom, customTo);
    } else if (customFrom) {
      onChangeDateRange(customFrom, customFrom);
    }
  };

  const clearDate = () => {
    setCustomFrom("");
    setCustomTo("");
    setShowCustom(false);
    onChangeDateRange(null, null);
  };

  const toggleDateRow = () => {
    const next = !showDateRow;
    setShowDateRow(next);
    if (!next) clearDate();
  };

  const hasDateFilter = Boolean(dateFrom);

  const showQChiclet = q !== null && q.length > 0;
  const showTagChiclet = tag !== null && tag.length > 0;
  const showPathChiclet = path !== null && path.length > 0;
  const presets = getDatePresets();

  return (
    <div className="flex flex-col gap-2">
      {/* Row 1: search input + date toggle + chiclets */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[200px] max-w-xl">
          <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-gray-500">
            <svg
              className="h-4 w-4"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden
            >
              <circle cx="11" cy="11" r="7" />
              <line x1="16.65" y1="16.65" x2="21" y2="21" />
            </svg>
          </span>
          <input
            type="search"
            className="w-full rounded-lg border border-gray-700 bg-gray-800 pl-9 pr-8 py-2 text-sm text-gray-100 placeholder:text-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            placeholder="Search …"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
          />
          {inputValue && (
            <button
              type="button"
              onClick={handleClear}
              className="absolute inset-y-0 right-2 flex items-center text-gray-500 hover:text-gray-200"
              aria-label="Clear search"
            >
              ×
            </button>
          )}
        </div>

        {/* Date filter toggle button */}
        <button
          type="button"
          onClick={toggleDateRow}
          title={showDateRow ? "Hide date filter" : "Filter by date"}
          aria-label="Toggle date filter"
          className={`relative flex h-9 w-9 items-center justify-center rounded-lg border transition-colors ${
            showDateRow
              ? "border-indigo-500 bg-indigo-600/20 text-indigo-300"
              : "border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600 hover:text-gray-200"
          }`}
        >
          <svg
            className="h-4 w-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden
          >
            <rect x="3" y="4" width="18" height="18" rx="2" />
            <line x1="16" y1="2" x2="16" y2="6" />
            <line x1="8" y1="2" x2="8" y2="6" />
            <line x1="3" y1="10" x2="21" y2="10" />
          </svg>
          {hasDateFilter && (
            <span className="absolute -top-0.5 -right-0.5 h-2 w-2 rounded-full bg-indigo-500" />
          )}
        </button>

        {/* Active chiclets */}
        <div className="flex flex-wrap items-center gap-2">
          {showPathChiclet && (
            <button
              type="button"
              onClick={() => onChangePath(null)}
              className="inline-flex items-center gap-1 rounded-full bg-gray-700 px-3 py-1 text-sm text-gray-200"
            >
              <span>📁 {path}</span>
              <span className="ml-1 text-gray-400 hover:text-gray-100">×</span>
            </button>
          )}
          {showQChiclet && (
            <button
              type="button"
              onClick={() => onChangeQ(null)}
              className="inline-flex items-center gap-1 rounded-full bg-gray-700 px-3 py-1 text-sm text-gray-200"
            >
              <span>🔍 {q}</span>
              <span className="ml-1 text-gray-400 hover:text-gray-100">×</span>
            </button>
          )}
          {showTagChiclet && (
            <button
              type="button"
              onClick={() => onChangeTag(null)}
              className="inline-flex items-center gap-1 rounded-full bg-gray-700 px-3 py-1 text-sm text-gray-200"
            >
              <span>🏷 {tag}</span>
              <span className="ml-1 text-gray-400 hover:text-gray-100">×</span>
            </button>
          )}
          {hasDateFilter && dateFrom && dateTo && (
            <button
              type="button"
              onClick={clearDate}
              className="inline-flex items-center gap-1 rounded-full bg-gray-700 px-3 py-1 text-sm text-gray-200"
            >
              <span>📅 {formatDateChiclet(dateFrom, dateTo)}</span>
              <span className="ml-1 text-gray-400 hover:text-gray-100">×</span>
            </button>
          )}
        </div>
      </div>

      {/* Row 2: date presets (shown when date row is open) */}
      {showDateRow && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-gray-700/50 bg-gray-900/60 px-3 py-2">
          {presets.map((p) => {
            const isActive = dateFrom === p.from && dateTo === p.to;
            return (
              <button
                key={p.label}
                type="button"
                onClick={() => applyPreset(p)}
                className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                  isActive
                    ? "bg-indigo-600 text-white"
                    : "bg-gray-800 text-gray-300 hover:bg-gray-700 hover:text-gray-100"
                }`}
              >
                {p.label}
              </button>
            );
          })}

          <button
            type="button"
            onClick={() => {
              setShowCustom((v) => !v);
              if (!showCustom) setTimeout(() => customFromRef.current?.focus(), 0);
            }}
            className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
              showCustom
                ? "bg-gray-700 text-gray-100"
                : "bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200"
            }`}
          >
            Custom ▸
          </button>

          {showCustom && (
            <div className="flex items-center gap-2 mt-1 w-full">
              <input
                ref={customFromRef}
                type="date"
                value={customFrom}
                onChange={(e) => setCustomFrom(e.target.value)}
                className="rounded-md border border-gray-700 bg-gray-800 px-2 py-1 text-xs text-gray-200 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 [color-scheme:dark]"
                aria-label="Date from"
              />
              <span className="text-xs text-gray-500">to</span>
              <input
                type="date"
                value={customTo}
                onChange={(e) => setCustomTo(e.target.value)}
                min={customFrom}
                className="rounded-md border border-gray-700 bg-gray-800 px-2 py-1 text-xs text-gray-200 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 [color-scheme:dark]"
                aria-label="Date to"
              />
              <button
                type="button"
                onClick={applyCustom}
                disabled={!customFrom}
                className="rounded-md bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-40"
              >
                Apply
              </button>
            </div>
          )}

          {hasDateFilter && (
            <button
              type="button"
              onClick={clearDate}
              className="ml-auto text-xs text-gray-500 hover:text-gray-300"
            >
              Clear date
            </button>
          )}
        </div>
      )}
    </div>
  );
}
