import { useEffect, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { getAsset } from "../api/client";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";
import type { AssetDetail, AssetPageItem } from "../api/types";

interface LightboxProps {
  asset: AssetPageItem;
  assets: AssetPageItem[];
  onClose: () => void;
  onNavigate: (index: number) => void;
}

function basename(relPath: string): string {
  const i = relPath.lastIndexOf("/");
  return i >= 0 ? relPath.slice(i + 1) : relPath;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso: string | null): string {
  if (!iso) return "Unknown";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return "Unknown";
  }
}

function MetadataSkeleton() {
  return (
    <div className="space-y-3">
      <div className="h-4 w-3/4 animate-pulse rounded bg-gray-700" />
      <div className="h-3 w-full animate-pulse rounded bg-gray-700/80" />
      <div className="h-3 w-1/2 animate-pulse rounded bg-gray-700/80" />
    </div>
  );
}

export function Lightbox({
  asset,
  assets,
  onClose,
  onNavigate,
}: LightboxProps) {
  const currentIndex = assets.findIndex((a) => a.asset_id === asset.asset_id);
  const hasPrev = currentIndex > 0;
  const hasNext = currentIndex >= 0 && currentIndex < assets.length - 1;

  const { url: proxyUrl, isLoading: proxyLoading } = useAuthenticatedImage(
    asset.asset_id,
    "proxy",
  );
  const { data: detail, isLoading: detailLoading } = useQuery({
    queryKey: ["asset", asset.asset_id],
    queryFn: () => getAsset(asset.asset_id),
  });

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowLeft" && hasPrev) onNavigate(currentIndex - 1);
      if (e.key === "ArrowRight" && hasNext) onNavigate(currentIndex + 1);
    },
    [onClose, onNavigate, hasPrev, hasNext, currentIndex],
  );

  useEffect(() => {
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  const filename = basename(asset.rel_path);

  return (
    <div className="fixed inset-0 z-50 flex bg-black/90">
      {/* Close button */}
      <button
        type="button"
        onClick={onClose}
        className="absolute right-4 top-4 z-10 rounded-full p-2 text-2xl text-white/80 transition-colors hover:bg-white/10 hover:text-white"
        aria-label="Close"
      >
        ×
      </button>

      {/* Two-column layout */}
      <div className="flex w-full flex-col lg:flex-row">
        {/* Left: image */}
        <div className="flex flex-1 items-center justify-center p-4 lg:p-8">
          <div className="relative flex max-h-full min-h-0 flex-1 items-center justify-center">
            {proxyLoading ? (
              <div className="flex h-64 w-64 items-center justify-center">
                <div
                  className="h-12 w-12 animate-spin rounded-full border-2 border-white/30 border-t-white"
                  aria-hidden
                />
              </div>
            ) : proxyUrl ? (
              <img
                src={proxyUrl}
                alt={filename}
                className="max-h-[calc(100vh-4rem)] max-w-full object-contain"
              />
            ) : (
              <div className="text-gray-500">Image unavailable</div>
            )}

            {/* Navigation arrows */}
            {hasPrev && (
              <button
                type="button"
                onClick={() => onNavigate(currentIndex - 1)}
                className="absolute left-0 top-1/2 -translate-y-1/2 rounded-full bg-white/10 p-4 text-white transition-colors hover:bg-white/20"
                aria-label="Previous"
              >
                <svg
                  className="h-8 w-8"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M15 19l-7-7 7-7"
                  />
                </svg>
              </button>
            )}
            {hasNext && (
              <button
                type="button"
                onClick={() => onNavigate(currentIndex + 1)}
                className="absolute right-0 top-1/2 -translate-y-1/2 rounded-full bg-white/10 p-4 text-white transition-colors hover:bg-white/20"
                aria-label="Next"
              >
                <svg
                  className="h-8 w-8"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M9 5l7 7-7 7"
                  />
                </svg>
              </button>
            )}
          </div>
        </div>

        {/* Right: metadata panel */}
        <div className="w-full overflow-y-auto border-t border-gray-700 bg-gray-900/50 p-6 lg:w-80 lg:border-l lg:border-t-0">
          <div className="space-y-4">
            <div>
              <div className="text-lg font-medium text-gray-100">{filename}</div>
              <div className="mt-1 font-mono text-xs text-gray-500">
                {asset.rel_path}
              </div>
            </div>

            <hr className="border-gray-700" />

            {/* AI description */}
            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wide text-gray-500">
                AI description
              </div>
              {detailLoading ? (
                <MetadataSkeleton />
              ) : detail?.ai_description ? (
                <p className="italic text-gray-400">{detail.ai_description}</p>
              ) : (
                <p className="text-gray-500">No description yet</p>
              )}
            </div>

            <hr className="border-gray-700" />

            {/* Details table */}
            <div>
              <div className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                Details
              </div>
              {detailLoading ? (
                <MetadataSkeleton />
              ) : (
                <table className="w-full text-sm">
                  <tbody className="space-y-1">
                    <tr>
                      <td className="text-gray-500">Taken at</td>
                      <td className="pl-4 text-gray-300">
                        {formatDate((detail as AssetDetail)?.taken_at ?? null)}
                      </td>
                    </tr>
                    <tr>
                      <td className="text-gray-500">Camera</td>
                      <td className="pl-4 text-gray-300">
                        {detail
                          ? [
                              (detail as AssetDetail).camera_make,
                              (detail as AssetDetail).camera_model,
                            ]
                              .filter(Boolean)
                              .join(" ") || "Unknown"
                          : "Unknown"}
                      </td>
                    </tr>
                    <tr>
                      <td className="text-gray-500">Dimensions</td>
                      <td className="pl-4 text-gray-300">
                        {(detail as AssetDetail)?.width != null &&
                        (detail as AssetDetail)?.height != null
                          ? `${(detail as AssetDetail).width} × ${(detail as AssetDetail).height}`
                          : "Unknown"}
                      </td>
                    </tr>
                    <tr>
                      <td className="text-gray-500">File size</td>
                      <td className="pl-4 text-gray-300">
                        {formatFileSize(asset.file_size)}
                      </td>
                    </tr>
                    <tr>
                      <td className="text-gray-500">SHA256</td>
                      <td
                        className="pl-4 font-mono text-gray-400"
                        title={asset.sha256 ?? undefined}
                      >
                        {asset.sha256
                          ? `${asset.sha256.slice(0, 16)}…`
                          : "Unknown"}
                      </td>
                    </tr>
                  </tbody>
                </table>
              )}
            </div>

            <hr className="border-gray-700" />

            {/* Media type badge */}
            <div>
              <span className="inline-flex rounded-full bg-gray-700/50 px-2.5 py-0.5 text-xs text-gray-300">
                {asset.media_type}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
