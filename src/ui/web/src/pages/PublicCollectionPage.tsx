import { useMemo, useRef, useEffect, useState, useCallback, useLayoutEffect } from "react";
import { useParams } from "react-router-dom";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { AssetCell } from "../components/AssetCell";
import { Lightbox } from "../components/Lightbox";
import type { AssetPageItem } from "../api/types";
import { groupAssetsByDate } from "../lib/groupByDate";
import { buildVirtualRows, buildFixedGridRows } from "../lib/virtualRows";
import type { VirtualRowKind } from "../lib/virtualRows";

const PAGE_SIZE = 200;
const ROW_GAP = 4;
const FIXED_GRID_BREAKPOINT = 700;
const TARGET_ROW_HEIGHT = 220;
const FIXED_CELL_WIDTH = 150;
const CELL_ASPECT_RATIO = 1.0;

interface PublicCollectionDetail {
  collection_id: string;
  name: string;
  description: string | null;
  cover_asset_id: string | null;
  asset_count: number;
}

interface PublicAssetItem {
  asset_id: string;
  media_type: string;
  width: number | null;
  height: number | null;
  taken_at: string | null;
  duration_sec: number | null;
}

interface PublicAssetsResponse {
  items: PublicAssetItem[];
  next_cursor: string | null;
}

async function fetchPublicCollection(collectionId: string): Promise<PublicCollectionDetail> {
  const res = await fetch(`/v1/public/collections/${collectionId}`);
  if (!res.ok) throw new Error("Collection not found");
  return res.json();
}

async function fetchPublicAssets(
  collectionId: string,
  cursor?: string,
): Promise<PublicAssetsResponse> {
  const qs = new URLSearchParams();
  if (cursor) qs.set("after", cursor);
  qs.set("limit", String(PAGE_SIZE));
  const res = await fetch(`/v1/public/collections/${collectionId}/assets?${qs.toString()}`);
  if (!res.ok) throw new Error("Failed to load assets");
  return res.json();
}

export default function PublicCollectionPage() {
  const { collectionId } = useParams<{ collectionId: string }>();
  const scrollRef = useRef<HTMLDivElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState(0);
  const [lightboxAsset, setLightboxAsset] = useState<AssetPageItem | null>(null);

  const { data: collection, isLoading: isCollectionLoading, error } = useQuery({
    queryKey: ["public-collection", collectionId],
    queryFn: () => fetchPublicCollection(collectionId!),
    enabled: !!collectionId,
  });

  const {
    data,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isLoading: isAssetsLoading,
  } = useInfiniteQuery({
    queryKey: ["public-collection-assets", collectionId],
    queryFn: ({ pageParam }) => fetchPublicAssets(collectionId!, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage?.next_cursor ?? undefined,
    enabled: !!collectionId,
  });

  // Adapt public assets to AssetPageItem shape for reuse of grid components
  const orderedAssets: AssetPageItem[] = useMemo(() => {
    if (!data?.pages) return [];
    return data.pages.flatMap((page) =>
      page.items.map((item) => ({
        asset_id: item.asset_id,
        rel_path: "",
        file_size: 0,
        file_mtime: null,
        sha256: null,
        media_type: item.media_type,
        width: item.width,
        height: item.height,
        taken_at: item.taken_at,
        status: "ready",
        duration_sec: item.duration_sec,
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
      })),
    );
  }, [data]);

  const groups = useMemo(
    () => groupAssetsByDate(orderedAssets),
    [orderedAssets],
  );

  useLayoutEffect(() => {
    const el = gridRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      setContainerWidth(entries[0]?.contentRect.width ?? 0);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const virtualRows: VirtualRowKind[] = useMemo(() => {
    if (containerWidth <= 0) return [];
    if (containerWidth <= FIXED_GRID_BREAKPOINT) {
      const columns = Math.max(2, Math.floor(containerWidth / FIXED_CELL_WIDTH));
      const cellWidth = Math.floor((containerWidth - ROW_GAP * (columns - 1)) / columns);
      const rowHeight = Math.round(cellWidth * CELL_ASPECT_RATIO);
      return buildFixedGridRows(groups, containerWidth, columns, rowHeight, ROW_GAP);
    }
    return buildVirtualRows(groups, containerWidth, TARGET_ROW_HEIGHT, ROW_GAP);
  }, [groups, containerWidth]);

  const rowVirtualizer = useVirtualizer({
    count: virtualRows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (i) => virtualRows[i]?.height ?? TARGET_ROW_HEIGHT,
    overscan: 3,
    gap: ROW_GAP,
  });

  // Infinite scroll
  const hasNextPageRef = useRef(hasNextPage);
  const isFetchingNextPageRef = useRef(isFetchingNextPage);
  hasNextPageRef.current = hasNextPage;
  isFetchingNextPageRef.current = isFetchingNextPage;

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const handleScroll = () => {
      const { scrollHeight, scrollTop, clientHeight } = el;
      if (scrollHeight - scrollTop - clientHeight < 400 && hasNextPageRef.current && !isFetchingNextPageRef.current) {
        fetchNextPage();
      }
    };
    el.addEventListener("scroll", handleScroll, { passive: true });
    return () => el.removeEventListener("scroll", handleScroll);
  }, [fetchNextPage]);

  const handleAssetClick = useCallback((asset: AssetPageItem) => {
    setLightboxAsset(asset);
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

  if (isCollectionLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-950 text-gray-500">
        Loading...
      </div>
    );
  }

  if (error || !collection) {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-950 text-gray-500">
        Collection not found
      </div>
    );
  }

  return (
    <div className="flex h-screen flex-col bg-gray-950 text-gray-100">
      {/* Header */}
      <div className="shrink-0 border-b border-gray-800 px-6 py-4">
        <h1 className="text-xl font-semibold">{collection.name}</h1>
        {collection.description && (
          <p className="mt-1 text-sm text-gray-400">{collection.description}</p>
        )}
        <p className="mt-1 text-xs text-gray-600">
          {collection.asset_count} {collection.asset_count === 1 ? "item" : "items"}
        </p>
      </div>

      {/* Grid */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        <div ref={gridRef} className="px-2 py-2">
          {isAssetsLoading ? (
            <div className="flex h-32 items-center justify-center text-gray-500">
              Loading...
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
                    <div key={virtualItem.key} style={commonStyle} className="flex items-end">
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
                      style={{ height: `${justifiedRow.height}px`, marginTop: `${ROW_GAP}px` }}
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
                            style={{ left, top: 0, width, height: "100%" }}
                          >
                            <AssetCell
                              asset={asset}
                              onClick={() => handleAssetClick(asset)}
                              aspectRatio={aspectRatio}
                              isPublic
                              publicLibraryId={collectionId}
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
      </div>

      {/* Lightbox */}
      {lightboxAsset && (
        <Lightbox
          asset={lightboxAsset}
          assets={orderedAssets}
          hasMore={hasNextPage}
          onClose={() => setLightboxAsset(null)}
          onNavigate={handleLightboxNavigate}
          isPublic
          publicLibraryId={collectionId}
        />
      )}
    </div>
  );
}
