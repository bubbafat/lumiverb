/**
 * Diagnostic test: verify that paramsToFilters produces stable output
 * when called with the same URL string. If this test passes but the
 * render loop persists, the instability is in React component state
 * (useSearchParams identity, useMemo deps) not in the filter parsing.
 */
import { describe, expect, it } from "vitest";
import { paramsToFilters, filtersToParams } from "./queryFilter";

describe("filter parsing stability", () => {
  it("paramsToFilters returns structurally identical output for same input", () => {
    const url = "f=library%3Alib_01KNAC1HB4TGRH339ZAQJAQV1S&sort=taken_at&dir=desc";
    const r1 = paramsToFilters(new URLSearchParams(url));
    const r2 = paramsToFilters(new URLSearchParams(url));

    // Deep equality (content)
    expect(r1.filters).toEqual(r2.filters);
    expect(r1.sort).toBe(r2.sort);
    expect(r1.direction).toBe(r2.direction);

    // JSON stability (what TanStack Query uses for key hashing)
    expect(JSON.stringify(r1.filters)).toBe(JSON.stringify(r2.filters));
  });

  it("paramsToFilters returns stable output for empty URL", () => {
    const r1 = paramsToFilters(new URLSearchParams(""));
    const r2 = paramsToFilters(new URLSearchParams(""));

    expect(r1.filters).toEqual(r2.filters);
    expect(JSON.stringify(r1)).toBe(JSON.stringify(r2));
  });

  it("filtersToParams produces deterministic URL string", () => {
    const filters = [
      { type: "library", value: "lib_01KNAC1HB4TGRH339ZAQJAQV1S" },
      { type: "camera_make", value: "Canon" },
    ];
    const s1 = filtersToParams(filters, "taken_at", "desc").toString();
    const s2 = filtersToParams(filters, "taken_at", "desc").toString();
    expect(s1).toBe(s2);
  });

  it("simulates the BrowsePage useMemo chain — fKey is stable", () => {
    // This simulates what BrowsePage does: read getAll("f"), join with \0, use as memo key
    const params = new URLSearchParams("f=library%3Alib_xxx");

    const fParams1 = params.getAll("f");
    const fKey1 = fParams1.join("\0");

    const fParams2 = params.getAll("f");
    const fKey2 = fParams2.join("\0");

    expect(fKey1).toBe(fKey2);

    // But a NEW URLSearchParams from the same string produces the same fKey
    const params2 = new URLSearchParams("f=library%3Alib_xxx");
    const fKey3 = params2.getAll("f").join("\0");
    expect(fKey1).toBe(fKey3);
  });

  it("simulates empty URL — fKey is empty string (stable)", () => {
    const params = new URLSearchParams("");
    const fKey = params.getAll("f").join("\0");
    expect(fKey).toBe("");

    const params2 = new URLSearchParams("");
    const fKey2 = params2.getAll("f").join("\0");
    expect(fKey).toBe(fKey2);
  });

  it("TanStack Query key hash is stable for same filter content", () => {
    // Simulate what TanStack Query does: JSON.stringify the query key
    const filters1 = [{ type: "library", value: "lib_xxx" }];
    const filters2 = [{ type: "library", value: "lib_xxx" }];

    const key1 = JSON.stringify(["unified-query", filters1, "taken_at", "desc"]);
    const key2 = JSON.stringify(["unified-query", filters2, "taken_at", "desc"]);
    expect(key1).toBe(key2);
  });
});
