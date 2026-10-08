import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ApiError } from "../api/client";
import TrashPage from "./TrashPage";

const api = vi.hoisted(() => ({
  listTrash: vi.fn(),
  listLibraries: vi.fn(),
  restoreClips: vi.fn(),
  emptyClipTrash: vi.fn(),
  getCurrentUser: vi.fn(),
}));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));
vi.mock("../api/useAuthenticatedImage", () => ({ useAuthenticatedImage: () => ({ url: null }) }));

const clip = (id: string, path: string) => ({
  asset_id: id, library_id: "lib_1", library_name: "Media", rel_path: path, media_type: "video",
  trashed_at: "2026-10-01T12:00:00Z", expires_at: "2026-10-31T12:00:00Z",
});

beforeEach(() => {
  api.listLibraries.mockResolvedValue([{ library_id: "lib_1", name: "Media", root_path: "/m", status: "active", is_public: false, last_scan_at: null }]);
  api.listTrash.mockResolvedValue({ items: [clip("a1", "Day 1/A001.mov"), clip("a2", "Day 1/A002.mov")], next_cursor: null, total: 2, trash_days: 30 });
  api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "admin" });
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
        <TrashPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TrashPage", () => {
  it("lists what's in the trash with when each is deleted for good", async () => {
    renderPage();
    expect(await screen.findByText("A001.mov")).toBeTruthy();
    expect(screen.getAllByText(/Deleted for good Oct 31/)).toHaveLength(2);
    expect(screen.getByText(/deleted for good 30 days after they went in/)).toBeTruthy();
    expect(screen.getAllByText(/Media · Day 1/)).toHaveLength(2);
  });

  it("restores what's picked", async () => {
    api.restoreClips.mockResolvedValue({ restored: ["a1"], skipped: [] });
    renderPage();
    fireEvent.click(await screen.findByRole("checkbox", { name: "A001.mov" }));
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(api.restoreClips).toHaveBeenCalledWith(["a1"]));
    expect((await screen.findByRole("status")).textContent).toMatch(/Restored 1 clip\./);
  });

  it("deletes picked clips for good only after asking, and again about projects", async () => {
    const usage = { assets_in_projects: 1, projects: [{ project_id: "p", name: "Reel", status: "active", in_trash: false, clips: 1 }], other_projects: 0 };
    api.emptyClipTrash.mockImplementation(async (_ids: string[] | undefined, remove: boolean) => {
      if (!remove) throw new ApiError(409, "in projects", "in_projects", usage);
      return { deleted: 1 };
    });
    renderPage();
    fireEvent.click(await screen.findByRole("checkbox", { name: "A002.mov" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete for good" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete for good?" });
    expect(dialog.textContent).toMatch(/1 clip will be deleted for good/);
    expect(dialog.textContent).toMatch(/original files on your drives aren't touched/);
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete 1 clip for good" }));
    expect(await within(dialog).findByText(/Reel: 1 clip/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete and remove from projects" }));
    await waitFor(() => expect(api.emptyClipTrash).toHaveBeenLastCalledWith(["a2"], true));
  });

  it("empties the whole trash", async () => {
    api.emptyClipTrash.mockResolvedValue({ deleted: 2 });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Empty trash (2)" }));
    const dialog = await screen.findByRole("dialog", { name: "Empty the trash?" });
    expect(dialog.textContent).toMatch(/Every clip in the trash \(2 clips\)/);
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete 2 clips for good" }));
    await waitFor(() => expect(api.emptyClipTrash).toHaveBeenCalledWith(undefined, false));
  });

  it("editors restore but don't delete for good", async () => {
    api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "editor" });
    renderPage();
    fireEvent.click(await screen.findByRole("checkbox", { name: "A001.mov" }));
    expect(screen.getByRole("button", { name: "Restore" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Delete for good" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Empty trash/ })).toBeNull();
  });

  it("viewers only look", async () => {
    api.getCurrentUser.mockResolvedValue({ email: "a@b.c", role: "viewer" });
    renderPage();
    expect(await screen.findByText("A001.mov")).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("says when the trash is emptied by hand only", async () => {
    api.listTrash.mockResolvedValue({ items: [{ ...clip("a1", "x.mov"), expires_at: null }], next_cursor: null, total: 1, trash_days: null });
    renderPage();
    expect(await screen.findByText("Kept until emptied")).toBeTruthy();
    expect(screen.getByText(/They stay until someone deletes them for good/)).toBeTruthy();
  });

  it("filters by library and says when it's empty", async () => {
    api.listTrash.mockResolvedValue({ items: [], next_cursor: null, total: 0, trash_days: 30 });
    renderPage();
    expect(await screen.findByText("The trash is empty.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Library"), { target: { value: "lib_1" } });
    await waitFor(() => expect(api.listTrash).toHaveBeenLastCalledWith(expect.objectContaining({ libraryId: "lib_1" })));
  });

  it("pages through a long trash", async () => {
    api.listTrash
      .mockResolvedValueOnce({ items: [clip("a1", "1.mov")], next_cursor: "c1", total: 2, trash_days: 30 })
      .mockResolvedValueOnce({ items: [clip("a2", "2.mov")], next_cursor: null, total: 2, trash_days: 30 });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Show more" }));
    expect(await screen.findByText("2.mov")).toBeTruthy();
    expect(api.listTrash).toHaveBeenLastCalledWith(expect.objectContaining({ after: "c1" }));
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
  });
});
