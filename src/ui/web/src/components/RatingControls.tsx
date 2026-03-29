import { type RatingColor, RATING_COLORS, COLOR_HEX } from "../api/types";

// ---------------------------------------------------------------------------
// Heart (favorite) toggle
// ---------------------------------------------------------------------------

export function HeartButton({
  favorite,
  onClick,
  size = "md",
}: {
  favorite: boolean;
  onClick: () => void;
  size?: "sm" | "md";
}) {
  const cls = size === "sm" ? "h-4 w-4" : "h-5 w-5";
  return (
    <button
      type="button"
      onClick={onClick}
      title={favorite ? "Remove from favorites" : "Add to favorites"}
      className={`transition-colors ${favorite ? "text-red-500 hover:text-red-400" : "text-gray-500 hover:text-red-400"}`}
    >
      <svg className={cls} viewBox="0 0 24 24" fill={favorite ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2" aria-hidden>
        <path strokeLinecap="round" strokeLinejoin="round" d="M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z" />
      </svg>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Star rating picker
// ---------------------------------------------------------------------------

export function StarPicker({
  stars,
  onChange,
  size = "md",
}: {
  stars: number;
  onChange: (stars: number) => void;
  size?: "sm" | "md";
}) {
  const cls = size === "sm" ? "h-4 w-4" : "h-5 w-5";
  return (
    <div className="flex items-center gap-0.5">
      {[1, 2, 3, 4, 5].map((n) => (
        <button
          key={n}
          type="button"
          onClick={() => onChange(n === stars ? 0 : n)}
          title={n === stars ? "Clear rating" : `${n} star${n > 1 ? "s" : ""}`}
          className={`transition-colors ${n <= stars ? "text-amber-400" : "text-gray-600 hover:text-amber-300"}`}
        >
          <svg className={cls} viewBox="0 0 24 24" fill={n <= stars ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2" aria-hidden>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
          </svg>
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Color label picker
// ---------------------------------------------------------------------------

export function ColorPicker({
  color,
  onChange,
  size = "md",
}: {
  color: RatingColor | null;
  onChange: (color: RatingColor | null) => void;
  size?: "sm" | "md";
}) {
  const dotCls = size === "sm" ? "h-3.5 w-3.5" : "h-4 w-4";
  return (
    <div className="flex items-center gap-1.5">
      {RATING_COLORS.map((c) => (
        <button
          key={c}
          type="button"
          onClick={() => onChange(c === color ? null : c)}
          title={c === color ? "Clear color" : c.charAt(0).toUpperCase() + c.slice(1)}
          className={`rounded-full ${dotCls} transition-all ${
            c === color
              ? "ring-2 ring-white/70 ring-offset-1 ring-offset-gray-900"
              : "hover:ring-1 hover:ring-white/30 hover:ring-offset-1 hover:ring-offset-gray-900"
          }`}
          style={{ backgroundColor: COLOR_HEX[c] }}
        />
      ))}
      {color && (
        <button
          type="button"
          onClick={() => onChange(null)}
          title="Clear color"
          className="ml-0.5 text-gray-500 hover:text-gray-300"
        >
          <svg className={dotCls} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Compact inline rating display (for grid overlays)
// ---------------------------------------------------------------------------

export function RatingBadges({
  favorite,
  stars,
  color,
  isVideo,
}: {
  favorite: boolean;
  stars: number;
  color: RatingColor | null;
  isVideo?: boolean;
}) {
  if (!favorite && stars === 0 && !color) return null;

  return (
    <>
      {/* Heart — bottom-right corner */}
      {favorite && (
        <div className="pointer-events-none absolute right-1.5 bottom-1.5 flex items-center">
          <svg className="h-3.5 w-3.5 text-red-500 drop-shadow-md" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
            <path d="M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z" />
          </svg>
        </div>
      )}

      {/* Stars — top-right for images, below video badge for videos */}
      {stars > 0 && !isVideo && (
        <div className="pointer-events-none absolute right-1.5 top-1.5 flex items-center gap-0.5 rounded-full bg-black/60 px-1.5 py-0.5">
          <svg className="h-3 w-3 text-amber-400" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
            <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
          </svg>
          <span className="text-xs font-medium tabular-nums text-white">{stars}</span>
        </div>
      )}
      {stars > 0 && isVideo && (
        <div className="pointer-events-none absolute left-1.5 bottom-1.5 flex items-center gap-0.5 rounded-full bg-black/60 px-1.5 py-0.5">
          <svg className="h-3 w-3 text-amber-400" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
            <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
          </svg>
          <span className="text-xs font-medium tabular-nums text-white">{stars}</span>
        </div>
      )}

      {/* Color — bottom border */}
      {color && (
        <div
          className="pointer-events-none absolute bottom-0 left-0 right-0 h-[3px] rounded-b-lg"
          style={{ backgroundColor: COLOR_HEX[color] }}
        />
      )}
    </>
  );
}
