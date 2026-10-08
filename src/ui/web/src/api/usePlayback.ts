import { useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { getPlayback } from "./client";
import { useAuthenticatedImage } from "./useAuthenticatedImage";

type Source = "analysis_proxy" | "preview";

/**
 * What a video's player streams: the signed playback link (full length,
 * seekable, within the account's cap), or the 10-second preview fetched as a
 * blob when no link is available.
 *
 * While only the preview exists it asks again every `pollMs`, so the player
 * switches to the whole video as soon as it's processed. Each answer carries
 * a fresh link; the first link for a source is kept, since a new src would
 * restart the video.
 */
export function usePlayback(
  assetId: string,
  {
    enabled,
    isPublic = false,
    publicLibraryId,
    pollMs = 30_000,
  }: { enabled: boolean; isPublic?: boolean; publicLibraryId?: string; pollMs?: number },
): {
  src: string | null;
  source: Source | null;
  maxSeconds: number | null;
  isLoading: boolean;
} {
  const canAsk = enabled && (!isPublic || !!publicLibraryId);
  const playback = useQuery({
    queryKey: ["playback", assetId, publicLibraryId ?? null],
    queryFn: () => getPlayback(assetId, isPublic ? publicLibraryId : undefined),
    enabled: canAsk,
    retry: false,
    // Links last six hours; ask again well before that.
    staleTime: 60 * 60_000,
    refetchInterval: (query) => (query.state.data?.source === "preview" ? pollMs : false),
  });
  const fallback = useAuthenticatedImage(assetId, "video-preview", {
    enabled: enabled && playback.isError,
    isPublic,
    publicLibraryId,
  });
  const kept = useRef<{ assetId: string; source: Source; url: string } | null>(null);

  if (!enabled) return { src: null, source: null, maxSeconds: null, isLoading: false };
  if (playback.data) {
    const { source, url, max_seconds } = playback.data;
    if (!kept.current || kept.current.assetId !== assetId || kept.current.source !== source) {
      kept.current = { assetId, source, url };
    }
    return { src: kept.current.url, source, maxSeconds: max_seconds, isLoading: false };
  }
  if (playback.isError) {
    return { src: fallback.url, source: fallback.url ? "preview" : null, maxSeconds: null, isLoading: fallback.isLoading };
  }
  return { src: null, source: null, maxSeconds: null, isLoading: canAsk };
}
