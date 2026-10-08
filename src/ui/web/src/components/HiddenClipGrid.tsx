import { useAuthenticatedImage } from "../api/useAuthenticatedImage";
import type { HiddenClip } from "../api/client";

function basename(relPath: string): string {
  const i = relPath.lastIndexOf("/");
  return i >= 0 ? relPath.slice(i + 1) : relPath;
}

function folderOf(relPath: string): string {
  const i = relPath.lastIndexOf("/");
  return i >= 0 ? relPath.slice(0, i) : "";
}

function Card<T extends HiddenClip>({
  clip,
  selected,
  selectable,
  onToggle,
  meta,
}: {
  clip: T;
  selected: boolean;
  selectable: boolean;
  onToggle: () => void;
  meta: React.ReactNode;
}) {
  const { url } = useAuthenticatedImage(clip.asset_id, "thumbnail");
  const name = basename(clip.rel_path);
  const where = [clip.library_name, folderOf(clip.rel_path)].filter(Boolean).join(" · ");
  const inner = (
    <>
      <div className="relative aspect-[4/3] w-full overflow-hidden rounded-lg bg-gray-800">
        {url ? (
          <img src={url} alt="" className="h-full w-full object-cover" loading="lazy" />
        ) : (
          <div className="h-full w-full animate-pulse bg-gray-800" />
        )}
        {selectable && (
          <span
            aria-hidden
            className={`absolute left-2 top-2 flex h-5 w-5 items-center justify-center rounded border text-xs ${
              selected ? "border-indigo-400 bg-indigo-500 text-white" : "border-gray-400/70 bg-black/40 text-transparent"
            }`}
          >
            ✓
          </span>
        )}
      </div>
      <div className="mt-1.5 min-w-0 text-left">
        <p className="truncate text-sm text-gray-200" title={clip.rel_path}>{name}</p>
        <p className="truncate text-xs text-gray-500" title={where}>{where}</p>
        <div className="text-xs text-gray-400">{meta}</div>
      </div>
    </>
  );
  if (!selectable) return <div className="min-w-0">{inner}</div>;
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={selected}
      aria-label={name}
      onClick={onToggle}
      className={`min-w-0 rounded-xl p-1.5 text-left transition-colors ${
        selected ? "bg-indigo-600/20 ring-1 ring-indigo-500/60" : "hover:bg-gray-800/60"
      }`}
    >
      {inner}
    </button>
  );
}

/** Clips out of sight (in the trash or archived), with their pictures, to pick and act on. */
export function HiddenClipGrid<T extends HiddenClip>({
  clips,
  selected,
  selectable,
  onToggle,
  meta,
}: {
  clips: T[];
  selected: Set<string>;
  selectable: boolean;
  onToggle: (assetId: string) => void;
  meta: (clip: T) => React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
      {clips.map((clip) => (
        <Card
          key={clip.asset_id}
          clip={clip}
          selected={selected.has(clip.asset_id)}
          selectable={selectable}
          onToggle={() => onToggle(clip.asset_id)}
          meta={meta(clip)}
        />
      ))}
    </div>
  );
}
