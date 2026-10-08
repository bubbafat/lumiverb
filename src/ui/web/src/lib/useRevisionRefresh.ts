import { useEffect, useRef } from "react";

/** The least time between two refreshes while the revision keeps changing. */
export const REVISION_REFRESH_MIN_MS = 30_000;

/**
 * Call `refresh` when `revision` changes: right away the first time, then at
 * most once per `minIntervalMs` while it keeps changing, with one last call
 * once it stops, so the page ends up current. A long ingest bumps a library's
 * revision on every poll; this keeps an open grid from reloading every page
 * on every poll.
 *
 * The first revision seen is the starting point, not a change (the page has
 * just loaded). `undefined` (not polled yet, or the poll failed) is ignored.
 * A new `scope` (another library) starts over and drops a pending refresh.
 */
export function useRevisionRefresh(
  revision: number | string | undefined,
  refresh: () => void,
  {
    scope,
    minIntervalMs = REVISION_REFRESH_MIN_MS,
  }: { scope?: string; minIntervalMs?: number } = {},
): void {
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  const seenRef = useRef<number | string | undefined>(undefined);
  const lastRefreshRef = useRef(Number.NEGATIVE_INFINITY);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Start over for each scope; drop a pending refresh on the way out.
  useEffect(() => {
    seenRef.current = undefined;
    lastRefreshRef.current = Number.NEGATIVE_INFINITY;
    return () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      timerRef.current = null;
    };
  }, [scope]);

  useEffect(() => {
    if (revision === undefined) return;
    if (seenRef.current === undefined) {
      seenRef.current = revision;
      return;
    }
    if (revision === seenRef.current) return;
    seenRef.current = revision;

    const run = () => {
      timerRef.current = null;
      lastRefreshRef.current = Date.now();
      refreshRef.current();
    };
    if (timerRef.current !== null) return; // the pending refresh covers this change
    const wait = lastRefreshRef.current + minIntervalMs - Date.now();
    if (wait <= 0) run();
    else timerRef.current = setTimeout(run, wait);
  }, [revision, scope, minIntervalMs]);
}
