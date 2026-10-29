import type { DateGroup } from "./groupByDate";
import type { JustifiedRow, JustifiedItem } from "./justifiedLayout";
import { computeJustifiedRows } from "./justifiedLayout";

export const HEADER_HEIGHT = 40;

export type VirtualRowKind =
  | {
      type: "header";
      label: string;
      dateIso: string | null;
      height: number;
      groupIndex: number;
      /** How many it says it has, when not its clips shown (a search's best matches: all of them). */
      count?: number;
    }
  | {
      type: "images";
      groupIndex: number;
      rowIndex: number;
      justifiedRow: JustifiedRow;
      height: number;
      /** Its last tile says how many more there are (+N) and shows them (a search's best matches, on a phone). */
      more?: number;
    }
  /** A button showing more of the group above (a search's best matches): `hidden` not shown yet. */
  | { type: "more"; groupIndex: number; height: number; hidden: number };

export function buildVirtualRows(
  groups: DateGroup[],
  containerWidth: number,
  targetRowHeight: number,
  rowGap = 4,
): VirtualRowKind[] {
  if (containerWidth <= 0 || !groups.length) return [];

  const virtualRows: VirtualRowKind[] = [];

  groups.forEach((group, groupIndex) => {
    virtualRows.push({ type: "header", label: group.label, dateIso: group.dateIso, height: HEADER_HEIGHT, groupIndex });

    const items: JustifiedItem[] = group.assets.map((asset) => {
      const w = asset.width ?? 0;
      const h = asset.height ?? 0;
      const aspectRatio = w > 0 && h > 0 ? w / h : 4 / 3;

      return { aspectRatio };
    });

    const justifiedRows = computeJustifiedRows(
      items,
      containerWidth,
      targetRowHeight,
      undefined,
      undefined,
      rowGap,
    );

    justifiedRows.forEach((justifiedRow, rowIndex) => {
      virtualRows.push({
        type: "images",
        groupIndex,
        rowIndex,
        justifiedRow,
        height: justifiedRow.height + rowGap,
      });
    });
  });

  return virtualRows;
}

export function buildFixedGridRows(
  groups: DateGroup[],
  containerWidth: number,
  columns: number,
  rowHeight: number,
  rowGap = 4,
): VirtualRowKind[] {
  if (containerWidth <= 0 || !groups.length) return [];
  const virtualRows: VirtualRowKind[] = [];
  const colWidth = Math.floor((containerWidth - rowGap * (columns - 1)) / columns);

  groups.forEach((group, groupIndex) => {
    virtualRows.push({ type: "header", label: group.label, dateIso: group.dateIso, height: HEADER_HEIGHT, groupIndex });

    for (let i = 0; i < group.assets.length; i += columns) {
      const items = group.assets.slice(i, i + columns);
      const justifiedRow: JustifiedRow = {
        items: items.map((_, idx) => i + idx),
        widths: items.map(() => colWidth),
        height: rowHeight,
      };
      virtualRows.push({
        type: "images",
        groupIndex,
        rowIndex: Math.floor(i / columns),
        justifiedRow,
        height: rowHeight + rowGap,
      });
    }
  });

  return virtualRows;
}

