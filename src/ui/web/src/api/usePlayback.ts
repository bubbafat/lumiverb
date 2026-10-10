import { useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { getPlayback } from "./client";
import { useAuthenticatedImage } from "./useAuthenticatedImage";

type Source = "analysis_proxy" | "preview";

/**
 * What a video's player streams: the signed playback link (full length,
 * seekable, within the cap for this viewer), or the 10-second preview fetched
 * as a blob when no link is available.
 *
 * While only the preview exists it asks again every `pollMs`, so the player
 * switches to the whole video as soon as it's processed. While the capped
 * copy is made (ready false) it asks again every `preparingMs` and plays
 * nothing yet. Each answer carries
 * a fresh link; the first link for a source is kept, since a new src would
 * restart the video. `renew()` swaps it for a fresh one, for a link that
 * failed (links last an hour).
 */
export function usePlayback(
  assetId: string,
  {
    enabled,
    isPublic = false,
    publicLibraryId,
    publicProjectId,
    pollMs = 30_000,
    preparingMs = 2_000,
  }: {
    enabled: boolean;
    isPublic?: boolean;
    publicLibraryId?: string;
    publicProjectId?: string;
    pollMs?: number;
    preparingMs?: number;
  },
): {
  src: string | null;
  source: Source | null;
  maxSeconds: number | null;
  isLoading: boolean;
  renew: () => void;
  /** A renewal found nothing to play (the link can't be replaced). */
  failed: boolean;
} {
  const canAsk = enabled && (!isPublic || !!publicLibraryId || !!publicProjectId);
  const playback = useQuery({
    queryKey: ["playback", assetId, publicLibraryId ?? null, publicProjectId ?? null],
    queryFn: () =>
      getPlayback(assetId, isPublic ? publicLibraryId : undefined, isPublic ? publicProjectId : undefined),
    enabled: canAsk,
    retry: false,
    staleTime: 30 * 60_000,
    // A new link would restart a playing video; failures renew instead.
    refetchOnWindowFocus: false,
    refetchInterval: (query) =>
      query.state.status === "error"
        ? false
        : query.state.data && !query.state.data.ready
        ? preparingMs
        : query.state.data?.source === "preview"
          ? pollMs
          : false,
  });
  const fallback = useAuthenticatedImage(assetId, "video-preview", {
    enabled: enabled && playback.isError,
    isPublic,
    publicLibraryId,
    publicProjectId,
  });
  const kept = useRef<{ assetId: string; source: Source; url: string } | null>(null);
  const renewing = useRef(false);
  const renew = () => {
    renewing.current = true;
    void playback.refetch();
  };

  const failed = playback.isRefetchError;
  if (!enabled) return { src: null, source: null, maxSeconds: null, isLoading: false, renew, failed: false };
  if (playback.data && !playback.data.ready && !playback.isError) {
    // A link already playing keeps playing until the new one is ready.
    if (kept.current?.assetId === assetId) {
      return { src: kept.current.url, source: kept.current.source, maxSeconds: null, isLoading: false, renew, failed };
    }
    return { src: null, source: null, maxSeconds: null, isLoading: true, renew, failed: false };
  }
  if (playback.data && playback.data.ready) {
    const { source, url, max_seconds } = playback.data;
    const stale = !kept.current || kept.current.assetId !== assetId || kept.current.source !== source;
    if (stale || (renewing.current && kept.current?.url !== url)) {
      kept.current = { assetId, source, url };
      renewing.current = false;
    }
    return { src: kept.current!.url, source, maxSeconds: max_seconds, isLoading: false, renew, failed };
  }
  if (playback.isError) {
    return {
      src: fallback.url,
      source: fallback.url ? "preview" : null,
      maxSeconds: null,
      isLoading: fallback.isLoading,
      renew,
      failed: false,
    };
  }
  return { src: null, source: null, maxSeconds: null, isLoading: canAsk, renew, failed: false };
}
