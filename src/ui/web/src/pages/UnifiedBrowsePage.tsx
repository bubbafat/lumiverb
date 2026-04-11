import { useMemo, useRef, useEffect, useState, useCallback, useLayoutEffect } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  batchRateAssets,
  createSavedView,
  getFilteredFacets,
  lookupRatings,
  queryAssets,
  rateAsset,
} from "../api/client";
import type { QueryItem } from "../api/client";
import { AssetCell } from "../components/AssetCell";
import { CollectionPicker } from "../components/CollectionPicker";
import { Lightbox } from "../components/Lightbox";
import { FilterBar } from "../components/FilterBar";
import { SelectionToolbar } from "../components/SelectionToolbar";
import { SaveSmartCollectionModal } from "../components/SaveSmartCollectionModal";
import { ZoomControl } from "../components/ZoomControl";
import type { AssetPageItem, AssetRating, BrowseItem, RatingColor } from "../api/types";
import { HeartButton, StarPicker, ColorPicker } from "../components/RatingControls";
import { useScrollContainer } from "../context/ScrollContainerContext";
import { groupAssetsByDate } from "../lib/groupByDate";
import { useSelection } from "../lib/useSelection";
import { buildVirtualRows, buildFixedGridRows } from "../lib/virtualRows";
import { useLocalStorage } from "../lib/useLocalStorage";
import type { VirtualRowKind } from "../lib/virtualRows";
import { buildSavedQuery, composeDate, composeNear } from "../lib/queryFilter";

const PAGE_SIZE = 100;
const ROW_GAP = 4;
const FIXED_GRID_BREAKPOINT = 700;

const ZOOM_LEVELS = [
  { justifiedHeight: 120, fixedCellWidth: 100 },
  { justifiedHeight: 170, fixedCellWidth: 130 },
  { justifiedHeight: 220, fixedCellWidth: 150 },
  { justifiedHeight: 300, fixedCellWidth: 200 },
  { justifiedHeight: 420, fixedCellWidth: 280 },
] as const;

const CELL_ASPECT_RATIO = 1.0;

export default function UnifiedBrowsePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const queryClient = useQueryClient();
  const parentEl = useScrollContainer();
  const isFetchingNextPageRef = useRef(false);
  const hasNextPageRef = useRef(false);
  const [containerWidth, setContainerWidth] = useState(0);
  const [lightboxAsset, setLightboxAsset] = useState<AssetPageItem | null>(null);
  const [zoomLevel, setZoomLevel] = useLocalStorage("lv_grid_zoom", 2);
  const [showSaveModal, setShowSaveModal] = useState(false);
  const [saveViewName, setSaveViewName] = useState("");
  const [savingView, setSavingView] = useState(false);

  // --- Filter algebra: read filters from URL ---
  // Read raw values — primitives are stable across renders
  const fParams = searchParams.getAll("f");
  const fKey = fParams.join("\0");
  const sortParam = searchParams.get("sort");
  const dirParam = searchParams.get("dir");
  const { filters, sort: browseSort, direction: browseDir } = useMemo(() => {
    const parsed = fParams.map((raw) => {
      const colon = raw.indexOf(":");
      if (colon <= 0) return null;
      return { type: raw.slice(0, colon), value: raw.slice(colon + 1) };
    }).filter((f): f is { type: string; value: string } => f !== null);
    const sort = sortParam ?? "taken_at";
    const direction: "asc" | "desc" = dirParam === "asc" ? "asc" : "desc";
    return { filters: parsed, sort, direction };
  }, [fKey, sortParam, dirParam]); // eslint-disable-line react-hooks/exhaustive-deps

  // Ref to avoid setSearchParams identity in useCallback deps
  const setSearchParamsRef = useRef(setSearchParams);
  setSearchParamsRef.current = setSearchParams;

  const handleSetFilter = useCallback(
    (type: string, value: string | null) => {
      setSearchParamsRef.current((prev) => {
        const next = new URLSearchParams(prev);
        const existing = next.getAll("f");
        next.delete("f");
        for (const raw of existing) {
          const colon = raw.indexOf(":");
          if (colon > 0 && raw.slice(0, colon) === type) continue;
          next.append("f", raw);
        }
        if (value != null && value !== "") {
          next.append("f", `${type}:${value}`);
        }
        return next;
      });
    },
    [],
  );

  const handleSetSort = useCallback(
    (sort: string, dir: "asc" | "desc") => {
      setSearchParamsRef.current((prev) => {
        const next = new URLSearchParams(prev);
        if (sort && sort !== "taken_at") next.set("sort", sort);
        else next.delete("sort");
        if (dir && dir !== "desc") next.set("dir", dir);
        else next.delete("dir");
        return next;
      });
    },
    [],
  );

  const handleClearAll = useCallback(() => {
    setSearchParamsRef.current(new URLSearchParams());
  }, []);

  const browseFavorite = filters.some((f) => f.type === "favorite" && f.value === "yes");

  // --- Unified query: one call for both browse and search ---
  const browseQuery = useInfiniteQuery({
    queryKey: ["unified-query", filters, browseSort, browseDir],
    queryFn: ({ pageParam }) =>
      queryAssets(filters, {
        sort: browseSort,
        dir: browseDir,
        after: pageParam,
        limit: PAGE_SIZE,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage?.next_cursor ?? undefined,
  });

  // --- Facets scoped to active filters ---
  const facetsQuery = useQuery({
    queryKey: ["filtered-facets", filters],
    queryFn: () => getFilteredFacets(filters),
    staleTime: 30_000,
  });

  const flatAssets: BrowseItem[] = useMemo(() => {
    if (!browseQuery.data?.pages) return [];
    return browseQuery.data.pages.flatMap((p) =>
      (p?.items ?? []).map((item: QueryItem): BrowseItem => ({
        asset_id: item.asset_id,
        library_id: item.library_id,
        library_name: item.library_name,
        rel_path: item.rel_path,
        file_size: item.file_size,
        file_mtime: null,
        sha256: null,
        media_type: item.media_type,
        width: item.width,
        height: item.height,
        taken_at: item.taken_at,
        status: item.status,
        duration_sec: item.duration_sec,
        camera_make: item.camera_make,
        camera_model: item.camera_model,
        iso: item.iso,
        aperture: item.aperture,
        focal_length: item.focal_length,
        focal_length_35mm: item.focal_length_35mm,
        lens_model: item.lens_model,
        flash_fired: item.flash_fired,
        gps_lat: item.gps_lat,
        gps_lon: item.gps_lon,
        created_at: item.created_at,
      })),
    );
  }, [browseQuery.data]);

  // Build a library name lookup from the browse results
  const libraryNames: Record<string, string> = useMemo(() => {
    const map: Record<string, string> = {};
    for (const a of flatAssets) {
      if (a.library_id && a.library_name) {
        map[a.library_id] = a.library_name;
      }
    }
    return map;
  }, [flatAssets]);

  const isLoading = browseQuery.isLoading;
  const isFetchingNextPage = browseQuery.isFetchingNextPage;
  const hasNextPage = browseQuery.hasNextPage;
  const fetchNextPage = browseQuery.fetchNextPage;

  // Stable ref for scroll handler — avoids re-attaching listener on every data update
  const fetchNextPageRef = useRef(fetchNextPage);
  fetchNextPageRef.current = fetchNextPage;

  const browseCount = flatAssets.length;

  const groups = useMemo(
    () => groupAssetsByDate(flatAssets),
    [flatAssets],
  );

  const orderedAssets = useMemo(
    () => groups.flatMap((g) => g.assets),
    [groups],
  );

  const orderedAssetIds = useMemo(
    () => orderedAssets.map((a) => a.asset_id),
    [orderedAssets],
  );
  const selection = useSelection(orderedAssetIds);
  const [pickerAssetIds, setPickerAssetIds] = useState<string[] | null>(null);
  const [showSmartColModal, setShowSmartColModal] = useState(false);

  // ---------------------------------------------------------------------------
  // Ratings
  // ---------------------------------------------------------------------------

  const ratingsQuery = useQuery({
    queryKey: ["ratings", orderedAssetIds.slice(0, 500)],
    queryFn: () => lookupRatings(orderedAssetIds.slice(0, 500)),
    enabled: orderedAssetIds.length > 0,
    staleTime: 30_000,
  });
  const ratingsMap: Record<string, AssetRating> = ratingsQuery.data?.ratings ?? {};

  const handleRatingChange = useCallback(
    async (assetId: string, update: { favorite?: boolean; stars?: number; color?: RatingColor | null }) => {
      const prev = ratingsMap[assetId] ?? { favorite: false, stars: 0, color: null };
      const optimistic: AssetRating = {
        favorite: update.favorite !== undefined ? update.favorite : prev.favorite,
        stars: update.stars !== undefined ? update.stars : prev.stars,
        color: update.color !== undefined ? update.color : prev.color,
      };
      queryClient.setQueryData(
        ["ratings", orderedAssetIds.slice(0, 500)],
        (old: { ratings: Record<string, AssetRating> } | undefined) => ({
          ratings: { ...(old?.ratings ?? {}), [assetId]: optimistic },
        }),
      );
      try {
        await rateAsset(assetId, update);
      } catch {
        queryClient.invalidateQueries({ queryKey: ["ratings"] });
      }
    },
    [ratingsMap, orderedAssetIds, queryClient],
  );

  const handleBatchRating = useCallback(
    async (update: { favorite?: boolean; stars?: number; color?: RatingColor | null }) => {
      const ids = selection.toArray();
      if (ids.length === 0) return;
      try {
        await batchRateAssets(ids, update);
        queryClient.invalidateQueries({ queryKey: ["ratings"] });
      } catch {
        // silently fail — user can retry
      }
    },
    [selection, queryClient],
  );

  // Clear selection on navigation
  useEffect(() => {
    selection.clear();
  }, [location.pathname, location.search]); // eslint-disable-line react-hooks/exhaustive-deps

  const zoom = ZOOM_LEVELS[zoomLevel] ?? ZOOM_LEVELS[2];

  const virtualRows: VirtualRowKind[] = useMemo(() => {
    if (containerWidth <= 0) return [];
    if (containerWidth <= FIXED_GRID_BREAKPOINT) {
      const columns = Math.max(2, Math.floor(containerWidth / zoom.fixedCellWidth));
      const cellWidth = Math.floor(
        (containerWidth - ROW_GAP * (columns - 1)) / columns,
      );
      const rowHeight = Math.round(cellWidth * CELL_ASPECT_RATIO);
      return buildFixedGridRows(groups, containerWidth, columns, rowHeight, ROW_GAP);
    }
    return buildVirtualRows(groups, containerWidth, zoom.justifiedHeight, ROW_GAP);
  }, [groups, containerWidth, zoom]);

  const rowVirtualizer = useVirtualizer({
    count: virtualRows.length,
    getScrollElement: () => parentEl,
    estimateSize: (index) =>
      virtualRows[index]?.height ?? zoom.justifiedHeight + ROW_GAP,
    overscan: 3,
  });

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
    if (!parentEl) return;
    const onScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = parentEl;
      if (
        scrollHeight - scrollTop - clientHeight < 400 &&
        hasNextPageRef.current &&
        !isFetchingNextPageRef.current
      ) {
        fetchNextPageRef.current();
      }
    };
    parentEl.addEventListener("scroll", onScroll);
    return () => parentEl.removeEventListener("scroll", onScroll);
  }, [parentEl]);

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
      if (hasNextPage && !isFetchingNextPage && index >= orderedAssets.length - 20) {
        fetchNextPageRef.current();
      }
    },
    [orderedAssets, hasNextPage, isFetchingNextPage],
  );

  const handleLightboxDateClick = useCallback(
    (dateStr: string) => {
      handleSetFilter("date", composeDate(dateStr, dateStr));
      setLightboxAsset(null);
    },
    [handleSetFilter],
  );

  // Determine page title based on active filters
  const pageTitle = useMemo(() => {
    if (browseFavorite) return "Favorites";
    return "All Photos";
  }, [browseFavorite]);

  const hasActiveFilters = filters.length > 0;

  const handleSaveView = useCallback(async () => {
    if (!saveViewName.trim()) return;
    setSavingView(true);
    try {
      await createSavedView(saveViewName.trim(), searchParams.toString());
      queryClient.invalidateQueries({ queryKey: ["saved-views"] });
      setShowSaveModal(false);
      setSaveViewName("");
    } finally {
      setSavingView(false);
    }
  }, [saveViewName, searchParams, queryClient]);

  return (
    <div className="flex flex-col gap-4 px-6 py-6">
      {/* Breadcrumb + zoom control */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2 text-sm text-gray-400 min-w-0">
          <span className="text-gray-300 truncate">{pageTitle}</span>
        </div>
        <div className="flex items-center gap-2">
          {hasActiveFilters && (
            <button
              type="button"
              onClick={() => {
                setSaveViewName("");
                setShowSaveModal(true);
              }}
              className="rounded-md bg-gray-700 px-2.5 py-1 text-xs font-medium text-gray-200 hover:bg-gray-600"
            >
              Save view
            </button>
          )}
          <ZoomControl value={zoomLevel} onChange={setZoomLevel} />
        </div>
      </div>

      {/* Save view modal */}
      {showSaveModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={() => setShowSaveModal(false)}>
          <div className="w-80 rounded-lg border border-gray-700 bg-gray-900 p-4 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <h3 className="mb-3 text-sm font-semibold text-gray-200">Save current filters as a view</h3>
            <input
              type="text"
              value={saveViewName}
              onChange={(e) => setSaveViewName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") handleSaveView(); }}
              placeholder="View name"
              autoFocus
              className="mb-3 w-full rounded-md border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-200 placeholder-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowSaveModal(false)}
                className="rounded-md px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleSaveView}
                disabled={!saveViewName.trim() || savingView}
                className="rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                {savingView ? "Saving..." : "Save"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Filter bar */}
      <FilterBar
        filters={filters}
        sort={browseSort}
        dir={browseDir}
        onSetFilter={handleSetFilter}
        onSetSort={handleSetSort}
        onClearAll={handleClearAll}
        facets={facetsQuery.data ?? null}
        onSaveSmartCollection={() => setShowSmartColModal(true)}
      />

      {showSmartColModal && (
        <SaveSmartCollectionModal
          savedQuery={buildSavedQuery(filters, browseSort, browseDir)}
          onClose={() => setShowSmartColModal(false)}
        />
      )}

      {/* Status line */}
      {!isLoading && browseCount > 0 && (
        <p className="text-xs text-gray-500">
          {browseCount.toLocaleString()} photo{browseCount === 1 ? "" : "s"}
        </p>
      )}

      {isLoading ? (
        <div className="flex gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <div
              key={i}
              className="h-[220px] flex-1 animate-pulse rounded-lg bg-gray-800"
            />
          ))}
        </div>
      ) : flatAssets.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-lg border border-gray-700/50 bg-gray-900/50 py-16 text-center px-6">
          {browseFavorite ? (
            <>
              <svg className="mb-3 h-10 w-10 text-gray-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
                <path strokeLinecap="round" strokeLinejoin="round" d="M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z" />
              </svg>
              <p className="text-sm text-gray-400">No favorites yet</p>
              <p className="mt-1 text-xs text-gray-600">Heart an image to add it here.</p>
            </>
          ) : filters.length > 0 ? (
            <>
              <svg className="mb-3 h-10 w-10 text-gray-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
                <line x1="4" y1="6" x2="20" y2="6" />
                <line x1="7" y1="12" x2="17" y2="12" />
                <line x1="10" y1="18" x2="14" y2="18" />
              </svg>
              <p className="text-sm text-gray-400 mb-2">No photos match your filters</p>
              <button
                type="button"
                onClick={() => {
                  setSearchParams(new URLSearchParams());
                }}
                className="mt-2 rounded-md bg-gray-700 px-3 py-1.5 text-xs text-gray-200 hover:bg-gray-600"
              >
                Clear all filters
              </button>
            </>
          ) : (
            <>
              <svg className="mb-3 h-10 w-10 text-gray-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
                <rect x="3" y="5" width="18" height="14" rx="2" />
                <circle cx="8.5" cy="10.5" r="1.5" />
                <path d="M21 15l-5-5L5 19" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <p className="text-sm text-gray-400">No photos yet</p>
              <p className="mt-1 text-xs text-gray-600">Add a library and ingest some photos to get started.</p>
            </>
          )}
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
                const headerGroup = groups[vr.groupIndex];
                const groupIds = headerGroup?.assets.map((a) => a.asset_id) ?? [];
                const allSelected = groupIds.length > 0 && groupIds.every((id) => selection.has(id));
                return (
                  <div
                    key={virtualItem.key}
                    style={commonStyle}
                    className="flex items-end"
                  >
                    <div className="flex items-center gap-1">
                      <button
                        type="button"
                        onClick={() => selection.selectGroup(groupIds, headerGroup?.dateIso)}
                        className="flex items-center gap-2 px-1 py-2 text-sm font-semibold text-gray-400 hover:text-gray-200"
                      >
                        <span className={`inline-flex h-4 w-4 items-center justify-center rounded border transition-all ${
                          allSelected
                            ? "border-indigo-500 bg-indigo-600"
                            : "border-gray-600 opacity-0 group-hover:opacity-100"
                        } ${selection.isActive ? "opacity-100" : ""}`}>
                          {allSelected && (
                            <svg className="h-2.5 w-2.5 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                              <polyline points="20 6 9 17 4 12" />
                            </svg>
                          )}
                        </span>
                        {vr.label}
                      </button>
                      <span className="rounded-full bg-gray-700 px-2 py-0.5 text-xs text-gray-400">
                        {headerGroup?.assets.length ?? 0}
                      </span>
                      {vr.dateIso && (
                        <button
                          type="button"
                          title="Browse all photos from this date"
                          onClick={() => {
                            handleSetFilter("date", composeDate(vr.dateIso!, vr.dateIso!));
                          }}
                          className="p-1 text-gray-500 hover:text-indigo-400 transition-colors"
                        >
                          <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                            <path d="M5 12h14M12 5l7 7-7 7" />
                          </svg>
                        </button>
                      )}
                    </div>
                  </div>
                );
              }

              const group = groups[vr.groupIndex];
              if (!group) return null;
              const { justifiedRow } = vr;

              let x = 0;

              return (
                <div key={virtualItem.key} style={commonStyle}>
                  <div
                    className="relative"
                    style={{
                      height: `${justifiedRow.height}px`,
                      marginTop: `${ROW_GAP}px`,
                    }}
                  >
                    {justifiedRow.items.map((itemIndex, idx) => {
                      const asset = group.assets[itemIndex] as BrowseItem | undefined;
                      if (!asset) return null;
                      const width = justifiedRow.widths[idx];
                      const left = x;
                      x += width + ROW_GAP;

                      const aspectRatio =
                        justifiedRow.widths[idx] / justifiedRow.height;

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
                            selected={selection.has(asset.asset_id)}
                            selectionActive={selection.isActive}
                            onSelect={(e) => selection.toggle(asset.asset_id, { shiftKey: e.shiftKey })}
                            rating={ratingsMap[asset.asset_id]}
                            onFavoriteToggle={(id) => handleRatingChange(id, { favorite: !(ratingsMap[id]?.favorite ?? false) })}
                            libraryName={libraryNames[asset.library_id]}
                          />
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>

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
          onDateClick={handleLightboxDateClick}
          onAddToCollection={(assetId) => setPickerAssetIds([assetId])}
          rating={lightboxAsset ? ratingsMap[lightboxAsset.asset_id] : undefined}
          onRatingChange={handleRatingChange}
          onFilterClick={(params) => {
            setLightboxAsset(null);
            // Translate old-style lightbox filter params to filter algebra
            for (const [k, v] of Object.entries(params)) {
              handleSetFilter(k, v);
            }
          }}
          onNearbyClick={(lat, lon) => {
            setLightboxAsset(null);
            handleSetFilter("near", composeNear(String(lat), String(lon), "1"));
          }}
        />
      )}

      {/* Selection toolbar */}
      <SelectionToolbar count={selection.count} onClear={selection.clear}>
        <HeartButton
          favorite={false}
          onClick={() => handleBatchRating({ favorite: true })}
          size="sm"
        />
        <StarPicker
          stars={0}
          onChange={(stars) => handleBatchRating({ stars })}
          size="sm"
        />
        <ColorPicker
          color={null}
          onChange={(color) => handleBatchRating({ color })}
          size="sm"
        />
        <div className="h-4 w-px bg-gray-700" />
        <button
          type="button"
          onClick={() => setPickerAssetIds(selection.toArray())}
          className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-indigo-500"
        >
          Add to collection
        </button>
      </SelectionToolbar>

      {/* Collection picker */}
      {pickerAssetIds && (
        <CollectionPicker
          assetIds={pickerAssetIds}
          onClose={() => setPickerAssetIds(null)}
          onDone={selection.clear}
        />
      )}
    </div>
  );
}
