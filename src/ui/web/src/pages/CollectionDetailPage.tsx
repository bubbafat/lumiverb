import { useMemo, useRef, useEffect, useState, useCallback, useLayoutEffect } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useInfiniteQuery, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  getCollection,
  listCollectionAssets,
  updateCollection,
  deleteCollection,
  ApiError,
} from "../api/client";
import { AssetCell } from "../components/AssetCell";
import { Lightbox } from "../components/Lightbox";
import { ZoomControl } from "../components/ZoomControl";
import type { AssetPageItem } from "../api/types";
import { useScrollContainer } from "../context/ScrollContainerContext";
import { groupAssetsByDate } from "../lib/groupByDate";
import { buildVirtualRows, buildFixedGridRows } from "../lib/virtualRows";
import { useLocalStorage } from "../lib/useLocalStorage";
import type { VirtualRowKind } from "../lib/virtualRows";

const PAGE_SIZE = 200;
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

export default function CollectionDetailPage() {
  const { collectionId } = useParams<{ collectionId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const parentEl = useScrollContainer();

  // Collection metadata
  const { data: collection, isLoading: isCollectionLoading } = useQuery({
    queryKey: ["collection", collectionId],
    queryFn: () => getCollection(collectionId!),
    enabled: !!collectionId,
  });

  // Settings state
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [editSort, setEditSort] = useState("manual");
  const [settingsError, setSettingsError] = useState("");

  // Delete state
  const [deleteConfirm, setDeleteConfirm] = useState(false);

  // Grid state
  const [zoomLevel, setZoomLevel] = useLocalStorage("lv_grid_zoom", 2);
  const [containerWidth, setContainerWidth] = useState(0);
  const gridRef = useRef<HTMLDivElement>(null);
  const [lightboxAsset, setLightboxAsset] = useState<AssetPageItem | null>(null);

  // Fetch collection assets with infinite query
  const {
    data,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isLoading: isAssetsLoading,
  } = useInfiniteQuery({
    queryKey: ["collection-assets", collectionId],
    queryFn: ({ pageParam }) =>
      listCollectionAssets(collectionId!, pageParam, PAGE_SIZE),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage?.next_cursor ?? undefined,
    enabled: !!collectionId,
  });

  // Flatten pages into ordered asset list, adapted to AssetPageItem shape
  const orderedAssets: AssetPageItem[] = useMemo(() => {
    if (!data?.pages) return [];
    return data.pages.flatMap((page) =>
      page.items.map((item) => ({
        ...item,
        file_mtime: null,
        sha256: null,
        iso: null,
        aperture: null,
        focal_length: null,
        focal_length_35mm: null,
        lens_model: null,
        flash_fired: null,
        gps_lat: null,
        gps_lon: null,
        created_at: null,
      })),
    );
  }, [data]);

  // Group by date
  const groups = useMemo(
    () => groupAssetsByDate(orderedAssets),
    [orderedAssets],
  );

  // Measure container width
  useLayoutEffect(() => {
    const el = gridRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      setContainerWidth(entries[0]?.contentRect.width ?? 0);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

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
    estimateSize: (i) => virtualRows[i]?.height ?? zoom.justifiedHeight,
    overscan: 3,
    gap: ROW_GAP,
  });

  // Infinite scroll trigger
  const hasNextPageRef = useRef(hasNextPage);
  const isFetchingNextPageRef = useRef(isFetchingNextPage);
  hasNextPageRef.current = hasNextPage;
  isFetchingNextPageRef.current = isFetchingNextPage;

  useEffect(() => {
    if (!parentEl) return;
    const handleScroll = () => {
      const { scrollHeight, scrollTop, clientHeight } = parentEl;
      if (
        scrollHeight - scrollTop - clientHeight < 400 &&
        hasNextPageRef.current &&
        !isFetchingNextPageRef.current
      ) {
        fetchNextPage();
      }
    };
    parentEl.addEventListener("scroll", handleScroll, { passive: true });
    return () => parentEl.removeEventListener("scroll", handleScroll);
  }, [parentEl, fetchNextPage]);

  // Lightbox handlers
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

  // Settings mutations
  const updateMutation = useMutation({
    mutationFn: (body: Parameters<typeof updateCollection>[1]) =>
      updateCollection(collectionId!, body),
    onSuccess: () => {
      setSettingsOpen(false);
      setSettingsError("");
      queryClient.invalidateQueries({ queryKey: ["collection", collectionId] });
      queryClient.invalidateQueries({ queryKey: ["collections"] });
    },
    onError: (err: ApiError) => setSettingsError(err.message),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteCollection(collectionId!),
    onSuccess: () => navigate("/collections"),
  });

  const openSettings = () => {
    if (collection) {
      setEditName(collection.name);
      setEditDesc(collection.description ?? "");
      setEditSort(collection.sort_order);
      setSettingsError("");
    }
    setSettingsOpen(true);
  };

  const handleSettingsSave = (e: React.FormEvent) => {
    e.preventDefault();
    updateMutation.mutate({
      name: editName.trim(),
      description: editDesc.trim() || null,
      sort_order: editSort,
    });
  };

  if (isCollectionLoading) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500">
        Loading...
      </div>
    );
  }

  if (!collection) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 text-gray-500">
        <p>Collection not found</p>
        <Link to="/collections" className="text-indigo-400 hover:text-indigo-300">
          Back to collections
        </Link>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex shrink-0 items-center justify-between border-b border-gray-800 px-4 py-3">
        <div className="flex items-center gap-3 min-w-0">
          <Link
            to="/collections"
            className="text-sm text-gray-500 hover:text-gray-300"
          >
            Collections
          </Link>
          <span className="text-gray-600">/</span>
          <h1 className="truncate text-lg font-semibold text-gray-100">
            {collection.name}
          </h1>
          <span className="shrink-0 text-sm text-gray-500">
            {collection.asset_count} {collection.asset_count === 1 ? "item" : "items"}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <ZoomControl value={zoomLevel} onChange={setZoomLevel} />
          <button
            type="button"
            onClick={openSettings}
            className="rounded-lg border border-gray-600 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-800/50"
          >
            Settings
          </button>
        </div>
      </div>

      {/* Description */}
      {collection.description && (
        <div className="shrink-0 border-b border-gray-800/50 px-4 py-2">
          <p className="text-sm text-gray-400">{collection.description}</p>
        </div>
      )}

      {/* Grid */}
      <div ref={gridRef} className="flex-1 min-h-0 px-2 py-2">
        {isAssetsLoading ? (
          <div className="flex h-32 items-center justify-center text-gray-500">
            Loading assets...
          </div>
        ) : orderedAssets.length === 0 ? (
          <div className="flex h-32 items-center justify-center text-gray-500">
            This collection is empty.
          </div>
        ) : (
          <div
            style={{
              height: rowVirtualizer.getTotalSize(),
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
                          />
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Lightbox */}
      {lightboxAsset && (
        <Lightbox
          asset={lightboxAsset}
          assets={orderedAssets}
          hasMore={hasNextPage}
          onClose={handleLightboxClose}
          onNavigate={handleLightboxNavigate}
          onSimilarClick={(similarAsset) => setLightboxAsset(similarAsset)}
        />
      )}

      {/* Settings modal */}
      {settingsOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setSettingsOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-xl bg-gray-900 p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="mb-4 text-lg font-semibold text-gray-100">
              Collection settings
            </h2>
            <form onSubmit={handleSettingsSave} className="space-y-4">
              {settingsError && (
                <div className="rounded-lg border border-red-800/50 bg-red-900/20 px-3 py-2 text-sm text-red-400">
                  {settingsError}
                </div>
              )}
              <div>
                <label htmlFor="edit-name" className="mb-1 block text-sm text-gray-400">
                  Name
                </label>
                <input
                  id="edit-name"
                  type="text"
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                  required
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-100 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label htmlFor="edit-desc" className="mb-1 block text-sm text-gray-400">
                  Description
                </label>
                <input
                  id="edit-desc"
                  type="text"
                  value={editDesc}
                  onChange={(e) => setEditDesc(e.target.value)}
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-100 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label htmlFor="edit-sort" className="mb-1 block text-sm text-gray-400">
                  Sort order
                </label>
                <select
                  id="edit-sort"
                  value={editSort}
                  onChange={(e) => setEditSort(e.target.value)}
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-100 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                >
                  <option value="manual">Manual</option>
                  <option value="added_at">Date added</option>
                  <option value="taken_at">Date taken</option>
                </select>
              </div>
              <div className="flex items-center justify-between pt-2">
                <button
                  type="button"
                  onClick={() => setDeleteConfirm(true)}
                  className="text-sm text-red-400 hover:text-red-300"
                >
                  Delete collection
                </button>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setSettingsOpen(false)}
                    className="rounded-lg border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 hover:bg-gray-800"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={updateMutation.isPending || !editName.trim()}
                    className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                  >
                    {updateMutation.isPending ? "Saving..." : "Save"}
                  </button>
                </div>
              </div>
            </form>

            {deleteConfirm && (
              <div className="mt-4 rounded-lg border border-red-800/50 bg-red-900/20 p-4">
                <p className="mb-3 text-sm text-red-400">
                  Delete &quot;{collection.name}&quot;? Source assets will not be affected.
                </p>
                <div className="flex justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => setDeleteConfirm(false)}
                    className="rounded px-3 py-1.5 text-sm text-gray-400 hover:text-gray-300"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={() => deleteMutation.mutate()}
                    disabled={deleteMutation.isPending}
                    className="rounded bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
                  >
                    {deleteMutation.isPending ? "Deleting..." : "Delete"}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
