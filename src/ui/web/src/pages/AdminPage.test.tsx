import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ApiError, type HealthRow, type SystemHealth } from "../api/client";
import AdminPage from "./AdminPage";

const api = vi.hoisted(() => ({
  listLibraries: vi.fn(),
  getSystemHealth: vi.fn(),
  getApiKey: vi.fn(() => "key"),
}));
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  ...api,
}));

function row(key: HealthRow["key"], title: string, over: Partial<HealthRow> = {}): HealthRow {
  return { key, title, state: "green", reason: `${title} is fine.`, link: null, checked_at: null, ...over };
}

function health(over: Partial<SystemHealth> = {}): SystemHealth {
  return {
    state: "green",
    rows: [
      row("website", "Website"),
      row("processing", "Processing", { reason: "Running.", link: "/settings/processing" }),
      row("ai", "AI machines", { link: "/settings/ai" }),
      row("search", "Search"),
      row("storage", "Storage", { link: "/libraries" }),
      row("disk", "Disk"),
    ],
    libraries: [
      { library_id: "lib_1", name: "Photos", reachable: true },
      { library_id: "lib_2", name: "Footage", reachable: false },
      { library_id: "lib_3", name: "Archive", reachable: null },
    ],
    ...over,
  };
}

beforeEach(() => {
  api.getSystemHealth.mockResolvedValue(health());
  api.listLibraries.mockResolvedValue([
    { library_id: "lib_1", name: "Photos", root_path: "/p", status: "active", is_public: false, last_scan_at: null },
    { library_id: "lib_2", name: "Footage", root_path: "/f", status: "active", is_public: false, last_scan_at: null },
    { library_id: "lib_3", name: "Archive", root_path: "/a", status: "active", is_public: false, last_scan_at: null },
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
        <AdminPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AdminPage", () => {
  it("shows the six rows, each with its state and reason", async () => {
    renderPage();
    const list = await screen.findByRole("list", { name: "System health" });
    const items = within(list).getAllByRole("listitem");
    expect(items.map((i) => within(i).getByRole("heading").textContent)).toEqual(
      ["Website", "Processing", "AI machines", "Search", "Storage", "Disk"]);
    expect(within(items[1]).getByText("Running.")).toBeTruthy();
    expect(within(items[1]).getByRole("img", { name: "OK" })).toBeTruthy();
  });

  it("links a row to where it's fixed", async () => {
    api.getSystemHealth.mockResolvedValue(health({
      state: "red",
      rows: [
        row("website", "Website"),
        row("processing", "Processing", { state: "yellow", reason: "Partly paused: scans.", link: "/settings/processing" }),
        row("ai", "AI machines", { state: "red", reason: "No machine can do transcripts: work waits.", link: "/settings/ai" }),
        row("storage", "Storage", { state: "red", reason: "Can't reach Footage.", link: "/libraries/lib_2/settings" }),
      ],
    }));
    renderPage();
    const ai = (await screen.findByText("No machine can do transcripts: work waits.")).closest("li")!;
    expect(within(ai).getByRole("img", { name: "Not working" })).toBeTruthy();
    expect(within(ai).getByRole("link").getAttribute("href")).toBe("/settings/ai");
    const processing = screen.getByText("Partly paused: scans.").closest("li")!;
    expect(within(processing).getByRole("img", { name: "Needs a look" })).toBeTruthy();
    const storage = screen.getByText("Can't reach Footage.").closest("li")!;
    expect(within(storage).getByRole("link").getAttribute("href")).toBe("/libraries/lib_2/settings");
  });

  it("says the website is down when the API doesn't answer", async () => {
    api.getSystemHealth.mockRejectedValue(new ApiError(502, "Bad gateway"));
    renderPage();
    const website = (await screen.findByText(/The API isn't answering/)).closest("li")!;
    expect(within(website).getByRole("img", { name: "Not working" })).toBeTruthy();
  });

  it("dots each library by whether its storage can be reached", async () => {
    renderPage();
    const libs = await screen.findByRole("list", { name: "Libraries" });
    const dot = (name: string) => within(within(libs).getByText(name).closest("li")!).getByRole("img");
    expect(dot("Photos").getAttribute("aria-label")).toBe("Reachable");
    expect(dot("Footage").getAttribute("aria-label")).toBe("Can't be reached");
    expect(dot("Archive").getAttribute("aria-label")).toBe("Not checked lately");
  });

  it("keeps the link to manage users", async () => {
    renderPage();
    expect((await screen.findByRole("link", { name: /Manage users/ })).getAttribute("href")).toBe("/admin/users");
  });
});
