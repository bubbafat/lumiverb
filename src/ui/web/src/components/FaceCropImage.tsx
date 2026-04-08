import { useState, useEffect } from "react";
import { getApiKey } from "../api/client";

/**
 * Displays a face crop thumbnail from the server with auth.
 * Fetches `/v1/faces/{faceId}/crop` as a blob and renders as an `<img>`.
 */
export function FaceCropImage({
  faceId,
  size = 28,
  className = "",
}: {
  faceId: string;
  size?: number;
  className?: string;
}) {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!faceId) return;
    let objectUrl: string | null = null;
    let cancelled = false;

    const key = getApiKey();
    const headers: HeadersInit = key ? { Authorization: `Bearer ${key}` } : {};

    fetch(`/v1/faces/${faceId}/crop`, { headers })
      .then(async (res) => {
        if (cancelled || !res.ok) return;
        const blob = await res.blob();
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {});

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      setUrl(null);
    };
  }, [faceId]);

  if (!url) {
    return (
      <div
        className={`rounded-full bg-gray-700 flex items-center justify-center ${className}`}
        style={{ width: size, height: size }}
      >
        <svg
          className="text-gray-500"
          style={{ width: size * 0.6, height: size * 0.6 }}
          viewBox="0 0 24 24"
          fill="currentColor"
        >
          <path d="M12 12c2.7 0 4.8-2.1 4.8-4.8S14.7 2.4 12 2.4 7.2 4.5 7.2 7.2 9.3 12 12 12zm0 2.4c-3.2 0-9.6 1.6-9.6 4.8v2.4h19.2v-2.4c0-3.2-6.4-4.8-9.6-4.8z" />
        </svg>
      </div>
    );
  }

  return (
    <img
      src={url}
      alt=""
      className={`rounded-full object-cover ${className}`}
      style={{ width: size, height: size }}
    />
  );
}
