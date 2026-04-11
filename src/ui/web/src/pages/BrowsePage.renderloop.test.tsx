/**
 * Diagnostic test: mount a minimal component that replicates BrowsePage's
 * filter → query → render cycle. Verify queryAssets is called exactly once
 * for a stable set of filters (no render loop).
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, act, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { MemoryRouter, useSearchParams } from "react-router-dom";
import React, { useRef, useMemo, useCallback } from "react";

// Mock queryAssets to count calls
const queryAssetsMock = vi.fn().mockResolvedValue({
  items: [],
  next_cursor: null,
  total_estimate: 0,
});

afterEach(cleanup);

/**
 * Minimal component that replicates BrowsePage's filter → query chain.
 * No rendering of assets, no FilterBar, no scroll handler — just the
 * hooks that read filters from URL and feed them into useInfiniteQuery.
 */
function TestHarness({ libraryId }: { libraryId: string }) {
  const [searchParams, setSearchParams] = useSearchParams();

  // Same pattern as BrowsePage
  const fParams = searchParams.getAll("f");
  const fKey = fParams.join("\0");
  const sortParam = searchParams.get("sort");
  const dirParam = searchParams.get("dir");

  const { filters: urlFilters, sort: browseSort, direction: browseDir } = useMemo(() => {
    const filters = fParams
      .map((raw) => {
        const colon = raw.indexOf(":");
        if (colon <= 0) return null;
        return { type: raw.slice(0, colon), value: raw.slice(colon + 1) };
      })
      .filter((f): f is { type: string; value: string } => f !== null);
    return {
      filters,
      sort: sortParam ?? "taken_at",
      direction: (dirParam === "asc" ? "asc" : "desc") as "asc" | "desc",
    };
  }, [fKey, sortParam, dirParam]); // eslint-disable-line react-hooks/exhaustive-deps

  // Add implicit library scope
  const filters = useMemo(() => {
    const hasLib = urlFilters.some((f) => f.type === "library");
    if (hasLib || !libraryId) return urlFilters;
    return [...urlFilters, { type: "library", value: libraryId }];
  }, [urlFilters, libraryId]);

  // Facets query — same as BrowsePage
  void useQuery({
    queryKey: ["filtered-facets", filters],
    queryFn: () => Promise.resolve({
      media_types: ["image"], camera_makes: ["Canon"], camera_models: [],
      lens_models: [], iso_range: [100, 3200], aperture_range: [1.4, 22],
      focal_length_range: [24, 200], has_gps_count: 5, has_face_count: 3,
    }),
    staleTime: 5 * 60_000,
  });

  const browseQuery = useInfiniteQuery({
    queryKey: ["unified-query", filters, browseSort, browseDir],
    queryFn: ({ pageParam }) => {
      queryAssetsMock({ filters, sort: browseSort, dir: browseDir, after: pageParam });
      // Return 100 fake items with a next_cursor to simulate real pagination
      const items = Array.from({ length: 100 }, (_, i) => ({
        asset_id: `ast_${pageParam ?? "0"}_${i}`,
        library_id: libraryId,
        library_name: "Test",
        rel_path: `photo_${i}.jpg`,
        file_size: 1000,
        media_type: "image",
        width: 100,
        height: 100,
        taken_at: "2024-01-01T00:00:00Z",
        status: "active",
        duration_sec: null,
        camera_make: null,
        camera_model: null,
        iso: null,
        aperture: null,
        focal_length: null,
        focal_length_35mm: null,
        lens_model: null,
        flash_fired: null,
        gps_lat: null,
        gps_lon: null,
        face_count: null,
        thumbnail_key: null,
        proxy_key: null,
        created_at: null,
        search_context: null,
      }));
      return Promise.resolve({
        items,
        next_cursor: pageParam ? null : "cursor_page2",  // only 1 more page
        total_estimate: 200,
      });
    },
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage: { next_cursor: string | null }) =>
      lastPage?.next_cursor ?? undefined,
  });

  // Simulate FilterBar's debounce effect (the suspected cause)
  const q = useMemo(
    () => filters.find((f) => f.type === "query")?.value ?? null,
    [filters],
  );
  const [inputValue, setInputValue] = React.useState(q ?? "");

  // Sync input with external q (same as FilterBar)
  React.useEffect(() => {
    setInputValue(q ?? "");
  }, [q]);

  // Ref for stable callback (same as BrowsePage)
  const setSearchParamsRef = useRef(setSearchParams);
  setSearchParamsRef.current = setSearchParams;

  const handleSetFilter = useCallback(
    (type: string, value: string | null) => {
      setSearchParamsRef.current((prev: URLSearchParams) => {
        const next = new URLSearchParams(prev);
        const existing = next.getAll("f");
        next.delete("f");
        for (const raw of existing) {
          const colon = raw.indexOf(":");
          if (colon > 0 && raw.slice(0, colon) === type) continue;
          next.append("f", raw);
        }
        if (value != null && value !== "") {
          next.append("f", `${type}:${value}`);
        }
        return next;
      });
    },
    [],
  );

  const applySearch = useCallback(
    (value: string) => {
      const trimmed = value.trim();
      handleSetFilter("query", trimmed.length > 0 ? trimmed : null);
    },
    [handleSetFilter],
  );

  // Debounce effect (same as FilterBar)
  React.useEffect(() => {
    const handle = window.setTimeout(() => {
      applySearch(inputValue);
    }, 500);
    return () => window.clearTimeout(handle);
  }, [inputValue, applySearch]);

  return (
    <div data-testid="harness">
      {browseQuery.isLoading ? "loading" : `items:${browseQuery.data?.pages.length ?? 0}`}
    </div>
  );
}

function renderWithProviders(libraryId: string, initialUrl = "/browse") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialUrl]}>
        <TestHarness libraryId={libraryId} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("BrowsePage render loop diagnostic", () => {
  it("calls queryAssets exactly once on initial mount with no URL filters", async () => {
    queryAssetsMock.mockClear();

    renderWithProviders("lib_test123");

    // Let all effects and queries settle
    await act(() => new Promise((r) => setTimeout(r, 100)));

    // Should have called queryAssets exactly once (initial fetch)
    expect(queryAssetsMock).toHaveBeenCalledTimes(1);

    // Wait a bit more to detect any loop
    await act(() => new Promise((r) => setTimeout(r, 500)));

    // Still just 1 call — no loop
    expect(queryAssetsMock).toHaveBeenCalledTimes(1);
  });

  it("calls queryAssets exactly once with URL filters present", async () => {
    queryAssetsMock.mockClear();

    renderWithProviders("lib_test123", "/browse?f=camera_make:Canon");

    await act(() => new Promise((r) => setTimeout(r, 100)));
    expect(queryAssetsMock).toHaveBeenCalledTimes(1);

    await act(() => new Promise((r) => setTimeout(r, 500)));
    expect(queryAssetsMock).toHaveBeenCalledTimes(1);
  });

  it("query key is deterministic for same filters", async () => {
    queryAssetsMock.mockClear();

    renderWithProviders("lib_test123");

    await act(() => new Promise((r) => setTimeout(r, 200)));

    // All calls should have the same filter content
    const calls = queryAssetsMock.mock.calls;
    if (calls.length > 1) {
      const first = JSON.stringify(calls[0][0].filters);
      for (let i = 1; i < calls.length; i++) {
        expect(JSON.stringify(calls[i][0].filters)).toBe(first);
      }
      // If we get here with >1 call but same filters, TanStack Query
      // is re-fetching despite stable key — that's the bug
      expect(calls.length).toBe(1); // fail with useful message
    }
  });
});
