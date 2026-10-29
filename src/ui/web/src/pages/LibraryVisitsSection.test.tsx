/** A public library's visits (ADR-025): its admins', while it's public. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import LibraryVisitsSection from "./LibraryVisitsSection";

const api = vi.hoisted(() => ({ getCurrentUser: vi.fn(), getLibrary: vi.fn(), getLibraryVisits: vi.fn() }));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));

const none = { visitors: 0, times: 0 };

beforeEach(() => {
  api.getCurrentUser.mockResolvedValue({ role: "editor" });
  api.getLibraryVisits.mockResolvedValue({
    since: "2026-10-01", days: [], clips: [], sources: [], bots: 0,
    totals: { view: { visitors: 2, times: 2 }, open: none, play: none, export: none, demo: none },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><LibraryVisitsSection libraryId="lib_1" /></QueryClientProvider>);
}

describe("a library's visits", () => {
  it("are its admin's while it's public", async () => {
    api.getLibrary.mockResolvedValue({ library_id: "lib_1", name: "Photos", is_public: true, role: "admin" });
    show();
    expect(await screen.findByText("Page views")).toBeTruthy();
    expect(api.getLibraryVisits).toHaveBeenCalledWith("lib_1", 30);
  });

  it("aren't shown to its editors, nor while it's private", async () => {
    api.getLibrary.mockResolvedValue({ library_id: "lib_1", name: "Photos", is_public: true, role: "editor" });
    show();
    await vi.waitFor(() => expect(api.getLibrary).toHaveBeenCalled());
    expect(screen.queryByRole("generic", { name: "Visits" })).toBeNull();
    cleanup();
    api.getLibrary.mockResolvedValue({ library_id: "lib_1", name: "Photos", is_public: false, role: "admin" });
    show();
    await vi.waitFor(() => expect(api.getLibrary).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("Visits")).toBeNull();
    expect(api.getLibraryVisits).not.toHaveBeenCalled();
  });
});
