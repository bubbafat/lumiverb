import { useEffect, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { getAsset } from "../api/client";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";
import type { AssetDetail, AssetPageItem } from "../api/types";
import { basename, formatFileSize, formatDate } from "../lib/format";

interface LightboxProps {
  asset: AssetPageItem;
  assets: AssetPageItem[];
  onClose: () => void;
  onNavigate: (index: number) => void;
  onTagClick?: (tag: string) => void;
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
  onTagClick,
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
  const formatGps = (lat: number, lon: number): string => {
    return `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
  };

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
            {/* Section 1: Basic info */}
            <div>
              <div className="text-lg font-medium text-gray-100">{filename}</div>
              <div className="mt-1 font-mono text-xs text-gray-500">
                {asset.rel_path}
              </div>
              <div className="mt-2 text-sm text-gray-400">
                {formatFileSize(asset.file_size)} · {asset.media_type}
              </div>
            </div>

            <hr className="border-gray-700" />

            {/* Section 2: AI description */}
            <div>
              <div className="mb-1 text-xs font-medium uppercase tracking-wide text-gray-500">
                Description
              </div>
              {detailLoading ? (
                <MetadataSkeleton />
              ) : detail?.ai_description ? (
                <p className="italic text-gray-300">{detail.ai_description}</p>
              ) : (
                <p className="text-gray-500">No description yet</p>
              )}
            </div>

            {/* Section 3: Tags */}
            {(detailLoading || (detail?.ai_tags && detail.ai_tags.length > 0)) && (
              <>
                <hr className="border-gray-700" />
                <div>
                  <div className="mb-1 text-xs font-medium uppercase tracking-wide text-gray-500">
                    Tags
                  </div>
                  {detailLoading && !detail ? (
                    <MetadataSkeleton />
                  ) : (
                    <div className="mt-1 flex flex-wrap gap-1.5">
                      {detail?.ai_tags?.map((tag) =>
                        onTagClick ? (
                          <button
                            key={tag}
                            type="button"
                            onClick={() => {
                              onTagClick(tag);
                              onClose();
                            }}
                            className="rounded-full bg-gray-700/60 px-2.5 py-0.5 text-xs text-gray-300 hover:bg-indigo-600/40 hover:text-indigo-200 transition-colors cursor-pointer"
                          >
                            {tag}
                          </button>
                        ) : (
                          <span
                            key={tag}
                            className="rounded-full bg-gray-700/60 px-2.5 py-0.5 text-xs text-gray-300"
                          >
                            {tag}
                          </span>
                        ),
                      )}
                    </div>
                  )}
                </div>
              </>
            )}

            {/* Section 4: Details */}
            <hr className="border-gray-700" />
            <div>
              <div className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                Details
              </div>
              {detailLoading ? (
                <MetadataSkeleton />
              ) : (
                <dl className="space-y-2">
                  {detail?.taken_at && (
                    <div className="flex">
                      <dt className="w-2/5 text-xs text-gray-500">Taken</dt>
                      <dd className="w-3/5 text-sm text-gray-300">
                        {formatDate(detail.taken_at)}
                      </dd>
                    </div>
                  )}
                  {detail &&
                    (detail.camera_make || detail.camera_model) && (
                      <div className="flex">
                        <dt className="w-2/5 text-xs text-gray-500">Camera</dt>
                        <dd className="w-3/5 text-sm text-gray-300">
                          {[detail.camera_make, detail.camera_model]
                            .filter(Boolean)
                            .join(" ")}
                        </dd>
                      </div>
                    )}
                  {detail &&
                    detail.width != null &&
                    detail.height != null && (
                      <div className="flex">
                        <dt className="w-2/5 text-xs text-gray-500">
                          Dimensions
                        </dt>
                        <dd className="w-3/5 text-sm text-gray-300">
                          {detail.width} × {detail.height}
                        </dd>
                      </div>
                    )}
                  {detail &&
                    detail.gps_lat != null &&
                    detail.gps_lon != null && (
                      <div className="flex">
                        <dt className="w-2/5 text-xs text-gray-500">GPS</dt>
                        <dd className="w-3/5 text-sm text-gray-300">
                          {formatGps(detail.gps_lat, detail.gps_lon)}
                        </dd>
                      </div>
                    )}
                  <div className="flex">
                    <dt className="w-2/5 text-xs text-gray-500">File size</dt>
                    <dd className="w-3/5 text-sm text-gray-300">
                      {formatFileSize(asset.file_size)}
                    </dd>
                  </div>
                  {(detail?.sha256 || asset.sha256) && (
                    <div className="flex">
                      <dt className="w-2/5 text-xs text-gray-500">SHA256</dt>
                      <dd
                        className="w-3/5 font-mono text-xs text-gray-400"
                        title={(detail?.sha256 || asset.sha256) ?? undefined}
                      >
                        {(detail?.sha256 || asset.sha256)?.slice(0, 16)}
                        …
                      </dd>
                    </div>
                  )}
                </dl>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
