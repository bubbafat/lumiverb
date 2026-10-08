import { useQuery } from "@tanstack/react-query";
import { getPlayback } from "./client";
import { useAuthenticatedImage } from "./useAuthenticatedImage";

/**
 * What a video's player streams: the signed playback link (full length,
 * seekable, within the account's cap), or the 10-second preview fetched as a
 * blob when no link is available.
 */
export function usePlayback(
  assetId: string,
  {
    enabled,
    isPublic = false,
    publicLibraryId,
  }: { enabled: boolean; isPublic?: boolean; publicLibraryId?: string },
): {
  src: string | null;
  source: "analysis_proxy" | "preview" | null;
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
  });
  const fallback = useAuthenticatedImage(assetId, "video-preview", {
    enabled: enabled && playback.isError,
    isPublic,
    publicLibraryId,
  });

  if (!enabled) return { src: null, source: null, maxSeconds: null, isLoading: false };
  if (playback.data) {
    return {
      src: playback.data.url,
      source: playback.data.source,
      maxSeconds: playback.data.max_seconds,
      isLoading: false,
    };
  }
  if (playback.isError) {
    return { src: fallback.url, source: fallback.url ? "preview" : null, maxSeconds: null, isLoading: fallback.isLoading };
  }
  return { src: null, source: null, maxSeconds: null, isLoading: canAsk };
}
