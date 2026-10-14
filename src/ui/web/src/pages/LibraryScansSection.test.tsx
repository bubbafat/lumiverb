import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import LibraryScansSection from "./LibraryScansSection";

const fetchMock = vi.fn();
let role = "admin";
let paused = false;
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
});
