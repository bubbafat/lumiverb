import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { DateGroup } from "./groupByDate";
import type { VirtualRowKind } from "./virtualRows";

/** A clip on screen and where its row's top sat, relative to the grid's scroll position. */
export interface AnchorClip {
  assetId: string;
  /** The row's top minus how far the grid is scrolled (≤ 0 for a row cut off at the top). */
  offset: number;
}

// Each row's top, from the top of the grid; worked out once per set of rows,
// not on every scroll event.
const startsByRows = new WeakMap<VirtualRowKind[], number[]>();

function rowStarts(rows: VirtualRowKind[]): number[] {
  let starts = startsByRows.get(rows);
  if (!starts) {
    starts = [];
    let y = 0;
    for (const row of rows) {
      starts.push(y);
      y += row.height;
    }
    startsByRows.set(rows, starts);
  }
  return starts;
}

/** The first row whose bottom is below `y`. */
function firstRowBelow(rows: VirtualRowKind[], starts: number[], y: number): number {
  let lo = 0;
  let hi = rows.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (starts[mid] + rows[mid].height <= y) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function rowClipIds(row: VirtualRowKind, groups: DateGroup[]): string[] {
  if (row.type !== "images") return [];
  const assets = groups[row.groupIndex]?.assets ?? [];
  return row.justifiedRow.items.flatMap((i) => (assets[i] ? [assets[i].asset_id] : []));
}

// Each clip's row, worked out once per set of rows.
const rowOfByRows = new WeakMap<VirtualRowKind[], Map<string, number>>();

function rowIndex(rows: VirtualRowKind[], groups: DateGroup[]): Map<string, number> {
  let rowOf = rowOfByRows.get(rows);
  if (!rowOf) {
    rowOf = new Map();
    for (let i = 0; i < rows.length; i++) {
      for (const id of rowClipIds(rows[i], groups)) rowOf.set(id, i);
    }
    rowOfByRows.set(rows, rowOf);
  }
  return rowOf;
}

/**
 * Of `ids`, the ones whose row is wholly above the screen when the grid is
 * scrolled `scrolled` px past its top. None when the grid's top is on screen.
 */
export function clipsAbove(
  rows: VirtualRowKind[],
  groups: DateGroup[],
  scrolled: number,
  ids: Iterable<string>,
): Set<string> {
  const above = new Set<string>();
  if (scrolled <= 0) return above;
  const starts = rowStarts(rows);
  const rowOf = rowIndex(rows, groups);
  for (const id of ids) {
    const i = rowOf.get(id);
    if (i !== undefined && starts[i] + rows[i].height <= scrolled) above.add(id);
  }
  return above;
}

/** Every clip in the grid. */
export function clipIds(groups: DateGroup[]): Set<string> {
  return new Set(groups.flatMap((g) => g.assets.map((a) => a.asset_id)));
}

/** "3 new clips", "1 new clip". */
export function newClipsLabel(count: number): string {
  return `${count} new ${count === 1 ? "clip" : "clips"}`;
}

/**
 * The clips on screen, first to last, with where each one's row sits, when
 * the grid is scrolled `scrolled` px past its top. None when the grid's top
 * is on screen: new clips at the top should show there.
 */
export function clipsOnScreen(
  rows: VirtualRowKind[],
  groups: DateGroup[],
  scrolled: number,
  viewportHeight: number,
): AnchorClip[] {
  if (scrolled <= 0) return [];
  const starts = rowStarts(rows);
  const clips: AnchorClip[] = [];
  for (let i = firstRowBelow(rows, starts, scrolled); i < rows.length; i++) {
    const top = starts[i];
    // At least one row of clips, even with no viewport height to go on.
    if (top >= scrolled + viewportHeight && clips.length > 0) break;
    for (const assetId of rowClipIds(rows[i], groups)) clips.push({ assetId, offset: top - scrolled });
  }
  return clips;
}

/**
 * How far to scroll the grid (from its top) so the first of `clips` that's
 * still in it sits where it did. Null when none of them is.
 */
export function anchoredScroll(
  rows: VirtualRowKind[],
  groups: DateGroup[],
  clips: AnchorClip[],
): number | null {
  if (clips.length === 0) return null;
  const starts = rowStarts(rows);
  const rowOf = rowIndex(rows, groups);
  for (const clip of clips) {
    const i = rowOf.get(clip.assetId);
    if (i !== undefined) return starts[i] - clip.offset;
  }
  return null;
}

export interface ScrollAnchor {
  /** Clips a refresh added above the screen that haven't been scrolled to yet. */
  newAbove: number;
  /** Scroll to the top, where they are. */
  showNewAbove: () => void;
}

/**
 * Keep what's on screen in a scrolled grid where it is when the grid's rows
 * change under it: a refresh that adds or removes clips above it, a zoom, a
 * new width. The first clip on screen keeps its place (or, if it's gone, the
 * next one on screen that's left). Browsers' own scroll anchoring can't do
 * this: the grid's rows are absolutely positioned, and Safari has none.
 *
 * Not when the grid's top is on screen (new clips show at the top), and not
 * across a change of `resetKey` (another query: its clips start where they
 * start).
 *
 * Clips that a change adds above the screen while it holds the grid in place
 * are counted in `newAbove`. A clip stops counting once its row is scrolled
 * onto the screen or it leaves the grid, and all of them do once the grid's
 * top is on screen.
 */
export function useScrollAnchor(
  scrollEl: HTMLElement | null,
  gridEl: HTMLElement | null,
  rows: VirtualRowKind[],
  groups: DateGroup[],
  resetKey: string,
): ScrollAnchor {
  const anchorRef = useRef<AnchorClip[]>([]);
  const layoutRef = useRef({ rows, groups });
  layoutRef.current = { rows, groups };
  const keyRef = useRef(resetKey);
  // The clips of the last rows laid out, to tell new ones by.
  const shownRef = useRef<Set<string> | null>(null);
  // New clips above the screen.
  const newRef = useRef(new Set<string>());
  const [newAbove, setNewAbove] = useState(0);

  // The grid's top, in the scroller's content.
  const gridTop = () => {
    if (!scrollEl || !gridEl) return 0;
    return gridEl.getBoundingClientRect().top - scrollEl.getBoundingClientRect().top + scrollEl.scrollTop;
  };
  const gridTopRef = useRef(gridTop);
  gridTopRef.current = gridTop;

  const captureRef = useRef(() => {});
  captureRef.current = () => {
    if (!scrollEl) return;
    const { rows: r, groups: g } = layoutRef.current;
    if (r.length === 0) return; // still loading: keep what was on screen
    const scrolled = scrollEl.scrollTop - gridTopRef.current();
    anchorRef.current = clipsOnScreen(r, g, scrolled, scrollEl.clientHeight);
    if (newRef.current.size > 0) newRef.current = clipsAbove(r, g, scrolled, newRef.current);
    setNewAbove(newRef.current.size);
  };

  useEffect(() => {
    if (!scrollEl) return;
    const onScroll = () => captureRef.current();
    scrollEl.addEventListener("scroll", onScroll, { passive: true });
    return () => scrollEl.removeEventListener("scroll", onScroll);
  }, [scrollEl]);

  // Before paint, so the grid never shows at the old scroll position.
  useLayoutEffect(() => {
    if (!scrollEl) return;
    if (keyRef.current !== resetKey) {
      keyRef.current = resetKey;
      anchorRef.current = [];
      shownRef.current = null;
      newRef.current = new Set();
    } else if (rows.length > 0) {
      const target = anchoredScroll(rows, groups, anchorRef.current);
      if (target !== null) {
        const top = gridTopRef.current() + target;
        if (Math.abs(top - scrollEl.scrollTop) >= 1) scrollEl.scrollTop = top;
        const shown = shownRef.current;
        if (shown) {
          const added = [...clipIds(groups)].filter((id) => !shown.has(id));
          for (const id of clipsAbove(rows, groups, target, added)) newRef.current.add(id);
        }
      }
    }
    if (rows.length > 0) shownRef.current = clipIds(groups);
    captureRef.current();
  }, [scrollEl, rows, groups, resetKey]);

  const showNewAbove = useCallback(() => {
    newRef.current = new Set();
    setNewAbove(0);
    scrollEl?.scrollTo({ top: 0, behavior: "smooth" });
  }, [scrollEl]);

  return { newAbove, showNewAbove };
}

