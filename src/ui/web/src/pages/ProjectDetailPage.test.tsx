import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { ScrollContainerContext } from "../context/ScrollContainerContext";
import ProjectDetailPage from "./ProjectDetailPage";

const api = vi.hoisted(() => ({
  getProject: vi.fn(),
  listProjectAssets: vi.fn(),
  trashProject: vi.fn(),
  restoreProjectClips: vi.fn(),
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
const baseProject = {
  project_id: "prj_1",
  name: "Test",
  description: null,
  cover_asset_id: null,
  owner_user_id: "usr_1",
  visibility: "private",
  ownership: "own",
  sort_order: "manual",
  asset_count: 2,
  created_at: "2026-10-07T00:00:00Z",
  updated_at: "2026-10-07T00:00:00Z",
  status: "active",
  };

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
  api.trashProject.mockResolvedValue(undefined);
  api.restoreProjectClips.mockResolvedValue({ restored: 1, missing: 0 });
  api.getProject.mockResolvedValue(baseProject);
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

function LocationProbe() {
  const location = useLocation();
  const state = location.state as { justTrashed?: { name: string } } | null;
  return <div>Projects list; trashed: {state?.justTrashed?.name ?? "none"}</div>;
}

function withProject(overrides: Record<string, unknown>) {
  api.getProject.mockImplementation(async () => ({ ...baseProject, ...overrides }));
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const scroller = document.createElement("div");
  return render(
    <QueryClientProvider client={client}>
      <ScrollContainerContext.Provider value={scroller}>
        <MemoryRouter initialEntries={["/projects/prj_1"]}>
          <Routes>
            <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
            <Route path="/projects" element={<LocationProbe />} />
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

  it("says how many clips are in the trash and restores them", async () => {
    withProject({ trashed_asset_count: 2, missing_asset_count: 1 });
    renderPage();
    const banner = await screen.findByRole("status");
    expect(banner.textContent).toMatch(/2 clips are in the trash/);
    expect(banner.textContent).toMatch(/1 clip is missing from disk/);
    fireEvent.click(screen.getByRole("button", { name: "Restore them" }));
    await waitFor(() => expect(api.restoreProjectClips).toHaveBeenCalledWith("prj_1"));
  });

  it("says it, not them, for one clip", async () => {
    withProject({ trashed_asset_count: 1 });
    renderPage();
    expect((await screen.findByRole("status")).textContent).toMatch(/1 clip is in the trash/);
    expect(screen.getByRole("button", { name: "Restore it" })).toBeTruthy();
  });

  it("shows no banner when nothing is in the trash", async () => {
    renderPage();
    await screen.findByAltText("camA.mov");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("moves the project to the trash from settings and offers an undo on the list", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    fireEvent.click(screen.getByRole("button", { name: "Move to trash" }));
    await waitFor(() => expect(api.trashProject).toHaveBeenCalledWith("prj_1"));
    expect(await screen.findByText("Projects list; trashed: Test")).toBeTruthy();
  });

  it("says when clips are only missing from disk, with nothing to restore", async () => {
    withProject({ missing_asset_count: 2 });
    renderPage();
    expect((await screen.findByRole("status")).textContent).toMatch(/2 clips are missing from disk/);
    expect(screen.queryByRole("button", { name: /^Restore/ })).toBeNull();
  });

  it("says when clips went with a library in the trash, and that restoring it brings them back", async () => {
    withProject({ library_trashed_asset_count: 1 });
    renderPage();
    expect((await screen.findByRole("status")).textContent).toMatch(
      /1 clip is in a library in the trash, and comes back if the library is restored/,
    );
  });

  it("says when clips are archived, and where to find them", async () => {
    withProject({ archived_asset_count: 3 });
    renderPage();
    const status = await screen.findByRole("status");
    expect(status.textContent).toMatch(/3 clips are archived, so they aren't shown or exported/);
    expect(within(status).getByRole("link", { name: "Go to Archive" }).getAttribute("href")).toBe("/archive?kind=by_hand");
    expect(screen.queryByRole("button", { name: /^Restore/ })).toBeNull();
  });

  it("offers no move to trash on someone else's project", async () => {
    withProject({ ownership: "shared", owner_user_id: "usr_2" });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    expect(screen.queryByRole("button", { name: "Move to trash" })).toBeNull();
  });

  it("offers move to trash on an older project with no owner", async () => {
    withProject({ ownership: "shared", owner_user_id: null });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    expect(screen.getByRole("button", { name: "Move to trash" })).toBeTruthy();
  });
});
