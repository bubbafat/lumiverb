import type { ReactNode } from "react";

interface SelectionToolbarProps {
  count: number;
  onClear: () => void;
  children: ReactNode;
}

export function SelectionToolbar({ count, onClear, children }: SelectionToolbarProps) {
  if (count === 0) return null;

  return (
    <div className="fixed bottom-6 left-1/2 z-40 -translate-x-1/2">
      <div className="flex items-center gap-3 rounded-xl border border-gray-700 bg-gray-900/95 px-4 py-2.5 shadow-2xl backdrop-blur-sm">
        <span className="text-sm font-medium text-gray-300">
          {count} selected
        </span>
        <div className="h-4 w-px bg-gray-700" />
        {children}
        <div className="h-4 w-px bg-gray-700" />
        <button
          type="button"
          onClick={onClear}
          className="rounded-lg px-2 py-1 text-xs text-gray-500 transition-colors hover:bg-gray-800 hover:text-gray-300"
        >
          Clear
        </button>
      </div>
    </div>
  );
}
