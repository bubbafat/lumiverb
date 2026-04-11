import { describe, it, expect } from "vitest";
import type { CollectionItem } from "./types";
import { toSnakeCaseFilters, formatSavedQuery } from "../components/SaveSmartCollectionModal";

/**
 * Tests for smart collection type fields and saved query serialization.
 * These tests verify the type contracts that the UI depends on.
 * Tests are written first — they will fail until types.ts and client.ts are updated.
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

  it("supports smart type with saved_query", () => {
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
        filters: {
          camera_make: "Canon",
          favorite: true,
          star_min: 3,
        },
        library_id: "lib_1",
      },
      asset_count: 42,
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
    };
    expect(item.type).toBe("smart");
    expect(item.saved_query).not.toBeNull();
    expect(item.saved_query!.filters.camera_make).toBe("Canon");
    expect(item.saved_query!.library_id).toBe("lib_1");
  });

  it("supports smart type with search query text", () => {
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
        q: "sunset",
        filters: {
          color: "orange",
          media_types: ["image"],
        },
      },
      asset_count: 15,
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
    };
    expect(item.saved_query!.q).toBe("sunset");
  });
});

describe("PageAssetsOptions type", () => {
  it("includes hasColor field", () => {
    // This import will fail if the field doesn't exist on the type
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

  it("all new filter fields coexist with existing ones", () => {
    const opts: import("./client").PageAssetsOptions = {
      cameraMake: "Canon",
      favorite: true,
      starMin: 3,
      color: "red",
      hasRating: true,
      hasColor: true,
      dateFrom: "2024-06-01",
      dateTo: "2024-06-30",
      hasFaces: true,
    };
    expect(opts.cameraMake).toBe("Canon");
    expect(opts.hasColor).toBe(true);
    expect(opts.dateFrom).toBe("2024-06-01");
  });
});

describe("camelCase to snake_case conversion", () => {
  it("converts PageAssetsOptions keys to server format", () => {
    // This is the exact shape browseOpts has in the browse pages
    const browseOpts = {
      cameraMake: "Canon",
      starMin: 3,
      favorite: true,
      hasGps: true,
      mediaType: "image",
      sort: "taken_at",
      dir: "desc",
    };

    // uses top-level import
    const snaked = toSnakeCaseFilters(browseOpts);

    // Server expects snake_case keys
    expect(snaked["camera_make"]).toBe("Canon");
    expect(snaked["star_min"]).toBe(3);
    expect(snaked["favorite"]).toBe(true);
    expect(snaked["has_gps"]).toBe(true);
    expect(snaked["media_type"]).toBe("image");

    // camelCase keys must NOT be present
    expect(snaked["cameraMake"]).toBeUndefined();
    expect(snaked["starMin"]).toBeUndefined();
    expect(snaked["hasGps"]).toBeUndefined();
    expect(snaked["mediaType"]).toBeUndefined();
  });

  it("drops null/undefined values", () => {
    const snaked = toSnakeCaseFilters({
      cameraMake: "Canon",
      cameraModel: null,
      isoMin: undefined,
    });

    expect(snaked["camera_make"]).toBe("Canon");
    expect("camera_model" in snaked).toBe(false);
    expect("iso_min" in snaked).toBe(false);
  });

  it("drops false boolean defaults", () => {
    const snaked = toSnakeCaseFilters({
      cameraMake: "Canon",
      hasGps: false,    // page default, not a user selection
      hasFaces: false,  // page default, not a user selection
      favorite: true,   // this IS a user selection
    });

    expect(snaked["camera_make"]).toBe("Canon");
    expect(snaked["favorite"]).toBe(true);
    expect("has_gps" in snaked).toBe(false);
    expect("has_faces" in snaked).toBe(false);
  });

  it("strips sort/dir defaults and library_id from filters", () => {
    const snaked = toSnakeCaseFilters({
      sort: "taken_at",
      dir: "desc",
      libraryId: "lib_1",
      cameraMake: "Canon",
    });

    expect(snaked["camera_make"]).toBe("Canon");
    expect("sort" in snaked).toBe(false);
    expect("dir" in snaked).toBe(false);
    expect("library_id" in snaked).toBe(false);
  });
});

describe("saved query serialization", () => {
  it("round-trips through JSON", () => {
    const savedQuery = {
      filters: {
        camera_make: "Canon",
        star_min: 4,
        color: "red",
        has_gps: true,
        date_from: "2024-01-01T00:00:00+00:00",
      },
      library_id: "lib_abc",
    };

    const json = JSON.stringify(savedQuery);
    const parsed = JSON.parse(json);

    expect(parsed.filters.camera_make).toBe("Canon");
    expect(parsed.filters.star_min).toBe(4);
    expect(parsed.filters.color).toBe("red");
    expect(parsed.library_id).toBe("lib_abc");
  });

  it("handles empty filters", () => {
    const savedQuery = { filters: {} };
    const json = JSON.stringify(savedQuery);
    const parsed = JSON.parse(json);
    expect(parsed.filters).toEqual({});
  });

  it("handles search query with filters", () => {
    const savedQuery = {
      q: "portrait",
      filters: {
        media_types: ["image"],
        has_faces: true,
      },
    };
    const json = JSON.stringify(savedQuery);
    const parsed = JSON.parse(json);
    expect(parsed.q).toBe("portrait");
    expect(parsed.filters.has_faces).toBe(true);
  });
});

describe("saved query display helpers", () => {
  it("formatSavedQuery produces human-readable filter descriptions", () => {
    // uses top-level import
    const labels = formatSavedQuery({
      filters: {
        camera_make: "Canon",
        star_min: 3,
        favorite: true,
        media_type: "image",
      },
    });

    expect(labels).toContain("Camera: Canon");
    expect(labels).toContain("Stars: 3+");
    expect(labels).toContain("Favorites");
    expect(labels).toContain("Photos only");
  });

  it("returns empty array for empty filters", () => {
    // uses top-level import
    expect(formatSavedQuery({ filters: {} })).toEqual([]);
  });

  it("includes search query", () => {
    // uses top-level import
    const labels = formatSavedQuery({
      q: "sunset",
      filters: { color: "orange" },
    });
    expect(labels).toContain('Search: "sunset"');
    expect(labels).toContain("Color: orange");
  });
});
