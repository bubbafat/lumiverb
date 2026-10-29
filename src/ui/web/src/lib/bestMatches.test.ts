import { describe, expect, it } from "vitest";
import { layoutRows, MORE_HEIGHT, searchRows, type GridLayout } from "./bestMatches";
import type { AssetPageItem } from "../api/types";
import type { DateGroup } from "./groupByDate";

function clip(id: string): AssetPageItem {
  return {
    asset_id: id, rel_path: `${id}.jpg`, file_size: 1, file_mtime: null, content_hash: null, media_type: "image",
    width: 4, height: 3, taken_at: null, status: "indexed", duration_sec: null, camera_make: null, camera_model: null,
    iso: null, aperture: null, focal_length: null, focal_length_35mm: null, lens_model: null, flash_fired: null,
    gps_lat: null, gps_lon: null, created_at: null,
  };
}

const best = Array.from({ length: 24 }, (_, i) => clip(`b${i}`));
const days: DateGroup[] = [{ label: "Monday", dateIso: "2026-10-26", assets: [clip("d1"), clip("d2")] }];
const phone: GridLayout = { kind: "fixed", columns: 4, rowHeight: 90 };
const desktop: GridLayout = { kind: "justified", rowHeight: 200 };

describe("a search page's rows", () => {
  it("are the days alone without best matches", () => {
    expect(searchRows([], 0, false, days, 1200, desktop, 4)).toEqual({
      groups: days, rows: layoutRows(days, 1200, desktop, 4) });
  });

  it("on a desktop: the best's header with how many, one row of them, More, then the days", () => {
    const { groups, rows } = searchRows(best, 30, false, days, 1200, desktop, 4);
    expect(groups.map((g) => g.label)).toEqual(["Best matches", "Monday"]);
    expect(rows[0]).toMatchObject({ type: "header", label: "Best matches", count: 30, groupIndex: 0 });
    expect(rows[1]).toMatchObject({ type: "images", groupIndex: 0 });
    const shown = rows[1].type === "images" ? rows[1].justifiedRow.items.length : 0;
    expect(rows[2]).toEqual({ type: "more", groupIndex: 0, height: MORE_HEIGHT, hidden: 30 - shown });
    expect(rows[3]).toMatchObject({ type: "header", label: "Monday", groupIndex: 1 });
  });

  it("on a phone: two rows, the last tile saying how many more instead of More", () => {
    const { rows } = searchRows(best, 30, false, days, 360, phone, 4);
    const images = rows.filter((r) => r.type === "images" && r.groupIndex === 0);
    expect(images).toHaveLength(2);
    expect(images[1]).toMatchObject({ more: 30 - 8 + 1 });
    expect(rows.some((r) => r.type === "more")).toBe(false);
  });

  it("opened: every row of them, and More while there are more than shown", () => {
    const open = searchRows(best, 30, true, days, 360, phone, 4).rows;
    expect(open.filter((r) => r.type === "images" && r.groupIndex === 0)).toHaveLength(6);
    expect(open.find((r) => r.type === "more")).toMatchObject({ hidden: 6 });
    const all = searchRows(best.slice(0, 20), 20, true, days, 360, phone, 4).rows;
    expect(all.some((r) => r.type === "more")).toBe(false);
  });

  it("with no more than fit, says nothing of more", () => {
    const { rows } = searchRows(best.slice(0, 3), 3, false, days, 360, phone, 4);
    expect(rows.some((r) => r.type === "more" || (r.type === "images" && r.more))).toBe(false);
  });
});
