import { useMemo, useRef, useEffect, useState, useCallback } from "react";
import { useParams, Link, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { pageAssets, listLibraries } from "../api/client";
import { AssetCell } from "../components/AssetCell";
import { Lightbox } from "../components/Lightbox";
import type { AssetPageItem } from "../api/types";
import { useScrollContainer } from "../context/ScrollContainerContext";
import { groupAssetsByDate } from "../lib/groupByDate";
import { buildVirtualRows } from "../lib/virtualRows";
import type { VirtualRowKind } from "../lib/virtualRows";

const PAGE_SIZE = 100;
const TARGET_ROW_HEIGHT = 220;
const HEADER_HEIGHT = 40;
const ROW_GAP = 4;

export default function BrowsePage() {
  const { libraryId } = useParams<{ libraryId: string }>();
  const [searchParams] = useSearchParams();
  const pathPrefix = searchParams.get("path") ?? undefined;
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
    queryKey: ["assets", libraryId!, pathPrefix ?? null],
    queryFn: ({ pageParam }) =>
      pageAssets(libraryId!, pageParam, PAGE_SIZE, pathPrefix),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => {
      if (!lastPage || lastPage.length < PAGE_SIZE) return undefined;
      return lastPage[lastPage.length - 1].asset_id;
    },
    enabled: !!libraryId,
  });

  const flatAssets = useMemo(() => {
    if (!data?.pages) return [];
    return data.pages.flatMap((p) => p ?? []);
  }, [data?.pages]);

  const groups = useMemo(
    () => groupAssetsByDate(flatAssets),
    [flatAssets],
  );

  const virtualRows: VirtualRowKind[] = useMemo(
    () =>
      containerWidth > 0
        ? buildVirtualRows(groups, containerWidth, TARGET_ROW_HEIGHT, ROW_GAP)
        : [],
    [groups, containerWidth],
  );

  const rowVirtualizer = useVirtualizer({
    count: virtualRows.length,
    getScrollElement: () => parentEl,
    estimateSize: (index) =>
      virtualRows[index]?.type === "header"
        ? HEADER_HEIGHT
        : TARGET_ROW_HEIGHT + ROW_GAP,
    overscan: 3,
  });

  // Observe the scroll container for width changes.
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

  const handleLightboxNavigate = useCallback(
    (index: number) => {
      const asset = flatAssets[index];
      if (asset) setLightboxAsset(asset);
    },
    [flatAssets],
  );

  if (!libraryId) {
    return (
      <div className="text-gray-400">
        Invalid library.{" "}
        <Link to="/" className="text-indigo-400 hover:underline">
          Go to libraries
        </Link>
      </div>
    );
  }

  if (!library) {
    return (
      <div className="space-y-4">
        <div className="animate-pulse space-y-4">
          <div className="h-8 w-48 rounded bg-gray-800" />
          <div className="flex gap-4">
            {Array.from({ length: 6 }).map((_, i) => (
              <div
                // eslint-disable-next-line react/no-array-index-key
                key={i}
                className="h-[220px] flex-1 rounded-lg bg-gray-800"
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
            <Link to="/" className="hover:text-gray-300">
              Libraries
            </Link>
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
        {pathPrefix && (
          <>
            <span>/</span>
            <span className="text-gray-500">{pathPrefix}</span>
          </>
        )}
      </div>

      {isLoading ? (
        <div className="flex gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <div
              // eslint-disable-next-line react/no-array-index-key
              key={i}
              className="h-[220px] flex-1 animate-pulse rounded-lg bg-gray-800"
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
            {rowVirtualizer.getVirtualItems().map((virtualItem) => {
              const vr = virtualRows[virtualItem.index];
              if (!vr) return null;

              const commonStyle: React.CSSProperties = {
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                height: `${virtualItem.size}px`,
                transform: `translateY(${virtualItem.start}px)`,
              };

              if (vr.type === "header") {
                return (
                  <div
                    key={virtualItem.key}
                    style={commonStyle}
                    className="flex items-end"
                  >
                    <div className="px-1 py-2 text-sm font-semibold text-gray-400">
                      {vr.label}
                    </div>
                  </div>
                );
              }

              const group = groups[vr.groupIndex];
              if (!group) return null;
              const { justifiedRow } = vr;

              let x = 0;

              return (
                <div
                  key={virtualItem.key}
                  style={commonStyle}
                  className="pt-1"
                >
                  <div
                    className="relative"
                    style={{
                      height: `${justifiedRow.height}px`,
                    }}
                  >
                    {justifiedRow.items.map((itemIndex, idx) => {
                      const asset = group.assets[itemIndex];
                      if (!asset) return null;
                      const width = justifiedRow.widths[idx];
                      const left = x;
                      x += width + ROW_GAP;

                      const aspectRatio = justifiedRow.widths[idx] / justifiedRow.height;

                      return (
                        <div
                          key={asset.asset_id}
                          className="absolute"
                          style={{
                            left,
                            top: 0,
                            width,
                            height: "100%",
                          }}
                        >
                          <AssetCell
                            asset={asset}
                            onClick={() => handleAssetClick(asset)}
                            aspectRatio={aspectRatio}
                          />
                        </div>
                      );
                    })}
                  </div>
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
