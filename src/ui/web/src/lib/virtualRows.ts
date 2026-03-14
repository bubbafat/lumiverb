import type { DateGroup } from "./groupByDate";
import type { JustifiedRow, JustifiedItem } from "./justifiedLayout";
import { computeJustifiedRows } from "./justifiedLayout";

export const HEADER_HEIGHT = 40;

export type VirtualRowKind =
  | { type: "header"; label: string; height: number }
  | {
      type: "images";
      groupIndex: number;
      rowIndex: number;
      justifiedRow: JustifiedRow;
      height: number;
    };

export function buildVirtualRows(
  groups: DateGroup[],
  containerWidth: number,
  targetRowHeight: number,
  rowGap = 4,
): VirtualRowKind[] {
  if (containerWidth <= 0 || !groups.length) return [];

  const virtualRows: VirtualRowKind[] = [];

  groups.forEach((group, groupIndex) => {
    virtualRows.push({ type: "header", label: group.label, height: HEADER_HEIGHT });

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
  const colWidth = (containerWidth - rowGap * (columns - 1)) / columns;

  groups.forEach((group, groupIndex) => {
    virtualRows.push({ type: "header", label: group.label, height: HEADER_HEIGHT });

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

