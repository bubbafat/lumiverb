import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import ProjectsPage from "./ProjectsPage";

const api = vi.hoisted(() => ({
  listProjects: vi.fn(),
  trashProject: vi.fn(),
  restoreProject: vi.fn(),
  restoreProjectClips: vi.fn(),
  emptyProjectTrash: vi.fn(),
}));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));
vi.mock("../api/useAuthenticatedImage", () => ({
  useAuthenticatedImage: () => ({ url: null, isLoading: false, error: null }),
}));

function project(overrides: Record<string, unknown> = {}) {
  return {
    project_id: "prj_1",
    name: "Customer Video 123",
    description: null,
    cover_asset_id: null,
    owner_user_id: "usr_1",
    visibility: "private",
    ownership: "own",
    sort_order: "manual",
    type: "static",
    saved_query: null,
    asset_count: 3,
    trashed_asset_count: 0,
    missing_asset_count: 0,
    created_at: "2026-10-07T00:00:00Z",
    updated_at: "2026-10-07T00:00:00Z",
    status: "active",
    deleted_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  api.trashProject.mockResolvedValue(undefined);
  api.restoreProject.mockResolvedValue(undefined);
  api.restoreProjectClips.mockResolvedValue({ restored: 2, missing: 0 });
  api.emptyProjectTrash.mockResolvedValue({ deleted: 1 });
  api.listProjects.mockResolvedValue([
    {
      project_id: "prj_1",
      name: "Customer Video 123",
      description: null,
      cover_asset_id: null,
      owner_user_id: "usr_1",
      visibility: "private",
      ownership: "own",
      sort_order: "manual",
      type: "static",
      saved_query: null,
      asset_count: 3,
      created_at: "2026-10-07T00:00:00Z",
      updated_at: "2026-10-07T00:00:00Z",
      status: "active",
    },
  ]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function inTrash(overrides: Record<string, unknown> = {}) {
  api.listProjects.mockImplementation(async (view: string) =>
    view === "trashed" ? [project({ deleted_at: "2026-10-07T01:00:00Z", ...overrides })] : [],
  );
}

async function openTrash() {
  fireEvent.click(await screen.findByRole("button", { name: "Trash" }));
  return screen.findByText("Customer Video 123");
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ProjectsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ProjectsPage", () => {
  it("hides card actions until hover only where hovering exists", async () => {
    // A phone can't hover: hidden-until-hover buttons are invisible there.
    renderPage();
    const archive = await screen.findByRole("button", { name: "Archive" });
    const actions = archive.parentElement!;
    expect(actions.className.split(/\s+/)).not.toContain("opacity-0");
    expect(actions.className).toContain("[@media(hover:hover)]:opacity-0");
  });

  it("puts card actions under the name, not beside it", async () => {
    // Beside the name, two buttons left a phone card room for one letter and
    // a desktop card about 50px, even while they were hidden.
    renderPage();
    const links = await screen.findAllByRole("link", { name: "Customer Video 123" });
    const row = links[links.length - 1].parentElement!.parentElement!;
    expect(row.className.split(/\s+/)).toContain("flex-col");
    expect(row.className).not.toMatch(/flex-row/);
  });

  it("moves a project to the trash at once, with an undo", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.trashProject).toHaveBeenCalledWith("prj_1"));
    const notice = await screen.findByRole("status");
    expect(notice.textContent).toContain("Customer Video 123");
    fireEvent.click(within(notice).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(api.restoreProject).toHaveBeenCalledWith("prj_1"));
  });

  it("lists the trash on its own tab", async () => {
    inTrash();
    renderPage();
    await openTrash();
    expect(api.listProjects).toHaveBeenCalledWith("trashed");
    expect(screen.getByRole("button", { name: "Restore" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Delete forever" })).toBeTruthy();
  });

  it("asks before deleting a project forever", async () => {
    inTrash();
    renderPage();
    await openTrash();
    fireEvent.click(screen.getByRole("button", { name: "Delete forever" }));
    expect(api.emptyProjectTrash).not.toHaveBeenCalled();
    expect(screen.getByText(/Delete for good\? Its clips stay in your library/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(api.emptyProjectTrash).toHaveBeenCalledWith(["prj_1"]));
  });

  it("restores a project with no trashed clips without asking", async () => {
    inTrash();
    renderPage();
    await openTrash();
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(api.restoreProject).toHaveBeenCalledWith("prj_1"));
    expect(api.restoreProjectClips).not.toHaveBeenCalled();
  });

  it("offers to restore a project's trashed clips with it", async () => {
    inTrash({ trashed_asset_count: 2, missing_asset_count: 1 });
    renderPage();
    await openTrash();
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toMatch(/2 clips in the trash/);
    expect(dialog.textContent).toMatch(/1 clip is missing from disk/);
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore project and clips" }));
    await waitFor(() => expect(api.restoreProjectClips).toHaveBeenCalledWith("prj_1"));
    expect(api.restoreProject).toHaveBeenCalledWith("prj_1");
  });

  it("can restore just the project", async () => {
    inTrash({ trashed_asset_count: 2 });
    renderPage();
    await openTrash();
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Project only" }));
    await waitFor(() => expect(api.restoreProject).toHaveBeenCalledWith("prj_1"));
    expect(api.restoreProjectClips).not.toHaveBeenCalled();
  });

  it("empties the whole trash after asking", async () => {
    inTrash();
    renderPage();
    await openTrash();
    fireEvent.click(screen.getByRole("button", { name: "Empty trash" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(api.emptyProjectTrash).toHaveBeenCalledWith(undefined));
  });
});
