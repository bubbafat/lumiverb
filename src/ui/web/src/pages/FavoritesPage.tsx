import { useMemo, useCallback, useState } from "react";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { listFavorites, lookupRatings, rateAsset } from "../api/client";
import type { AssetPageItem, AssetRating, RatingColor } from "../api/types";
import { AssetCell } from "../components/AssetCell";
import { Lightbox } from "../components/Lightbox";
import { groupAssetsByDate } from "../lib/groupByDate";
import { buildVirtualRows } from "../lib/virtualRows";
import { useScrollContainer } from "../context/ScrollContainerContext";
import { useVirtualizer } from "@tanstack/react-virtual";

const PAGE_SIZE = 200;
const ROW_GAP = 4;
const TARGET_ROW_HEIGHT = 220;

export default function FavoritesPage() {
  const queryClient = useQueryClient();
  const scrollContainer = useScrollContainer();
  const [lightboxAsset, setLightboxAsset] = useState<AssetPageItem | null>(null);
  const [containerWidth, setContainerWidth] = useState(0);

  const favQuery = useInfiniteQuery({
    queryKey: ["favorites"],
    queryFn: ({ pageParam }) => listFavorites(pageParam, PAGE_SIZE),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage?.next_cursor ?? undefined,
  });

  const flatAssets: AssetPageItem[] = useMemo(() => {
    if (!favQuery.data?.pages) return [];
    return favQuery.data.pages.flatMap((p) => p?.items ?? []);
  }, [favQuery.data]);

  const assetIds = useMemo(() => flatAssets.map((a) => a.asset_id), [flatAssets]);
  const ratingsQuery = useInfiniteQuery({
    queryKey: ["fav-ratings", assetIds.slice(0, 500)],
    queryFn: () => lookupRatings(assetIds.slice(0, 500)),
    initialPageParam: undefined,
    getNextPageParam: () => undefined,
    enabled: assetIds.length > 0,
    staleTime: 30_000,
  });
  const ratingsMap: Record<string, AssetRating> = ratingsQuery.data?.pages?.[0]?.ratings ?? {};

  const handleRatingChange = useCallback(
    async (assetId: string, update: { favorite?: boolean; stars?: number; color?: RatingColor | null }) => {
      const prev = ratingsMap[assetId] ?? { favorite: true, stars: 0, color: null };
      const optimistic: AssetRating = {
        favorite: update.favorite !== undefined ? update.favorite : prev.favorite,
        stars: update.stars !== undefined ? update.stars : prev.stars,
        color: update.color !== undefined ? update.color : prev.color,
      };
      queryClient.setQueryData(
        ["fav-ratings", assetIds.slice(0, 500)],
        (old: { pages: [{ ratings: Record<string, AssetRating> }] } | undefined) => ({
          pages: [{ ratings: { ...(old?.pages?.[0]?.ratings ?? {}), [assetId]: optimistic } }],
          pageParams: [undefined],
        }),
      );
      try {
        await rateAsset(assetId, update);
        if (update.favorite === false) {
          queryClient.invalidateQueries({ queryKey: ["favorites"] });
        }
      } catch {
        queryClient.invalidateQueries({ queryKey: ["fav-ratings"] });
      }
    },
    [ratingsMap, assetIds, queryClient],
  );

  const groups = useMemo(() => groupAssetsByDate(flatAssets), [flatAssets]);

  const virtualRows = useMemo(() => {
    if (containerWidth <= 0) return [];
    return buildVirtualRows(groups, containerWidth, TARGET_ROW_HEIGHT, ROW_GAP);
  }, [groups, containerWidth]);

  const virtualizer = useVirtualizer({
    count: virtualRows.length,
    getScrollElement: () => scrollContainer,
    estimateSize: (i) => {
      const row = virtualRows[i];
      if (!row) return TARGET_ROW_HEIGHT;
      return row.height;
    },
    overscan: 5,
    gap: ROW_GAP,
  });

  const handleAssetClick = useCallback((asset: AssetPageItem) => {
    setLightboxAsset(asset);
  }, []);

  const isEmpty = !favQuery.isLoading && flatAssets.length === 0;

  return (
    <div className="mx-auto max-w-6xl px-6 py-6">
      <h1 className="mb-6 text-2xl font-semibold">Favorites</h1>

      {favQuery.isLoading && (
        <div className="flex justify-center py-12">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-600 border-t-indigo-500" />
        </div>
      )}

      {isEmpty && (
        <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-8 text-center text-gray-400">
          No favorites yet. Heart an image to add it here.
        </div>
      )}

      {!isEmpty && (
        <div
          ref={(el) => {
            if (el) {
              const ro = new ResizeObserver((entries) => {
                setContainerWidth(entries[0].contentRect.width);
              });
              ro.observe(el);
            }
          }}
        >
          {containerWidth > 0 && (
            <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
              {virtualizer.getVirtualItems().map((virtualRow) => {
                const row = virtualRows[virtualRow.index];
                if (!row) return null;

                if (row.type === "header") {
                  return (
                    <div
                      key={virtualRow.key}
                      style={{
                        position: "absolute",
                        top: virtualRow.start,
                        left: 0,
                        width: "100%",
                        height: row.height,
                      }}
                      className="flex items-end px-1 pb-1"
                    >
                      <span className="text-xs font-medium text-gray-500">{row.label}</span>
                    </div>
                  );
                }

                const group = groups[row.groupIndex];
                const { justifiedRow } = row;
                let x = 0;
                return (
                  <div
                    key={virtualRow.key}
                    style={{
                      position: "absolute",
                      top: virtualRow.start,
                      left: 0,
                      width: "100%",
                      height: justifiedRow.height,
                    }}
                  >
                    {justifiedRow.items.map((itemIndex: number, idx: number) => {
                      const asset = group.assets[itemIndex];
                      if (!asset) return null;
                      const width = justifiedRow.widths[idx];
                      const left = x;
                      x += width + ROW_GAP;
                      const aspectRatio = width / justifiedRow.height;

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
                            rating={ratingsMap[asset.asset_id]}
                            onFavoriteToggle={(id) => handleRatingChange(id, { favorite: !(ratingsMap[id]?.favorite ?? false) })}
                          />
                        </div>
                      );
                    })}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {lightboxAsset && (
        <Lightbox
          asset={lightboxAsset}
          assets={flatAssets}
          onClose={() => setLightboxAsset(null)}
          onNavigate={(index) => {
            if (flatAssets[index]) setLightboxAsset(flatAssets[index]);
          }}
          rating={ratingsMap[lightboxAsset.asset_id]}
          onRatingChange={handleRatingChange}
        />
      )}
    </div>
  );
}
