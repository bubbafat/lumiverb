/**
 * A search's best matches apart (as Google Photos shows a search, Robert, Oct 29): one row of them with More on
 * a desktop, two on a phone with "+N" on the last tile; then everything it found by day. Asked for with the
 * search's first page (GET /v1/query best=N); More pages through them by relevance.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ScrollContainerContext } from "../context/ScrollContainerContext";
import UnifiedBrowsePage from "./UnifiedBrowsePage";
import { BEST_FIRST, BEST_MORE } from "../lib/bestMatches";
import { stubLayout } from "./scrolledGrid.testutil";

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

function clip(name: string) {
  return {
    asset_id: `ast_${name}`, library_id: "lib_1", library_name: "Footage", rel_path: `trip/${name}.jpg`,
    file_size: 1000, media_type: "image", width: 1600, height: 1200, taken_at: "2026-04-09T12:00:00Z",
    status: "described", duration_sec: null, camera_make: null, camera_model: null, iso: null, aperture: null,
    focal_length: null, focal_length_35mm: null, lens_model: null, flash_fired: null, gps_lat: null, gps_lon: null,
    face_count: null, created_at: null, search_context: null,
  };
}

const TOTAL = 30; // best matches the search has
const best = (from: number, n: number) => Array.from({ length: n }, (_, i) => clip(`best${from + i}`));

let width = 1200;
let scrollerEl: Element | null = null;

class FakeResizeObserver {
  constructor(private cb: ResizeObserverCallback) {}
  observe(target: Element) {
    const w = target === scrollerEl ? 1200 : width;
    this.cb([{ target, contentRect: { width: w, height: 800 } } as unknown as ResizeObserverEntry],
            this as unknown as ResizeObserver);
  }
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  width = 1200;
  api.listLibraries.mockResolvedValue([{ library_id: "lib_1", name: "Footage", root_path: "/m", revision: 1,
                                         last_scan_at: null, status: "active", is_public: false }]);
  api.queryAssets.mockImplementation(async (_f: unknown, opts?: { best?: number; after?: string }) => {
    if (opts?.best) {  // the search: its best matches with the first page, everything by date
      return { items: [clip("day1"), clip("day2")], next_cursor: null, total_estimate: 40,
               best: opts.after ? null : { items: best(0, opts.best), total: TOTAL } };
    }
    // More: by relevance, a page at a time
    const from = opts?.after ? Number(opts.after) : 0;
    return { items: best(from, Math.min(BEST_MORE, TOTAL - from)), next_cursor: null, total_estimate: 40 };
  });
  api.getFilteredFacets.mockResolvedValue({ media_types: ["image"], camera_makes: [], camera_models: [],
    lens_models: [], iso_range: [null, null], aperture_range: [null, null], focal_length_range: [null, null],
    has_gps_count: 0, has_face_count: 0 });
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
  vi.restoreAllMocks();
});

async function renderAt(at: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 30_000 } } });
  const scroller = document.createElement("div");
  scrollerEl = scroller;
  stubLayout(scroller);
  render(
    <QueryClientProvider client={client}>
      <ScrollContainerContext.Provider value={scroller}>
        <MemoryRouter initialEntries={[at]}>
          <Routes><Route path="/browse" element={<UnifiedBrowsePage />} /></Routes>
        </MemoryRouter>
      </ScrollContainerContext.Provider>
    </QueryClientProvider>,
  );
  await act(async () => { await vi.advanceTimersByTimeAsync(100); });
}

const bestShown = () => screen.queryAllByAltText(/^best\d+\.jpg$/).length;

describe("a search's best matches", () => {
  it("are asked for with the search and shown first, one row with how many there are, then More", async () => {
    await renderAt("/browse?f=query:dundee");
    expect(api.queryAssets).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ best: BEST_FIRST }));
    expect(screen.getByRole("button", { name: /Best matches/ })).toBeTruthy();
    expect(screen.getByText(String(TOTAL))).toBeTruthy();
    const firstRow = bestShown();
    expect(firstRow).toBeGreaterThan(0);
    expect(firstRow).toBeLessThan(BEST_FIRST);
    expect(screen.getAllByAltText(/^day\d\.jpg$/).length).toBe(2); // everything found, below

    fireEvent.click(screen.getByRole("button", { name: "More" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    // By relevance, a page at a time, without asking for best (relevance order).
    expect(api.queryAssets).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ limit: BEST_MORE }));
    expect(api.queryAssets.mock.calls.some(([, o]) => o?.limit === BEST_MORE && !o?.best)).toBe(true);
    expect(bestShown()).toBeGreaterThan(firstRow);
  });

  it("on a phone, are two rows with the last tile saying how many more, which shows them", async () => {
    width = 360;
    await renderAt("/browse?f=query:dundee");
    const plus = screen.getByText(/^\+\d+$/);
    const tiles = bestShown();
    expect(Number(plus.textContent!.slice(1))).toBe(TOTAL - tiles + 1);
    expect(screen.queryByRole("button", { name: "More" })).toBeNull();
    fireEvent.click(screen.getByAltText(`best${tiles - 1}.jpg`));
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(screen.queryByText(/^\+\d+$/)).toBeNull();
    expect(bestShown()).toBeGreaterThan(tiles);
  });

  it("aren't asked for or shown outside a search", async () => {
    await renderAt("/browse?f=path:trip");
    expect(api.queryAssets).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ best: undefined }));
    expect(screen.queryByRole("button", { name: /Best matches/ })).toBeNull();
  });
});
