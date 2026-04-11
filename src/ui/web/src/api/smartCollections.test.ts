import { describe, it, expect } from "vitest";
import type { CollectionItem } from "./types";
import type { LeafFilter, SavedQueryV2 } from "../lib/queryFilter";
import { savedQueryLabels, buildSavedQuery } from "../lib/queryFilter";

/**
 * Tests for smart collection type fields and saved query serialization.
 * Updated for the filter algebra format.
 */

describe("CollectionItem type", () => {
  it("includes type field defaulting to static", () => {
    const item: CollectionItem = {
      collection_id: "col_123",
      name: "Test",
      description: null,
      cover_asset_id: null,
      owner_user_id: "user_1",
      visibility: "private",
      ownership: "own",
      sort_order: "manual",
      type: "static",
      saved_query: null,
      asset_count: 0,
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
    };
    expect(item.type).toBe("static");
    expect(item.saved_query).toBeNull();
  });

  it("supports smart type with saved_query using filter algebra", () => {
    const item: CollectionItem = {
      collection_id: "col_456",
      name: "Canon Favorites",
      description: null,
      cover_asset_id: null,
      owner_user_id: "user_1",
      visibility: "private",
      ownership: "own",
      sort_order: "manual",
      type: "smart",
      saved_query: {
        filters: [
          { type: "camera_make", value: "Canon" },
          { type: "favorite", value: "yes" },
          { type: "stars", value: "3+" },
          { type: "library", value: "lib_1" },
        ],
        sort: "taken_at",
        direction: "desc",
      },
      asset_count: 42,
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
    };
    expect(item.type).toBe("smart");
    expect(item.saved_query).not.toBeNull();
    const cameraMake = item.saved_query!.filters.find((f) => f.type === "camera_make");
    expect(cameraMake?.value).toBe("Canon");
  });

  it("supports smart type with search query filter", () => {
    const item: CollectionItem = {
      collection_id: "col_789",
      name: "Sunset Search",
      description: null,
      cover_asset_id: null,
      owner_user_id: "user_1",
      visibility: "shared",
      ownership: "own",
      sort_order: "manual",
      type: "smart",
      saved_query: {
        filters: [
          { type: "query", value: "sunset" },
          { type: "color", value: "orange" },
          { type: "media", value: "image" },
        ],
      },
      asset_count: 15,
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
    };
    const queryFilter = item.saved_query!.filters.find((f) => f.type === "query");
    expect(queryFilter?.value).toBe("sunset");
  });
});

describe("PageAssetsOptions type", () => {
  it("includes hasColor field", () => {
    const opts: import("./client").PageAssetsOptions = {
      hasColor: true,
    };
    expect(opts.hasColor).toBe(true);
  });

  it("includes dateFrom and dateTo fields", () => {
    const opts: import("./client").PageAssetsOptions = {
      dateFrom: "2024-01-01",
      dateTo: "2024-12-31",
    };
    expect(opts.dateFrom).toBe("2024-01-01");
    expect(opts.dateTo).toBe("2024-12-31");
  });
});

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
    expect(labels).toContain('Search: "sunset"');
    expect(labels).toContain("Color: orange");
  });
});
