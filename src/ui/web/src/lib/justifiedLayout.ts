export interface JustifiedItem {
  aspectRatio: number; // width / height, must be > 0
}

export interface JustifiedRow {
  items: number[]; // indices into the input array
  height: number; // computed row height in px
  widths: number[]; // computed width for each item in px
}

export function computeJustifiedRows(
  items: JustifiedItem[],
  containerWidth: number,
  targetRowHeight: number,
  minAspectRatio = 0.5,
  maxAspectRatio = 3.0,
  gap = 4,
): JustifiedRow[] {
  if (containerWidth <= 0 || items.length === 0) return [];

  const clamped: number[] = items.map((item) => {
    const ar = item.aspectRatio > 0 ? item.aspectRatio : 4 / 3;
    return Math.min(Math.max(ar, minAspectRatio), maxAspectRatio);
  });

  const rows: JustifiedRow[] = [];
  let currentItems: number[] = [];
  let currentAspectSum = 0;

  const pushRow = (indices: number[], aspectSum: number, isLastRow: boolean) => {
    if (indices.length === 0) return;

    if (isLastRow) {
      const widths = indices.map((idx) => clamped[idx] * targetRowHeight);
      rows.push({
        items: [...indices],
        height: targetRowHeight,
        widths,
      });
      return;
    }

    const totalGap = gap * Math.max(indices.length - 1, 0);
    const availableWidth = Math.max(containerWidth - totalGap, 1);
    const rowHeight = availableWidth / aspectSum;
    const widths = indices.map((idx) => clamped[idx] * rowHeight);

    rows.push({
      items: [...indices],
      height: rowHeight,
      widths,
    });
  };

  for (let i = 0; i < clamped.length; i++) {
    const ar = clamped[i];
    const nextItems = [...currentItems, i];
    const gaps = gap * Math.max(nextItems.length - 1, 0);
    const naturalWidthAtTarget =
      nextItems.reduce((sum, idx) => sum + clamped[idx] * targetRowHeight, 0) +
      gaps;

    if (naturalWidthAtTarget > containerWidth && currentItems.length > 0) {
      // Close current row (not last, it will be stretched)
      pushRow(currentItems, currentAspectSum, false);
      currentItems = [i];
      currentAspectSum = ar;
    } else {
      currentItems = nextItems;
      currentAspectSum += ar;
    }
  }

  // Last row: do not stretch, left-aligned at target height
  if (currentItems.length > 0) {
    pushRow(currentItems, currentAspectSum, true);
  }

  return rows;
}

