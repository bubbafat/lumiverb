import { useMemo, useRef, useEffect, useState, useCallback, useLayoutEffect } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ApiError,
  batchRateAssets,
  getApiKey,
  getFacets,
  getLibrary,
  getLibraryRevision,
  listDirectories,
  lookupRatings,
  pageAssets,
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
import { DrawerOverlay } from "../components/DrawerOverlay";
import { DirectoryTree } from "../components/DirectoryTree";
import type { AssetPageItem, AssetRating, FacetsResponse, RatingColor } from "../api/types";
import { HeartButton, StarPicker, ColorPicker } from "../components/RatingControls";
import { useScrollContainer } from "../context/ScrollContainerContext";
import { groupAssetsByDate } from "../lib/groupByDate";
import { useSelection } from "../lib/useSelection";
import { buildVirtualRows, buildFixedGridRows } from "../lib/virtualRows";
import { useLocalStorage } from "../lib/useLocalStorage";
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
const SEARCH_RESULT_CAP = 500;

type SortKey = "date" | "filename" | "score";
type SortDir = "asc" | "desc";

export default function BrowsePage() {
  const { libraryId } = useParams<{ libraryId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const pathPrefix = searchParams.get("path") ?? undefined;
  const activeQ = searchParams.get("q");
  const activeTag = searchParams.get("tag");
  const dateFrom = searchParams.get("date_from") ?? undefined;
  const dateTo = searchParams.get("date_to") ?? undefined;
  const sortKey = (searchParams.get("sort") as SortKey | null) ?? "date";
  const sortDir = (searchParams.get("dir") as SortDir | null) ?? "desc";

  const parentEl = useScrollContainer();
  const isFetchingNextPageRef = useRef(false);
  const hasNextPageRef = useRef(false);
  const [containerWidth, setContainerWidth] = useState(0);
  const [lightboxAsset, setLightboxAsset] = useState<AssetPageItem | null>(null);
  const [errorDismissed, setErrorDismissed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [zoomLevel, setZoomLevel] = useLocalStorage("lv_grid_zoom", 2);

  const isSearchMode = !!(activeQ || dateFrom);

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

  // Facets query for filter dropdowns
  const facetsQuery = useQuery({
    queryKey: ["facets", libraryId!, pathPrefix ?? null],
    queryFn: () => getFacets(libraryId!, pathPrefix),
    enabled: !!libraryId && canFetchAssets,
    staleTime: 5 * 60_000, // 5 minutes
  });
  const facets: FacetsResponse | null = facetsQuery.data ?? null;

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
  const browseNearLat = searchParams.get("near_lat") ? Number(searchParams.get("near_lat")) : undefined;
  const browseNearLon = searchParams.get("near_lon") ? Number(searchParams.get("near_lon")) : undefined;
  const browseNearRadiusKm = searchParams.get("near_radius_km") ? Number(searchParams.get("near_radius_km")) : undefined;
  const browseFavorite = searchParams.has("favorite") ? searchParams.get("favorite") === "true" : undefined;
  const browseStarMin = searchParams.get("star_min") ? Number(searchParams.get("star_min")) : undefined;
  const browseStarMax = searchParams.get("star_max") ? Number(searchParams.get("star_max")) : undefined;
  const browseColor = searchParams.get("color") ?? undefined;

  const browseOpts: PageAssetsOptions = useMemo(() => ({
    pathPrefix,
    tag: activeTag ?? undefined,
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
    nearLat: browseNearLat,
    nearLon: browseNearLon,
    nearRadiusKm: browseNearRadiusKm,
    favorite: browseFavorite,
    starMin: browseStarMin,
    starMax: browseStarMax,
    color: browseColor,
  }), [
    pathPrefix, activeTag, browseSort, browseDir, browseMediaType,
    browseCameraMake, browseCameraModel, browseLensModel,
    browseIsoMin, browseIsoMax, browseExposureMinUs, browseExposureMaxUs,
    browseApertureMin, browseApertureMax,
    browseFocalLengthMin, browseFocalLengthMax, browseHasExposure, browseHasGps,
    browseNearLat, browseNearLon, browseNearRadiusKm,
    browseFavorite, browseStarMin, browseStarMax, browseColor,
  ]);

  const browseQuery = useInfiniteQuery({
    queryKey: ["assets", libraryId!, browseOpts],
    queryFn: ({ pageParam }) =>
      pageAssets(libraryId!, pageParam, PAGE_SIZE, browseOpts),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage?.next_cursor ?? undefined,
    enabled: !!libraryId && !isSearchMode && canFetchAssets,
  });

  const searchQuery = useQuery({
    queryKey: [
      "search",
      libraryId!,
      activeQ,
      pathPrefix ?? null,
      activeTag ?? null,
      dateFrom ?? null,
      dateTo ?? null,
    ],
    queryFn: () =>
      searchAssets({
        libraryId: libraryId!,
        q: activeQ ?? "",
        pathPrefix,
        tag: activeTag ?? undefined,
        dateFrom,
        dateTo,
        limit: SEARCH_RESULT_CAP,
        favorite: browseFavorite,
        starMin: browseStarMin,
        starMax: browseStarMax,
        color: browseColor,
      }),
    enabled: !!libraryId && isSearchMode && canFetchAssets,
  });

  const flatAssets = useMemo(() => {
    if (isSearchMode) {
      return (searchQuery.data?.hits ?? []).map((h): AssetPageItem => ({
        asset_id: h.asset_id,
        rel_path: h.rel_path,
        file_size: h.file_size ?? 0,
        file_mtime: null,
        sha256: null,
        media_type: h.media_type ?? "image/jpeg",
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

  // Sort in search mode (client-side, all results are loaded)
  const sortedAssets = useMemo(() => {
    if (!isSearchMode) return flatAssets;
    const copy = [...flatAssets];
    if (sortKey === "date") {
      copy.sort((a, b) => {
        const da = new Date(a.taken_at ?? a.file_mtime ?? "").getTime() || 0;
        const db = new Date(b.taken_at ?? b.file_mtime ?? "").getTime() || 0;
        return sortDir === "asc" ? da - db : db - da;
      });
    } else if (sortKey === "filename") {
      copy.sort((a, b) => a.rel_path.localeCompare(b.rel_path));
      if (sortDir === "desc") copy.reverse();
    }
    return copy;
  }, [flatAssets, isSearchMode, sortKey, sortDir]);

  // For relevance sort in search mode, use the original hit order (already sorted by score)
  const displayAssets = useMemo(() => {
    if (isSearchMode && sortKey === "score") {
      return sortDir === "asc" ? [...flatAssets].reverse() : flatAssets;
    }
    return sortedAssets;
  }, [sortedAssets, flatAssets, isSearchMode, sortKey, sortDir]);

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

  const searchTotal = searchQuery.data?.total ?? 0;
  const isCapped = isSearchMode && searchTotal >= SEARCH_RESULT_CAP;

  const browseCount = useMemo(() => {
    if (isSearchMode) return 0;
    return browseQuery.data?.pages.flatMap((p) => p?.items ?? []).length ?? 0;
  }, [isSearchMode, browseQuery.data]);

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
        q={activeQ}
        tag={activeTag}
        path={pathPrefix ?? null}
        dateFrom={dateFrom ?? null}
        dateTo={dateTo ?? null}
        onChangeQ={(v) => setParam("q", v)}
        onChangeTag={(v) => setParam("tag", v)}
        onChangePath={(v) => setParam("path", v)}
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
        nearLat={searchParams.get("near_lat")}
        nearLon={searchParams.get("near_lon")}
        nearRadiusKm={searchParams.get("near_radius_km")}
        favorite={browseFavorite ?? null}
        starMin={searchParams.get("star_min")}
        starMax={searchParams.get("star_max")}
        color={searchParams.get("color")}
        onChangeFilter={(key, value) => setParam(key, value)}
        facets={facets}
      />

      {/* Toolbar: status line + sort */}
      {!isLoading && (
        <div className="flex items-center justify-between gap-4">
          <p className="text-xs text-gray-500">
            {isSearchMode ? (
              displayAssets.length === 0 ? null : (
                <>
                  {displayAssets.length.toLocaleString()}
                  {isCapped ? `+ ` : " "}
                  {displayAssets.length === 1 ? "result" : "results"}
                  {isCapped && (
                    <span className="ml-1 text-yellow-600">
                      — narrow your filters to see more
                    </span>
                  )}
                </>
              )
            ) : browseCount > 0 ? (
              `${browseCount.toLocaleString()}${currentDirTotal != null ? ` of ${currentDirTotal.toLocaleString()}` : ""} photo${browseCount === 1 ? "" : "s"}`
            ) : null}
          </p>

          {isSearchMode && displayAssets.length > 0 && (
            <select
              value={`${sortKey}-${sortDir}`}
              onChange={(e) => {
                const [key, dir] = e.target.value.split("-");
                setSearchParams((prev) => {
                  const next = new URLSearchParams(prev);
                  if (key === "date" && dir === "desc") {
                    next.delete("sort");
                    next.delete("dir");
                  } else {
                    next.set("sort", key);
                    next.set("dir", dir);
                  }
                  return next;
                });
              }}
              className="rounded-md border border-gray-700 bg-gray-800 px-2 py-1 text-xs text-gray-300 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              aria-label="Sort results"
            >
              <option value="date-desc">Newest first</option>
              <option value="date-asc">Oldest first</option>
              <option value="filename-asc">Filename A–Z</option>
              <option value="filename-desc">Filename Z–A</option>
              {activeQ && <option value="score-desc">Most relevant</option>}
            </select>
          )}
        </div>
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
          {isSearchMode || activeTag ? (
            // Search/filter mode — no results
            <>
              <svg
                className="mb-3 h-10 w-10 text-gray-600"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                aria-hidden
              >
                <circle cx="11" cy="11" r="7" />
                <line x1="16.65" y1="16.65" x2="21" y2="21" />
              </svg>
              <p className="text-sm text-gray-400 mb-2">No results</p>
              <ul className="text-xs text-gray-600 space-y-1">
                {activeQ && <li>Try different search terms</li>}
                {dateFrom && <li>Try a wider date range</li>}
                {activeTag && <li>Remove the tag filter</li>}
                {pathPrefix && <li>Browse a different folder</li>}
              </ul>
            </>
          ) : (browseMediaType || browseCameraMake || browseLensModel ||
                searchParams.get("iso_min") || searchParams.get("iso_max") ||
                searchParams.get("aperture_min") || searchParams.get("aperture_max") ||
                searchParams.get("focal_length_min") || searchParams.get("focal_length_max") ||
                browseHasGps || searchParams.get("near_lat")) ? (
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
                onClick={() => {
                  setSearchParams((prev) => {
                    const next = new URLSearchParams(prev);
                    for (const key of [
                      "media_type", "camera_make", "camera_model", "lens_model",
                      "iso_min", "iso_max", "exposure_min_us", "exposure_max_us",
                      "aperture_min", "aperture_max",
                      "focal_length_min", "focal_length_max", "has_exposure", "has_gps",
                      "near_lat", "near_lon", "near_radius_km",
                    ]) {
                      next.delete(key);
                    }
                    return next;
                  });
                }}
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
                    <button
                      type="button"
                      onClick={() => selection.selectGroup(groupIds)}
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
            setParam("tag", tag);
            setLightboxAsset(null);
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
