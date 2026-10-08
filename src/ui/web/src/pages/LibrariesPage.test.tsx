import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ApiError } from "../api/client";
import LibrariesPage from "./LibrariesPage";

const api = vi.hoisted(() => ({ listLibraries: vi.fn(), emptyTrash: vi.fn() }));
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

    expect(await within(dialog).findByText(/4 clips from these libraries are in 3 projects/)).toBeTruthy();
    expect(within(dialog).getByText(/Customer Video/)).toBeTruthy();
    expect(within(dialog).getByText(/Old reel/)).toBeTruthy();
    expect(within(dialog).getByText(/1 more you can't see/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete and remove from projects" }));
    await waitFor(() => expect(api.emptyTrash).toHaveBeenLastCalledWith(true));
  });
});
