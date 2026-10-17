import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import LibraryScansSection from "./LibraryScansSection";

const fetchMock = vi.fn();
let role = "admin";
let paused = false;
let asked: string | null = null;
let checkedIn: Record<string, unknown> | null = null;
const sent: { method: string; url: string; body: unknown }[] = [];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    sent.push({ method, url, body });
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    if (path === "/me") return json({ email: "a@b.c", role });
    if (path === "/producers/queue") {
      return json({ live: true, at: null, running: {}, waiting: {}, pools: {}, gpu_hold: 0, state: "running",
                    switches: [], libraries: [{ library_id: "lib_v", name: "Video", reader: "server", paused,
                                                paused_at: null, paused_by: null, waits: [] }] });
    }
    if (path === "/libraries" && method === "GET") {
      return json([{ library_id: "lib_v", name: "Video", root_path: "/v", last_scan_at: null, status: "active",
                     is_public: false, reader: "server", scans_paused: paused, scan_requested_at: asked,
                     checked_in: checkedIn }]);
    }
    if (path === "/libraries/lib_v/scan-now" && method === "POST") {
      asked = new Date().toISOString();
      return new Response(null, { status: 204 });
    }
    const scans = path.match(/^\/libraries\/scans\/(pause|resume)$/);
    if (scans && method === "POST") {
      paused = scans[1] === "pause";
      return new Response(null, { status: 204 });
    }
    return json({}, 404);
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  role = "admin";
  paused = false;
  asked = null;
  checkedIn = null;
  sent.length = 0;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LibraryScansSection libraryId="lib_v" />
    </QueryClientProvider>,
  );
}

describe("LibraryScansSection", () => {
  it("says who reads the library, and an admin pauses and resumes its scans alone", async () => {
    renderSection();
    const section = await screen.findByLabelText("Scans");
    expect(section.textContent).toContain("This server reads its files. Its scans find new and changed files.");
    fireEvent.click(await screen.findByRole("button", { name: "Pause scans" }));
    await waitFor(() => expect(screen.getByLabelText("Scans").textContent).toContain(
      "Its scans are paused: no new or changed files are found in it. Everything else goes on."));
    fireEvent.click(screen.getByRole("button", { name: "Resume scans" }));
    await screen.findByRole("button", { name: "Pause scans" });
    expect(sent.filter((r) => r.method === "POST").map((r) => [new URL(r.url, "http://x").pathname, r.body])).toEqual([
      ["/v1/libraries/scans/pause", { library_ids: ["lib_v"] }],
      ["/v1/libraries/scans/resume", { library_ids: ["lib_v"] }]]);
  });

  it("shows anyone else the state, with nothing to flip", async () => {
    role = "editor";
    paused = true;
    renderSection();
    await waitFor(() => expect(screen.getByLabelText("Scans").textContent).toContain("Its scans are paused"));
    await screen.findByText(/This server reads its files/);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("says when its reader last checked in, and how it was", async () => {
    const ago = (ms: number) => new Date(Date.now() - ms).toISOString();
    checkedIn = { client: "this server", version: "0.1.59", state: "scanning", waiting_files: 0, waiting_bytes: 0,
                  last_full_scan_at: ago(3 * 60_000), checked_in_at: ago(60_000) };
    renderSection();
    await waitFor(() => expect(screen.getByLabelText("Scans").textContent).toContain(
      "This server checked in 1 minute ago: scanning now. Last full scan 3 minutes ago."));
  });

  it("says when a reader hasn't checked in for a while", async () => {
    checkedIn = { client: "Robert's Mac Studio", version: "0.1.59", state: "idle", waiting_files: 0, waiting_bytes: 0,
                  last_full_scan_at: null, checked_in_at: new Date(Date.now() - 3 * 3600_000).toISOString() };
    renderSection();
    await waitFor(() => expect(screen.getByLabelText("Scans").textContent).toContain(
      "Robert's Mac Studio hasn't checked in since 3 hours ago."));
  });

  it("lets an editor ask for a scan of the whole library, once", async () => {
    role = "editor";
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Scan now" }));
    const done = await screen.findByRole("button", { name: "Scan asked for" });
    expect((done as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByLabelText("Scans").textContent).toContain(
      "A scan of the whole library is asked for: it starts within a minute.");
    expect(sent.filter((r) => r.method === "POST").map((r) => new URL(r.url, "http://x").pathname)).toEqual([
      "/v1/libraries/lib_v/scan-now"]);
  });

  it("offers no scan to a viewer, nor while its scans are paused", async () => {
    role = "viewer";
    renderSection();
    await screen.findByText(/This server reads its files/);
    await waitFor(() => expect(sent.some((r) => r.url.includes("/libraries"))).toBe(true));
    expect(screen.queryByRole("button")).toBeNull();
  });
});
