import { newClipsLabel } from "../lib/useScrollAnchor";

interface NewClipsPillProps {
  count: number;
  onClick: () => void;
}

/**
 * "3 new clips", held at the top of the screen over a scrolled grid. Put it
 * first inside the grid's box: it takes no height there, so showing it moves
 * nothing.
 */
export function NewClipsPill({ count, onClick }: NewClipsPillProps) {
  return (
    <div className="pointer-events-none sticky top-2 z-20 flex h-0 justify-center">
      {count > 0 && (
        <button
          type="button"
          onClick={onClick}
          className="pointer-events-auto flex h-8 items-center gap-1.5 whitespace-nowrap rounded-full bg-indigo-600 px-3 text-xs font-medium text-white shadow-lg shadow-black/40 hover:bg-indigo-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-300"
        >
          <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" aria-hidden>
            <path d="M12 19V5M5 12l7-7 7 7" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          {newClipsLabel(count)}
        </button>
      )}
    </div>
  );
}
