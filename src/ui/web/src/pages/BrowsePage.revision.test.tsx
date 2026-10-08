/**
 * An open library grid follows the library's revision: when another tab,
 * another person or a scan changes the library, the grid and its facets
 * refetch, but a long ingest (a new revision on every poll) doesn't reload
 * the grid on every poll.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import type { NavigateFunction } from "react-router-dom";
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
  pageRequests = [];
  holdFirstPage = null;
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

// The library's settings page, with the sidebar's revision poll: it keeps
// polling while the grid isn't on screen.
function SettingsWithSidebar() {
  useQuery({
    queryKey: ["library-revision", "lib_1"],
    queryFn: () => api.getLibraryRevision("lib_1"),
    refetchInterval: POLL_MS,
  });
  return <div>Settings</div>;
}

let navigate: NavigateFunction;
function NavigateProbe() {
  navigate = useNavigate();
  return null;
}

function renderPage() {
  // The app's own defaults (main.tsx), so stale times behave as they do there.
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
  });
  const scroller = document.createElement("div");
  const view = render(
    <QueryClientProvider client={client}>
      <ScrollContainerContext.Provider value={scroller}>
        <MemoryRouter initialEntries={["/libraries/lib_1/browse"]}>
          <NavigateProbe />
          <Routes>
            <Route path="/libraries/:libraryId/browse" element={<BrowsePage />} />
            <Route path="/libraries/:libraryId/settings" element={<SettingsWithSidebar />} />
          </Routes>
        </MemoryRouter>
      </ScrollContainerContext.Provider>
    </QueryClientProvider>,
  );
  return { ...view, client, scroller };
}

/** The grid's loaded pages, as the file names of their clips. */
function gridPages(client: QueryClient): string[][] {
  const [[, data]] = client.getQueriesData<{ pages: { items: { rel_path: string }[] }[] }>({
    queryKey: ["unified-query"],
  });
  return data!.pages.map((p) => p.items.map((i) => i.rel_path));
}

/** Endless pages of one clip each, named for the revision they were read at. */
function servePages() {
  api.queryAssets.mockImplementation(async (_filters: unknown, opts?: { after?: string }) => {
    const page = opts?.after ? Number(opts.after.split("@")[0].slice(1)) : 1;
    const rev = serverRevision;
    gridFetches.push(rev);
    pageRequests.push(opts?.after);
    const result = {
      items: [{ ...clip, asset_id: `ast_p${page}_r${rev}`, rel_path: `r${rev}/p${page}.mov` }],
      next_cursor: `p${page + 1}@r${rev}`,
      total_estimate: 100,
    };
    if (holdFirstPage && page === 1) {
      const hold = holdFirstPage;
      holdFirstPage = null;
      await hold;
    }
    return result;
  });
}
let pageRequests: (string | undefined)[] = [];
let holdFirstPage: Promise<void> | null = null;

function scrollToBottom(scroller: HTMLElement) {
  // jsdom has no layout: every scroll is at the bottom.
  act(() => {
    scroller.dispatchEvent(new Event("scroll"));
  });
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

  it("refetches the grid on coming back from settings when the library changed meanwhile", async () => {
    renderPage();
    await advance(100);
    expect(gridFetches).toEqual([1]);

    act(() => navigate("/libraries/lib_1/settings"));
    await advance(100);
    // A scan changes the library; the sidebar's poll sees it.
    serverRevision = 2;
    await advance(POLL_MS);

    // Back within 30 seconds: the cached grid is still fresh, so only the
    // revision says it's out of date.
    act(() => navigate("/libraries/lib_1/browse"));
    await advance(100);
    expect(gridFetches).toEqual([1, 2]);
  });

  it("doesn't refetch the grid on coming back from settings when nothing changed", async () => {
    renderPage();
    await advance(100);
    act(() => navigate("/libraries/lib_1/settings"));
    await advance(POLL_MS);
    act(() => navigate("/libraries/lib_1/browse"));
    await advance(100);
    expect(gridFetches).toEqual([1]);
  });

  it("refetches every loaded page, each from the new cursor of the page before", async () => {
    servePages();
    const { client, scroller } = renderPage();
    await advance(100);
    scrollToBottom(scroller);
    await advance(100);
    expect(gridPages(client)).toEqual([["r1/p1.mov"], ["r1/p2.mov"]]);

    pageRequests = [];
    serverRevision = 2;
    await advance(POLL_MS);
    expect(pageRequests).toEqual([undefined, "p2@r2"]);
    expect(gridPages(client)).toEqual([["r2/p1.mov"], ["r2/p2.mov"]]);
  });

  it("finishes the refresh when scrolling reaches the end while it runs", async () => {
    servePages();
    const { client, scroller } = renderPage();
    await advance(100);
    scrollToBottom(scroller);
    await advance(100);

    // The refresh starts and its first page is slow to come back...
    let release!: () => void;
    holdFirstPage = new Promise<void>((resolve) => (release = resolve));
    serverRevision = 2;
    await advance(POLL_MS);
    // ...while the grid is scrolled to its end.
    scrollToBottom(scroller);
    await advance(100);
    release();
    await advance(100);

    expect(gridPages(client)).toEqual([["r2/p1.mov"], ["r2/p2.mov"]]);
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
