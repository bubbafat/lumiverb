import { useEffect, useCallback, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getAsset, findSimilar } from "../api/client";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";
import type { AssetPageItem, SimilarHit } from "../api/types";
import { basename, formatFileSize, formatDate } from "../lib/format";

interface LightboxProps {
  asset: AssetPageItem;
  assets: AssetPageItem[];
  hasMore?: boolean;
  onClose: () => void;
  onNavigate: (index: number) => void;
  onTagClick?: (tag: string) => void;
  onSimilarClick?: (asset: AssetPageItem) => void;
  libraryId?: string;
}

function SimilarThumbnail({
  hit,
  onClick,
}: {
  hit: SimilarHit;
  onClick: () => void;
}) {
  const { url, isLoading } = useAuthenticatedImage(hit.asset_id, "thumbnail");
  const filename = basename(hit.rel_path);
  return (
    <button
      type="button"
      onClick={onClick}
      className="group relative aspect-square w-full overflow-hidden rounded-lg bg-gray-800 focus:outline-none focus:ring-2 focus:ring-indigo-500"
    >
      {isLoading && (
        <div className="absolute inset-0 animate-pulse bg-gray-700" />
      )}
      {url && (
        <img
          src={url}
          alt={filename}
          className="h-full w-full object-cover transition-opacity group-hover:opacity-80"
        />
      )}
    </button>
  );
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
  hasMore = false,
  onClose,
  onNavigate,
  onTagClick,
  onSimilarClick,
  libraryId,
}: LightboxProps) {
  const [showSimilar, setShowSimilar] = useState(false);
  const currentIndex = assets.findIndex((a) => a.asset_id === asset.asset_id);
  const hasPrev = currentIndex > 0;
  const hasNext = (currentIndex >= 0 && currentIndex < assets.length - 1) || (currentIndex === assets.length - 1 && hasMore);

  useEffect(() => {
    setShowSimilar(false);
  }, [asset.asset_id]);

  const isVideo = asset.media_type === "video" || asset.media_type.startsWith("video/");

  const { url: mediaUrl, isLoading: mediaLoading, generating } = useAuthenticatedImage(
    asset.asset_id,
    isVideo ? "video-preview" : "proxy",
  );
  const { data: detail, isLoading: detailLoading } = useQuery({
    queryKey: ["asset", asset.asset_id],
    queryFn: () => getAsset(asset.asset_id),
  });

  const { data: similarData, isLoading: similarLoading } = useQuery({
    queryKey: ["similar", asset.asset_id, libraryId],
    queryFn: () =>
      findSimilar({
        assetId: asset.asset_id,
        libraryId: libraryId!,
        limit: 20,
      }),
    enabled: showSimilar && !!libraryId,
  });

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      const el = document.activeElement as HTMLElement | null;
      if (el?.tagName === "INPUT" || el?.tagName === "TEXTAREA" || el?.isContentEditable) return;
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
            {mediaLoading ? (
              <div className="flex h-64 w-64 items-center justify-center">
                <div
                  className="h-12 w-12 animate-spin rounded-full border-2 border-white/30 border-t-white"
                  aria-hidden
                />
              </div>
            ) : generating ? (
              <div className="flex flex-col items-center gap-3 text-gray-400">
                <svg className="h-12 w-12" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden>
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M15 10l4.553-2.277A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M3 8a2 2 0 012-2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V8z" />
                </svg>
                <span className="text-sm">Preview generating…</span>
              </div>
            ) : mediaUrl && isVideo ? (
              <video
                key={asset.asset_id}
                src={mediaUrl}
                controls
                playsInline
                className="max-h-[calc(100vh-4rem)] max-w-full"
              />
            ) : mediaUrl ? (
              <img
                src={mediaUrl}
                alt={filename}
                className="max-h-[calc(100vh-4rem)] max-w-full object-contain"
              />
            ) : (
              <div className="text-gray-500">{isVideo ? "Preview unavailable" : "Image unavailable"}</div>
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
                {formatFileSize(asset.file_size)} · {detail?.media_type ?? asset.media_type}
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

            {libraryId && (
              <>
                <hr className="border-gray-700" />
                <button
                  type="button"
                  onClick={() => setShowSimilar((s) => !s)}
                  className="w-full rounded-lg border border-gray-700 bg-gray-800/50 px-3 py-2 text-sm text-gray-300 transition-colors hover:bg-gray-700 hover:text-gray-100"
                >
                  {showSimilar ? "Hide similar" : "Find similar"}
                </button>
                {showSimilar && (
                  <div className="space-y-2">
                    {similarLoading && (
                      <div className="grid grid-cols-2 gap-2">
                        {[1, 2, 3, 4].map((i) => (
                          <div
                            key={i}
                            className="aspect-square animate-pulse rounded-lg bg-gray-700"
                          />
                        ))}
                      </div>
                    )}
                    {!similarLoading && !similarData?.embedding_available && (
                      <p className="text-xs text-gray-500">
                        No visual embedding yet for this image.
                      </p>
                    )}
                    {!similarLoading &&
                      similarData?.embedding_available &&
                      similarData.hits.length === 0 && (
                        <p className="text-xs text-gray-500">
                          No similar images found.
                        </p>
                      )}
                    {!similarLoading &&
                      similarData?.embedding_available &&
                      similarData.hits.length > 0 && (
                        <div className="grid grid-cols-2 gap-2">
                          {similarData.hits.map((hit) => (
                            <SimilarThumbnail
                              key={hit.asset_id}
                              hit={hit}
                              onClick={() => {
                                if (onSimilarClick) {
                                  onSimilarClick({
                                    asset_id: hit.asset_id,
                                    rel_path: hit.rel_path,
                                    file_size: hit.file_size ?? 0,
                                    file_mtime: null,
                                    sha256: null,
                                    media_type: hit.media_type ?? "image/jpeg",
                                    width: hit.width ?? null,
                                    height: hit.height ?? null,
                                    taken_at: null,
                                    status: "indexed",
                                    duration_sec: null,
                                  });
                                }
                              }}
                            />
                          ))}
                        </div>
                      )}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
