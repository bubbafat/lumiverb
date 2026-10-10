import { useLayoutEffect, useRef, useState, type MutableRefObject } from "react";

/**
 * The lightbox's video, framed by how much of it plays: amber while it's the
 * short preview, green once the whole video is ready. The note under it says
 * the same in words. When the source changes mid-play (the whole video
 * arrives, or a failed link is renewed), it carries on from the same moment.
 *
 * iOS loads nothing until a tap, so the clip's still shows first; with no
 * still, a #t fragment makes it paint the first frame.
 */
export default function VideoPlayer({
  src: link,
  poster,
  source,
  maxSeconds,
  videoRef,
  onRenew,
  failed: renewFailed = false,
}: {
  src: string;
  /** The clip's still, shown before it plays. */
  poster?: string;
  source: "analysis_proxy" | "preview" | null;
  maxSeconds: number | null;
  videoRef?: MutableRefObject<HTMLVideoElement | null>;
  /** Ask for a fresh link: this one failed (expired, say). */
  onRenew?: () => void;
  /** No fresh link could be had. */
  failed?: boolean;
}) {
  const src = poster || link.includes("#") ? link : `${link}#t=0.001`;
  const ownRef = useRef<HTMLVideoElement | null>(null);
  const ref = videoRef ?? ownRef;
  const last = useRef({ time: 0, playing: false });
  const resumeOn = useRef<string | null>(null);
  const shown = useRef(src);
  // A fresh link was asked for and hasn't loaded yet.
  const renewing = useRef(false);
  const [failed, setFailed] = useState(false);
  const [duration, setDuration] = useState<number | null>(null);

  // Before the browser can load the new source: its metadata event must find this.
  useLayoutEffect(() => {
    if (shown.current !== src) {
      resumeOn.current = src;
      shown.current = src;
    }
  }, [src]);

  const full = source === "analysis_proxy";
  const previewSeconds = Math.min(10, maxSeconds ?? 10, duration ? Math.round(duration) : 10);
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
          poster={poster}
          controls
          playsInline
          preload="metadata"
          className="block max-h-[calc(100svh-8rem)] max-w-full lg:max-h-[calc(100svh-6rem)]"
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
          onError={() => {
            // One fresh link per failure; if that fails too, say so.
            if (onRenew && !renewing.current) {
              renewing.current = true;
              onRenew();
            } else {
              setFailed(true);
            }
          }}
          onLoadedMetadata={(e) => {
            const video = e.currentTarget;
            renewing.current = false;
            setFailed(false);
            setDuration(Number.isFinite(video.duration) ? video.duration : null);
            if (resumeOn.current !== src) return;
            resumeOn.current = null;
            const length = Number.isFinite(video.duration) ? video.duration : last.current.time;
            video.currentTime = Math.min(last.current.time, length);
            if (last.current.playing) video.play().catch(() => {});
          }}
        />
      </div>
      {failed || renewFailed ? (
        <p className="text-xs text-red-300">This video can't play right now. Try again later.</p>
      ) : full ? (
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
