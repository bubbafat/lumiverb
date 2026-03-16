import { useEffect, useState } from "react";

type MediaType = "thumbnail" | "proxy" | "video-preview";

function mediaPath(assetId: string, type: MediaType): string {
  if (type === "thumbnail") return `/assets/${assetId}/thumbnail`;
  if (type === "proxy") return `/assets/${assetId}/proxy`;
  return `/assets/${assetId}/preview`;
}

const apiKey = import.meta.env.VITE_API_KEY as string | undefined;
const authHeaders = (): HeadersInit =>
  apiKey ? { Authorization: `Bearer ${apiKey}` } : {};

/**
 * Fetches an image or video preview with auth (src attributes can't send headers).
 * Returns an object URL for use in img/video src.
 * For video-preview: generating=true means the server returned 202 (not ready yet).
 * Pass enabled=false to defer fetching until ready (e.g. on hover).
 */
export function useAuthenticatedImage(
  assetId: string,
  type: MediaType = "thumbnail",
  { enabled = true }: { enabled?: boolean } = {},
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

    fetch(`/v1${path}`, { headers: authHeaders() })
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
  }, [assetId, type, enabled]);

  return { url, isLoading, error, generating };
}
