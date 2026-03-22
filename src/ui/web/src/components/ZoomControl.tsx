interface ZoomControlProps {
  value: number; // 0–4
  onChange: (v: number) => void;
}

export function ZoomControl({ value, onChange }: ZoomControlProps) {
  return (
    <div
      className="hidden sm:flex items-center gap-1.5 select-none"
      title="Grid density"
    >
      {/* Small photo icon */}
      <svg
        className="h-3 w-3 text-gray-500 shrink-0"
        viewBox="0 0 24 24"
        fill="none"
        aria-hidden
      >
        <rect x="3" y="5" width="18" height="14" rx="2" stroke="currentColor" strokeWidth="2" />
        <circle cx="8.5" cy="10.5" r="1.5" stroke="currentColor" strokeWidth="1.5" />
        <path d="M21 15l-5-5L5 19" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <input
        type="range"
        min={0}
        max={4}
        step={1}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-20 accent-indigo-500 cursor-ew-resize"
        aria-label="Grid density"
      />
      {/* Large photo icon */}
      <svg
        className="h-5 w-5 text-gray-500 shrink-0"
        viewBox="0 0 24 24"
        fill="none"
        aria-hidden
      >
        <rect x="3" y="5" width="18" height="14" rx="2" stroke="currentColor" strokeWidth="2" />
        <circle cx="8.5" cy="10.5" r="1.5" stroke="currentColor" strokeWidth="1.5" />
        <path d="M21 15l-5-5L5 19" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  );
}
