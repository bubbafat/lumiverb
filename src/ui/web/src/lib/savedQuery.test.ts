import { describe, it, expect } from "vitest";
import type { LeafFilter, SavedQueryV2 } from "./queryFilter";
import { savedQueryLabels, buildSavedQuery } from "./queryFilter";

/** A search as saved (Save search) or sent to make a project from (from_search). */

describe("filter algebra serialization", () => {
  it("builds a saved query from filters", () => {
    const filters: LeafFilter[] = [
      { type: "camera_make", value: "Canon" },
      { type: "stars", value: "4+" },
      { type: "favorite", value: "yes" },
    ];
    const sq = buildSavedQuery(filters, "taken_at", "desc");
    expect(sq.filters).toEqual(filters);
    expect(sq.sort).toBe("taken_at");
    expect(sq.direction).toBe("desc");
  });

  it("round-trips through JSON", () => {
    const sq: SavedQueryV2 = {
      filters: [
        { type: "camera_make", value: "Canon" },
        { type: "stars", value: "4+" },
        { type: "color", value: "red" },
        { type: "has_gps", value: "yes" },
        { type: "library", value: "lib_abc" },
      ],
      sort: "taken_at",
      direction: "desc",
    };

    const json = JSON.stringify(sq);
    const parsed: SavedQueryV2 = JSON.parse(json);

    expect(parsed.filters).toEqual(sq.filters);
    expect(parsed.sort).toBe("taken_at");
  });

  it("handles empty filters", () => {
    const sq: SavedQueryV2 = { filters: [] };
    const json = JSON.stringify(sq);
    const parsed: SavedQueryV2 = JSON.parse(json);
    expect(parsed.filters).toEqual([]);
  });
});

describe("saved query display helpers", () => {
  it("savedQueryLabels produces human-readable labels excluding library", () => {
    const labels = savedQueryLabels({
      filters: [
        { type: "camera_make", value: "Canon" },
        { type: "stars", value: "3+" },
        { type: "favorite", value: "yes" },
        { type: "media", value: "image" },
        { type: "library", value: "lib_1" },
      ],
    });

    expect(labels).toContain("Camera Make: Canon");
    expect(labels).toContain("3+ stars");
    expect(labels).toContain("Favorites");
    expect(labels).toContain("Photos");
    // Library should be excluded
    expect(labels.some((l) => l.includes("lib_1"))).toBe(false);
  });

  it("returns empty array for empty filters", () => {
    expect(savedQueryLabels({ filters: [] })).toEqual([]);
  });

  it("includes search query label", () => {
    const labels = savedQueryLabels({
      filters: [
        { type: "query", value: "sunset" },
        { type: "color", value: "orange" },
      ],
    });
    expect(labels).toContain("Search: sunset");
    expect(labels).toContain("Color: orange");
  });
});
