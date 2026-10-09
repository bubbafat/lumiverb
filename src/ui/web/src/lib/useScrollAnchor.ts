import { useEffect, useLayoutEffect, useRef } from "react";
import type { DateGroup } from "./groupByDate";
import type { VirtualRowKind } from "./virtualRows";

/** A clip on screen and where its row's top sat, relative to the grid's scroll position. */
export interface AnchorClip {
  assetId: string;
  /** The row's top minus how far the grid is scrolled (≤ 0 for a row cut off at the top). */
  offset: number;
}

/** Each row's top, from the top of the grid. */
function rowStarts(rows: VirtualRowKind[]): number[] {
  const starts: number[] = [];
  let y = 0;
  for (const row of rows) {
    starts.push(y);
    y += row.height;
  }
  return starts;
}

function rowClipIds(row: VirtualRowKind, groups: DateGroup[]): string[] {
  if (row.type !== "images") return [];
  const assets = groups[row.groupIndex]?.assets ?? [];
  return row.justifiedRow.items.flatMap((i) => (assets[i] ? [assets[i].asset_id] : []));
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
  for (let i = 0; i < rows.length; i++) {
    const top = starts[i];
    if (top + rows[i].height <= scrolled) continue;
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
  const rowOf = new Map<string, number>();
  rows.forEach((row, i) => {
    for (const id of rowClipIds(row, groups)) rowOf.set(id, i);
  });
  for (const clip of clips) {
    const i = rowOf.get(clip.assetId);
    if (i !== undefined) return starts[i] - clip.offset;
  }
  return null;
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
 */
export function useScrollAnchor(
  scrollEl: HTMLElement | null,
  gridEl: HTMLElement | null,
  rows: VirtualRowKind[],
  groups: DateGroup[],
  resetKey: string,
): void {
  const anchorRef = useRef<AnchorClip[]>([]);
  const layoutRef = useRef({ rows, groups });
  layoutRef.current = { rows, groups };
  const keyRef = useRef(resetKey);

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
    anchorRef.current = clipsOnScreen(r, g, scrollEl.scrollTop - gridTopRef.current(), scrollEl.clientHeight);
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
    } else if (rows.length > 0) {
      const target = anchoredScroll(rows, groups, anchorRef.current);
      if (target !== null) {
        const top = gridTopRef.current() + target;
        if (Math.abs(top - scrollEl.scrollTop) >= 1) scrollEl.scrollTop = top;
      }
    }
    captureRef.current();
  }, [scrollEl, rows, groups, resetKey]);
}
