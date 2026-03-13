import { useEffect, useState } from "react";
import { apiFetchBlob } from "./client";

type ImageType = "thumbnail" | "proxy";

function imagePath(assetId: string, type: ImageType): string {
  return type === "thumbnail"
    ? `/assets/${assetId}/thumbnail`
    : `/assets/${assetId}/proxy`;
}

/**
 * Fetches an image with auth (img src can't send headers).
 * Returns an object URL for use in img src.
 */
export function useAuthenticatedImage(
  assetId: string,
  type: ImageType = "thumbnail",
): { url: string | null; isLoading: boolean; error: Error | null } {
  const [url, setUrl] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (!assetId) {
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    setError(null);
    const path = imagePath(assetId, type);
    let objectUrl: string | null = null;
    let cancelled = false;

    apiFetchBlob(path)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
        setError(null);
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
  }, [assetId, type]);

  return { url, isLoading, error };
}
