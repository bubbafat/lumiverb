import type { AssetPageItem } from "../api/types";
import type { DateGroup } from "./groupByDate";
import { buildFixedGridRows, buildVirtualRows, type VirtualRowKind } from "./virtualRows";

/** A search's best matches asked for with its first page (GET /v1/query best=N), and how many each More adds. */
export const BEST_FIRST = 24;
export const BEST_MORE = 60;
export const MORE_HEIGHT = 56;

/** How a page lays out clips: a phone's grid of so many columns, or justified rows of about this height. */
export type GridLayout =
  | { kind: "fixed"; columns: number; rowHeight: number }
  | { kind: "justified"; rowHeight: number };

export function layoutRows(groups: DateGroup[], width: number, layout: GridLayout, gap: number): VirtualRowKind[] {
  return layout.kind === "fixed"
    ? buildFixedGridRows(groups, width, layout.columns, layout.rowHeight, gap)
    : buildVirtualRows(groups, width, layout.rowHeight, gap);
}

/** A search page's groups and rows (as Google Photos shows a search): its best matches first, under their own
 * header with how many there are, one row of them (two on a phone) until opened, then More (a phone's last tile
 * saying +N instead); then everything found, by day. Without best matches, the days alone. */
export function searchRows(
  best: AssetPageItem[],
  total: number,
  open: boolean,
  days: DateGroup[],
  width: number,
  layout: GridLayout,
  gap: number,
): { groups: DateGroup[]; rows: VirtualRowKind[] } {
  const dayRows = layoutRows(days, width, layout, gap);
  if (!best.length || total <= 0 || width <= 0) return { groups: days, rows: dayRows };

  const bestGroup: DateGroup = { label: "Best matches", dateIso: null, assets: best };
  const [header, ...images] = layoutRows([bestGroup], width, layout, gap);
  const kept = open ? images : images.slice(0, layout.kind === "fixed" ? 2 : 1);
  const shown = kept.reduce((n, r) => n + (r.type === "images" ? r.justifiedRow.items.length : 0), 0);
  const hidden = Math.max(0, total - shown);
  const rows: VirtualRowKind[] = [header.type === "header" ? { ...header, count: total } : header];
  const last = kept[kept.length - 1];
  if (!open && hidden > 0 && layout.kind === "fixed" && last?.type === "images") {
    rows.push(...kept.slice(0, -1), { ...last, more: hidden + 1 }); // the last tile is one of the more
  } else {
    rows.push(...kept);
    if (hidden > 0) rows.push({ type: "more", groupIndex: 0, height: MORE_HEIGHT, hidden });
  }
  return {
    groups: [bestGroup, ...days],
    rows: [...rows, ...dayRows.map((r) => ({ ...r, groupIndex: r.groupIndex + 1 }))],
  };
}
