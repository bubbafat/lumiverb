/**
 * An open library grid follows the library's revision: when another tab,
 * another person or a scan changes the library, the grid and its facets
 * refetch, but a long ingest (a new revision on every poll) doesn't reload
 * the grid on every poll.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ScrollContainerContext } from "../context/ScrollContainerContext";
import BrowsePage from "./BrowsePage";

const api = vi.hoisted(() => ({
  getApiKey: vi.fn(),
  getLibrary: vi.fn(),
  getLibraryRevision: vi.fn(),
  queryAssets: vi.fn(),
  getFilteredFacets: vi.fn(),
  listDirectories: vi.fn(),
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

// The server's revision for lib_1; a test bumps it the way an ingest would.
let serverRevision = 1;
// The server revision each grid fetch saw, in order.
let gridFetches: number[] = [];

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

// jsdom has no layout: a ResizeObserver that reports a desktop-width grid.
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
  serverRevision = 1;
  gridFetches = [];
  api.getApiKey.mockReturnValue("test-key");
  api.getLibrary.mockResolvedValue({
    library_id: "lib_1",
    name: "Footage",
    root_path: "/media/footage",
    is_public: false,
  });
  api.getLibraryRevision.mockImplementation(async () => ({
    library_id: "lib_1",
    revision: serverRevision,
    asset_count: 1,
  }));
  api.queryAssets.mockImplementation(async () => {
    gridFetches.push(serverRevision);
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
  api.listDirectories.mockResolvedValue([]);
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
  // The app's own defaults (main.tsx), so stale times behave as they do there.
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
  });
  const scroller = document.createElement("div");
  return render(
    <QueryClientProvider client={client}>
      <ScrollContainerContext.Provider value={scroller}>
        <MemoryRouter initialEntries={["/libraries/lib_1/browse"]}>
          <Routes>
            <Route path="/libraries/:libraryId/browse" element={<BrowsePage />} />
          </Routes>
        </MemoryRouter>
      </ScrollContainerContext.Provider>
    </QueryClientProvider>,
  );
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("BrowsePage revision polling", () => {
  it("loads the grid once and leaves it alone while the revision stays put", async () => {
    renderPage();
    await advance(100);
    expect(gridFetches).toEqual([1]);
    await advance(POLL_MS * 3);
    expect(gridFetches).toEqual([1]);
    expect(api.getFilteredFacets).toHaveBeenCalledTimes(1);
  });

  it("refetches the grid and its facets when the library changes elsewhere", async () => {
    renderPage();
    await advance(100);
    expect(gridFetches).toEqual([1]);

    // Another tab trashes a clip: the server bumps the revision.
    serverRevision = 2;
    await advance(POLL_MS);

    expect(gridFetches).toEqual([1, 2]);
    expect(api.getFilteredFacets).toHaveBeenCalledTimes(2);
    const [filters] = api.queryAssets.mock.calls[api.queryAssets.mock.calls.length - 1];
    expect(filters).toContainEqual({ type: "library", value: "lib_1" });
  });

  it("doesn't refetch the grid on every poll during a long ingest, and ends up current", async () => {
    renderPage();
    await advance(100);

    // Two minutes of ingest: every poll sees a new revision.
    const polls = 12;
    for (let i = 0; i < polls; i++) {
      serverRevision += 1;
      await advance(POLL_MS);
    }
    const refreshesDuringIngest = gridFetches.length - 1;
    expect(refreshesDuringIngest).toBeGreaterThan(0);
    // One right away, then at most one per 30 seconds: 4 in two minutes, not 12.
    expect(refreshesDuringIngest).toBeLessThanOrEqual(polls / 3);

    // Once the ingest stops, one last refresh shows its final state.
    await advance(30_000);
    expect(gridFetches[gridFetches.length - 1]).toBe(serverRevision);
    await advance(60_000);
    expect(gridFetches.length - 1).toBeLessThanOrEqual(polls / 3 + 1);
  });
});
