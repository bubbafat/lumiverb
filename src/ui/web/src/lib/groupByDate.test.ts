import { describe, expect, it } from "vitest";
import type { AssetPageItem } from "../api/types";
import { groupAssetsByDate } from "./groupByDate";

function clip(id: string, dates: Partial<Pick<AssetPageItem, "taken_at" | "created_at" | "changed_at">>): AssetPageItem {
  return {
    asset_id: id, rel_path: `${id}.jpg`, file_size: 1, file_mtime: null, content_hash: null, media_type: "image",
    width: null, height: null, taken_at: null, status: "ready", duration_sec: null, camera_make: null,
    camera_model: null, iso: null, aperture: null, focal_length: null, focal_length_35mm: null, lens_model: null,
    flash_fired: null, gps_lat: null, gps_lon: null, created_at: null, changed_at: null, ...dates,
  };
}

const ids = (groups: ReturnType<typeof groupAssetsByDate>) => groups.map((g) => g.assets.map((a) => a.asset_id));

describe("groupAssetsByDate", () => {
  it("groups a browse by the date it's sorted by, in the order the server sent", () => {
    // Last modified, newest first: their taken dates say nothing about it.
    const sorted = [
      clip("a", { changed_at: "2026-10-20T21:00:00Z", taken_at: "2019-05-01T10:00:00Z" }),
      clip("b", { changed_at: "2026-10-20T08:00:00Z", taken_at: "2024-01-01T10:00:00Z" }),
      clip("c", { changed_at: "2026-10-18T12:00:00Z", taken_at: "2024-01-01T11:00:00Z" }),
    ];
    const groups = groupAssetsByDate(sorted, "changed_at");
    expect(ids(groups)).toEqual([["a", "b"], ["c"]]);
    expect(groups.map((g) => g.dateIso)).toEqual([null, null]); // only a taken date's header filters by its day
  });

  it("keeps an ascending sort ascending", () => {
    const oldestFirst = [
      clip("old", { taken_at: "2020-01-01T12:00:00Z" }),
      clip("new", { taken_at: "2026-01-01T12:00:00Z" }),
    ];
    const groups = groupAssetsByDate(oldestFirst, "taken_at");
    expect(ids(groups)).toEqual([["old"], ["new"]]);
    expect(groups[0].dateIso).toBe("2020-01-01");
  });

  it("keeps a sort that isn't by a date as one group, in its order", () => {
    const bySize = [clip("big", { taken_at: "2020-01-01T12:00:00Z" }), clip("small", { taken_at: "2026-01-01T12:00:00Z" })];
    expect(groupAssetsByDate(bySize, "file_size")).toEqual([{ label: "", dateIso: null, assets: bySize }]);
  });

  it("groups clips in their own order (a project's) by taken date, newest day first, an unknown date last", () => {
    const projectOrder = [
      clip("undated", {}),
      clip("old", { taken_at: "2020-01-01T12:00:00Z" }),
      clip("new", { taken_at: "2026-01-01T12:00:00Z" }),
    ];
    const groups = groupAssetsByDate(projectOrder);
    expect(ids(groups)).toEqual([["new"], ["old"], ["undated"]]);
    expect(groups[2].label).toBe("Unknown date");
  });
});
