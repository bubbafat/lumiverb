import { useEffect, useState } from "react";
import { authFetch, publicQuery } from "./client";

type MediaType = "thumbnail" | "proxy" | "video-preview";

function mediaPath(assetId: string, type: MediaType): string {
  if (type === "thumbnail") return `/assets/${assetId}/thumbnail`;
  if (type === "proxy") return `/assets/${assetId}/proxy`;
  return `/assets/${assetId}/preview`;
}

/**
 * Fetches an image or video preview with auth (src attributes can't send headers).
 * Returns an object URL for use in img/video src.
 * For video-preview: generating=true means the server returned 202 (not ready yet).
 * Pass enabled=false to defer fetching until ready (e.g. on hover).
 */
export function useAuthenticatedImage(
  assetId: string,
  type: MediaType = "thumbnail",
  {
    enabled = true,
    isPublic = false,
    publicLibraryId,
    publicProjectId,
  }: { enabled?: boolean; isPublic?: boolean; publicLibraryId?: string; publicProjectId?: string } = {},
): { url: string | null; isLoading: boolean; error: Error | null; generating: boolean } {
  const [url, setUrl] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(enabled);
  const [error, setError] = useState<Error | null>(null);
  const [generating, setGenerating] = useState(false);

  useEffect(() => {
    if (!assetId || !enabled) {
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    setError(null);
    setGenerating(false);
    const path = mediaPath(assetId, type);
    let objectUrl: string | null = null;
    let cancelled = false;

    // Public pages send no token, so there is nothing to refresh; signed-in
    // requests go through authFetch, which refreshes on a 401 and signs out
    // only when that fails.
    const request = isPublic
      ? fetch(`/v1${path}${publicQuery(publicLibraryId, publicProjectId)}`)
      : authFetch(path);

    request
      .then(async (res) => {
        if (cancelled) return;
        if (res.status === 202) {
          setGenerating(true);
          return;
        }
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const blob = await res.blob();
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err : new Error(String(err)));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      setUrl(null);
    };
  }, [assetId, type, enabled, isPublic, publicLibraryId, publicProjectId]);

  return { url, isLoading, error, generating };
}
