import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ApiError } from "../api/client";
import LibrariesPage from "./LibrariesPage";

const api = vi.hoisted(() => ({ listLibraries: vi.fn(), emptyTrash: vi.fn(), deleteLibrary: vi.fn() }));
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
    { library_id: "lib_1", name: "Old card", root_path: "/old", status: "trashed", is_public: false, last_scan_at: null },
  ]);
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
    await waitFor(() => expect(api.emptyTrash).toHaveBeenCalledWith(false));
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
    await waitFor(() => expect(api.emptyTrash).toHaveBeenLastCalledWith(true));
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

describe("LibrariesPage delete with archived clips", () => {
  beforeEach(() => {
    api.listLibraries.mockResolvedValue([
      { library_id: "lib_2", name: "Media", root_path: "/media", status: "active", is_public: false, last_scan_at: null },
    ]);
  });

  async function startDelete() {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
  }

  const archived = () => new ApiError(409, "archived", "archived_clips", { archived_clips: 3 });

  it("asks what happens to archived clips, and deletes them for good when told to", async () => {
    api.deleteLibrary.mockImplementation(async (_id: string, opts?: { archived?: string }) => {
      if (!opts?.archived) throw archived();
    });
    await startDelete();
    await screen.findByText(/3 clips in Media are archived/);
    fireEvent.click(screen.getByRole("button", { name: "Delete them for good" }));
    await waitFor(() => expect(api.deleteLibrary).toHaveBeenLastCalledWith("lib_2", { archived: "delete" }));
  });

  it("keeps them with the library when told to", async () => {
    api.deleteLibrary.mockImplementation(async (_id: string, opts?: { archived?: string }) => {
      if (!opts?.archived) throw archived();
    });
    await startDelete();
    fireEvent.click(await screen.findByRole("button", { name: "Keep them" }));
    await waitFor(() => expect(api.deleteLibrary).toHaveBeenLastCalledWith("lib_2", { archived: "keep" }));
  });

  it("asks again when deleting them would take them out of projects", async () => {
    api.deleteLibrary.mockImplementation(async (_id: string, opts?: { archived?: string; removeFromProjects?: boolean }) => {
      if (!opts?.archived) throw archived();
      if (opts.archived === "delete" && !opts.removeFromProjects) {
        throw new ApiError(409, "in projects", "in_projects", usage as unknown as Record<string, unknown>);
      }
    });
    await startDelete();
    fireEvent.click(await screen.findByRole("button", { name: "Delete them for good" }));
    await screen.findByText(/in projects/i);
    fireEvent.click(screen.getByRole("button", { name: "Delete anyway" }));
    await waitFor(() =>
      expect(api.deleteLibrary).toHaveBeenLastCalledWith("lib_2", { archived: "delete", removeFromProjects: true }),
    );
  });

  it("a library without archived clips deletes straight away", async () => {
    api.deleteLibrary.mockResolvedValue(undefined);
    await startDelete();
    await waitFor(() => expect(api.deleteLibrary).toHaveBeenCalledWith("lib_2", undefined));
    expect(screen.queryByText(/archived/)).toBeNull();
  });
});
