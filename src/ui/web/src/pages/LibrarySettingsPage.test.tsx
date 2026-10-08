import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ApiError } from "../api/client";
import LibrarySettingsPage from "./LibrarySettingsPage";

const api = vi.hoisted(() => ({
  listLibraries: vi.fn(),
  getLibraryFilters: vi.fn(),
  addLibraryFilter: vi.fn(),
  deleteLibraryFilter: vi.fn(),
  previewLibraryFilter: vi.fn(),
  getTenantSettings: vi.fn(),
}));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));

const usage = {
  assets_in_projects: 2,
  projects: [{ project_id: "prj_1", name: "Customer Video", status: "active", in_trash: false, clips: 2 }],
  other_projects: 0,
};

beforeEach(() => {
  api.listLibraries.mockResolvedValue([{ library_id: "lib_1", name: "Media", root_path: "/m", status: "active", is_public: false, last_scan_at: null }]);
  api.getLibraryFilters.mockResolvedValue({ includes: [], excludes: [] });
  api.previewLibraryFilter.mockResolvedValue({ matching_asset_count: 5 });
  api.getTenantSettings.mockResolvedValue({ trash_days: 30 });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/libraries/lib_1/settings?tab=filters&exclude=Rejects%2F**"]}>
        <Routes>
          <Route path="/libraries/:libraryId/settings" element={<LibrarySettingsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function excludeRejects() {
  renderPage();
  const add = await screen.findAllByRole("button", { name: "Add" });
  fireEvent.click(add[add.length - 1]);
  expect(await screen.findByText(/existing clips to the trash, deleted for good after 30 days unless restored, and prevent future ingestion/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
}

describe("LibrarySettingsPage exclude filter that trashes clips", () => {
  it("names the projects that use them and adds the filter only when told to", async () => {
    api.addLibraryFilter.mockImplementation(async (_l: string, _t: string, _p: string, _tm: boolean, remove: boolean) => {
      if (!remove) throw new ApiError(409, "in projects", "in_projects", usage);
      return { filter_id: "f1", type: "exclude", pattern: "Rejects/**", created_at: "2026-10-08T00:00:00Z", trashed_count: 5 };
    });
    await excludeRejects();
    const ask = await screen.findByRole("alertdialog", { name: "Clips in projects" });
    expect(ask.textContent).toMatch(/2 clips that Rejects\/\*\* matches are in projects/);
    expect(within(ask).getByText(/Customer Video: 2 clips/)).toBeTruthy();
    fireEvent.click(within(ask).getByRole("button", { name: "Exclude and move them to the trash" }));
    await waitFor(() => expect(api.addLibraryFilter).toHaveBeenLastCalledWith("lib_1", "exclude", "Rejects/**", true, true));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  });

  it("changes nothing when that's cancelled", async () => {
    api.addLibraryFilter.mockRejectedValue(new ApiError(409, "in projects", "in_projects", usage));
    await excludeRejects();
    const ask = await screen.findByRole("alertdialog", { name: "Clips in projects" });
    fireEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.addLibraryFilter).toHaveBeenCalledTimes(1);
  });
});

describe("LibrarySettingsPage exclude filter errors", () => {
  it("keeps the typed pattern when adding fails", async () => {
    api.previewLibraryFilter.mockResolvedValue({ matching_asset_count: 0 });
    api.addLibraryFilter.mockRejectedValue(new ApiError(400, "Invalid pattern", "bad_pattern"));
    renderPage();
    const add = await screen.findAllByRole("button", { name: "Add" });
    fireEvent.click(add[add.length - 1]);
    expect(await screen.findByText("Invalid pattern")).toBeTruthy();
    const inputs = screen.getAllByRole("textbox") as HTMLInputElement[];
    expect(inputs.some((i) => i.value === "Rejects/**")).toBe(true);
  });
});
