import { useEffect, useCallback, useState, useRef } from "react";
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
  onDateClick?: (dateStr: string) => void;
  onNearbyClick?: (lat: number, lon: number) => void;
  onFilterClick?: (params: Record<string, string>) => void;
  libraryId?: string;
  isPublic?: boolean;
  publicLibraryId?: string;
}

function SimilarThumbnail({
  hit,
  onClick,
  isPublic,
  publicLibraryId,
}: {
  hit: SimilarHit;
  onClick: () => void;
  isPublic?: boolean;
  publicLibraryId?: string;
}) {
  const { url, isLoading } = useAuthenticatedImage(hit.asset_id, "thumbnail", {
    isPublic,
    publicLibraryId,
  });
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

function FilterLink({
  children,
  params,
  onFilterClick,
  onClose,
  title,
}: {
  children: React.ReactNode;
  params: Record<string, string>;
  onFilterClick?: (params: Record<string, string>) => void;
  onClose: () => void;
  title?: string;
}) {
  if (!onFilterClick) {
    return <span className="text-gray-300">{children}</span>;
  }
  return (
    <button
      type="button"
      onClick={() => {
        onFilterClick(params);
        onClose();
      }}
      title={title ?? "Filter by this value"}
      className="text-gray-300 hover:text-indigo-400 hover:underline transition-colors text-left"
    >
      {children}
    </button>
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
  onDateClick,
  onNearbyClick,
  onFilterClick,
  libraryId,
  isPublic,
  publicLibraryId,
}: LightboxProps) {
  const [showSimilar, setShowSimilar] = useState(false);
  const [metaOpen, setMetaOpen] = useState(true);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [showHints, setShowHints] = useState(true);
  const [slideshowActive, setSlideshowActive] = useState(false);
  const lightboxRef = useRef<HTMLDivElement>(null);
  const hintTimerRef = useRef<number>();
  const slideshowRef = useRef<number>();

  const currentIndex = assets.findIndex((a) => a.asset_id === asset.asset_id);
  const hasPrev = currentIndex > 0;
  const hasNext =
    (currentIndex >= 0 && currentIndex < assets.length - 1) ||
    (currentIndex === assets.length - 1 && hasMore);

  // Reset state when asset changes
  useEffect(() => {
    setShowSimilar(false);
    setMetaOpen(true);
  }, [asset.asset_id]);

  // Keyboard hint: show for 3s on open, reset on key use
  const resetHintTimer = useCallback(() => {
    setShowHints(true);
    clearTimeout(hintTimerRef.current);
    hintTimerRef.current = window.setTimeout(() => setShowHints(false), 3000);
  }, []);

  useEffect(() => {
    resetHintTimer();
    return () => clearTimeout(hintTimerRef.current);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Slideshow
  useEffect(() => {
    clearInterval(slideshowRef.current);
    if (!slideshowActive) return;
    slideshowRef.current = window.setInterval(() => {
      onNavigate(Math.min(currentIndex + 1, assets.length - 1));
    }, 3000);
    return () => clearInterval(slideshowRef.current);
  }, [slideshowActive, currentIndex, assets.length, onNavigate]);

  // Stop slideshow at end
  useEffect(() => {
    if (slideshowActive && currentIndex === assets.length - 1 && !hasMore) {
      setSlideshowActive(false);
    }
  }, [slideshowActive, currentIndex, assets.length, hasMore]);

  // Fullscreen sync
  useEffect(() => {
    const onFsChange = () =>
      setIsFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (!isFullscreen) {
      lightboxRef.current?.requestFullscreen().catch(() => {
        setIsFullscreen(true);
      });
    } else {
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else {
        setIsFullscreen(false);
      }
    }
  }, [isFullscreen]);

  const isVideo =
    asset.media_type === "video" || asset.media_type.startsWith("video/");

  const {
    url: mediaUrl,
    isLoading: mediaLoading,
    generating,
  } = useAuthenticatedImage(asset.asset_id, isVideo ? "video-preview" : "proxy", {
    isPublic,
    publicLibraryId,
  });
  const { data: detail, isLoading: detailLoading } = useQuery({
    queryKey: ["asset", asset.asset_id, publicLibraryId ?? null],
    queryFn: () => getAsset(asset.asset_id, isPublic ? publicLibraryId : undefined),
    enabled: !isPublic || !!publicLibraryId,
    refetchInterval: 10_000,
  });

  const { data: similarData, isLoading: similarLoading } = useQuery({
    queryKey: ["similar", asset.asset_id, libraryId],
    queryFn: () =>
      findSimilar({
        assetId: asset.asset_id,
        libraryId: libraryId ?? "",
        limit: 20,
      }),
    enabled: showSimilar && !!libraryId,
  });

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      const el = document.activeElement as HTMLElement | null;
      if (
        el?.tagName === "INPUT" ||
        el?.tagName === "TEXTAREA" ||
        el?.isContentEditable
      )
        return;

      switch (e.key) {
        case "Escape":
          if (isFullscreen) {
            toggleFullscreen();
          } else {
            onClose();
          }
          break;
        case "ArrowLeft":
          if (hasPrev) {
            setSlideshowActive(false);
            onNavigate(currentIndex - 1);
            resetHintTimer();
          }
          break;
        case "ArrowRight":
          if (hasNext) {
            setSlideshowActive(false);
            onNavigate(currentIndex + 1);
            resetHintTimer();
          }
          break;
        case "f":
        case "F":
          toggleFullscreen();
          break;
        case " ":
          e.preventDefault();
          setSlideshowActive((a) => !a);
          resetHintTimer();
          break;
        case "?":
          setShowHints((h) => {
            clearTimeout(hintTimerRef.current);
            return !h;
          });
          break;
        default:
          resetHintTimer();
      }
    },
    [
      onClose,
      onNavigate,
      hasPrev,
      hasNext,
      currentIndex,
      isFullscreen,
      toggleFullscreen,
      resetHintTimer,
    ],
  );

  useEffect(() => {
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  const filename = basename(asset.rel_path);
  const formatGps = (lat: number, lon: number): string =>
    `${lat.toFixed(5)}, ${lon.toFixed(5)}`;

  return (
    <div
      ref={lightboxRef}
      data-lightbox="true"
      className={`fixed inset-0 z-50 flex bg-black/90 ${isFullscreen ? "bg-black" : ""}`}
    >
      {/* Slideshow progress bar */}
      {slideshowActive && (
        <div className="absolute top-0 inset-x-0 h-0.5 bg-gray-800 z-10">
          <div
            key={`${asset.asset_id}-slideshow`}
            className="h-full bg-indigo-500 motion-reduce:hidden"
            style={{
              animation: "lv-progress 3s linear forwards",
            }}
          />
        </div>
      )}

      {/* Header buttons */}
      <div className="absolute right-4 top-4 z-10 mt-safe mr-safe flex items-center gap-2">
        {/* Fullscreen toggle */}
        <button
          type="button"
          onClick={toggleFullscreen}
          className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-full p-2 text-white/80 transition-colors hover:bg-white/10 hover:text-white"
          aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
        >
          {isFullscreen ? (
            <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3" />
            </svg>
          ) : (
            <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3" />
            </svg>
          )}
        </button>
        {/* Close */}
        <button
          type="button"
          onClick={onClose}
          className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-full p-2 text-2xl text-white/80 transition-colors hover:bg-white/10 hover:text-white"
          aria-label="Close"
        >
          ×
        </button>
      </div>

      {/* Two-column layout */}
      <div className="flex w-full flex-col lg:flex-row">
        {/* Left: image */}
        <div className="relative flex flex-1 items-center justify-center p-4 lg:p-8 min-h-[50vh] lg:min-h-0">
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
                <svg
                  className="h-12 w-12"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  aria-hidden
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={1.5}
                    d="M15 10l4.553-2.277A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M3 8a2 2 0 012-2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V8z"
                  />
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
              <div className="text-gray-500">
                {isVideo ? "Preview unavailable" : "Image unavailable"}
              </div>
            )}

            {/* Navigation arrows */}
            {hasPrev && (
              <button
                type="button"
                onClick={() => {
                  setSlideshowActive(false);
                  onNavigate(currentIndex - 1);
                }}
                className="absolute left-0 top-1/2 -translate-y-1/2 ml-safe rounded-full bg-white/10 p-5 min-h-[44px] min-w-[44px] text-white transition-colors hover:bg-white/20"
                aria-label="Previous"
              >
                <svg className="h-8 w-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
                </svg>
              </button>
            )}
            {hasNext && (
              <button
                type="button"
                onClick={() => {
                  setSlideshowActive(false);
                  onNavigate(currentIndex + 1);
                }}
                className="absolute right-0 top-1/2 -translate-y-1/2 mr-safe rounded-full bg-white/10 p-5 min-h-[44px] min-w-[44px] text-white transition-colors hover:bg-white/20"
                aria-label="Next"
              >
                <svg className="h-8 w-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                </svg>
              </button>
            )}
          </div>

          {/* Keyboard hint overlay */}
          <div
            className={`absolute bottom-12 inset-x-0 flex justify-center pointer-events-none transition-opacity duration-500 motion-reduce:transition-none ${
              showHints ? "opacity-100" : "opacity-0"
            }`}
            aria-hidden
          >
            <div className="flex flex-wrap justify-center gap-3 rounded-lg bg-black/60 px-4 py-2 text-xs text-gray-400 backdrop-blur-sm">
              <span><kbd className="font-mono">←/→</kbd> Navigate</span>
              <span><kbd className="font-mono">Esc</kbd> Close</span>
              <span><kbd className="font-mono">F</kbd> Fullscreen</span>
              <span><kbd className="font-mono">Space</kbd> Slideshow</span>
              <span><kbd className="font-mono">?</kbd> Hints</span>
            </div>
          </div>
        </div>

        {/* Right: metadata panel — hidden in fullscreen */}
        {!isFullscreen && (
          <div
            className={`w-full border-t border-gray-700 bg-gray-900/50 p-4 lg:p-6 lg:w-80 lg:border-l lg:border-t-0 transition-[max-height] duration-200 motion-reduce:transition-none ${
              metaOpen
                ? "overflow-y-auto max-h-none"
                : "max-h-[3.5rem] overflow-hidden"
            } lg:overflow-y-auto lg:max-h-none`}
          >
            {/* Details toggle — mobile only */}
            <button
              type="button"
              aria-expanded={metaOpen}
              className="flex w-full items-center justify-between md:hidden mb-4"
              onClick={() => setMetaOpen((o) => !o)}
            >
              <span className="text-sm font-medium text-gray-300">Details</span>
              <svg
                className={`h-4 w-4 text-gray-400 transition-transform motion-reduce:transition-none ${
                  metaOpen ? "" : "rotate-180"
                }`}
                viewBox="0 0 24 24"
                fill="none"
                aria-hidden
              >
                <path
                  d="M5 15l7-7 7 7"
                  stroke="currentColor"
                  strokeWidth="1.7"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>

            <div className="space-y-4">
              {/* Section 1: Basic info */}
              <div>
                <div className="text-lg font-medium text-gray-100">{filename}</div>
                <div className="mt-1 font-mono text-xs text-gray-500">
                  {asset.rel_path}
                </div>
                <div className="mt-2 text-sm text-gray-400">
                  {formatFileSize(asset.file_size)} ·{" "}
                  {detail?.media_type ?? asset.media_type}
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
              {(detailLoading ||
                (detail?.ai_tags && detail.ai_tags.length > 0)) && (
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
                    {(detail?.taken_at || asset.file_mtime) && (
                      <div className="flex">
                        <dt className="w-2/5 text-xs text-gray-500">
                          {detail?.taken_at ? "Taken" : "File date"}
                        </dt>
                        <dd className="w-3/5 text-sm">
                          {onDateClick ? (
                            <button
                              type="button"
                              onClick={() =>
                                onDateClick(
                                  (detail?.taken_at ?? asset.file_mtime)!.slice(0, 10),
                                )
                              }
                              title="Filter to this date"
                              className="text-gray-300 hover:text-indigo-400 hover:underline transition-colors text-left"
                            >
                              {formatDate(detail?.taken_at ?? asset.file_mtime)}
                            </button>
                          ) : (
                            <span className="text-gray-300">
                              {formatDate(detail?.taken_at ?? asset.file_mtime)}
                            </span>
                          )}
                        </dd>
                      </div>
                    )}
                    {detail &&
                      (detail.camera_make || detail.camera_model) && (
                        <div className="flex">
                          <dt className="w-2/5 text-xs text-gray-500">Camera</dt>
                          <dd className="w-3/5 text-sm">
                            <FilterLink
                              params={{
                                ...(detail.camera_make
                                  ? { camera_make: detail.camera_make }
                                  : {}),
                                ...(detail.camera_model
                                  ? { camera_model: detail.camera_model }
                                  : {}),
                              }}
                              onFilterClick={onFilterClick}
                              onClose={onClose}
                              title="Filter by this camera"
                            >
                              {[detail.camera_make, detail.camera_model]
                                .filter(Boolean)
                                .join(" ")}
                            </FilterLink>
                          </dd>
                        </div>
                      )}
                    {detail && detail.lens_model && (
                      <div className="flex">
                        <dt className="w-2/5 text-xs text-gray-500">Lens</dt>
                        <dd className="w-3/5 text-sm">
                          <FilterLink
                            params={{ lens_model: detail.lens_model }}
                            onFilterClick={onFilterClick}
                            onClose={onClose}
                            title="Filter by this lens"
                          >
                            {detail.lens_model}
                          </FilterLink>
                        </dd>
                      </div>
                    )}
                    {detail && (
                      <div className="flex">
                        <dt className="w-2/5 text-xs text-gray-500">Exposure</dt>
                        <dd className="w-3/5 text-sm">
                          {detail.shutter_speed != null ||
                          detail.aperture != null ||
                          detail.iso != null ? (
                            <span className="flex flex-wrap items-center gap-x-1.5">
                              {onFilterClick ? (
                                <button
                                  type="button"
                                  onClick={() => {
                                    const p: Record<string, string> = {};
                                    if (detail.iso != null) {
                                      p.iso_min = String(detail.iso);
                                      p.iso_max = String(detail.iso);
                                    }
                                    if (detail.aperture != null) {
                                      p.aperture_min = String(detail.aperture);
                                      p.aperture_max = String(detail.aperture);
                                    }
                                    onFilterClick(p);
                                    onClose();
                                  }}
                                  title="Filter by all exposure settings"
                                  className="text-gray-500 hover:text-indigo-400 transition-colors text-xs mr-0.5"
                                >
                                  ▸
                                </button>
                              ) : null}
                              {detail.shutter_speed != null && (
                                <span className="text-gray-300">
                                  {detail.shutter_speed}
                                </span>
                              )}
                              {detail.aperture != null && (
                                <FilterLink
                                  params={{
                                    aperture_min: String(detail.aperture),
                                    aperture_max: String(detail.aperture),
                                  }}
                                  onFilterClick={onFilterClick}
                                  onClose={onClose}
                                  title="Filter by this aperture"
                                >
                                  f/{detail.aperture}
                                </FilterLink>
                              )}
                              {detail.iso != null && (
                                <FilterLink
                                  params={{
                                    iso_min: String(detail.iso),
                                    iso_max: String(detail.iso),
                                  }}
                                  onFilterClick={onFilterClick}
                                  onClose={onClose}
                                  title="Filter by this ISO"
                                >
                                  ISO {detail.iso}
                                </FilterLink>
                              )}
                            </span>
                          ) : (
                            <FilterLink
                              params={{ has_exposure: "false" }}
                              onFilterClick={onFilterClick}
                              onClose={onClose}
                              title="Find images with no exposure data"
                            >
                              Unknown
                            </FilterLink>
                          )}
                        </dd>
                      </div>
                    )}
                    {detail &&
                      detail.focal_length != null && (
                        <div className="flex">
                          <dt className="w-2/5 text-xs text-gray-500">
                            Focal length
                          </dt>
                          <dd className="w-3/5 text-sm">
                            <FilterLink
                              params={{
                                focal_length_min: String(detail.focal_length),
                                focal_length_max: String(detail.focal_length),
                              }}
                              onFilterClick={onFilterClick}
                              onClose={onClose}
                              title="Filter by this focal length"
                            >
                              {detail.focal_length}mm
                              {detail.focal_length_35mm != null &&
                                detail.focal_length_35mm !== detail.focal_length &&
                                ` (${detail.focal_length_35mm}mm eq.)`}
                            </FilterLink>
                          </dd>
                        </div>
                      )}
                    {detail && detail.flash_fired != null && (
                      <div className="flex">
                        <dt className="w-2/5 text-xs text-gray-500">Flash</dt>
                        <dd className="w-3/5 text-sm text-gray-300">
                          {detail.flash_fired ? "Fired" : "No flash"}
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
                        <>
                          <div className="flex">
                            <dt className="w-2/5 text-xs text-gray-500">GPS</dt>
                            <dd className="w-3/5 text-sm">
                              <a
                                href={`https://maps.google.com/?q=${detail.gps_lat},${detail.gps_lon}`}
                                target="_blank"
                                rel="noopener noreferrer"
                                title={formatGps(detail.gps_lat, detail.gps_lon)}
                                className="text-indigo-400 hover:text-indigo-300 hover:underline transition-colors"
                              >
                                {formatGps(detail.gps_lat, detail.gps_lon)}
                              </a>
                            </dd>
                          </div>
                          {onNearbyClick && (
                            <div className="flex">
                              <dt className="w-2/5" />
                              <dd className="w-3/5">
                                <button
                                  type="button"
                                  onClick={() => onNearbyClick(detail.gps_lat!, detail.gps_lon!)}
                                  className="text-xs text-indigo-400 hover:text-indigo-300 hover:underline transition-colors"
                                >
                                  Photos nearby
                                </button>
                              </dd>
                            </div>
                          )}
                        </>
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
                          title={
                            (detail?.sha256 || asset.sha256) ?? undefined
                          }
                        >
                          {(detail?.sha256 || asset.sha256)?.slice(0, 16)}…
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
                      {!similarLoading &&
                        !similarData?.embedding_available && (
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
                                isPublic={isPublic}
                                publicLibraryId={publicLibraryId ?? libraryId}
                                onClick={() => {
                                  if (onSimilarClick) {
                                    onSimilarClick({
                                      asset_id: hit.asset_id,
                                      rel_path: hit.rel_path,
                                      file_size: hit.file_size ?? 0,
                                      file_mtime: null,
                                      sha256: null,
                                      media_type:
                                        hit.media_type ?? "image/jpeg",
                                      width: hit.width ?? null,
                                      height: hit.height ?? null,
                                      taken_at: null,
                                      status: "indexed",
                                      duration_sec: null,
                                      camera_make: null,
                                      camera_model: null,
                                      iso: null,
                                      aperture: null,
                                      focal_length: null,
                                      focal_length_35mm: null,
                                      lens_model: null,
                                      flash_fired: null,
                                      gps_lat: null,
                                      gps_lon: null,
                                      created_at: null,
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
        )}
      </div>
    </div>
  );
}
