import { useEffect, useRef, useState, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { listLibraries } from "../api/client";

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
}

export function CommandPalette({ open, onClose }: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  const { data: libraries } = useQuery({
    queryKey: ["libraries", false],
    queryFn: () => listLibraries(false),
    staleTime: 60_000,
  });

  const items = useMemo(
    () => (libraries ?? []).filter((l) => l.status !== "trashed"),
    [libraries],
  );

  const filtered = useMemo(() => {
    if (!query.trim()) return items;
    const lower = query.toLowerCase();
    return items.filter((l) => l.name.toLowerCase().includes(lower));
  }, [items, query]);

  // Reset state on open/close
  useEffect(() => {
    if (open) {
      setQuery("");
      setActiveIndex(0);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  // Clamp active index when filtered list changes
  useEffect(() => {
    setActiveIndex((i) => Math.min(i, Math.max(filtered.length - 1, 0)));
  }, [filtered.length]);

  const selectLibrary = (libraryId: string) => {
    navigate(`/libraries/${libraryId}/browse`);
    onClose();
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      const lib = filtered[activeIndex];
      if (lib) selectLibrary(lib.library_id);
    } else if (e.key === "Escape") {
      onClose();
    }
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-24 px-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg rounded-xl bg-gray-900 border border-gray-700
                   shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Search input */}
        <div className="flex items-center gap-3 px-4 py-3 border-b border-gray-800">
          <svg
            className="h-4 w-4 text-gray-500 shrink-0"
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
          <input
            ref={inputRef}
            placeholder="Go to library…"
            className="flex-1 bg-transparent text-sm text-gray-100 placeholder-gray-500 outline-none"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            aria-label="Search libraries"
          />
          <kbd className="text-xs text-gray-600 font-mono">Esc</kbd>
        </div>

        {/* Results */}
        <ul className="max-h-72 overflow-y-auto py-2" role="listbox">
          {filtered.length === 0 ? (
            <li className="px-4 py-3 text-sm text-gray-500">
              {query ? `No libraries match "${query}"` : "No libraries yet"}
            </li>
          ) : (
            filtered.map((lib, i) => (
              <li key={lib.library_id} role="option" aria-selected={i === activeIndex}>
                <button
                  type="button"
                  className={`w-full text-left px-4 py-2.5 flex items-center gap-3 text-sm transition-colors ${
                    i === activeIndex
                      ? "bg-indigo-600/30 text-indigo-200"
                      : "text-gray-300 hover:bg-gray-800"
                  }`}
                  onClick={() => selectLibrary(lib.library_id)}
                  onMouseEnter={() => setActiveIndex(i)}
                >
                  {/* Library icon */}
                  <svg
                    className="h-4 w-4 text-gray-500 shrink-0"
                    viewBox="0 0 24 24"
                    fill="none"
                    aria-hidden
                  >
                    <rect x="4" y="7" width="14" height="11" rx="2" stroke="currentColor" strokeWidth="1.5" />
                    <rect x="7" y="4" width="13" height="11" rx="2" stroke="currentColor" strokeWidth="1.5" strokeOpacity="0.5" />
                  </svg>
                  <span className="flex-1 truncate">{lib.name}</span>
                  <span
                    className={`h-2 w-2 rounded-full shrink-0 ${
                      lib.scan_status === "running" || lib.scan_status === "scanning"
                        ? "bg-amber-500"
                        : lib.scan_status === "error"
                          ? "bg-red-500"
                          : "bg-emerald-500"
                    }`}
                  />
                </button>
              </li>
            ))
          )}
        </ul>
      </div>
    </div>
  );
}
