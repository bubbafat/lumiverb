import { useState } from "react";
import { usePlayback } from "../api/usePlayback";

/**
 * A Live Photo's moving part, over its photo: LIVE plays the few seconds
 * around it once, with its sound where the browser allows (else without),
 * and the still is back when it ends. It's the photo's own playback link
 * (the server plays its motion: GET /v1/assets/{id}/playback), so a public
 * page's visitor plays it as they see the photo.
 */
export function LiveMotion({
  assetId,
  isPublic = false,
  publicLibraryId,
  publicProjectId,
}: {
  assetId: string;
  isPublic?: boolean;
  publicLibraryId?: string;
  publicProjectId?: string;
}) {
  const [playing, setPlaying] = useState(false);
  const playback = usePlayback(assetId, { enabled: true, isPublic, publicLibraryId, publicProjectId });

  return (
    <>
      {playing && playback.src && (
        <video
          src={playback.src}
          autoPlay
          playsInline
          aria-label="Live Photo"
          className="absolute inset-0 h-full w-full bg-black object-contain"
          onCanPlay={(e) => {
            const v = e.currentTarget;
            v.play().catch(() => {
              v.muted = true; // sound needs the browser's say: the picture plays anyway
              v.play().catch(() => setPlaying(false));
            });
          }}
          onEnded={() => setPlaying(false)}
          onError={() => setPlaying(false)}
        />
      )}
      <button
        type="button"
        aria-pressed={playing}
        disabled={!playback.src}
        onClick={() => setPlaying((p) => !p)}
        title={playing ? "Show the photo" : "Play the moments around it"}
        className="absolute left-2 top-2 rounded-full bg-black/60 px-2.5 py-1 text-xs font-semibold tracking-wider text-white hover:bg-black/80 disabled:opacity-50"
      >
        LIVE
      </button>
    </>
  );
}
