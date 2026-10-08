import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import ArchivePage from "./ArchivePage";

const api = vi.hoisted(() => ({
  listArchive: vi.fn(),
  listLibraries: vi.fn(),
  unarchiveClips: vi.fn(),
  trashClips: vi.fn(),
  restoreClips: vi.fn(),
  getCurrentUser: vi.fn(),
  getTenantSettings: vi.fn(),
}));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));
vi.mock("../api/useAuthenticatedImage", () => ({ useAuthenticatedImage: () => ({ url: null }) }));

const clip = (id: string, path: string, missing = false) => ({
  asset_id: id, library_id: "lib_1", library_name: "Media", rel_path: path, media_type: "video",
  archived_at: "2026-10-08T12:00:00Z", file_missing: missing,
});

beforeEach(() => {
  api.listLibraries.mockResolvedValue([{ library_id: "lib_1", name: "Media", root_path: "/m", status: "active", is_public: false, last_scan_at: null }]);
  api.listArchive.mockResolvedValue({ items: [clip("a1", "Trips/Paris/a.mov"), clip("a2", "Trips/Paris/b.mov", true)], next_cursor: null, total: 2 });
  api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "editor" });
  api.getTenantSettings.mockResolvedValue({ trash_days: 30 });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderPage(url = "/archive") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <ArchivePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ArchivePage", () => {
  it("lists archived clips and marks the ones whose file is missing", async () => {
    renderPage();
    expect(await screen.findByText("a.mov")).toBeTruthy();
    expect(screen.getAllByText("File missing", { selector: "span" })).toHaveLength(1);
    expect(screen.getByText(/come back by themselves when the file does/)).toBeTruthy();
  });

  it("shows one kind at a time", async () => {
    renderPage();
    await screen.findByText("a.mov");
    fireEvent.click(screen.getByRole("tab", { name: "File missing" }));
    await waitFor(() => expect(api.listArchive).toHaveBeenLastCalledWith(expect.objectContaining({ kind: "missing" })));
    expect(screen.getByRole("tab", { name: "File missing" }).getAttribute("aria-selected")).toBe("true");
  });

  it("unarchives what's picked, and says which wait for their file", async () => {
    api.unarchiveClips.mockResolvedValue({ unarchived: ["a1"], skipped: ["a2"] });
    renderPage();
    fireEvent.click(await screen.findByRole("checkbox", { name: "a.mov" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "b.mov" }));
    fireEvent.click(screen.getByRole("button", { name: "Unarchive" }));
    await waitFor(() => expect(api.unarchiveClips).toHaveBeenCalledWith({ asset_ids: ["a1", "a2"] }));
    expect((await screen.findByRole("status")).textContent).toMatch(
      /Unarchived 1 clip\. 1 clip with a missing file stays archived until the file is back\./,
    );
  });

  it("moves picked clips to the trash", async () => {
    api.trashClips.mockResolvedValue({ trashed: ["a1"], not_found: [] });
    renderPage();
    fireEvent.click(await screen.findByRole("checkbox", { name: "a.mov" }));
    fireEvent.click(screen.getByRole("button", { name: "Move to trash" }));
    await waitFor(() => expect(api.trashClips).toHaveBeenCalledWith(["a1"], false));
  });

  it("unarchives everything in a folder it's looking at", async () => {
    api.unarchiveClips.mockResolvedValue({ unarchived: ["a1"], skipped: [] });
    renderPage("/archive?library=lib_1&path=Trips/Paris");
    await screen.findByText("a.mov");
    expect(api.listArchive).toHaveBeenCalledWith(expect.objectContaining({ libraryId: "lib_1", path: "Trips/Paris" }));
    fireEvent.click(screen.getByRole("button", { name: "Unarchive everything in Paris" }));
    await waitFor(() => expect(api.unarchiveClips).toHaveBeenCalledWith({ library_id: "lib_1", path: "Trips/Paris" }));
  });

  it("viewers only look", async () => {
    api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "viewer" });
    renderPage("/archive?library=lib_1&path=Trips/Paris");
    expect(await screen.findByText("a.mov")).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /Unarchive/ })).toBeNull();
  });
});
