/**
 * Where a public page's visitor came from (ADR-025): the site this page was opened from, as its host, sent with
 * the page's first request so its visit says so. The API's own requests come from the page itself, so only the
 * page knows. Nothing when it was opened from this site, typed in, or from a bookmark.
 */
export function cameFrom(referrer: string = document.referrer, here: string = window.location.host): string | null {
  if (!referrer) return null;
  try {
    const url = new URL(referrer);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return url.host === here ? null : url.hostname;
  } catch {
    return null;
  }
}

/** The query string that says it ("?from=facebook.com"), or "". */
export function cameFromQuery(): string {
  const host = cameFrom();
  return host ? `?${new URLSearchParams({ from: host })}` : "";
}
