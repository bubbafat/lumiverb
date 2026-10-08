import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ApiError } from "../api/client";
import LibrariesPage from "./LibrariesPage";

const api = vi.hoisted(() => ({
  listLibraries: vi.fn(),
  emptyTrash: vi.fn(),
  deleteLibrary: vi.fn(),
  restoreLibrary: vi.fn(),
  getTenantSettings: vi.fn(),
  listLibraryHealth: vi.fn(),
  getCurrentUser: vi.fn(),
  listArchive: vi.fn(),
}));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));

const usage = {
  assets_in_projects: 4,
  projects: [
    { project_id: "prj_1", name: "Customer Video", status: "active", in_trash: false, clips: 3 },
    { project_id: "prj_2", name: "Old reel", status: "archived", in_trash: false, clips: 1 },
  ],
  other_projects: 1,
};

beforeEach(() => {
  api.listLibraries.mockResolvedValue([
    { library_id: "lib_1", name: "Old card", root_path: "/old", status: "trashed", is_public: false, last_scan_at: null,
      trashed_at: "2026-10-01T12:00:00Z" },
  ]);
  api.getTenantSettings.mockResolvedValue({ video_preview_max_seconds: null, public_video_preview_max_seconds: 10,
                                           trash_days: 30 });
  api.listLibraryHealth.mockResolvedValue([]);
  api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "admin" });
  api.listArchive.mockResolvedValue({ items: [], next_cursor: null, total: 0 });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <LibrariesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openEmptyTrash() {
  fireEvent.click(await screen.findByRole("button", { name: /Empty trash \(1\)/ }));
  return screen.findByRole("dialog");
}

describe("LibrariesPage empty trash", () => {
  it("empties the trash when no project uses its clips", async () => {
    api.emptyTrash.mockResolvedValue({ deleted: 1 });
    renderPage();
    const dialog = await openEmptyTrash();
    fireEvent.click(within(dialog).getByRole("button", { name: "Empty trash" }));
    await waitFor(() => expect(api.emptyTrash).toHaveBeenCalledWith(false, ["lib_1"]));
  });

  it("names the projects that would lose clips and asks again", async () => {
    api.emptyTrash.mockImplementation(async (remove: boolean) => {
      if (!remove) throw new ApiError(409, "in projects", "in_projects", usage);
      return { deleted: 1 };
    });
    renderPage();
    const dialog = await openEmptyTrash();
    fireEvent.click(within(dialog).getByRole("button", { name: "Empty trash" }));

    expect(await within(dialog).findByText(/4 clips from this library are in 3 projects\. Deleting them for good removes them from those projects\./)).toBeTruthy();
    expect(within(dialog).getByText(/Customer Video/)).toBeTruthy();
    expect(within(dialog).getByText(/Old reel/)).toBeTruthy();
    expect(within(dialog).getByText(/1 more you can't see/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete and remove from projects" }));
    await waitFor(() => expect(api.emptyTrash).toHaveBeenLastCalledWith(true, ["lib_1"]));
  });

  it("says why when emptying the trash fails", async () => {
    api.emptyTrash.mockRejectedValue(new ApiError(500, "Couldn't reach the database", "internal"));
    renderPage();
    const dialog = await openEmptyTrash();
    fireEvent.click(within(dialog).getByRole("button", { name: "Empty trash" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain("Couldn't reach the database");
  });

  it("says it, not them, for one clip in one project", async () => {
    api.emptyTrash.mockRejectedValue(new ApiError(409, "in projects", "in_projects", {
      assets_in_projects: 1,
      projects: [{ project_id: "prj_1", name: "Customer Video", status: "active", in_trash: false, clips: 1 }],
      other_projects: 0,
    }));
    renderPage();
    const dialog = await openEmptyTrash();
    expect(dialog.textContent).toMatch(/1 trashed library and all its assets/);
    fireEvent.click(within(dialog).getByRole("button", { name: "Empty trash" }));
    expect(await within(dialog).findByText(/1 clip from this library is in 1 project\. Deleting it for good removes it from that project\./)).toBeTruthy();
  });
});

describe("LibrariesPage a library in the trash", () => {
  it("says when it's deleted for good, and restores it", async () => {
    api.restoreLibrary.mockResolvedValue({});
    renderPage();
    expect(await screen.findByText(/Deleted for good on Oct 31 unless restored\./)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(api.restoreLibrary).toHaveBeenCalledWith("lib_1"));
  });

  it("offers viewers nothing to change", async () => {
    api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "viewer" });
    renderPage();
    expect(await screen.findByText(/Deleted for good on/)).toBeTruthy();
    for (const name of ["Restore", "Delete for good", /Empty trash/]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  it("editors restore but don't delete for good", async () => {
    api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "editor" });
    api.restoreLibrary.mockResolvedValue({});
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    await waitFor(() => expect(api.restoreLibrary).toHaveBeenCalledWith("lib_1"));
    expect(screen.queryByRole("button", { name: "Delete for good" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Empty trash/ })).toBeNull();
  });

  it("says it stays when the trash is emptied by hand only", async () => {
    api.getTenantSettings.mockResolvedValue({ trash_days: null });
    renderPage();
    expect(await screen.findByText(/In the trash until you delete it for good\./)).toBeTruthy();
  });

  it("deletes just that one for good", async () => {
    api.emptyTrash.mockResolvedValue({ deleted: 1 });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Delete for good" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete Old card for good" });
    expect(dialog.textContent).toMatch(/permanently delete Old card and all its assets/);
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete for good" }));
    await waitFor(() => expect(api.emptyTrash).toHaveBeenCalledWith(false, ["lib_1"]));
  });
});

describe("LibrariesPage delete asks about projects", () => {
  beforeEach(() => {
    api.listLibraries.mockResolvedValue([
      { library_id: "lib_2", name: "Media", root_path: "/media", status: "active", is_public: false, last_scan_at: null },
    ]);
  });

  async function startDelete() {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    expect(screen.getByText(/moves to the trash with everything in it and is deleted for good after 30 days/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
  }

  it("says its archived clips go with it", async () => {
    api.listArchive.mockResolvedValue({ items: [], next_cursor: null, total: 3 });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    expect(await screen.findByText(/with everything in it, its 3 archived clips too, and is deleted for good/)).toBeTruthy();
    expect(api.listArchive).toHaveBeenCalledWith({ libraryId: "lib_2", limit: 1 });
  });

  it("a library no project uses goes to the trash straight away", async () => {
    api.deleteLibrary.mockResolvedValue(undefined);
    await startDelete();
    await waitFor(() => expect(api.deleteLibrary).toHaveBeenCalledWith("lib_2", false));
    expect(screen.queryByText(/in projects/)).toBeNull();
  });

  it("names the projects that use its clips, then moves it to the trash when told to", async () => {
    api.deleteLibrary.mockImplementation(async (_id: string, removeFromProjects: boolean) => {
      if (!removeFromProjects) throw new ApiError(409, "in projects", "in_projects", usage as unknown as Record<string, unknown>);
    });
    await startDelete();
    const ask = await screen.findByRole("alertdialog", { name: "Delete Media" });
    expect(ask.textContent).toMatch(/4 clips in Media are in projects/);
    expect(within(ask).getByText(/Customer Video/)).toBeTruthy();
    fireEvent.click(within(ask).getByRole("button", { name: "Move to trash anyway" }));
    await waitFor(() => expect(api.deleteLibrary).toHaveBeenLastCalledWith("lib_2", true));
  });

  it("changes nothing when cancelled at the projects question", async () => {
    api.deleteLibrary.mockRejectedValue(new ApiError(409, "in projects", "in_projects", usage as unknown as Record<string, unknown>));
    await startDelete();
    const ask = await screen.findByRole("alertdialog", { name: "Delete Media" });
    fireEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.deleteLibrary).toHaveBeenCalledTimes(1);
  });

  it("says why when a delete fails", async () => {
    api.deleteLibrary.mockRejectedValue(new ApiError(403, "Only an admin can delete libraries", "forbidden"));
    await startDelete();
    expect((await screen.findByRole("alert")).textContent).toContain("Only an admin can delete libraries");
  });
});
