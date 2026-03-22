import { useMemo, useRef, useEffect, useState, useCallback, useLayoutEffect } from "react";
import { useParams, Link, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { pageAssets, listLibraries, searchAssets } from "../api/client";
import { AssetCell } from "../components/AssetCell";
import { Lightbox } from "../components/Lightbox";
import { FilterBar } from "../components/FilterBar";
import { DrawerOverlay } from "../components/DrawerOverlay";
import { DirectoryTree } from "../components/DirectoryTree";
import type { AssetPageItem } from "../api/types";
import { useScrollContainer } from "../context/ScrollContainerContext";
import { groupAssetsByDate } from "../lib/groupByDate";
import { buildVirtualRows, buildFixedGridRows } from "../lib/virtualRows";
import type { VirtualRowKind } from "../lib/virtualRows";

const PAGE_SIZE = 100;
const TARGET_ROW_HEIGHT = 220;
const ROW_GAP = 4;
const MOBILE_BREAKPOINT = 500;
const MOBILE_COLUMNS = 2;
const MOBILE_ROW_HEIGHT = 160;

export default function BrowsePage() {
  const { libraryId } = useParams<{ libraryId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const pathPrefix = searchParams.get("path") ?? undefined;
  const activeQ = searchParams.get("q");
  const activeTag = searchParams.get("tag");
  const parentEl = useScrollContainer();
  const sentinelRef = useRef<HTMLDivElement>(null);
  const isFetchingNextPageRef = useRef(false);
  const hasNextPageRef = useRef(false);
  const [containerWidth, setContainerWidth] = useState(0);
  const [lightboxAsset, setLightboxAsset] = useState<AssetPageItem | null>(null);
  const [errorDismissed, setErrorDismissed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const isSearchMode = !!activeQ;

  function setParam(key: string, value: string | null) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set(key, value);
      else next.delete(key);
      return next;
    });
  }

  const { data: libraries } = useQuery({
    queryKey: ["libraries", true],
    queryFn: () => listLibraries(true),
  });
  const library = libraries?.find((l) => l.library_id === libraryId);

  const browseQuery = useInfiniteQuery({
    queryKey: ["assets", libraryId!, pathPrefix ?? null, activeTag ?? null],
    queryFn: ({ pageParam }) =>
      pageAssets(
        libraryId!,
        pageParam,
        PAGE_SIZE,
        pathPrefix,
        activeTag ?? undefined,
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => {
      if (!lastPage || lastPage.length < PAGE_SIZE) return undefined;
      return lastPage[lastPage.length - 1].asset_id;
    },
    enabled: !!libraryId && !isSearchMode,
  });

  const searchQuery = useQuery({
    queryKey: [
      "search",
      libraryId!,
      activeQ,
      pathPrefix ?? null,
      activeTag ?? null,
    ],
    queryFn: () =>
      searchAssets({
        libraryId: libraryId!,
        q: activeQ!,
        pathPrefix,
        tag: activeTag ?? undefined,
        limit: 100,
      }),
    enabled: !!libraryId && isSearchMode,
  });

  const flatAssets = useMemo(() => {
    if (isSearchMode) {
      return (searchQuery.data?.hits ?? []).map((h) => ({
        asset_id: h.asset_id,
        rel_path: h.rel_path,
        file_size: h.file_size ?? 0,
        file_mtime: null,
        sha256: null,
        media_type: h.media_type ?? "image/jpeg",
        width: h.width ?? null,
        height: h.height ?? null,
        taken_at: null,
        status: "indexed",
        duration_sec: h.duration_sec ?? null,
      }));
    }
    if (!browseQuery.data?.pages) return [];
    return browseQuery.data.pages.flatMap((p) => p ?? []);
  }, [isSearchMode, searchQuery.data, browseQuery.data]);

  const isLoading = isSearchMode
    ? searchQuery.isLoading
    : browseQuery.isLoading;
  const isFetchingNextPage = isSearchMode
    ? false
    : browseQuery.isFetchingNextPage;
  const hasNextPage = isSearchMode ? false : browseQuery.hasNextPage;
  const fetchNextPage = browseQuery.fetchNextPage;
  const error = isSearchMode ? searchQuery.error : browseQuery.error;
  const isError = isSearchMode ? searchQuery.isError : browseQuery.isError;

  const groups = useMemo(
    () => groupAssetsByDate(flatAssets),
    [flatAssets],
  );

  // Navigation order must match visual grid order (groups sorted date-desc, assets within each group in group order)
  const orderedAssets = useMemo(
    () => groups.flatMap((g) => g.assets),
    [groups],
  );

  const virtualRows: VirtualRowKind[] = useMemo(
    () => {
      if (containerWidth <= 0) return [];
      if (containerWidth < MOBILE_BREAKPOINT) {
        return buildFixedGridRows(
          groups,
          containerWidth,
          MOBILE_COLUMNS,
          MOBILE_ROW_HEIGHT,
          ROW_GAP,
        );
      }
      return buildVirtualRows(groups, containerWidth, TARGET_ROW_HEIGHT, ROW_GAP);
    },
    [groups, containerWidth],
  );

  const rowVirtualizer = useVirtualizer({
    count: virtualRows.length,
    getScrollElement: () => parentEl,
    estimateSize: (index) =>
      virtualRows[index]?.height ?? TARGET_ROW_HEIGHT + ROW_GAP,
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

  useLayoutEffect(() => {
    isFetchingNextPageRef.current = isFetchingNextPage;
  }, [isFetchingNextPage]);

  useLayoutEffect(() => {
    hasNextPageRef.current = hasNextPage;
  }, [hasNextPage]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !parentEl) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && hasNextPageRef.current && !isFetchingNextPageRef.current) {
          fetchNextPage();
        }
      },
      { root: parentEl, rootMargin: "200px", threshold: 0 },
    );
    io.observe(sentinel);
    return () => io.disconnect();
  }, [fetchNextPage, parentEl]);

  const handleAssetClick = useCallback((asset: AssetPageItem) => {
    setLightboxAsset(asset);
  }, []);

  const handleLightboxClose = useCallback(() => {
    setLightboxAsset(null);
  }, []);

  const handleLightboxNavigate = useCallback(
    (index: number) => {
      const asset = orderedAssets[index];
      if (asset) setLightboxAsset(asset);
      // Prefetch next page when within 20 assets of the end
      if (hasNextPage && !isFetchingNextPage && index >= orderedAssets.length - 20) {
        fetchNextPage();
      }
    },
    [orderedAssets, hasNextPage, isFetchingNextPage, fetchNextPage],
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
        {/* Hamburger — mobile only; opens the directory tree drawer */}
        <button
          type="button"
          className="md:hidden -ml-1 flex h-8 w-8 items-center justify-center rounded-lg text-gray-400 hover:bg-gray-800 hover:text-gray-100"
          onClick={() => setDrawerOpen(true)}
          aria-label="Open folder browser"
        >
          <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path
              d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
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

      {/* Directory tree drawer — mobile only; on md+ the sidebar handles this */}
      <DrawerOverlay open={drawerOpen} onClose={() => setDrawerOpen(false)}>
        <div className="p-3">
          <DirectoryTree
            libraryId={libraryId}
            activePath={pathPrefix ?? null}
            onNavigate={(path) => {
              setParam("path", path);
              setDrawerOpen(false);
            }}
          />
        </div>
      </DrawerOverlay>

      <FilterBar
        q={activeQ}
        tag={activeTag}
        path={pathPrefix ?? null}
        onChangeQ={(v) => setParam("q", v)}
        onChangeTag={(v) => setParam("tag", v)}
        onChangePath={(v) => setParam("path", v)}
      />

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
                >
                  <div
                    className="relative"
                    style={{
                      height: `${justifiedRow.height}px`,
                      marginTop: `${ROW_GAP}px`,
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
          assets={orderedAssets}
          hasMore={hasNextPage}
          onClose={handleLightboxClose}
          onNavigate={handleLightboxNavigate}
          onTagClick={(tag) => {
            setParam("tag", tag);
            setLightboxAsset(null);
          }}
          libraryId={libraryId}
          onSimilarClick={(similarAsset) => {
            setLightboxAsset(similarAsset);
          }}
        />
      )}
    </div>
  );
}
