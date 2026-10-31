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
  getSetupProgress: vi.fn(),
  setSetupProgress: vi.fn(),
  getCurrentUser: vi.fn(),
  getAccountVisits: vi.fn(),
  getSiteVisits: vi.fn(),
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
      { library_id: "lib_1", name: "Photos", reachable: true, seen_at: new Date().toISOString() },
      { library_id: "lib_2", name: "Footage", reachable: false, seen_at: null },
      { library_id: "lib_3", name: "Archive", reachable: null,
        seen_at: new Date(Date.now() - 2 * 3600_000 - 60_000).toISOString() },
    ],
    ...over,
  };
}

const STEPS = ["storage", "network", "find", "ai", "people", "library"];

beforeEach(() => {
  api.getAccountVisits.mockResolvedValue({ since: "2026-10-01", places: [] });
  api.getSiteVisits.mockResolvedValue({ logged: false, days: [] });
  api.getCurrentUser.mockResolvedValue({ user_id: "usr_1", email: "a@b.c", role: "admin", server_admin: true });
  api.getSetupProgress.mockResolvedValue({ steps: STEPS, done: STEPS, dismissed: false, finished: true });
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

  it("says which Lumiverb is running", async () => {
    api.getSystemHealth.mockResolvedValue(health({ version: "0.1.6" }));
    renderPage();
    expect(await screen.findByText("Lumiverb 0.1.6")).toBeTruthy();
    cleanup();
    api.getSystemHealth.mockResolvedValue(health({ version: "v0.1.6-3-g1a2b3c4" }));  // a checkout past a tag
    renderPage();
    expect(await screen.findByText("Lumiverb 0.1.6-3-g1a2b3c4")).toBeTruthy();
    cleanup();
    api.getSystemHealth.mockResolvedValue(health());  // an older server leaves it out
    renderPage();
    await screen.findByRole("list", { name: "System health" });
    expect(screen.queryByText(/^Lumiverb /)).toBeNull();
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
    expect(within(ai).getAllByRole("link").map((a) => a.getAttribute("href"))).toContain("/settings/ai");
    const processing = screen.getByText("Partly paused: scans.").closest("li")!;
    expect(within(processing).getByRole("img", { name: "Needs a look" })).toBeTruthy();
    const storage = screen.getByText("Can't reach Footage.").closest("li")!;
    expect(within(storage).getAllByRole("link")[0].getAttribute("href")).toBe("/libraries/lib_2/settings");
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
    expect(dot("Archive").getAttribute("aria-label")).toBe("Not checked lately · last seen 2 hours ago");
  });

  it("says a device's library by its check-ins, never as out of this server's reach", async () => {
    const checkedIn = (minutes: number) => ({
      client: "studio.local", version: "0.1.68", state: "idle" as const, waiting_files: 0, waiting_bytes: 0,
      last_full_scan_at: null, checked_in_at: new Date(Date.now() - minutes * 60_000).toISOString() });
    api.listLibraries.mockResolvedValue([
      { library_id: "lib_4", name: "Trips", root_path: "/Users/robert/Trips", status: "active", is_public: false,
        last_scan_at: null, reader: "dev_1", reader_name: "Studio Mac", checked_in: checkedIn(1) },
      { library_id: "lib_5", name: "Old drive", root_path: "/Volumes/Old", status: "active", is_public: false,
        last_scan_at: null, reader: "dev_2", reader_name: "Garage PC", checked_in: checkedIn(3 * 24 * 60) },
    ]);
    renderPage();
    const libs = await screen.findByRole("list", { name: "Libraries" });
    const row = (name: string) => within(libs).getByText(name).closest("li")!;
    expect(within(row("Trips")).getByRole("img").getAttribute("aria-label"))
      .toBe("Read by Studio Mac · checked in 1 minute ago");
    expect(within(row("Old drive")).getByText("Read by Garage PC · not heard from in 3 days")).toBeTruthy();
    expect(within(libs).queryByRole("img", { name: /Can't be reached|Not checked lately/ })).toBeNull();
  });

  it("says a library not checked lately was never seen when it wasn't", async () => {
    api.getSystemHealth.mockResolvedValue(health({
      libraries: [{ library_id: "lib_3", name: "Archive", reachable: null, seen_at: null }],
    }));
    renderPage();
    const libs = await screen.findByRole("list", { name: "Libraries" });
    const dot = within(within(libs).getByText("Archive").closest("li")!).getByRole("img");
    expect(dot.getAttribute("aria-label")).toBe("Not checked lately · never seen");
  });

  it("is for admins: anyone else at its address is told so, with nothing of the system shown", async () => {
    api.getSystemHealth.mockRejectedValue(new ApiError(403, "Admin API key required"));
    renderPage();
    expect(await screen.findByText("Admin access required.")).toBeTruthy();
    expect(screen.queryByRole("list", { name: "System health" })).toBeNull();
    expect(screen.queryByText(/The API isn't answering/)).toBeNull();
  });

  it("keeps the link to manage users", async () => {
    renderPage();
    expect((await screen.findByRole("link", { name: /Manage users/ })).getAttribute("href")).toBe("/admin/users");
  });

  it("links to the service's log", async () => {
    renderPage();
    expect((await screen.findByRole("link", { name: /Logs/ })).getAttribute("href")).toBe("/admin/logs");
  });

  it("links the log, the install's, only for a server admin", async () => {
    api.getCurrentUser.mockResolvedValue({ user_id: "usr_2", email: "b@b.c", role: "admin", server_admin: false });
    renderPage();
    await screen.findByRole("link", { name: /Manage users/ });
    await screen.findByText("Photos");
    expect(screen.queryByRole("link", { name: /Logs/ })).toBeNull();
  });
});


describe("AdminPage setting up", () => {
  it("offers to carry on setting up until it's finished, or put away", async () => {
    api.getSetupProgress.mockResolvedValue({ steps: STEPS, done: ["storage", "network"], dismissed: false, finished: false });
    api.setSetupProgress.mockResolvedValue({ steps: STEPS, done: ["storage", "network"], dismissed: true, finished: false });
    renderPage();
    const card = await screen.findByRole("region", { name: "Setting up" });
    expect(card.textContent).toContain("Setting up: 2 of 6 done");
    expect(within(card).getByRole("link", { name: "Continue setting up" }).getAttribute("href")).toBe("/welcome");
    within(card).getByRole("button", { name: "Don't show this again" }).click();
    await vi.waitFor(() => expect(screen.queryByRole("region", { name: "Setting up" })).toBeNull());
    expect(api.setSetupProgress).toHaveBeenCalledWith({ dismissed: true });
  });

  it("isn't there once it's finished", async () => {
    renderPage();
    await screen.findByRole("list", { name: "System health" });
    expect(screen.queryByRole("region", { name: "Setting up" })).toBeNull();
  });
});


describe("AdminPage's visits (ADR-025)", () => {
  const none = { visitors: 0, times: 0 };
  it("lists every place with visits and the demos, each linked to its page", async () => {
    api.getAccountVisits.mockResolvedValue({ since: "2026-10-01", places: [
      { place: "project", place_id: "prj_1", name: "Beach day", bots: 0, view: { visitors: 3, times: 4 },
        open: { visitors: 2, times: 5 }, play: none, export: none, demo: none },
      { place: "account", place_id: "ten_1", name: null, bots: 0, view: none, open: none, play: none, export: none,
        demo: { visitors: 2, times: 2 } },
    ] });
    renderPage();
    const list = await screen.findByRole("list", { name: "Visits" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows[0].textContent).toContain("Beach day");
    expect(rows[0].textContent).toContain("3 visitors, 4 views · 5 opened");
    expect(within(rows[0]).getByRole("link").getAttribute("href")).toBe("/projects/prj_1");
    expect(rows[1].textContent).toContain("Demo");
    expect(rows[1].textContent).toContain("2 demos started");
  });

  it("says when there are none", async () => {
    renderPage();
    expect(await screen.findByText("No visits in the last 30 days.")).toBeTruthy();
  });

  it("shows a server admin who else looked at the server's sites, a day at a time", async () => {
    api.getSiteVisits.mockResolvedValue({
      logged: true,
      days: [
        { day: "2026-10-30", browsers: { "try.example.org": 3, "example.org": 2 }, sources: { "reddit.com": 1 },
          people: [{ first_at: "2026-10-30T02:16:00Z", sites: ["example.org", "try.example.org"],
                     device: "Mac Chrome", why: ["tried the demo x1", "from reddit.com"] }] },
        { day: "2026-10-29", browsers: { "try.example.org": 1, "example.org": 0 }, sources: { "reddit.com": 1 },
          people: [{ first_at: "2026-10-29T15:10:00Z", sites: ["docs.example.org"], device: "Mac Chrome",
                     why: ["from reddit.com", "read 6 docs.example.org pages"] }] },
      ],
    });
    renderPage();
    const people = await screen.findByRole("list", { name: "Likely people" });
    const items = within(people).getAllByRole("listitem");
    expect(items.map((i) => i.textContent)).toEqual([
      expect.stringContaining("tried the demo x1; from reddit.com"),
      expect.stringContaining("from reddit.com; read 6 docs.example.org pages"),
    ]);
    expect(within(items[0]).getByText("example.org + try.example.org")).toBeTruthy();
    expect(screen.getByText(/2 likely people/)).toBeTruthy();
    expect(screen.getByText(/came from reddit\.com 2/)).toBeTruthy();
    const table = screen.getByRole("table", { name: "Browser-like addresses by day" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell").map((c) => c.textContent))).toEqual([
      ["3", "2", "1"], ["1", "0", "1"]]);
    expect(api.getSiteVisits).toHaveBeenCalledWith(30);
  });

  it("shows no site visits where the server keeps no log of its sites, nor to an account admin", async () => {
    renderPage();
    await screen.findByRole("list", { name: "System health" });
    await vi.waitFor(() => expect(api.getSiteVisits).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByRole("heading", { name: "Site visits" })).toBeNull();

    cleanup();
    api.getCurrentUser.mockResolvedValue({ user_id: "usr_2", email: "b@b.c", role: "admin", server_admin: false });
    api.getSiteVisits.mockResolvedValue({ logged: true, days: [] });
    renderPage();
    await screen.findByRole("list", { name: "System health" });
    expect(screen.queryByRole("heading", { name: "Site visits" })).toBeNull();
    expect(api.getSiteVisits).toHaveBeenCalledTimes(1);  // the account admin's page never asked
  });

  it("says when no day is added up yet", async () => {
    api.getSiteVisits.mockResolvedValue({ logged: true, days: [] });
    renderPage();
    expect(await screen.findByText(/No day added up yet/)).toBeTruthy();
  });
});
