import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import ProjectsPage from "./ProjectsPage";

const api = vi.hoisted(() => ({ listProjects: vi.fn() }));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));
vi.mock("../api/useAuthenticatedImage", () => ({
  useAuthenticatedImage: () => ({ url: null, isLoading: false, error: null }),
}));

beforeEach(() => {
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

afterEach(cleanup);

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

  it("puts card actions under the name on small screens", async () => {
    // Beside the name, two buttons left a phone-width card room for one letter.
    renderPage();
    const name = await screen.findByRole("link", { name: "Customer Video 123" });
    const row = name.parentElement!.parentElement!;
    expect(row.className.split(/\s+/)).toContain("flex-col");
    expect(row.className).toContain("sm:flex-row");
  });
});
