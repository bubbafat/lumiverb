import { useMemo, useRef, useEffect, useState, useCallback, useLayoutEffect } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ApiError,
  batchRateAssets,
  getApiKey,
  getFilteredFacets,
  getLibrary,
  getLibraryRevision,
  listDirectories,
  lookupRatings,
  queryAssets,
  rateAsset,
} from "../api/client";
import type { QueryItem } from "../api/client";
import { AssetCell } from "../components/AssetCell";
import { CollectionPicker } from "../components/CollectionPicker";
import { Lightbox } from "../components/Lightbox";
import { FilterBar } from "../components/FilterBar";
import { SaveSmartCollectionModal } from "../components/SaveSmartCollectionModal";
import { SelectionToolbar } from "../components/SelectionToolbar";
import { ZoomControl } from "../components/ZoomControl";
import { DrawerOverlay } from "../components/DrawerOverlay";
import { DirectoryTree } from "../components/DirectoryTree";
import type { AssetPageItem, AssetRating, FacetsResponse, RatingColor } from "../api/types";
import { HeartButton, StarPicker, ColorPicker } from "../components/RatingControls";
import { buildSavedQuery, composeDate, composeNear } from "../lib/queryFilter";
import type { LeafFilter } from "../lib/queryFilter";
import { useScrollContainer } from "../context/ScrollContainerContext";
import { groupAssetsByDate } from "../lib/groupByDate";
import { useSelection } from "../lib/useSelection";
import { buildVirtualRows, buildFixedGridRows } from "../lib/virtualRows";
import { useLocalStorage } from "../lib/useLocalStorage";
// parseSearchQuery available for future prefix query support
import type { VirtualRowKind } from "../lib/virtualRows";

const PAGE_SIZE = 100;
const ROW_GAP = 4;
const FIXED_GRID_BREAKPOINT = 700; // px — at or below this, use fixed-column grid

const ZOOM_LEVELS = [
  { justifiedHeight: 120, fixedCellWidth: 100 },
  { justifiedHeight: 170, fixedCellWidth: 130 },
  { justifiedHeight: 220, fixedCellWidth: 150 }, // default (index 2)
  { justifiedHeight: 300, fixedCellWidth: 200 },
  { justifiedHeight: 420, fixedCellWidth: 280 },
] as const;

const CELL_ASPECT_RATIO = 1.0;

export default function BrowsePage() {
  const { libraryId } = useParams<{ libraryId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const parentEl = useScrollContainer();
  const isFetchingNextPageRef = useRef(false);
  const hasNextPageRef = useRef(false);
  const [containerWidth, setContainerWidth] = useState(0);
  const [lightboxAsset, setLightboxAsset] = useState<AssetPageItem | null>(null);
  const [errorDismissed, setErrorDismissed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [zoomLevel, setZoomLevel] = useLocalStorage("lv_grid_zoom", 2);

  // --- Migrate legacy URL params to f= format on mount ---
  useEffect(() => {
    const LEGACY_MAP: Record<string, string> = {
      q: "query", tag: "tag", media_type: "media",
      camera_make: "camera_make", camera_model: "camera_model",
      lens_model: "lens", color: "color", favorite: "favorite",
      has_gps: "has_gps", has_faces: "has_faces", has_exposure: "has_exposure",
      has_rating: "has_rating", has_color: "has_color", person_id: "person",
    };
    const LEGACY_RANGE: Record<string, string> = {
      iso_min: "iso", iso_max: "iso",
      aperture_min: "aperture", aperture_max: "aperture",
      focal_length_min: "focal_length", focal_length_max: "focal_length",
      exposure_min_us: "exposure", exposure_max_us: "exposure",
      star_min: "stars", star_max: "stars",
    };
    const BOOL_YES = new Set(["has_gps", "has_faces", "has_exposure", "has_rating", "has_color", "favorite"]);

    let migrated = false;
    const next = new URLSearchParams(searchParams);

    for (const [oldKey, filterType] of Object.entries(LEGACY_MAP)) {
      const v = next.get(oldKey);
      if (v != null) {
        next.delete(oldKey);
        const fVal = BOOL_YES.has(oldKey) ? (v === "true" ? "yes" : "no") : v;
        next.append("f", `${filterType}:${fVal}`);
        migrated = true;
      }
    }
    // Range params: combine min/max into single filter
    const rangeDone = new Set<string>();
    for (const [oldKey, filterType] of Object.entries(LEGACY_RANGE)) {
      if (rangeDone.has(filterType)) continue;
      const isMin = oldKey.includes("min");
      const minKey = isMin ? oldKey : oldKey.replace("max", "min");
      const maxKey = isMin ? oldKey.replace("min", "max") : oldKey;
      const lo = next.get(minKey);
      const hi = next.get(maxKey);
      if (lo != null || hi != null) {
        next.delete(minKey);
        next.delete(maxKey);
        let val: string;
        if (lo && hi && lo === hi) val = lo;
        else if (lo && hi) val = `${lo}-${hi}`;
        else if (lo) val = `${lo}+`;
        else val = `-${hi}`;
        next.append("f", `${filterType}:${val}`);
        rangeDone.add(filterType);
        migrated = true;
      }
    }
    // Date params
    const df = next.get("date_from");
    const dt = next.get("date_to");
    if (df || dt) {
      next.delete("date_from");
      next.delete("date_to");
      next.append("f", `date:${df ?? ""},${dt ?? ""}`);
      migrated = true;
    }
    // Near params
    const nLat = next.get("near_lat");
    const nLon = next.get("near_lon");
    const nRad = next.get("near_radius_km");
    if (nLat && nLon) {
      next.delete("near_lat");
      next.delete("near_lon");
      next.delete("near_radius_km");
      next.append("f", `near:${nLat},${nLon},${nRad ?? "1"}`);
      migrated = true;
    }

    if (migrated) {
      setSearchParamsRef.current(next, { replace: true });
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // --- Filter algebra: read filters from URL ---
  // Read raw values from searchParams — primitives are stable across renders
  const fParams = searchParams.getAll("f");
  const fKey = fParams.join("\0");  // stable string for memo dep
  const sortParam = searchParams.get("sort");
  const dirParam = searchParams.get("dir");
  const { filters: urlFilters, sort: browseSort, direction: browseDir } = useMemo(() => {
    const filters = fParams.map((raw) => {
      const colon = raw.indexOf(":");
      if (colon <= 0) return null;
      return { type: raw.slice(0, colon), value: raw.slice(colon + 1) };
    }).filter((f): f is { type: string; value: string } => f !== null);
    const sort = sortParam ?? "taken_at";
    const direction: "asc" | "desc" = dirParam === "asc" ? "asc" : "desc";
    return { filters, sort, direction };
  }, [fKey, sortParam, dirParam]); // eslint-disable-line react-hooks/exhaustive-deps

  // Add implicit library scope filter
  const filters: LeafFilter[] = useMemo(() => {
    const hasLib = urlFilters.some((f) => f.type === "library");
    if (hasLib || !libraryId) return urlFilters;
    return [...urlFilters, { type: "library", value: libraryId }];
  }, [urlFilters, libraryId]);

  // Legacy compat: read path from URL for directory tree
  const pathPrefix = searchParams.get("path") ?? undefined;

  // Add path filter if set via directory tree
  const filtersWithPath: LeafFilter[] = useMemo(() => {
    if (!pathPrefix) return filters;
    const hasPath = filters.some((f) => f.type === "path");
    if (hasPath) return filters;
    return [...filters, { type: "path", value: pathPrefix }];
  }, [filters, pathPrefix]);

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

  function setParam(key: string, value: string | null) {
    setSearchParamsRef.current((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set(key, value);
      else next.delete(key);
      return next;
    });
  }

  const handleLightboxDateClick = useCallback(
    (dateStr: string) => {
      handleSetFilter("date", composeDate(dateStr, dateStr));
      setLightboxAsset(null);
    },
    [handleSetFilter],
  );

  const apiKey = getApiKey();
  const isAuthenticated = Boolean(apiKey);

  const location = useLocation();
  const navigate = useNavigate();

  const {
    data: library,
    error: libraryError,
    isLoading: libraryLoading,
    isError: libraryIsError,
  } = useQuery({
    queryKey: ["library", libraryId],
    queryFn: () => getLibrary(libraryId!),
    enabled: !!libraryId,
    retry: false,
  });

  const isPublicMode = !isAuthenticated && library?.is_public === true;
  const canFetchAssets = isAuthenticated || isPublicMode;

  useEffect(() => {
    if (
      libraryIsError &&
      !isAuthenticated &&
      libraryError instanceof ApiError &&
      libraryError.status === 401
    ) {
      const next = `${location.pathname}${location.search}`;
      navigate(`/login?next=${encodeURIComponent(next)}`, { replace: true });
    }
  }, [
    libraryIsError,
    isAuthenticated,
    libraryError,
    location.pathname,
    location.search,
    navigate,
  ]);

  // Poll library revision every 10 seconds; invalidate queries on change
  const queryClient = useQueryClient();
  const revisionQuery = useQuery({
    queryKey: ["library-revision", libraryId!],
    queryFn: () => getLibraryRevision(libraryId!),
    enabled: !!libraryId && canFetchAssets,
    refetchInterval: 10_000,
  });
  const revision = revisionQuery.data?.revision ?? 0;
  const prevRevisionRef = useRef(revision);
  useEffect(() => {
    if (revision !== prevRevisionRef.current) {
      prevRevisionRef.current = revision;
      queryClient.invalidateQueries({ queryKey: ["assets", libraryId!] });
      queryClient.invalidateQueries({ queryKey: ["directories", libraryId] });
      queryClient.invalidateQueries({ queryKey: ["facets", libraryId] });
    }
  }, [revision, libraryId, queryClient]);

  // Facets scoped to active filters
  const facetsQuery = useQuery({
    queryKey: ["filtered-facets", filtersWithPath],
    queryFn: () => getFilteredFacets(filtersWithPath),
    enabled: !!libraryId && canFetchAssets,
    staleTime: 5 * 60_000, // 5 minutes
  });
  const facets: FacetsResponse | null = facetsQuery.data ?? null;

  // --- Unified query: one call for both browse and search ---
  const browseQuery = useInfiniteQuery({
    queryKey: ["unified-query", filtersWithPath, browseSort, browseDir],
    queryFn: ({ pageParam }) =>
      queryAssets(filtersWithPath, {
        sort: browseSort,
        dir: browseDir,
        after: pageParam,
        limit: PAGE_SIZE,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage?.next_cursor ?? undefined,
    enabled: !!libraryId && canFetchAssets,
  });

  const flatAssets: AssetPageItem[] = useMemo(() => {
    if (!browseQuery.data?.pages) return [];
    return browseQuery.data.pages.flatMap((p) =>
      (p?.items ?? []).map((item: QueryItem): AssetPageItem => ({
        asset_id: item.asset_id,
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

  // No more client-side sort — server handles it
  const displayAssets = flatAssets;

  const isLoading = browseQuery.isLoading;
  const isFetchingNextPage = browseQuery.isFetchingNextPage;
  const hasNextPage = browseQuery.hasNextPage ?? false;
  const fetchNextPage = browseQuery.fetchNextPage;
  const error = browseQuery.error;
  const isError = browseQuery.isError;

  // Stable ref for scroll/pagination handlers
  const fetchNextPageRef = useRef(fetchNextPage);
  fetchNextPageRef.current = fetchNextPage;

  const browseCount = useMemo(() => {
    return browseQuery.data?.pages.flatMap((p) => p?.items ?? []).length ?? 0;
  }, [browseQuery.data]);

  // To show "X of Y photos", look up the current directory node from its parent listing.
  // Only relevant when a pathPrefix is active (root has no single directory node).
  const dirParent = pathPrefix
    ? pathPrefix.includes("/")
      ? pathPrefix.slice(0, pathPrefix.lastIndexOf("/"))
      : undefined
    : undefined;
  const { data: parentDirNodes } = useQuery({
    queryKey: ["directories", libraryId, dirParent ?? null],
    queryFn: () => listDirectories(libraryId!, dirParent),
    enabled: !!libraryId && !!pathPrefix && canFetchAssets,
  });
  const currentDirTotal = pathPrefix
    ? parentDirNodes?.find((d) => d.path === pathPrefix)?.asset_count
    : undefined;

  const groups = useMemo(
    () => groupAssetsByDate(displayAssets),
    [displayAssets],
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

  // Auto-select new arrivals for dates the user has already selected.
  const prevAssetCountRef = useRef(0);
  useEffect(() => {
    if (orderedAssets.length <= prevAssetCountRef.current) {
      prevAssetCountRef.current = orderedAssets.length;
      return;
    }
    const newAssets = orderedAssets.slice(prevAssetCountRef.current);
    selection.autoSelectForDates(newAssets);
    prevAssetCountRef.current = orderedAssets.length;
  }, [orderedAssets, selection]);

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
      // Optimistic update
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
        // Revert on failure
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

  if (libraryLoading) {
    return (
      <div className="space-y-4">
        <div className="animate-pulse space-y-4">
          <div className="h-8 w-48 rounded bg-gray-800" />
          <div className="flex gap-4">
            {Array.from({ length: 6 }).map((_, i) => (
              <div
                key={i}
                className="h-[220px] flex-1 rounded-lg bg-gray-800"
              />
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (libraryIsError) {
    const isPrivateUnauth =
      !isAuthenticated &&
      libraryError instanceof ApiError &&
      libraryError.status === 401;
    if (isPrivateUnauth) return null;

    return (
      <div className="flex flex-col gap-4">
        <div className="flex items-center gap-2 text-sm text-gray-400">
          <Link to="/" className="hover:text-gray-300">
            Libraries
          </Link>
          <span>/</span>
          <span className="text-gray-300">{libraryId}</span>
        </div>
        <div className="rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-red-400">
          <span>{(libraryError as ApiError).message}</span>
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
    <div className="flex flex-col gap-4 px-6 py-6">
      {isPublicMode && (
        <div className="flex items-center gap-2 text-xs text-gray-500">
          <span>🔒 Viewing as public guest</span>
          <span>·</span>
          <Link to="/login" className="text-indigo-400 hover:text-indigo-300">
            Sign in
          </Link>
        </div>
      )}
      {/* Breadcrumb + zoom control */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2 text-sm text-gray-400 min-w-0">
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
          <Link to="/" className="hover:text-gray-300 shrink-0">
            Libraries
          </Link>
          <span>/</span>
          <span className="text-gray-300 truncate">{library?.name ?? ""}</span>
          {pathPrefix && (
            <>
              <span>/</span>
              <span className="text-gray-500 truncate">{pathPrefix}</span>
            </>
          )}
        </div>
        <ZoomControl value={zoomLevel} onChange={setZoomLevel} />
      </div>

      {/* Directory tree drawer — mobile only */}
      <DrawerOverlay open={drawerOpen} onClose={() => setDrawerOpen(false)}>
        <div className="p-3">
          <DirectoryTree
            libraryId={libraryId}
            activePath={pathPrefix ?? null}
            onNavigate={(path) => {
              setParam("path", path);
              setDrawerOpen(false);
            }}
            revision={revision}
            onExcludeFolder={(path) => {
              setDrawerOpen(false);
              navigate(
                `/libraries/${libraryId}/settings?tab=filters&exclude=${encodeURIComponent(path + "/**")}`,
              );
            }}
          />
        </div>
      </DrawerOverlay>

      {/* Filter bar */}
      <FilterBar
        filters={urlFilters}
        sort={browseSort}
        dir={browseDir}
        onSetFilter={handleSetFilter}
        onSetSort={handleSetSort}
        onClearAll={handleClearAll}
        facets={facets}
        onSaveSmartCollection={() => setShowSmartColModal(true)}
      />

      {showSmartColModal && (
        <SaveSmartCollectionModal
          savedQuery={buildSavedQuery(filtersWithPath, browseSort, browseDir)}
          onClose={() => setShowSmartColModal(false)}
        />
      )}

      {/* Toolbar: status line */}
      {!isLoading && browseCount > 0 && (
        <p className="text-xs text-gray-500">
          {browseCount.toLocaleString()}{currentDirTotal != null ? ` of ${currentDirTotal.toLocaleString()}` : ""} photo{browseCount === 1 ? "" : "s"}
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
      ) : displayAssets.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-lg border border-gray-700/50 bg-gray-900/50 py-16 text-center px-6">
          {urlFilters.length > 0 ? (
            // Browse mode with active filters — no matches
            <>
              <svg
                className="mb-3 h-10 w-10 text-gray-600"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                aria-hidden
              >
                <line x1="4" y1="6" x2="20" y2="6" />
                <line x1="7" y1="12" x2="17" y2="12" />
                <line x1="10" y1="18" x2="14" y2="18" />
              </svg>
              <p className="text-sm text-gray-400 mb-2">No photos match your filters</p>
              <button
                type="button"
                onClick={handleClearAll}
                className="mt-2 rounded-md bg-gray-700 px-3 py-1.5 text-xs text-gray-200 hover:bg-gray-600"
              >
                Clear all filters
              </button>
            </>
          ) : pathPrefix ? (
            // Browse mode, path filter active, empty folder
            <p className="text-sm text-gray-400">No photos in this folder</p>
          ) : (
            <>
              <svg
                className="mb-3 h-10 w-10 text-gray-600"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                aria-hidden
              >
                <rect x="3" y="5" width="18" height="14" rx="2" />
                <circle cx="8.5" cy="10.5" r="1.5" />
                <path d="M21 15l-5-5L5 19" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <p className="text-sm text-gray-400">No photos yet</p>
              <p className="mt-1 text-xs text-gray-600">
                Run{" "}
                <code className="rounded bg-gray-800 px-1 py-0.5 text-gray-400">
                  lumiverb ingest
                </code>{" "}
                to add photos
              </p>
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
                      const asset = group.assets[itemIndex];
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
                            isPublic={isPublicMode}
                            publicLibraryId={libraryId}
                            selected={selection.has(asset.asset_id)}
                            selectionActive={selection.isActive}
                            onSelect={(e) => selection.toggle(asset.asset_id, { shiftKey: e.shiftKey })}
                            rating={ratingsMap[asset.asset_id]}
                            onFavoriteToggle={(id) => handleRatingChange(id, { favorite: !(ratingsMap[id]?.favorite ?? false) })}
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
          onTagClick={(tag) => {
            setLightboxAsset(null);
            handleSetFilter("tag", tag);
          }}
          onPathClick={(path) => {
            setLightboxAsset(null);
            setParam("path", path);
          }}
          onDateClick={handleLightboxDateClick}
          onAddToCollection={(assetId) => setPickerAssetIds([assetId])}
          rating={lightboxAsset ? ratingsMap[lightboxAsset.asset_id] : undefined}
          onRatingChange={handleRatingChange}
          libraryId={libraryId}
          isPublic={isPublicMode}
          publicLibraryId={libraryId}
          onSimilarClick={(similarAsset) => {
            setLightboxAsset(similarAsset);
          }}
          onFilterClick={(params) => {
            setLightboxAsset(null);
            if (params.camera_make) handleSetFilter("camera_make", params.camera_make);
            if (params.camera_model) handleSetFilter("camera_model", params.camera_model);
            if (params.lens_model) handleSetFilter("lens", params.lens_model);
            if (params.iso_min || params.iso_max) {
              const v = params.iso_min === params.iso_max ? params.iso_min : `${params.iso_min ?? ""}-${params.iso_max ?? ""}`;
              handleSetFilter("iso", v);
            }
            if (params.aperture_min || params.aperture_max) {
              const v = params.aperture_min === params.aperture_max ? params.aperture_min : `${params.aperture_min ?? ""}-${params.aperture_max ?? ""}`;
              handleSetFilter("aperture", v);
            }
            if (params.exposure_min_us || params.exposure_max_us) {
              const v = params.exposure_min_us === params.exposure_max_us ? params.exposure_min_us : `${params.exposure_min_us ?? ""}-${params.exposure_max_us ?? ""}`;
              handleSetFilter("exposure", v);
            }
            if (params.has_exposure) handleSetFilter("has_exposure", params.has_exposure === "true" ? "yes" : "no");
            if (params.has_gps) handleSetFilter("has_gps", params.has_gps === "true" ? "yes" : "no");
            if (params.media_type) handleSetFilter("media", params.media_type);
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
