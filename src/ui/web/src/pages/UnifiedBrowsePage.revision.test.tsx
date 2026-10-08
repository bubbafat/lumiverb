/**
 * The grid across libraries follows every library's revision (from the
 * library list): when one changes elsewhere, the grid and its facets
 * refetch, but not on every poll while a long ingest keeps changing it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ScrollContainerContext } from "../context/ScrollContainerContext";
import UnifiedBrowsePage from "./UnifiedBrowsePage";

const api = vi.hoisted(() => ({
  listLibraries: vi.fn(),
  queryAssets: vi.fn(),
  getFilteredFacets: vi.fn(),
  lookupRatings: vi.fn(),
  getCurrentUser: vi.fn(),
  getTenantSettings: vi.fn(),
  listProjects: vi.fn(),
}));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));
vi.mock("../api/useAuthenticatedImage", () => ({
  useAuthenticatedImage: () => ({ url: "blob:thumb", isLoading: false, error: null }),
}));

const POLL_MS = 10_000;

// The server's libraries and their revisions; a test changes them the way
// another tab or a scan would.
let libraries: { library_id: string; revision: number }[] = [];
// How many grid fetches each test saw.
let gridFetches = 0;

const clip = {
  asset_id: "ast_1",
  library_id: "lib_1",
  library_name: "Footage",
  rel_path: "day1/a.mov",
  file_size: 1000,
  media_type: "video",
  width: 1920,
  height: 1080,
  taken_at: "2026-04-09T12:00:00Z",
  status: "described",
  duration_sec: 8,
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
  created_at: null,
};

class FakeResizeObserver {
  constructor(private cb: ResizeObserverCallback) {}
  observe(target: Element) {
    this.cb(
      [{ target, contentRect: { width: 1200, height: 800 } } as unknown as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    );
  }
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  libraries = [
    { library_id: "lib_1", revision: 1 },
    { library_id: "lib_2", revision: 5 },
  ];
  gridFetches = 0;
  api.listLibraries.mockImplementation(async () =>
    libraries.map((l) => ({
      ...l,
      name: l.library_id,
      root_path: `/media/${l.library_id}`,
      last_scan_at: null,
      status: "active",
      is_public: false,
    })),
  );
  api.queryAssets.mockImplementation(async () => {
    gridFetches += 1;
    return { items: [clip], next_cursor: null, total_estimate: 1 };
  });
  api.getFilteredFacets.mockResolvedValue({
    media_types: ["video"],
    camera_makes: [],
    camera_models: [],
    lens_models: [],
    iso_range: [null, null],
    aperture_range: [null, null],
    focal_length_range: [null, null],
    has_gps_count: 0,
    has_face_count: 0,
  });
  api.lookupRatings.mockResolvedValue({ ratings: {} });
  api.getCurrentUser.mockResolvedValue({ role: "editor" });
  api.getTenantSettings.mockResolvedValue({});
  api.listProjects.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
  });
  const scroller = document.createElement("div");
  const view = render(
    <QueryClientProvider client={client}>
      <ScrollContainerContext.Provider value={scroller}>
        <MemoryRouter initialEntries={["/browse"]}>
          <Routes>
            <Route path="/browse" element={<UnifiedBrowsePage />} />
          </Routes>
        </MemoryRouter>
      </ScrollContainerContext.Provider>
    </QueryClientProvider>,
  );
  return { ...view, client, scroller };
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function bump(libraryId: string) {
  libraries = libraries.map((l) =>
    l.library_id === libraryId ? { ...l, revision: l.revision + 1 } : l,
  );
}

describe("UnifiedBrowsePage revision polling", () => {
  it("loads the grid once and leaves it alone while no library changes", async () => {
    renderPage();
    await advance(100);
    expect(gridFetches).toBe(1);
    await advance(POLL_MS * 3);
    expect(gridFetches).toBe(1);
    expect(api.getFilteredFacets).toHaveBeenCalledTimes(1);
  });

  it("refetches the grid and its facets when any library changes elsewhere", async () => {
    renderPage();
    await advance(100);
    bump("lib_2");
    await advance(POLL_MS);
    expect(gridFetches).toBe(2);
    expect(api.getFilteredFacets).toHaveBeenCalledTimes(2);
  });

  it("refetches the grid when a library goes to the trash elsewhere", async () => {
    renderPage();
    await advance(100);
    libraries = libraries.filter((l) => l.library_id !== "lib_2");
    await advance(POLL_MS);
    expect(gridFetches).toBe(2);
  });

  it("leaves the grid alone when the list comes back in another order", async () => {
    // The server doesn't promise an order; an update can reorder the rows.
    renderPage();
    await advance(100);
    libraries = [...libraries].reverse();
    await advance(POLL_MS);
    expect(gridFetches).toBe(1);
  });

  it("finishes the refresh when scrolling reaches the end while it runs", async () => {
    let release: (() => void) | null = null;
    let hold = false;
    api.queryAssets.mockImplementation(async (_filters: unknown, opts?: { after?: string }) => {
      const page = opts?.after ? Number(opts.after.split("@")[0].slice(1)) : 1;
      const rev = libraries[0].revision;
      gridFetches += 1;
      const result = {
        items: [{ ...clip, asset_id: `ast_p${page}_r${rev}`, rel_path: `r${rev}/p${page}.mov` }],
        next_cursor: `p${page + 1}@r${rev}`,
        total_estimate: 100,
      };
      if (hold && page === 1) {
        hold = false;
        await new Promise<void>((resolve) => (release = resolve));
      }
      return result;
    });
    const { client, scroller } = renderPage();
    await advance(100);
    act(() => {
      scroller.dispatchEvent(new Event("scroll"));
    });
    await advance(100);

    hold = true;
    bump("lib_1");
    await advance(POLL_MS);
    act(() => {
      scroller.dispatchEvent(new Event("scroll"));
    });
    await advance(100);
    release!();
    await advance(100);

    const [[, data]] = client.getQueriesData<{ pages: { items: { rel_path: string }[] }[] }>({
      queryKey: ["unified-query"],
    });
    expect(data!.pages.map((p) => p.items.map((i) => i.rel_path))).toEqual([
      ["r2/p1.mov"],
      ["r2/p2.mov"],
    ]);
  });

  it("doesn't refetch the grid on every poll during a long ingest", async () => {
    renderPage();
    await advance(100);
    const polls = 12;
    for (let i = 0; i < polls; i++) {
      bump("lib_1");
      await advance(POLL_MS);
    }
    expect(gridFetches - 1).toBeGreaterThan(0);
    expect(gridFetches - 1).toBeLessThanOrEqual(polls / 3);
  });
});
