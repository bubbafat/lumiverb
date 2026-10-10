import { useEffect, useState } from "react";
import { authFetch, publicQuery } from "./client";

type MediaType = "thumbnail" | "proxy" | "video-preview";

/** The one route for each picture: /assets/{id}/artifacts/{type}. */
function mediaPath(assetId: string, type: MediaType): string {
  return `/assets/${assetId}/artifacts/${type === "video-preview" ? "video_preview" : type}`;
}

/** A 503 that says when to ask again (a capped preview being made); null otherwise. */
function retryAfterMs(res: Response): number | null {
  if (res.status !== 503) return null;
  const seconds = Number(res.headers.get("Retry-After"));
  return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : null;
}

/** How many times a preview being made is asked for again before giving up. */
const PREPARING_TRIES = 15;

/**
 * Fetches an image or video preview with auth (src attributes can't send headers).
 * Returns an object URL for use in img/video src.
 * generating=true while the server makes a capped preview (503 with
 * Retry-After); it's asked for again after the time it says.
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
    let retry: ReturnType<typeof setTimeout> | undefined;

    // Public pages send no token, so there is nothing to refresh; signed-in
    // requests go through authFetch, which refreshes on a 401 and signs out
    // only when that fails.
    const ask = (): Promise<Response> =>
      isPublic ? fetch(`/v1${path}${publicQuery(publicLibraryId, publicProjectId)}`) : authFetch(path);
    const askUntilReady = async (tries: number): Promise<Response> => {
      const res = await ask();
      const wait = retryAfterMs(res);
      if (wait === null || tries <= 1 || cancelled) return res;
      setGenerating(true);
      await new Promise((resolve) => { retry = setTimeout(resolve, wait); });
      return askUntilReady(tries - 1);
    };

    askUntilReady(PREPARING_TRIES)
      .then(async (res) => {
        if (cancelled) return;
        setGenerating(false);
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
      if (retry) clearTimeout(retry);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      setUrl(null);
    };
  }, [assetId, type, enabled, isPublic, publicLibraryId, publicProjectId]);

  return { url, isLoading, error, generating };
}
