import { useMemo, useRef, useEffect, useState, useCallback, useLayoutEffect } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  batchRateAssets,
  browseAll,
  createSavedView,
  lookupRatings,
  rateAsset,
  searchAssets,
} from "../api/client";
import type { PageAssetsOptions } from "../api/client";
import { AssetCell } from "../components/AssetCell";
import { CollectionPicker } from "../components/CollectionPicker";
import { Lightbox } from "../components/Lightbox";
import { FilterBar } from "../components/FilterBar";
import { SelectionToolbar } from "../components/SelectionToolbar";
import { ZoomControl } from "../components/ZoomControl";
import type { AssetPageItem, AssetRating, BrowseItem, RatingColor } from "../api/types";
import { HeartButton, StarPicker, ColorPicker } from "../components/RatingControls";
import { useScrollContainer } from "../context/ScrollContainerContext";
import { groupAssetsByDate } from "../lib/groupByDate";
import { useSelection } from "../lib/useSelection";
import { buildVirtualRows, buildFixedGridRows } from "../lib/virtualRows";
import { useLocalStorage } from "../lib/useLocalStorage";
import type { VirtualRowKind } from "../lib/virtualRows";

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

  function setParam(key: string, value: string | null) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set(key, value);
      else next.delete(key);
      return next;
    });
  }

  const handleChangeDateRange = useCallback(
    (from: string | null, to: string | null) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        if (from) next.set("date_from", from);
        else next.delete("date_from");
        if (to) next.set("date_to", to);
        else next.delete("date_to");
        return next;
      });
    },
    [setSearchParams],
  );

  // Search query from URL
  const activeQ = searchParams.get("q");
  const dateFrom = searchParams.get("date_from") ?? undefined;
  const dateTo = searchParams.get("date_to") ?? undefined;
  const isSearchMode = !!(activeQ || dateFrom);

  // Build filter/sort options from URL search params
  const browseSort = searchParams.get("sort") ?? "taken_at";
  const browseDir = (searchParams.get("dir") as "asc" | "desc" | null) ?? "desc";
  const browseMediaType = searchParams.get("media_type") ?? undefined;
  const browseCameraMake = searchParams.get("camera_make") ?? undefined;
  const browseCameraModel = searchParams.get("camera_model") ?? undefined;
  const browseLensModel = searchParams.get("lens_model") ?? undefined;
  const browseIsoMin = searchParams.get("iso_min") ? Number(searchParams.get("iso_min")) : undefined;
  const browseIsoMax = searchParams.get("iso_max") ? Number(searchParams.get("iso_max")) : undefined;
  const browseExposureMinUs = searchParams.get("exposure_min_us") ? Number(searchParams.get("exposure_min_us")) : undefined;
  const browseExposureMaxUs = searchParams.get("exposure_max_us") ? Number(searchParams.get("exposure_max_us")) : undefined;
  const browseApertureMin = searchParams.get("aperture_min") ? Number(searchParams.get("aperture_min")) : undefined;
  const browseApertureMax = searchParams.get("aperture_max") ? Number(searchParams.get("aperture_max")) : undefined;
  const browseFocalLengthMin = searchParams.get("focal_length_min") ? Number(searchParams.get("focal_length_min")) : undefined;
  const browseFocalLengthMax = searchParams.get("focal_length_max") ? Number(searchParams.get("focal_length_max")) : undefined;
  const browseHasExposure = searchParams.has("has_exposure") ? searchParams.get("has_exposure") === "true" : undefined;
  const browseHasGps = searchParams.get("has_gps") === "true";
  const browseHasFaces = searchParams.get("has_faces") === "true";
  const browsePersonId = searchParams.get("person_id") ?? undefined;
  const browseNearLat = searchParams.get("near_lat") ? Number(searchParams.get("near_lat")) : undefined;
  const browseNearLon = searchParams.get("near_lon") ? Number(searchParams.get("near_lon")) : undefined;
  const browseNearRadiusKm = searchParams.get("near_radius_km") ? Number(searchParams.get("near_radius_km")) : undefined;
  const browseFavorite = searchParams.has("favorite") ? searchParams.get("favorite") === "true" : undefined;
  const browseStarMin = searchParams.get("star_min") ? Number(searchParams.get("star_min")) : undefined;
  const browseStarMax = searchParams.get("star_max") ? Number(searchParams.get("star_max")) : undefined;
  const browseColor = searchParams.get("color") ?? undefined;
  const browseLibraryId = searchParams.get("library_id") ?? undefined;

  const browseOpts: PageAssetsOptions & { libraryId?: string } = useMemo(() => ({
    sort: browseSort,
    dir: browseDir,
    mediaType: browseMediaType,
    cameraMake: browseCameraMake,
    cameraModel: browseCameraModel,
    lensModel: browseLensModel,
    isoMin: browseIsoMin,
    isoMax: browseIsoMax,
    exposureMinUs: browseExposureMinUs,
    exposureMaxUs: browseExposureMaxUs,
    apertureMin: browseApertureMin,
    apertureMax: browseApertureMax,
    focalLengthMin: browseFocalLengthMin,
    focalLengthMax: browseFocalLengthMax,
    hasExposure: browseHasExposure,
    hasGps: browseHasGps,
    hasFaces: browseHasFaces,
    personId: browsePersonId,
    nearLat: browseNearLat,
    nearLon: browseNearLon,
    nearRadiusKm: browseNearRadiusKm,
    favorite: browseFavorite,
    starMin: browseStarMin,
    starMax: browseStarMax,
    color: browseColor,
    libraryId: browseLibraryId,
  }), [
    browseSort, browseDir, browseMediaType,
    browseCameraMake, browseCameraModel, browseLensModel,
    browseIsoMin, browseIsoMax, browseExposureMinUs, browseExposureMaxUs,
    browseApertureMin, browseApertureMax,
    browseFocalLengthMin, browseFocalLengthMax, browseHasExposure, browseHasGps, browseHasFaces, browsePersonId,
    browseNearLat, browseNearLon, browseNearRadiusKm,
    browseFavorite, browseStarMin, browseStarMax, browseColor,
    browseLibraryId,
  ]);

  const SEARCH_RESULT_CAP = 500;

  const browseQuery = useInfiniteQuery({
    queryKey: ["unified-browse", browseOpts],
    queryFn: ({ pageParam }) =>
      browseAll(pageParam, PAGE_SIZE, browseOpts),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage?.next_cursor ?? undefined,
    enabled: !isSearchMode,
  });

  const searchQuery = useQuery({
    // Every filter dimension the server's /v1/search currently supports must
    // appear here or React Query won't refetch when the user toggles it.
    // NOTE: camera/exposure/gps/near filters are exposed in the sidebar but
    // are not yet accepted by /v1/search — they're silently ignored in
    // search mode until the server endpoint grows support. See
    // src/server/api/routers/search.py `def search(...)`.
    queryKey: [
      "unified-search",
      activeQ,
      dateFrom,
      dateTo,
      browseMediaType,
      browseLibraryId,
      browseHasFaces,
      browsePersonId,
      browseFavorite,
      browseStarMin,
      browseStarMax,
      browseColor,
    ],
    queryFn: () =>
      searchAssets({
        q: activeQ ?? "",
        mediaType: browseMediaType,
        libraryId: browseLibraryId,
        dateFrom,
        dateTo,
        limit: SEARCH_RESULT_CAP,
        hasFaces: browseHasFaces,
        personId: browsePersonId,
        favorite: browseFavorite,
        starMin: browseStarMin,
        starMax: browseStarMax,
        color: browseColor,
      }),
    enabled: isSearchMode,
  });

  const flatAssets: BrowseItem[] = useMemo(() => {
    if (isSearchMode) {
      return (searchQuery.data?.hits ?? []).map((h): BrowseItem => ({
        asset_id: h.asset_id,
        library_id: h.library_id ?? "",
        library_name: h.library_name ?? "",
        rel_path: h.rel_path,
        file_size: h.file_size ?? 0,
        file_mtime: null,
        sha256: null,
        media_type: h.media_type ?? "image",
        width: h.width ?? null,
        height: h.height ?? null,
        taken_at: h.taken_at ?? null,
        status: "indexed",
        duration_sec: h.duration_sec ?? null,
        camera_make: h.camera_make ?? null,
        camera_model: h.camera_model ?? null,
        iso: null,
        aperture: null,
        focal_length: null,
        focal_length_35mm: null,
        lens_model: null,
        flash_fired: null,
        gps_lat: null,
        gps_lon: null,
        created_at: null,
      }));
    }
    if (!browseQuery.data?.pages) return [];
    return browseQuery.data.pages.flatMap((p) => p?.items ?? []);
  }, [isSearchMode, searchQuery.data, browseQuery.data]);

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

  const isLoading = isSearchMode ? searchQuery.isLoading : browseQuery.isLoading;
  const isFetchingNextPage = isSearchMode ? false : browseQuery.isFetchingNextPage;
  const hasNextPage = isSearchMode ? false : browseQuery.hasNextPage;
  const fetchNextPage = browseQuery.fetchNextPage;

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
        fetchNextPage();
      }
    };
    parentEl.addEventListener("scroll", onScroll);
    return () => parentEl.removeEventListener("scroll", onScroll);
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
      if (hasNextPage && !isFetchingNextPage && index >= orderedAssets.length - 20) {
        fetchNextPage();
      }
    },
    [orderedAssets, hasNextPage, isFetchingNextPage, fetchNextPage],
  );

  const handleLightboxDateClick = useCallback(
    (dateStr: string) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        next.set("date_from", dateStr);
        next.set("date_to", dateStr);
        return next;
      });
      setLightboxAsset(null);
    },
    [setSearchParams],
  );

  // Determine page title based on active filters
  const pageTitle = useMemo(() => {
    if (browseFavorite) return "Favorites";
    return "All Photos";
  }, [browseFavorite]);

  const hasActiveFilters = searchParams.toString().length > 0;

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
        q={activeQ}
        tag={searchParams.get("tag")}
        path={null}
        dateFrom={searchParams.get("date_from")}
        dateTo={searchParams.get("date_to")}
        onChangeQ={(v) => setParam("q", v)}
        onChangeTag={(v) => setParam("tag", v)}
        onChangePath={() => {}}
        onChangeDateRange={handleChangeDateRange}
        sort={browseSort}
        dir={browseDir}
        mediaType={browseMediaType ?? null}
        cameraMake={browseCameraMake ?? null}
        cameraModel={browseCameraModel ?? null}
        lensModel={browseLensModel ?? null}
        isoMin={searchParams.get("iso_min")}
        isoMax={searchParams.get("iso_max")}
        exposureMinUs={searchParams.get("exposure_min_us")}
        exposureMaxUs={searchParams.get("exposure_max_us")}
        apertureMin={searchParams.get("aperture_min")}
        apertureMax={searchParams.get("aperture_max")}
        focalLengthMin={searchParams.get("focal_length_min")}
        focalLengthMax={searchParams.get("focal_length_max")}
        hasExposure={browseHasExposure ?? null}
        hasGps={browseHasGps}
        hasFaces={browseHasFaces}
        personId={browsePersonId ?? null}
        nearLat={searchParams.get("near_lat")}
        nearLon={searchParams.get("near_lon")}
        nearRadiusKm={searchParams.get("near_radius_km")}
        favorite={browseFavorite ?? null}
        starMin={searchParams.get("star_min")}
        starMax={searchParams.get("star_max")}
        color={searchParams.get("color")}
        onChangeFilter={(key, value) => setParam(key, value)}
        onChangeFilters={(changes) => {
          setSearchParams((prev) => {
            const next = new URLSearchParams(prev);
            for (const [k, v] of Object.entries(changes)) {
              if (v) next.set(k, v);
              else next.delete(k);
            }
            return next;
          });
        }}
        facets={null}
      />

      {/* Status line */}
      {!isLoading && browseCount > 0 && (
        <p className="text-xs text-gray-500">
          {browseCount.toLocaleString()} {isSearchMode ? "result" : "photo"}{browseCount === 1 ? "" : "s"}
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
          ) : (browseMediaType || browseCameraMake || browseLensModel || browseColor ||
                searchParams.get("iso_min") || searchParams.get("star_min") ||
                browseHasGps || searchParams.get("near_lat")) ? (
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
                            setSearchParams((prev) => {
                              const next = new URLSearchParams(prev);
                              next.set("date_from", vr.dateIso!);
                              next.set("date_to", vr.dateIso!);
                              next.delete("q");
                              return next;
                            });
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
            setSearchParams((prev) => {
              const next = new URLSearchParams(prev);
              for (const [k, v] of Object.entries(params)) {
                next.set(k, v);
              }
              return next;
            });
          }}
          onNearbyClick={(lat, lon) => {
            setLightboxAsset(null);
            setSearchParams((prev) => {
              const next = new URLSearchParams(prev);
              next.set("near_lat", String(lat));
              next.set("near_lon", String(lon));
              next.set("near_radius_km", "1");
              return next;
            });
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
