export function basename(relPath: string): string {
  const i = relPath.lastIndexOf("/");
  return i >= 0 ? relPath.slice(i + 1) : relPath;
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Format exposure time from microseconds to display string (e.g. 4000 → "1/250"). */
export function formatExposure(us: number | null): string | null {
  if (us == null || us <= 0) return null;
  const secs = us / 1_000_000;
  if (secs >= 1) return secs === Math.floor(secs) ? `${secs}s` : `${secs.toFixed(1)}s`;
  const denom = Math.round(1 / secs);
  return `1/${denom}`;
}

export function formatDate(iso: string | null): string {
  if (!iso) return "Unknown";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return "Unknown";
  }
}

/** "5 photos", "1 video", "3 of 10 items": what a list holds, by the media filter in force. */
export function mediaCount(n: number, media?: string | null, ofTotal?: number | null): string {
  const noun = media === "image" ? "photo" : media === "video" ? "video" : "item";
  const of = ofTotal != null ? ` of ${ofTotal.toLocaleString()}` : "";
  return `${n.toLocaleString()}${of} ${noun}${n === 1 && ofTotal == null ? "" : "s"}`;
}

/** When something in the trash is deleted for good: `days` after it went in.
 * Null when the trash is emptied by hand only (days null) or the time is unknown. */
export function deletedForGoodOn(trashedAt: string | null | undefined, days: number | null | undefined): Date | null {
  if (!trashedAt || days == null) return null;
  const t = new Date(trashedAt).getTime();
  return Number.isNaN(t) ? null : new Date(t + days * 86_400_000);
}

/** "Oct 31", or "Oct 31, 2027" outside this year. */
export function shortDate(d: Date): string {
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }) });
}

/** "40 seconds ago", "1 minute ago", "2 hours ago", "3 days ago": the server's `ago()` wording. */
export function timeAgo(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 1000));
  for (const [unit, size, least] of [["day", 86400, 2], ["hour", 3600, 1], ["minute", 60, 1]] as const) {
    if (seconds >= size * least) {
      const n = Math.round(seconds / size);
      return `${n} ${unit}${n === 1 ? "" : "s"} ago`;
    }
  }
  return `${seconds} second${seconds === 1 ? "" : "s"} ago`;
}
