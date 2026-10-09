import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";

/** The least time between two refreshes while the revision keeps changing. */
export const REVISION_REFRESH_MIN_MS = 30_000;

type Revision = number | string;

// The revision each scope's page last refreshed to, per query client: it
// outlives the page, because the cache a page comes back to can hold a newer
// revision (another page polled it) next to a grid fetched before it.
const shownByClient = new WeakMap<QueryClient, Map<string, Revision>>();

function shownFor(client: QueryClient): Map<string, Revision> {
  let shown = shownByClient.get(client);
  if (!shown) {
    shown = new Map();
    shownByClient.set(client, shown);
  }
  return shown;
}

/**
 * Call `refresh` when `scope`'s revision moves past the one the page last
 * refreshed to: right away the first time, then at most once per
 * `minIntervalMs` while it keeps changing, with one last call once it stops,
 * so the page ends up current. A long ingest bumps a library's revision on
 * every poll; this keeps an open grid from reloading every page on every
 * poll.
 *
 * The first revision a scope ever sees is the starting point (the page has
 * just loaded), and `undefined` (not polled yet, or the poll failed) is
 * ignored. A page that's left and opened again compares with what it last
 * refreshed to, so a change it missed while away, or left pending, still
 * refreshes it. One page per scope.
 */
export function useRevisionRefresh(
  scope: string,
  revision: Revision | undefined,
  refresh: () => void,
  { minIntervalMs = REVISION_REFRESH_MIN_MS }: { minIntervalMs?: number } = {},
): void {
  const shown = shownFor(useQueryClient());

  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const latestRef = useRef(revision);
  latestRef.current = revision;

  const lastRefreshRef = useRef(Number.NEGATIVE_INFINITY);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Each scope starts its own interval; a refresh pending for the one left
  // is dropped (it stays unshown, so coming back refreshes).
  useEffect(() => {
    lastRefreshRef.current = Number.NEGATIVE_INFINITY;
    return () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      timerRef.current = null;
    };
  }, [scope]);

  useEffect(() => {
    if (revision === undefined) return;
    const last = shown.get(scope);
    if (last === undefined) {
      shown.set(scope, revision);
      return;
    }
    if (revision === last || timerRef.current !== null) return;

    const run = () => {
      timerRef.current = null;
      lastRefreshRef.current = Date.now();
      if (latestRef.current !== undefined) shown.set(scope, latestRef.current);
      refreshRef.current();
    };
    const wait = lastRefreshRef.current + minIntervalMs - Date.now();
    if (wait <= 0) run();
    else timerRef.current = setTimeout(run, wait);
  }, [shown, scope, revision, minIntervalMs]);
}
