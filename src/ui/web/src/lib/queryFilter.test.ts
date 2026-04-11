import { describe, expect, it } from "vitest";
import {
  filtersToParams,
  paramsToFilters,
  filterLabel,
  setFilter,
  getFilterValue,
  hasActiveFilters,
  clearFilters,
  buildSavedQuery,
  savedQueryLabels,
} from "./queryFilter";
import type { LeafFilter, FilterCapability } from "./queryFilter";

// ---------------------------------------------------------------------------
// URL round-trip
// ---------------------------------------------------------------------------

describe("filtersToParams / paramsToFilters round-trip", () => {
  it("round-trips a single filter", () => {
    const filters: LeafFilter[] = [{ type: "camera_make", value: "Canon" }];
    const params = filtersToParams(filters);
    const result = paramsToFilters(params);
    expect(result.filters).toEqual(filters);
    expect(result.sort).toBe("taken_at");
    expect(result.direction).toBe("desc");
  });

  it("round-trips multiple filters", () => {
    const filters: LeafFilter[] = [
      { type: "camera_make", value: "Sony" },
      { type: "media", value: "image" },
      { type: "iso", value: "100-800" },
      { type: "favorite", value: "yes" },
    ];
    const params = filtersToParams(filters, "taken_at", "desc");
    const result = paramsToFilters(params);
    expect(result.filters).toEqual(filters);
  });

  it("round-trips sort and direction", () => {
    const filters: LeafFilter[] = [{ type: "media", value: "video" }];
    const params = filtersToParams(filters, "file_size", "asc");
    const result = paramsToFilters(params);
    expect(result.sort).toBe("file_size");
    expect(result.direction).toBe("asc");
  });

  it("defaults sort to taken_at and direction to desc", () => {
    const result = paramsToFilters(new URLSearchParams());
    expect(result.sort).toBe("taken_at");
    expect(result.direction).toBe("desc");
    expect(result.filters).toEqual([]);
  });

  it("omits default sort/dir from URL params", () => {
    const params = filtersToParams([], "taken_at", "desc");
    expect(params.get("sort")).toBeNull();
    expect(params.get("dir")).toBeNull();
  });

  it("ignores malformed f params", () => {
    const params = new URLSearchParams();
    params.append("f", "camera_make:Canon");
    params.append("f", "badparam");   // no colon
    params.append("f", ":noprefix");  // empty prefix
    const result = paramsToFilters(params);
    expect(result.filters).toEqual([{ type: "camera_make", value: "Canon" }]);
  });

  it("handles values containing colons", () => {
    const filters: LeafFilter[] = [{ type: "query", value: "time:10:30" }];
    const params = filtersToParams(filters);
    const result = paramsToFilters(params);
    expect(result.filters[0].value).toBe("time:10:30");
  });
});

// ---------------------------------------------------------------------------
// Filter helpers
// ---------------------------------------------------------------------------

describe("setFilter", () => {
  it("adds a new filter", () => {
    const result = setFilter([], "camera_make", "Canon");
    expect(result).toEqual([{ type: "camera_make", value: "Canon" }]);
  });

  it("replaces an existing filter of the same type", () => {
    const filters: LeafFilter[] = [{ type: "camera_make", value: "Canon" }];
    const result = setFilter(filters, "camera_make", "Sony");
    expect(result).toEqual([{ type: "camera_make", value: "Sony" }]);
  });

  it("removes a filter when value is null", () => {
    const filters: LeafFilter[] = [
      { type: "camera_make", value: "Canon" },
      { type: "media", value: "image" },
    ];
    const result = setFilter(filters, "camera_make", null);
    expect(result).toEqual([{ type: "media", value: "image" }]);
  });

  it("removes a filter when value is empty string", () => {
    const filters: LeafFilter[] = [{ type: "camera_make", value: "Canon" }];
    const result = setFilter(filters, "camera_make", "");
    expect(result).toEqual([]);
  });
});

describe("getFilterValue", () => {
  it("returns the value for an existing filter", () => {
    const filters: LeafFilter[] = [{ type: "camera_make", value: "Canon" }];
    expect(getFilterValue(filters, "camera_make")).toBe("Canon");
  });

  it("returns undefined for a missing filter", () => {
    expect(getFilterValue([], "camera_make")).toBeUndefined();
  });
});

describe("hasActiveFilters", () => {
  it("returns false for empty filters", () => {
    expect(hasActiveFilters([])).toBe(false);
  });

  it("returns true when filters exist", () => {
    expect(hasActiveFilters([{ type: "media", value: "image" }])).toBe(true);
  });
});

describe("clearFilters", () => {
  it("returns empty array", () => {
    expect(clearFilters()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------

describe("filterLabel", () => {
  it("formats search query", () => {
    expect(filterLabel({ type: "query", value: "sunset" })).toBe('Search: "sunset"');
  });

  it("formats media type", () => {
    expect(filterLabel({ type: "media", value: "image" })).toBe("Photos");
    expect(filterLabel({ type: "media", value: "video" })).toBe("Videos");
  });

  it("formats favorite", () => {
    expect(filterLabel({ type: "favorite", value: "yes" })).toBe("Favorites");
    expect(filterLabel({ type: "favorite", value: "no" })).toBe("Not favorites");
  });

  it("formats star range", () => {
    expect(filterLabel({ type: "stars", value: "4+" })).toBe("4+ stars");
    expect(filterLabel({ type: "stars", value: "3-5" })).toBe("3–5 stars");
    expect(filterLabel({ type: "stars", value: "1" })).toBe("1 star");
  });

  it("formats ISO range", () => {
    expect(filterLabel({ type: "iso", value: "100-800" })).toBe("ISO 100–800");
    expect(filterLabel({ type: "iso", value: "400+" })).toBe("ISO 400+");
  });

  it("formats boolean filters", () => {
    expect(filterLabel({ type: "has_gps", value: "yes" })).toBe("Has GPS");
    expect(filterLabel({ type: "has_faces", value: "yes" })).toBe("Has faces");
  });

  it("formats color label", () => {
    expect(filterLabel({ type: "color", value: "red" })).toBe("Color: red");
  });

  it("uses capability label for unknown filter", () => {
    const caps: FilterCapability[] = [
      { prefix: "custom", label: "Custom Filter", value_kind: "string" },
    ];
    expect(filterLabel({ type: "custom", value: "test" }, caps)).toBe("Custom Filter: test");
  });

  it("falls back to type name for completely unknown filter", () => {
    expect(filterLabel({ type: "xyz", value: "val" })).toBe("xyz: val");
  });

  it("formats camera_make with fallback label", () => {
    expect(filterLabel({ type: "camera_make", value: "Canon" })).toBe("Camera Make: Canon");
  });
});

// ---------------------------------------------------------------------------
// Smart collection serialization
// ---------------------------------------------------------------------------

describe("buildSavedQuery", () => {
  it("builds a saved query from filters", () => {
    const filters: LeafFilter[] = [
      { type: "camera_make", value: "Canon" },
      { type: "favorite", value: "yes" },
    ];
    const sq = buildSavedQuery(filters, "taken_at", "desc");
    expect(sq.filters).toEqual(filters);
    expect(sq.sort).toBe("taken_at");
    expect(sq.direction).toBe("desc");
  });
});

describe("savedQueryLabels", () => {
  it("generates labels excluding library filter", () => {
    const labels = savedQueryLabels({
      filters: [
        { type: "camera_make", value: "Canon" },
        { type: "library", value: "lib-123" },
        { type: "favorite", value: "yes" },
      ],
    });
    expect(labels).toEqual(["Camera Make: Canon", "Favorites"]);
  });
});
