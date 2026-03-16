import { memo, useState } from "react";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";
import type { AssetPageItem } from "../api/types";

interface AssetCellProps {
  asset: AssetPageItem;
  onClick: () => void;
  aspectRatio?: number;
}

function formatDuration(sec: number): string {
  const totalSec = Math.round(sec);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function basename(relPath: string): string {
  const i = relPath.lastIndexOf("/");
  return i >= 0 ? relPath.slice(i + 1) : relPath;
}

function AssetCellInner({ asset, onClick, aspectRatio }: AssetCellProps) {
  const { url, isLoading, error } = useAuthenticatedImage(
    asset.asset_id,
    "thumbnail",
  );
  const [hovered, setHovered] = useState(false);
  const filename = basename(asset.rel_path);
  const isVideo = asset.media_type === "video" || asset.media_type.startsWith("video/");
  const effectiveAspectRatio = aspectRatio ?? 4 / 3;

  const { url: videoUrl } = useAuthenticatedImage(
    asset.asset_id,
    "video-preview",
    { enabled: isVideo && hovered },
  );

  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      className="group relative w-full cursor-pointer overflow-hidden rounded-lg bg-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2 focus:ring-offset-gray-950"
      style={{ aspectRatio: String(effectiveAspectRatio) }}
    >
      {/* Letterbox/pillarbox dark fill */}
      <div className="absolute inset-0 flex items-center justify-center bg-gray-900">
        {isLoading && (
          <div className="h-full w-full animate-pulse bg-gray-800" aria-hidden />
        )}
        {error && (
          <div className="flex h-full w-full items-center justify-center bg-gray-900">
            <svg
              className="h-10 w-10 text-gray-600"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              aria-hidden
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={1.5}
                d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"
              />
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={1.5}
                d="M12 9v2m0 4h.01"
              />
            </svg>
          </div>
        )}
        {url && !error && (
          <img
            src={url}
            alt={filename}
            className="h-full w-full object-cover"
          />
        )}
        {/* Muted hover preview for videos */}
        {isVideo && videoUrl && (
          <video
            src={videoUrl}
            autoPlay
            muted
            loop
            playsInline
            className="absolute inset-0 h-full w-full object-cover"
          />
        )}
      </div>

      {/* Video duration badge */}
      {isVideo && (
        <div className="pointer-events-none absolute right-1.5 top-1.5 flex items-center gap-1 rounded-full bg-black/60 px-1.5 py-0.5">
          <svg className="h-3 w-3 shrink-0 text-white" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
            <path d="M8 5v14l11-7z" />
          </svg>
          {asset.duration_sec != null && (
            <span className="text-xs font-medium tabular-nums text-white">
              {formatDuration(asset.duration_sec)}
            </span>
          )}
        </div>
      )}

      {/* Hover overlay */}
      <div
        className="pointer-events-none absolute inset-0 bg-gradient-to-t from-black/70 via-transparent to-transparent opacity-0 transition-opacity duration-150 group-hover:opacity-100"
        aria-hidden
      />
      <div className="pointer-events-none absolute bottom-0 left-0 right-0 p-2 opacity-0 transition-opacity duration-150 group-hover:opacity-100">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium text-white">
            {filename}
          </span>
          {isVideo && (
            <span className="shrink-0 rounded bg-gray-700/80 px-2 py-0.5 text-xs text-gray-300">
              Video
            </span>
          )}
        </div>
      </div>
    </button>
  );
}

export const AssetCell = memo(AssetCellInner);
