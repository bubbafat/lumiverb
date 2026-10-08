import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { useClipActions } from "./useClipActions";

const api = vi.hoisted(() => ({
  archiveClips: vi.fn(),
  unarchiveClips: vi.fn(),
  trashClips: vi.fn(),
  restoreClips: vi.fn(),
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
const onDone = vi.fn();

function Harness() {
  const actions = useClipActions(onDone);
  return (
    <div>
      <button type="button" onClick={() => void actions.archive(["a1", "a2"])}>archive</button>
      <button type="button" onClick={() => actions.trash(["a1", "a2"])}>trash</button>
      <button type="button" onClick={() => actions.archiveFolder("lib_1", "Trips/Paris", 1234)}>folder</button>
      {actions.ui}
    </div>
  );
}

beforeEach(() => {
  api.getTenantSettings.mockResolvedValue({ trash_days: 30 });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/** The settings query has answered. */
async function settled() {
  await waitFor(() => expect(api.getTenantSettings).toHaveBeenCalled());
  await new Promise((r) => setTimeout(r, 0));
}

function renderHarness() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Harness />
    </QueryClientProvider>,
  );
}

describe("useClipActions", () => {
  it("promises no date before it knows the trash days", async () => {
    api.getTenantSettings.mockReturnValue(new Promise(() => {}));
    api.trashClips.mockResolvedValue({ trashed: ["a1"], not_found: [] });
    renderHarness();
    fireEvent.click(screen.getByRole("button", { name: "trash" }));
    expect((await screen.findByRole("status")).textContent).not.toMatch(/Deleted for good/);
  });

  it("archives, says so, and undoes it", async () => {
    api.archiveClips.mockResolvedValue({ archived: ["a1", "a2"], skipped: [] });
    api.unarchiveClips.mockResolvedValue({ unarchived: ["a1", "a2"], skipped: [] });
    renderHarness();
    fireEvent.click(screen.getByRole("button", { name: "archive" }));
    const status = await screen.findByRole("status");
    expect(status.textContent).toMatch(/Archived 2 clips\./);
    expect(api.archiveClips).toHaveBeenCalledWith({ asset_ids: ["a1", "a2"] });
    expect(onDone).toHaveBeenCalled();
    fireEvent.click(within(status).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(api.unarchiveClips).toHaveBeenCalledWith({ asset_ids: ["a1", "a2"] }));
    expect((await screen.findByRole("status")).textContent).toMatch(/Undone\./);
  });

  it("moves to the trash, says when it's deleted for good, and undoes it", async () => {
    api.trashClips.mockResolvedValue({ trashed: ["a1", "a2"], not_found: [] });
    api.restoreClips.mockResolvedValue({ restored: ["a1", "a2"], skipped: [] });
    renderHarness();
    await settled();
    fireEvent.click(screen.getByRole("button", { name: "trash" }));
    const status = await screen.findByRole("status");
    expect(status.textContent).toMatch(/Moved 2 clips to the trash\. Deleted for good in 30 days\./);
    expect(api.trashClips).toHaveBeenCalledWith(["a1", "a2"], false);
    fireEvent.click(within(status).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(api.restoreClips).toHaveBeenCalledWith(["a1", "a2"]));
  });

  it("doesn't promise a date when the trash is emptied by hand only", async () => {
    api.getTenantSettings.mockResolvedValue({ trash_days: null });
    api.trashClips.mockResolvedValue({ trashed: ["a1"], not_found: [] });
    renderHarness();
    await settled();
    fireEvent.click(screen.getByRole("button", { name: "trash" }));
    const status = await screen.findByRole("status");
    expect(status.textContent).toMatch(/Moved 1 clip to the trash\./);
    expect(status.textContent).not.toMatch(/Deleted for good/);
  });

  it("names the projects that use the clips and moves them only when told to", async () => {
    api.trashClips.mockImplementation(async (_ids: string[], remove: boolean) => {
      if (!remove) throw new ApiError(409, "in projects", "in_projects", usage);
      return { trashed: ["a1", "a2"], not_found: [] };
    });
    renderHarness();
    fireEvent.click(screen.getByRole("button", { name: "trash" }));
    const dialog = await screen.findByRole("dialog", { name: "Move to the trash?" });
    expect(dialog.textContent).toMatch(/2 clips are in projects\. In the trash they're hidden there/);
    expect(within(dialog).getByText(/Customer Video: 2 clips/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Move 2 clips to the trash" }));
    await waitFor(() => expect(api.trashClips).toHaveBeenLastCalledWith(["a1", "a2"], true));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("changes nothing when the projects question is cancelled", async () => {
    api.trashClips.mockRejectedValue(new ApiError(409, "in projects", "in_projects", usage));
    renderHarness();
    fireEvent.click(screen.getByRole("button", { name: "trash" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.trashClips).toHaveBeenCalledTimes(1);
    expect(onDone).not.toHaveBeenCalled();
  });

  it("says how many clips a folder holds before archiving it", async () => {
    api.archiveClips.mockResolvedValue({ archived: ["a1"], skipped: [] });
    renderHarness();
    fireEvent.click(screen.getByRole("button", { name: "folder" }));
    const dialog = await screen.findByRole("dialog", { name: "Archive this folder?" });
    expect(dialog.textContent).toMatch(/1,234 clips in Paris and the folders inside it leave browse and search/);
    expect(dialog.textContent).toMatch(/Files added to the folder later show up as usual/);
    expect(api.archiveClips).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive 1,234 clips" }));
    await waitFor(() => expect(api.archiveClips).toHaveBeenCalledWith({ library_id: "lib_1", path: "Trips/Paris" }));
    expect((await screen.findByRole("status")).textContent).toMatch(/Archived 1 clip in Paris\./);
  });

  it("says why when it fails", async () => {
    api.archiveClips.mockRejectedValue(new ApiError(403, "Editor access required", "forbidden"));
    renderHarness();
    fireEvent.click(screen.getByRole("button", { name: "archive" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/Couldn't archive: Editor access required/);
    expect(onDone).not.toHaveBeenCalled();
  });
});
