import { describe, expect, it } from "vitest";
import type { AssetPageItem } from "../api/types";
import type { DateGroup } from "./groupByDate";
import { anchoredScroll, clipsOnScreen } from "./useScrollAnchor";
import { buildFixedGridRows } from "./virtualRows";

// Two clips a row, rows 100 px plus a 4 px gap, a 40 px header per day.
function grid(days: string[][]) {
  const groups: DateGroup[] = days.map((ids, i) => ({
    label: `Day ${i}`,
    dateIso: `2026-04-0${i + 1}`,
    assets: ids.map((asset_id) => ({ asset_id }) as AssetPageItem),
  }));
  return { groups, rows: buildFixedGridRows(groups, 204, 2, 100, 4) };
}

describe("clipsOnScreen", () => {
  const { rows, groups } = grid([["a", "b", "c", "d"], ["e", "f"]]);
  // header 0–40, [a b] 40–144, [c d] 144–248, header 248–288, [e f] 288–392

  it("is nothing while the grid's top is on screen", () => {
    expect(clipsOnScreen(rows, groups, 0, 300)).toEqual([]);
  });

  it("starts with the row cut off at the top, and stops below the viewport", () => {
    expect(clipsOnScreen(rows, groups, 100, 100)).toEqual([
      { assetId: "a", offset: -60 },
      { assetId: "b", offset: -60 },
      { assetId: "c", offset: 44 },
      { assetId: "d", offset: 44 },
    ]);
  });

  it("skips a date header at the top for the clips under it", () => {
    expect(clipsOnScreen(rows, groups, 250, 0)).toEqual([
      { assetId: "e", offset: 38 },
      { assetId: "f", offset: 38 },
    ]);
  });
});

describe("anchoredScroll", () => {
  it("scrolls by what was added above the first clip on screen", () => {
    const before = grid([["c", "d"]]);
    const on = clipsOnScreen(before.rows, before.groups, 60, 100); // [c d] at 40, so offset -20
    const after = grid([["a", "b", "c", "d"]]);
    expect(anchoredScroll(after.rows, after.groups, on)).toBe(144 + 20);
  });

  it("falls back to the next clip on screen when the first one went away", () => {
    const before = grid([["a", "b", "c", "d"]]);
    const on = clipsOnScreen(before.rows, before.groups, 50, 200);
    const after = grid([["x", "c", "d"]]); // a and b gone: c now shares a row with x
    expect(anchoredScroll(after.rows, after.groups, on)).toBe(40 + (50 - 144));
  });

  it("is null when nothing that was on screen is left", () => {
    const before = grid([["a", "b"]]);
    const on = clipsOnScreen(before.rows, before.groups, 50, 100);
    const after = grid([["x", "y"]]);
    expect(anchoredScroll(after.rows, after.groups, on)).toBeNull();
  });
});
