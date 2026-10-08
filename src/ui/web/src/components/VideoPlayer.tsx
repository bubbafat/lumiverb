import { useLayoutEffect, useRef, type MutableRefObject } from "react";

/**
 * The lightbox's video, framed by how much of it plays: amber while it's the
 * short preview, green once the whole video is ready. The note under it says
 * the same in words. When the whole video arrives mid-play, it carries on
 * from the same moment.
 */
export default function VideoPlayer({
  src,
  source,
  maxSeconds,
  videoRef,
}: {
  src: string;
  source: "analysis_proxy" | "preview" | null;
  maxSeconds: number | null;
  videoRef?: MutableRefObject<HTMLVideoElement | null>;
}) {
  const ownRef = useRef<HTMLVideoElement | null>(null);
  const ref = videoRef ?? ownRef;
  const last = useRef({ time: 0, playing: false });
  const resumeOn = useRef<string | null>(null);
  const shown = useRef(src);

  // Before the browser can load the new source: its metadata event must find this.
  useLayoutEffect(() => {
    if (shown.current !== src) {
      resumeOn.current = src;
      shown.current = src;
    }
  }, [src]);

  const full = source === "analysis_proxy";
  const previewSeconds = Math.min(10, maxSeconds ?? 10);
  const ring = full ? "ring-emerald-500/60" : "ring-amber-500/60";

  return (
    <div className="flex max-w-full flex-col items-center gap-2">
      <div
        data-testid="video-frame"
        data-playback={full ? "full" : "preview"}
        className={`overflow-hidden rounded-md ring-2 ${ring} transition-[box-shadow] duration-500`}
      >
        <video
          ref={ref}
          src={src}
          controls
          playsInline
          preload="metadata"
          className="block max-h-[calc(100vh-6rem)] max-w-full"
          onTimeUpdate={(e) => {
            // A new source starts at 0 and says so; keep where the old one was.
            if (resumeOn.current === null) last.current.time = e.currentTarget.currentTime;
          }}
          onPlay={() => {
            last.current.playing = true;
          }}
          onPause={() => {
            last.current.playing = false;
          }}
          onLoadedMetadata={(e) => {
            if (resumeOn.current !== src) return;
            resumeOn.current = null;
            const video = e.currentTarget;
            const duration = Number.isFinite(video.duration) ? video.duration : last.current.time;
            video.currentTime = Math.min(last.current.time, duration);
            if (last.current.playing) video.play().catch(() => {});
          }}
        />
      </div>
      {full ? (
        <p className="text-xs text-emerald-300/90">
          {maxSeconds != null ? `Plays the first ${maxSeconds} seconds` : "Full video"}
        </p>
      ) : (
        <p className="text-xs text-amber-300/90">
          Short preview: first {previewSeconds} seconds.
          {maxSeconds == null || maxSeconds > 10 ? " The full video plays once it's processed." : null}
        </p>
      )}
    </div>
  );
}
