import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ScrollContainerContext } from "../context/ScrollContainerContext";
import ProjectDetailPage from "./ProjectDetailPage";

const api = vi.hoisted(() => ({
  getProject: vi.fn(),
  listProjectAssets: vi.fn(),
}));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));
vi.mock("../api/useAuthenticatedImage", () => ({
  useAuthenticatedImage: () => ({ url: "blob:thumb", isLoading: false, error: null }),
}));

// jsdom has no layout: give every element a size and a ResizeObserver that
// reports it, so the grid can measure itself the way a browser would.
class FakeResizeObserver {
  constructor(private cb: ResizeObserverCallback) {}
  observe(target: Element) {
    this.cb(
      [{ target, contentRect: { width: 800, height: 800 } } as unknown as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    );
  }
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(800);
  api.getProject.mockResolvedValue({
    project_id: "prj_1",
    name: "Test",
    description: null,
    cover_asset_id: null,
    owner_user_id: "usr_1",
    visibility: "private",
    ownership: "own",
    sort_order: "manual",
    type: "static",
    saved_query: null,
    asset_count: 2,
    created_at: "2026-10-07T00:00:00Z",
    updated_at: "2026-10-07T00:00:00Z",
    status: "active",
  });
  api.listProjectAssets.mockResolvedValue({
    items: ["camA.mov", "camB.mov"].map((name, i) => ({
      asset_id: `ast_${i}`,
      rel_path: `videos/${name}`,
      file_size: 1000,
      media_type: "video",
      width: 1920,
      height: 1080,
      taken_at: "2026-04-09T12:00:00Z",
      status: "described",
      duration_sec: 8,
      camera_make: null,
      camera_model: null,
    })),
    next_cursor: null,
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const scroller = document.createElement("div");
  return render(
    <QueryClientProvider client={client}>
      <ScrollContainerContext.Provider value={scroller}>
        <MemoryRouter initialEntries={["/projects/prj_1"]}>
          <Routes>
            <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
          </Routes>
        </MemoryRouter>
      </ScrollContainerContext.Provider>
    </QueryClientProvider>,
  );
}

describe("ProjectDetailPage", () => {
  it("shows the clips when the project loads after the first render", async () => {
    // A cold load renders "Loading..." first, so the grid mounts later.
    renderPage();
    expect(await screen.findByAltText("camA.mov")).toBeTruthy();
    expect(screen.getByAltText("camB.mov")).toBeTruthy();
  });

  it("lets the header wrap so the buttons fit on a phone", async () => {
    renderPage();
    const exportButton = await screen.findByRole("button", { name: "Export options" });
    const header = exportButton.closest(".border-b") as HTMLElement;
    expect(header.className.split(/\s+/)).toContain("flex-wrap");
  });
});
