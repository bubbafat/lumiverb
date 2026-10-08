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
