import { useEffect, useState } from "react";
import { authFetch } from "./client";

/**
 * Fetch a face's crop (`/v1/faces/{faceId}/crop`) with auth and return an
 * object URL. `url` stays null when the face has no crop. A 401 refreshes
 * the token and retries (authFetch).
 */
export function useFaceCrop(faceId: string): { url: string | null; isLoading: boolean } {
  const [url, setUrl] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(!!faceId);

  useEffect(() => {
    if (!faceId) {
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    let objectUrl: string | null = null;
    let cancelled = false;

    authFetch(`/faces/${faceId}/crop`)
      .then(async (res) => {
        if (cancelled || !res.ok) return; // no crop: stay null
        const blob = await res.blob();
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        // network error: no crop
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      setUrl(null);
    };
  }, [faceId]);

  return { url, isLoading };
}
