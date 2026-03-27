export function basename(relPath: string): string {
  const i = relPath.lastIndexOf("/");
  return i >= 0 ? relPath.slice(i + 1) : relPath;
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Format shutter speed: convert decimal like "0.004" to "1/250". */
export function formatShutter(val: string | null): string | null {
  if (!val) return null;
  // Already fractional
  if (val.includes("/")) return val;
  const t = parseFloat(val);
  if (isNaN(t) || t <= 0) return val;
  if (t >= 1) return t === Math.floor(t) ? `${t}s` : `${t.toFixed(1)}s`;
  return `1/${Math.round(1 / t)}`;
}

export function formatDate(iso: string | null): string {
  if (!iso) return "Unknown";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return "Unknown";
  }
}
