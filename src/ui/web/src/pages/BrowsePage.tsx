import { useMemo, useRef, useEffect, useState, useCallback } from "react";
import { useParams, Link } from "react-router-dom";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { pageAssets } from "../api/client";
import { listLibraries } from "../api/client";
import { AssetCell } from "../components/AssetCell";
import { Lightbox } from "../components/Lightbox";
import type { AssetPageItem } from "../api/types";
import { useScrollContainer } from "../context/ScrollContainerContext";

const MIN_CELL_SIZE = 220;
const ASPECT_RATIO = 4 / 3;
const PAGE_SIZE = 100;
const ESTIMATED_CELL_HEIGHT = MIN_CELL_SIZE / ASPECT_RATIO; // ~165px fallback before first measurement

function chunk<T>(arr: T[], size: number): T[][] {
  const result: T[][] = [];
  for (let i = 0; i < arr.length; i += size) {
    result.push(arr.slice(i, i + size));
  }
  return result;
}

export default function BrowsePage() {
  const { libraryId } = useParams<{ libraryId: string }>();
  const parentEl = useScrollContainer();
  const sentinelRef = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState(0);
  const [lightboxAsset, setLightboxAsset] = useState<AssetPageItem | null>(null);
  const [errorDismissed, setErrorDismissed] = useState(false);

  const { data: libraries } = useQuery({
    queryKey: ["libraries", true],
    queryFn: () => listLibraries(true),
  });
  const library = libraries?.find((l) => l.library_id === libraryId);

  const {
    data,
    isLoading,
    isFetchingNextPage,
    hasNextPage,
    fetchNextPage,
    error,
    isError,
  } = useInfiniteQuery({
    queryKey: ["assets", libraryId!],
    queryFn: ({ pageParam }) =>
      pageAssets(libraryId!, pageParam, PAGE_SIZE),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => {
      if (!lastPage || lastPage.length < PAGE_SIZE) return undefined;
      return lastPage[lastPage.length - 1].asset_id;
    },
    enabled: !!libraryId,
  });

  const flatAssets = useMemo(() => {
    if (!data?.pages) return [];
    return data.pages.flatMap((p) => (p ?? []));
  }, [data?.pages]);

  const columnCount = Math.max(
    1,
    containerWidth > 0 ? Math.floor(containerWidth / MIN_CELL_SIZE) : 4,
  );
  const cellWidth = containerWidth / columnCount;
  const cellHeight = cellWidth / ASPECT_RATIO;
  const rows = useMemo(
    () => chunk(flatAssets, columnCount),
    [flatAssets, columnCount],
  );

  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentEl,
    // Use a real minimum so getTotalSize() is never 0 before measurement.
    // A 0 total height places the sentinel at the top of the scroll area,
    // making it permanently visible and causing all pages to be fetched at once.
    estimateSize: () => Math.max(cellHeight, ESTIMATED_CELL_HEIGHT),
    overscan: 3,
  });

  // Observe the scroll container for width changes. Using parentEl (state, not ref)
  // ensures this effect re-runs if the element is replaced (e.g. after loading).
  useEffect(() => {
    if (!parentEl) return;
    const ro = new ResizeObserver((entries) => {
      setContainerWidth(entries[0]?.contentRect.width ?? 0);
    });
    ro.observe(parentEl);
    return () => ro.disconnect();
  }, [parentEl]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !hasNextPage || isFetchingNextPage || !parentEl) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) fetchNextPage();
      },
      { root: parentEl, rootMargin: "200px", threshold: 0 },
    );
    io.observe(sentinel);
    return () => io.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage, parentEl]);

  const handleAssetClick = useCallback((asset: AssetPageItem) => {
    setLightboxAsset(asset);
  }, []);

  const handleLightboxClose = useCallback(() => {
    setLightboxAsset(null);
  }, []);

  const handleLightboxNavigate = useCallback((index: number) => {
    const asset = flatAssets[index];
    if (asset) setLightboxAsset(asset);
  }, [flatAssets]);

  if (!libraryId) {
    return (
      <div className="text-gray-400">
        Invalid library. <Link to="/" className="text-indigo-400 hover:underline">Go to libraries</Link>
      </div>
    );
  }

  if (!library) {
    return (
      <div className="space-y-4">
        <div className="animate-pulse space-y-4">
          <div className="h-8 w-48 rounded bg-gray-800" />
          <div className="grid grid-cols-4 gap-4">
            {Array.from({ length: 12 }).map((_, i) => (
              <div
                key={i}
                className="rounded-lg bg-gray-800"
                style={{ aspectRatio: "4/3" }}
              />
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (isError && error && !errorDismissed) {
    return (
      <div className="flex flex-col gap-4">
        {library && (
          <div className="flex items-center gap-2 text-sm text-gray-400">
            <Link to="/" className="hover:text-gray-300">Libraries</Link>
            <span>/</span>
            <span className="text-gray-300">{library.name}</span>
          </div>
        )}
        <div className="flex items-center justify-between rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-red-400">
          <span>{(error as Error).message}</span>
          <button
            type="button"
            onClick={() => setErrorDismissed(true)}
            className="rounded px-2 py-1 text-sm font-medium hover:bg-red-900/30"
          >
            Dismiss
          </button>
        </div>
      </div>
    );
  }

  if (isError && errorDismissed) {
    return (
      <div className="flex flex-col gap-4">
        <Link to="/" className="text-indigo-400 hover:underline">
          ← Back to libraries
        </Link>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6 px-6 py-6">
      <div className="flex items-center gap-2 text-sm text-gray-400">
        <Link to="/" className="hover:text-gray-300">
          Libraries
        </Link>
        <span>/</span>
        <span className="text-gray-300">{library.name}</span>
      </div>

      {isLoading ? (
        <div
          className="grid gap-4"
          style={{
            gridTemplateColumns: `repeat(${columnCount}, 1fr)`,
          }}
        >
          {Array.from({ length: Math.min(3 * columnCount, 12) }).map((_, i) => (
            <div
              key={i}
              className="animate-pulse rounded-lg bg-gray-800"
              style={{ aspectRatio: "4/3" }}
            />
          ))}
        </div>
      ) : flatAssets.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-lg border border-gray-700/50 bg-gray-900/50 py-16 text-center">
          <p className="text-gray-400">
            This library has no assets yet. Run a scan from the CLI to add
            files.
          </p>
        </div>
      ) : (
        <div style={{ width: "100%" }}>
          <div
            style={{
              height: `${rowVirtualizer.getTotalSize()}px`,
              width: "100%",
              position: "relative",
            }}
          >
            {rowVirtualizer.getVirtualItems().map((virtualRow) => {
              const row = rows[virtualRow.index];
              if (!row) return null;
              return (
                <div
                  key={virtualRow.key}
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    height: `${virtualRow.size}px`,
                    transform: `translateY(${virtualRow.start}px)`,
                    display: "grid",
                    gridTemplateColumns: `repeat(${columnCount}, 1fr)`,
                    gap: 16,
                  }}
                  data-index={virtualRow.index}
                >
                  {row.map((asset) => (
                    <div key={asset.asset_id}>
                      <AssetCell
                        asset={asset}
                        onClick={() => handleAssetClick(asset)}
                      />
                    </div>
                  ))}
                </div>
              );
            })}
          </div>

          {/* Sentinel for infinite scroll */}
          <div ref={sentinelRef} className="h-4" />

          {isFetchingNextPage && (
            <div className="flex justify-center py-4">
              <div
                className="h-8 w-8 animate-spin rounded-full border-2 border-gray-600 border-t-indigo-500"
                aria-hidden
              />
            </div>
          )}
        </div>
      )}

      {lightboxAsset && (
        <Lightbox
          asset={lightboxAsset}
          assets={flatAssets}
          onClose={handleLightboxClose}
          onNavigate={handleLightboxNavigate}
        />
      )}
    </div>
  );
}
