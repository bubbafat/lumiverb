import { useCallback, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { batchRateAssets, lookupRatings, rateAsset } from "../api/client";
import type { AssetRating, RatingColor } from "../api/types";
import { clipCount } from "../components/ProjectUsageList";

export type RatingUpdate = { favorite?: boolean; stars?: number; color?: RatingColor | null };

type Ratings = { ratings: Record<string, AssetRating> };

const UNRATED: AssetRating = { favorite: false, stars: 0, color: null };

/**
 * The signed-in person's ratings of the first 500 clips shown, and rating them: one clip shown at
 * once and put back if it isn't saved, or a selection. A rating that isn't saved says so through
 * `fail` (useClipActions' notice), never quietly.
 */
export function useRatings(
  assetIds: string[],
  enabled: boolean,
  fail: (what: string, err: unknown) => void,
) {
  const queryClient = useQueryClient();
  const shown = assetIds.slice(0, 500);
  const ratingsQuery = useQuery({
    queryKey: ["ratings", shown],
    queryFn: () => lookupRatings(shown),
    enabled: shown.length > 0 && enabled,
    staleTime: 30_000,
  });
  const ratingsMap: Record<string, AssetRating> = ratingsQuery.data?.ratings ?? {};
  // Each clip's latest rating here, by number: one that fails puts back what it replaced only when
  // nothing has been rated since.
  const latest = useRef<Record<string, number>>({});

  const rate = useCallback(
    async (assetId: string, update: RatingUpdate) => {
      const mine = (latest.current[assetId] ?? 0) + 1;
      latest.current[assetId] = mine;
      const key = ["ratings", assetIds.slice(0, 500)];
      const prev = queryClient.getQueryData<Ratings>(key)?.ratings[assetId] ?? UNRATED;
      const optimistic: AssetRating = {
        favorite: update.favorite !== undefined ? update.favorite : prev.favorite,
        stars: update.stars !== undefined ? update.stars : prev.stars,
        color: update.color !== undefined ? update.color : prev.color,
      };
      queryClient.setQueryData(key, (old: Ratings | undefined) => ({
        ratings: { ...(old?.ratings ?? {}), [assetId]: optimistic },
      }));
      try {
        await rateAsset(assetId, update);
      } catch (err) {
        // Put back what it was, not asked of the server again: that could fail the same way and
        // leave this one shown.
        if (latest.current[assetId] === mine) {
          queryClient.setQueryData(key, (old: Ratings | undefined) => ({
            ratings: { ...(old?.ratings ?? {}), [assetId]: prev },
          }));
        }
        fail("save the rating", err);
      }
    },
    [assetIds, queryClient, fail],
  );

  const rateSelection = useCallback(
    async (ids: string[], update: RatingUpdate) => {
      if (ids.length === 0) return;
      try {
        await batchRateAssets(ids, update);
        void queryClient.invalidateQueries({ queryKey: ["ratings"] });
      } catch (err) {
        fail(`rate ${clipCount(ids.length)}`, err);
      }
    },
    [queryClient, fail],
  );

  return { ratingsMap, rate, rateSelection };
}
