import { useCallback, useEffect, useState } from "react";

interface FilterBarProps {
  q: string | null;
  tag: string | null;
  path: string | null;
  onChangeQ: (q: string | null) => void;
  onChangeTag: (tag: string | null) => void;
  onChangePath: (path: string | null) => void;
}

export function FilterBar({
  q,
  tag,
  path,
  onChangeQ,
  onChangeTag,
  onChangePath,
}: FilterBarProps) {
  const [inputValue, setInputValue] = useState(q ?? "");

  // Keep input in sync when external q changes
  useEffect(() => {
    setInputValue(q ?? "");
  }, [q]);

  const applySearch = useCallback(
    (value: string) => {
      const trimmed = value.trim();
      onChangeQ(trimmed.length > 0 ? trimmed : null);
    },
    [onChangeQ],
  );

  // Debounce search input
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

  const showQChiclet = q !== null && q.length > 0;
  const showTagChiclet = tag !== null && tag.length > 0;
  const showPathChiclet = path !== null && path.length > 0;

  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative flex-1 min-w-[220px] max-w-xl">
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
      </div>
    </div>
  );
}

