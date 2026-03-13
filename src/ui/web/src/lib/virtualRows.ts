import type { DateGroup } from "./groupByDate";
import type { JustifiedRow, JustifiedItem } from "./justifiedLayout";
import { computeJustifiedRows } from "./justifiedLayout";

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
    const headerHeight = 40;
    virtualRows.push({ type: "header", label: group.label, height: headerHeight });

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

