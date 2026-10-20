import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ProcessingSection, { duration } from "./ProcessingSection";
import type { Producer } from "../../api/client";
import { MemoryRouter } from "react-router-dom";

const fetchMock = vi.fn();
let role = "admin";
let producers: Producer[] = [];
// What the next stop, resume, pause or save answers (then 204).
let answers: Response[] = [];
let failures: unknown[] = [];
let queue: Record<string, unknown> = { live: false, at: null, running: {}, waiting: {}, pools: {}, gpu_hold: 0 };
// What's paused, as the server keeps it: the switches (Upkeep, each producer the scheduler makes), and each
// library's scans as `scans:<library_id>` (ADR-018 phase 1).
let paused = new Set<string>();
// Why a library's scans wait, as the queue says it on the library's row (each names its library_id).
let scanWaits: { library_id: string }[] = [];
const LIBS = [{ library_id: "lib_1", name: "Footage" }, { library_id: "lib_2", name: "Stills" }];
// A library a device reads, when a test adds one (its reader and latest check-in, as the queue says them).
let deviceLibs: Record<string, unknown>[] = [];

/** As the server: each switch, each library's scans, and the state they make (running, partly, paused). */
function switches() {
  const rows = [{ target: "upkeep", title: "Upkeep" },
                ...producers.filter((p) => p.scheduled).map((p) => ({ target: p.artifact, title: p.title }))]
    .map((r) => ({ ...r, paused: paused.has(r.target), paused_at: paused.has(r.target) ? "2026-10-09T10:00:00Z" : null,
                   paused_by: null, waits: [] }));
  const libraries = [...LIBS.map((l) => {
    const on = paused.has(`scans:${l.library_id}`);
    return { ...l, reader: "server", paused: on, paused_at: on ? "2026-10-09T10:00:00Z" : null, paused_by: null,
             waits: scanWaits.filter((w) => w.library_id === l.library_id) };
  }), ...deviceLibs];
  const n = rows.filter((r) => r.paused).length + libraries.filter((l) => l.paused).length;
  const total = rows.length + libraries.length;
  return { switches: rows, libraries, state: n === 0 ? "running" : n === total ? "paused" : "partly" };
}
const sent: { method: string; url: string; body: unknown }[] = [];

function producer(over: Partial<Producer>): Producer {
  return {
    artifact: "lumiverb.image.description", version: "1", title: "Descriptions and tags", media: ["image"],
    uniform: false, settings: { model: "qwen3-vl:8b-instruct", temperature: 0.2 }, settings_hash: "h",
    fields: [
      { key: "model", label: "Model", kind: "text", value: "qwen3-vl:8b-instruct", default: "", minimum: null,
        maximum: null, unit: "", advanced: false, fixed: "Chosen in Settings → AI, for every producer its machines run." },
      { key: "temperature", label: "Temperature", kind: "float", value: 0.2, default: 0.2, minimum: 0, maximum: 2,
        unit: "", advanced: true, fixed: null },
    ],
    counts: { applicable: 10, current: 6, stale: 3, missing: 1, failing: 0 }, redoable: true, why_not: null,
    scheduled: true, redo_stopped: false, redo_stopped_by: null, redo_stopped_at: null,
    paused: false, paused_by: null, paused_at: null, waiting: null, ...over,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

function err(status: number, code: string, message: string, details: unknown = {}) {
  return json({ error: { code, message, details } }, status);
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    sent.push({ method, url, body });
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    if (path === "/me") return json({ email: "a@b.c", role });
    if (path === "/libraries") return json(LIBS.map((l) => ({ ...l, root_path: "/f", status: "active" })));
    const scans = path.match(/^\/libraries\/scans\/(pause|resume)$/);
    if (scans && method === "POST") {
      const next = answers.shift();
      if (next) return next;
      for (const id of body.all ? LIBS.map((l) => l.library_id) : body.library_ids) {
        if (scans[1] === "pause") paused.add(`scans:${id}`);
        else paused.delete(`scans:${id}`);
      }
      return new Response(null, { status: 204 });
    }
    if (path === "/projects") return json({ items: [{ project_id: "col_1", name: "Wedding" }] });
    if (path === "/producers") return json({ producers: producers.map((p) => ({ ...p, paused: paused.has(p.artifact) })) });
    if (path === "/producers/queue") return json({ ...switches(), ...queue });
    if (path === "/producers/failures" && method === "GET") return json({ items: failures, next_cursor: null });
    if (path === "/producers/failures/retry" && method === "POST") {
      const n = body.asset_ids ? body.asset_ids.length : failures.length;
      return json({ retried: n });
    }
    const set = path.match(/^\/producers\/([^/]+)\/settings$/);
    if (set && method === "PUT") {
      const next = answers.shift();
      if (next) return next;
      return json(producers.find((p) => p.artifact === set[1]));
    }
    // A producer's redo: its switch's other scope.
    const m = body?.scope === "redo" ? path.match(/^\/producers\/([^/]+)\/(pause|resume)$/) : null;
    if (m && method === "POST") {
      const next = answers.shift();
      if (next) return next;
      producers = producers.map((p) => (p.artifact === m[1] ? { ...p, redo_stopped: m[2] === "pause" } : p));
      return new Response(null, { status: 204 });
    }
    const below = path.match(/^\/producers\/([^/]+)\/below\/redo$/);
    if (below && method === "POST") return json({ redo: 4, paused: false, redo_stopped: false });
    const all = path.match(/^\/producers\/all\/(pause|resume)$/);
    if (all && method === "POST") {
      const next = answers.shift();
      if (next) return next;
      paused = all[1] === "pause"
        ? new Set([...switches().switches.map((r) => r.target), ...LIBS.map((l) => `scans:${l.library_id}`)])
        : new Set();
      return new Response(null, { status: 204 });
    }
    const one = path.match(/^\/producers\/([^/]+)\/(pause|resume)$/);
    if (one && method === "POST") {
      const next = answers.shift();
      if (next) return next;
      if (one[2] === "pause") paused.add(one[1]);
      else paused.delete(one[1]);
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
  producers = [];
  answers = [];
  failures = [];
  queue = { live: false, at: null, running: {}, waiting: {}, pools: {}, gpu_hold: 0 };
  paused = new Set();
  scanWaits = [];
  sent.length = 0;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return { client, ...render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ProcessingSection />
      </QueryClientProvider>
    </MemoryRouter>,
  ) };
}

async function row(title: string) {
  return (await screen.findByRole("heading", { name: title })).closest("li") as HTMLElement;
}

describe("ProcessingSection", () => {
  it("shows a loading indicator until the first status and counts arrive, and not on a refresh", async () => {
    producers = [producer({})];
    // Hold the queue and the counts until released, as a slow first load does.
    let release: () => void = () => {};
    let gate = new Promise<void>((r) => { release = r; });
    const answer = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
      if (path === "/producers" || path === "/producers/queue") await gate;
      return answer(url, init);
    });
    const { client } = renderSection();
    const loading = await screen.findByRole("status", { name: "Loading" });
    expect(loading.textContent).toContain("Loading…");

    release();
    await row("Descriptions and tags");
    await waitFor(() => expect(screen.getByLabelText("All processing")).toBeTruthy());
    expect(screen.queryByRole("status", { name: "Loading" })).toBeNull();

    // A background refresh keeps what's shown and shows no indicator.
    gate = new Promise<void>((r) => { release = r; });
    const refetch = client.refetchQueries();
    await waitFor(() => expect(client.isFetching()).toBeGreaterThan(0));
    expect(screen.queryByRole("status", { name: "Loading" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Descriptions and tags" })).toBeTruthy();
    release();
    await refetch;
    expect(screen.queryByRole("status", { name: "Loading" })).toBeNull();
  });

  it("lists each producer with its counts, its settings and why one isn't made again yet", async () => {
    producers = [
      producer({}),
      producer({ artifact: "lumiverb.image.likeness", title: "Visual search (CLIP)", uniform: true }),
      producer({ artifact: "lumiverb.video.scenes", title: "Scenes", media: ["video"], redoable: false,
                 why_not: "Finding a video's scenes again isn't built yet." }),
    ];
    renderSection();
    const vision = await row("Descriptions and tags");
    expect(vision.textContent).toContain("6 current · 1 missing · 3 made another way");
    expect(within(vision).getByText(/qwen3-vl:8b-instruct/)).toBeTruthy();
    expect((await row("Visual search (CLIP)")).textContent).toContain("one model for all");
    const scenes = await row("Scenes");
    expect(scenes.textContent).toContain("Not made again yet. Finding a video's scenes again isn't built yet.");
    expect(within(scenes).queryByRole("button", { name: /Stop|Resume/ })).toBeNull();
  });

  it("says why a producer's work waits (its AI job has no machine), linking to Settings → AI", async () => {
    // Robert, Oct 9: removing the last machine for a job never asks; this is where it shows.
    const why = "No machine does descriptions & text: its work waits until an admin adds one in Settings → AI.";
    producers = [
      producer({ waiting: why, waits: [{ reason: "ai_machines", clips: 4, job: "vision", why: "no_machine", message: why }] }),
      producer({ artifact: "lumiverb.image.likeness", title: "Visual search (CLIP)", waits: [] }),
    ];
    renderSection();
    const vision = await row("Descriptions and tags");
    const line = within(within(vision).getByRole("list", { name: "Why Descriptions and tags waits" })).getByRole("listitem");
    expect(line.textContent).toBe(`4 photos wait. ${why}`);
    expect(line.className).toContain("amber");
    expect(within(line).getByRole("link", { name: "Settings → AI" }).getAttribute("href")).toBe("/settings/ai");
    expect(within(await row("Visual search (CLIP)")).queryByRole("list", { name: /Why/ })).toBeNull();
  });

  it("says the stale ones are being redone, with no Upgrade step", async () => {
    producers = [producer({})];
    renderSection();
    const vision = await row("Descriptions and tags");
    expect(vision.textContent).toContain("Remaking 3 clips, after everything else.");
    expect(vision.textContent).toContain("3 made another way");
    expect(screen.queryByRole("button", { name: /Upgrade/ })).toBeNull();
    expect(screen.getByText(/asks whether to\s+make new files only or remake those too/)).toBeTruthy();
  });

  it("says nothing about redoing when nothing is stale", async () => {
    producers = [producer({ counts: { applicable: 10, current: 9, stale: 0, missing: 1, failing: 0 } })];
    renderSection();
    const vision = await row("Descriptions and tags");
    expect(vision.textContent).not.toContain("Remaking");
    expect(within(vision).queryByRole("button", { name: /Stop/ })).toBeNull();
  });

  it("stops remaking, keeping the rest as they are, and offers to remake them (ADR-018 decision 6)", async () => {
    producers = [producer({})];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Stop remaking Descriptions and tags" }));
    await waitFor(() => expect(sent.some((s) => s.url.endsWith("/producers/lumiverb.image.description/pause")
                                               && (s.body as { scope?: string } | undefined)?.scope === "redo")).toBe(true));
    expect(await screen.findByText("3 photos made another way than now stay as they are: an admin chose to keep them."))
      .toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "Remake Descriptions and tags" }));
    await waitFor(() => expect(sent.some((s) => s.url.endsWith("/producers/lumiverb.image.description/resume")
                                               && (s.body as { scope?: string } | undefined)?.scope === "redo")).toBe(true));
    expect(await screen.findByText("Remaking 3 clips, after everything else.")).toBeTruthy();
  });

  it("says why when the server refuses", async () => {
    producers = [producer({})];
    answers = [err(409, "cant_redo", "Descriptions and tags isn't made again yet.")];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Stop remaking Descriptions and tags" }));
    expect((await screen.findByRole("alert")).textContent).toContain("isn't made again yet");
  });

  it("counts one library", async () => {
    producers = [producer({})];
    renderSection();
    await screen.findByRole("option", { name: "Footage" });
    fireEvent.change(screen.getByLabelText("Counts for"), { target: { value: "library:lib_1" } });
    await waitFor(() => expect(sent.some((s) => s.url.includes("/producers?library_id=lib_1"))).toBe(true));
    fireEvent.change(screen.getByLabelText("Counts for"), { target: { value: "project:col_1" } });
    await waitFor(() => expect(sent.some((s) => s.url.includes("/producers?project_id=col_1"))).toBe(true));
  });

  it("shows everyone the counts and the remakes; only admins stop or remake", async () => {
    role = "viewer";
    producers = [producer({}), producer({ artifact: "lumiverb.image.text", title: "Text in images (OCR)", redo_stopped: true })];
    renderSection();
    expect(await screen.findByText("Only admins can pause processing or choose what's remade.")).toBeTruthy();
    expect((await row("Descriptions and tags")).textContent).toContain("Remaking 3 clips");
    expect((await row("Text in images (OCR)")).textContent).toContain("an admin chose to keep them");
    expect(screen.queryByRole("button", { name: /Stop|Remake/ })).toBeNull();
  });

  // Pause switches (Robert, Oct 9): one per processing action; the global one's color is derived from them.
  const state = (name: string) => screen.getByRole("switch", { name }).getAttribute("data-state");
  const flip = (name: string) => fireEvent.click(screen.getByRole("switch", { name }));

  it("shows a green switch for all processing, one for each library's scans, upkeep and each producer (admins)", async () => {
    producers = [producer({}), producer({ artifact: "lumiverb.preview", title: "Proxies and thumbnails", scheduled: false })];
    renderSection();
    await screen.findByRole("switch", { name: "Descriptions and tags" });
    expect(state("All processing")).toBe("running");
    for (const name of ["Scans of Footage", "Scans of Stills", "Upkeep", "Descriptions and tags"]) {
      expect(state(name)).toBe("running");
    }
    expect(screen.getByRole("switch", { name: "All processing" }).textContent).toBe("Running");
    expect(screen.queryByRole("switch", { name: "Proxies and thumbnails" })).toBeNull();  // a library's scans pause it
    const scans = screen.getByRole("list", { name: "Each library's scans" });
    expect(within(scans).getAllByText("Read by this server")).toHaveLength(2);
  });

  it("says which device reads a library, and when it's not heard from", async () => {
    producers = [producer({})];
    deviceLibs = [{
      library_id: "lib_3", name: "Trips", reader: "dev_1", reader_name: "Studio Mac", paused: false, paused_at: null,
      paused_by: null, waits: [], checked_in: { client: "studio.local", version: "0.1.68", state: "scanning",
        waiting_files: 3, waiting_bytes: 0, last_full_scan_at: null,
        checked_in_at: new Date(Date.now() - 2 * 3600_000).toISOString() } }];
    try {
      renderSection();
      const scans = await screen.findByRole("list", { name: "Each library's scans" });
      const line = within(scans).getByText("Read by Studio Mac · not heard from in 2 hours");
      expect(line.className).toContain("text-amber-300");
    } finally {
      deviceLibs = [];
    }
  });

  it("pauses or resumes every library's scans at once: each library's own (admins)", async () => {
    producers = [producer({})];
    renderSection();
    const pauseEvery = await screen.findByRole("button", { name: "Pause every library" });
    const resumeEvery = screen.getByRole("button", { name: "Resume every library" });
    expect(resumeEvery).toHaveProperty("disabled", true);  // nothing to resume
    fireEvent.click(pauseEvery);
    await waitFor(() => expect(state("Scans of Stills")).toBe("paused"));
    expect(state("Scans of Footage")).toBe("paused");
    expect(state("Upkeep")).toBe("running");  // only scans
    expect(state("All processing")).toBe("partly");
    expect(screen.getByRole("button", { name: "Pause every library" })).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("button", { name: "Resume every library" }));
    await waitFor(() => expect(state("All processing")).toBe("running"));
    expect(sent.filter((r) => r.method === "POST").map((r) => [new URL(r.url, "http://x").pathname, r.body])).toEqual([
      ["/v1/libraries/scans/pause", { all: true }], ["/v1/libraries/scans/resume", { all: true }]]);
  });

  it("green → yellow → red → yellow → green → red → green, as the rows are flipped", async () => {
    producers = [producer({}), producer({ artifact: "lumiverb.image.text", title: "Text in images (OCR)" })];
    renderSection();
    await screen.findByRole("switch", { name: "Descriptions and tags" });
    flip("Descriptions and tags");  // one row paused: yellow
    await waitFor(() => expect(state("All processing")).toBe("partly"));
    expect(state("Descriptions and tags")).toBe("paused");
    expect(screen.getByRole("switch", { name: "All processing" }).textContent).toBe("Partly paused");
    flip("All processing");  // from yellow: every row paused, red
    await waitFor(() => expect(state("All processing")).toBe("paused"));
    for (const name of ["Scans of Footage", "Scans of Stills", "Upkeep", "Descriptions and tags", "Text in images (OCR)"]) {
      expect(state(name)).toBe("paused");
    }
    flip("Scans of Footage");  // the rows stay usable while all are paused: one on again, yellow
    await waitFor(() => expect(state("All processing")).toBe("partly"));
    for (const name of ["Upkeep", "Descriptions and tags", "Text in images (OCR)", "Scans of Stills"]) flip(name);
    await waitFor(() => expect(state("All processing")).toBe("running"));  // the rest on, one by one: green
    flip("All processing");
    await waitFor(() => expect(state("All processing")).toBe("paused"));
    flip("All processing");  // from red: every row on, green
    await waitFor(() => expect(state("All processing")).toBe("running"));
    expect(state("Scans of Footage")).toBe("running");
    expect(sent.filter((r) => r.method === "POST").map((r) => new URL(r.url, "http://x").pathname)).toEqual([
      "/v1/producers/lumiverb.image.description/pause", "/v1/producers/all/pause", "/v1/libraries/scans/resume",
      "/v1/producers/upkeep/resume", "/v1/producers/lumiverb.image.description/resume", "/v1/producers/lumiverb.image.text/resume",
      "/v1/libraries/scans/resume", "/v1/producers/all/pause", "/v1/producers/all/resume"]);
    expect(sent.filter((r) => r.url.includes("/libraries/scans/")).map((r) => r.body)).toEqual([
      { library_ids: ["lib_1"] }, { library_ids: ["lib_2"] }]);
  });

  it("a paused producer's row says Paused, and its redo waits", async () => {
    paused = new Set(["lumiverb.image.description"]);
    producers = [producer({})];
    renderSection();
    await waitFor(async () => expect((await row("Descriptions and tags")).textContent).toContain(
      "Remakes 3 clips once it's resumed, after everything else."));
    expect(state("Descriptions and tags")).toBe("paused");
    expect(screen.getByRole("switch", { name: "Descriptions and tags" }).textContent).toBe("Paused");
  });

  it("what scans make says Paused with scans when every library's scans are, or the library shown's", async () => {
    paused = new Set(["scans:lib_1", "scans:lib_2"]);
    producers = [producer({ artifact: "lumiverb.preview", title: "Proxies and thumbnails", scheduled: false })];
    renderSection();
    expect((await row("Proxies and thumbnails")).textContent).toContain("Paused with scans");
  });

  it("one library's scans paused isn't every library's", async () => {
    paused = new Set(["scans:lib_2"]);
    producers = [producer({ artifact: "lumiverb.preview", title: "Proxies and thumbnails", scheduled: false })];
    renderSection();
    await waitFor(() => expect(state("Scans of Stills")).toBe("paused"));
    expect((await row("Proxies and thumbnails")).textContent).not.toContain("Paused with scans");
    fireEvent.change(screen.getByLabelText("Counts for"), { target: { value: "library:lib_2" } });
    await waitFor(async () => expect((await row("Proxies and thumbnails")).textContent).toContain("Paused with scans"));
  });

  it("a library's scans say they keep failing, why, and when they're tried again", async () => {
    scanWaits = [{ reason: "scan_failing", clips: 0, library_id: "lib_1", library_name: "Footage",
                   message: "[unknown]: the API said 500", since: "2026-10-13T13:50:00Z",
                   next_try: "2026-10-13T14:20:00Z" } as { library_id: string }];
    producers = [producer({})];
    renderSection();
    const why = await screen.findByRole("list", { name: "Why scans of Footage wait" });
    expect(why.textContent).toMatch(
      /^Scans of Footage keep failing \(\[unknown\]: the API said 500\); next try at .+\.$/);
    expect(within(why).getByText(/keep failing/).getAttribute("data-wait")).toBe("scan_failing");
    expect(screen.queryByRole("list", { name: "Why scans of Stills wait" })).toBeNull();
    expect(screen.queryByRole("list", { name: "Why Upkeep waits" })).toBeNull();
  });

  it("says why the server refused a pause", async () => {
    producers = [producer({})];
    answers = [err(403, "forbidden", "Admins only.")];
    renderSection();
    fireEvent.click(await screen.findByRole("switch", { name: "Descriptions and tags" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Admins only.");
  });

  it("the switches follow the queue, so a pause from the CLI shows without a reload", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      producers = [producer({})];
      renderSection();
      await screen.findByRole("switch", { name: "Descriptions and tags" });
      paused = new Set(["scans:lib_1", "scans:lib_2", "upkeep", "lumiverb.image.description"]);  // lumiverb pause all, elsewhere
      await vi.advanceTimersByTimeAsync(5_500);
      await waitFor(() => expect(state("All processing")).toBe("paused"));
      paused = new Set();  // lumiverb resume all
      await vi.advanceTimersByTimeAsync(5_500);
      await waitFor(() => expect(state("All processing")).toBe("running"));
    } finally {
      vi.useRealTimers();
    }
  });

  it("the header follows the queue even when the counts can't load", async () => {
    paused = new Set(["upkeep"]);
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) =>
      new URL(url, "http://x").pathname === "/v1/producers" ? err(403, "forbidden", "Editors only.") : base(url, init));
    renderSection();
    await waitFor(() => expect(state("All processing")).toBe("partly"));
    expect(state("Upkeep")).toBe("paused");
  });

  it("shows everyone each switch's state; only admins flip them", async () => {
    role = "viewer";
    paused = new Set(["scans:lib_1"]);
    producers = [producer({})];
    renderSection();
    expect((await screen.findByLabelText("All processing")).textContent).toBe("Partly paused");
    expect(screen.getByLabelText("Scans of Footage").textContent).toBe("Paused");
    expect(screen.getByLabelText("Scans of Stills").textContent).toBe("Running");
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByRole("button", { name: "Pause every library" })).toBeNull();
    expect(screen.getByText("Only admins can pause processing or choose what's remade.")).toBeTruthy();
  });

  it("says one clip right", async () => {
    producers = [producer({ counts: { applicable: 1, current: 0, stale: 1, missing: 0, failing: 0 } })];
    renderSection();
    expect((await row("Descriptions and tags")).textContent).toContain("Remaking 1 clip, after everything else.");
  });

  it("shows why clips fail, and tries them again", async () => {
    producers = [producer({ counts: { applicable: 10, current: 6, stale: 0, missing: 1, failing: 3, given_up: 1 } })];
    failures = [
      { asset_id: "ast_1", artifact: "lumiverb.image.description", title: "Descriptions and tags", rel_path: "Day 1/a.jpg",
        library_id: "lib_1", library_name: "Footage", media_type: "image", error: "the model said nothing",
        attempts: 10, failed_at: "2026-10-09T01:00:00Z", retry_at: null, given_up: true },
      { asset_id: "ast_2", artifact: "lumiverb.image.description", title: "Descriptions and tags", rel_path: "Day 1/b.jpg",
        library_id: "lib_1", library_name: "Footage", media_type: "image", error: "timed out",
        attempts: 2, failed_at: "2026-10-09T01:00:00Z", retry_at: "2026-10-09T01:10:00Z", given_up: false },
    ];
    renderSection();
    const vision = await row("Descriptions and tags");
    expect(vision.textContent).toContain("3 failing (1 given up)");
    fireEvent.click(within(vision).getByRole("button", { name: "Show failures: Descriptions and tags" }));
    expect(await within(vision).findByText("the model said nothing")).toBeTruthy();
    expect(vision.textContent).toContain("Gave up after 10 tries.");
    expect(vision.textContent).toContain("Tried 2 times; next try");
    fireEvent.click(within(vision).getByRole("button", { name: "Try again: Day 1/a.jpg" }));
    expect(await within(vision).findByText("1 clip will be tried again shortly.")).toBeTruthy();
    expect(sent.find((s) => s.url.endsWith("/producers/failures/retry"))!.body).toEqual({
      artifact: "lumiverb.image.description", asset_ids: ["ast_1"] });
    fireEvent.click(within(vision).getByRole("button", { name: "Try all again" }));
    await waitFor(() => expect(sent.filter((s) => s.url.endsWith("/producers/failures/retry")).length).toBe(2));
    // Every failing clip of the producer's: named (all: true), never inferred.
    expect(sent.filter((s) => s.url.endsWith("/producers/failures/retry"))[1].body).toEqual({
      artifact: "lumiverb.image.description", all: true });
  });

  it("lets viewers see failures but not try them again", async () => {
    role = "viewer";
    producers = [producer({ counts: { applicable: 10, current: 9, stale: 0, missing: 0, failing: 1, given_up: 0 } })];
    failures = [{ asset_id: "ast_1", artifact: "lumiverb.image.description", title: "Descriptions and tags", rel_path: "a.jpg",
                  library_id: "lib_1", library_name: "Footage", media_type: "image", error: "no",
                  attempts: 1, failed_at: null, retry_at: "2026-10-09T01:10:00Z", given_up: false }];
    renderSection();
    const vision = await row("Descriptions and tags");
    fireEvent.click(within(vision).getByRole("button", { name: "Show failures: Descriptions and tags" }));
    expect(await within(vision).findByText("no")).toBeTruthy();
    expect(within(vision).queryByRole("button", { name: /Try/ })).toBeNull();
  });

  it("asks for one library's failures when the counts are for it", async () => {
    producers = [producer({ counts: { applicable: 10, current: 9, stale: 0, missing: 0, failing: 1, given_up: 0 } })];
    renderSection();
    await screen.findByRole("option", { name: "Footage" });
    fireEvent.change(screen.getByLabelText("Counts for"), { target: { value: "library:lib_1" } });
    const vision = await row("Descriptions and tags");
    fireEvent.click(await within(vision).findByRole("button", { name: "Show failures: Descriptions and tags" }));
    await waitFor(() => expect(sent.some((s) => s.url.includes("/producers/failures?artifact=lumiverb.image.description&library_id=lib_1"))).toBe(true));
  });

  it("shows what the scheduler is doing now and what's next", async () => {
    queue = { live: true, at: new Date().toISOString(), running: { "lumiverb.video.analysis_proxy": 1, "lumiverb.image.description": 2, "redo:lumiverb.video.transcript": 1 },
              waiting: { "lumiverb.image.description": 40, scan: 1 }, pools: {}, gpu_hold: 1 };
    renderSection();
    const now = await screen.findByLabelText("Now");
    expect(now.textContent).toContain("Now: 1 analysis copy, 2 descriptions, 1 redo of transcripts");
    expect(now.textContent).toContain("Next: 40 descriptions, 1 scan");
    expect(now.textContent).toContain("AI machines sharing it take 1 fewer request meanwhile");
  });

  it("says how long until everything is made, and each job running", async () => {
    queue = { live: true, at: new Date().toISOString(), running: { "lumiverb.video.analysis_proxy": 1, "lumiverb.image.description": 1 }, waiting: {}, pools: {},
              gpu_hold: 0, eta: { producers: { "lumiverb.image.description": 7500, "lumiverb.video.analysis_proxy": 90 }, pools: {}, caught_up: 7500,
              jobs: [{ kind: "lumiverb.video.analysis_proxy", artifact: "lumiverb.video.analysis_proxy", unit: "second", units: 1440, elapsed: 60, left: 250 },
                     { kind: "lumiverb.image.description", artifact: "lumiverb.image.description", unit: "clip", units: 1, elapsed: 4, left: null }] } };
    producers = [producer({}), producer({ artifact: "lumiverb.video.analysis_proxy", title: "Analysis proxies", media: ["video"],
                                          unit: "second" })];
    renderSection();
    const now = await screen.findByLabelText("Now");
    expect(now.textContent).toContain("Caught up in about 2 h 5 min");
    expect(now.textContent).toContain("analysis copy of 24 min of video · about 4 min left");
    expect(now.textContent).toContain("description · time left not known yet");
    expect((await row("Descriptions and tags")).textContent).toContain("about 2 h 5 min left");
    expect((await row("Analysis proxies")).textContent).toContain("about 2 min left");
  });

  it("says when everything is made, and when how long isn't known yet", async () => {
    queue = { live: true, at: new Date().toISOString(), running: {}, waiting: {}, pools: {}, gpu_hold: 0,
              eta: { producers: { "lumiverb.image.description": 0 }, pools: {}, caught_up: 0, jobs: [] } };
    renderSection();
    expect((await screen.findByLabelText("Now")).textContent).toContain("Caught up");
    cleanup();
    queue = { ...(queue as object), eta: { producers: { "lumiverb.image.description": null }, pools: {}, caught_up: null, jobs: [] } };
    renderSection();
    expect((await screen.findByLabelText("Now")).textContent).toContain(
      "How long until everything is made isn't known yet: it's learned from the jobs as they finish.");
  });

  it("says Paused for paused work, and names it", async () => {
    queue = { live: true, at: new Date().toISOString(), running: {}, waiting: {}, pools: {}, gpu_hold: 0,
              eta: { producers: { "lumiverb.image.description": null }, pools: {}, caught_up: null, jobs: [],
                     not_counted: [{ artifact: "lumiverb.image.description", title: "Descriptions and tags", why: "paused" }] } };
    renderSection();
    expect((await screen.findByLabelText("Now")).textContent).toContain(
      "Paused.");
    cleanup();
    queue = { ...(queue as object), eta: { producers: { "lumiverb.image.description": null, "lumiverb.image.text": 1800 }, pools: { vision: 1800 },
              caught_up: 1800, jobs: [],
              not_counted: [{ artifact: "lumiverb.image.description", title: "Descriptions and tags", why: "paused" }] } };
    renderSection();
    expect((await screen.findByLabelText("Now")).textContent).toContain(
      "not counting descriptions and tags (paused)");
  });

  it("names what the headline leaves out, and says a job is late or how short its video is", async () => {
    queue = { live: true, at: new Date().toISOString(), running: { "lumiverb.video.analysis_proxy": 9 }, waiting: {}, pools: {}, gpu_hold: 0,
              eta: { producers: { "lumiverb.video.analysis_proxy": 600 }, pools: { render: 600 }, caught_up: 600,
                     not_counted: [{ artifact: "lumiverb.video.transcript", title: "Transcripts", why: "no_machine" },
                                   { artifact: "lumiverb.image.text", title: "Text in images (OCR)", why: "not_known_yet" }],
                     jobs: [{ kind: "lumiverb.video.analysis_proxy", artifact: "lumiverb.video.analysis_proxy", unit: "second", units: 45, elapsed: 400,
                              left: 0, late: true },
                            ...Array.from({ length: 9 }, () => ({ kind: "lumiverb.video.analysis_proxy", artifact: "lumiverb.video.analysis_proxy",
                              unit: "second", units: 600, elapsed: 10, left: 290, late: false }))] } };
    renderSection();
    const now = await screen.findByLabelText("Now");
    expect(now.textContent).toContain(
      "Caught up in about 10 min, not counting transcripts (no machine doing them now) and text in images (OCR) (not known yet).");
    expect(now.textContent).toContain("analysis copy of 45 s of video · taking longer than usual");
    expect(now.textContent).toContain("and 2 more");
  });

  it("says a producer's time isn't known yet, and caught up not counting what no machine is doing", async () => {
    queue = { live: true, at: new Date().toISOString(), running: {}, waiting: {}, pools: {}, gpu_hold: 0,
              eta: { producers: { "lumiverb.image.text": null, "lumiverb.video.transcript": null }, pools: {}, caught_up: 0, jobs: [],
                     not_counted: [{ artifact: "lumiverb.video.transcript", title: "Transcripts", why: "no_machine" }] } };
    producers = [producer({ artifact: "lumiverb.image.text", title: "Text in images (OCR)" })];
    renderSection();
    expect((await screen.findByLabelText("Now")).textContent).toContain(
      "Caught up, not counting transcripts (no machine doing them now).");
    expect((await row("Text in images (OCR)")).textContent).not.toContain("not known yet");
    cleanup();
    queue = { ...(queue as object), eta: { producers: { "lumiverb.image.text": null }, pools: {}, caught_up: null, jobs: [],
              not_counted: [{ artifact: "lumiverb.image.text", title: "Text in images (OCR)", why: "not_known_yet" }] } };
    renderSection();
    expect((await row("Text in images (OCR)")).textContent).toContain("time left not known yet");
  });

  it("asks the scheduler what it's doing once for the whole page, not once a row", async () => {
    queue = { live: true, at: new Date().toISOString(), running: {}, waiting: {}, pools: {}, gpu_hold: 0,
              eta: { producers: {}, pools: {}, caught_up: 0, not_counted: [], jobs: [] } };
    producers = [producer({}), producer({ artifact: "lumiverb.image.text", title: "Text in images (OCR)" }),
                 producer({ artifact: "lumiverb.image.likeness", title: "Visual search (CLIP)" })];
    renderSection();
    await row("Visual search (CLIP)");
    await screen.findByLabelText("Now");
    expect(sent.filter((r) => r.url.includes("/producers/queue"))).toHaveLength(1);
  });

  it("says when the scheduler isn't running", async () => {
    queue = { live: false, at: "2026-10-09T01:00:00Z", running: {}, waiting: {}, pools: {}, gpu_hold: 0 };
    renderSection();
    expect(await screen.findByText(/The scheduler isn't running: last heard from/)).toBeTruthy();
  });

  it("says when there's nothing to make", async () => {
    queue = { live: true, at: new Date().toISOString(), running: {}, waiting: {}, pools: {}, gpu_hold: 0 };
    renderSection();
    expect((await screen.findByLabelText("Now")).textContent).toContain("Now: nothing to make");
  });

  it("says a clip someone asked to try again is being tried", async () => {
    producers = [producer({ counts: { applicable: 10, current: 9, stale: 0, missing: 0, failing: 1, given_up: 0 } })];
    failures = [{ asset_id: "ast_1", artifact: "lumiverb.image.description", title: "Descriptions and tags", rel_path: "a.jpg",
                  library_id: "lib_1", library_name: "Test footage", media_type: "image", error: "no",
                  attempts: 0, failed_at: "2026-10-09T01:00:00Z", retry_at: null, given_up: false }];
    renderSection();
    const vision = await row("Descriptions and tags");
    fireEvent.click(within(vision).getByRole("button", { name: "Show failures: Descriptions and tags" }));
    expect(await within(vision).findByText(/Being tried again\./)).toBeTruthy();
    expect(vision.textContent).not.toContain("Tried 0 times");
  });
});


describe("ProcessingSection settings", () => {
  const fields = [
    { key: "model", label: "Model", kind: "text", value: "small", default: "small", minimum: null, maximum: null,
      unit: "", advanced: false, fixed: "Chosen in Settings → AI, for every producer its machines run." },
    { key: "vad_min_silence_ms", label: "Shortest silence skipped", kind: "int", value: 500, default: 500,
      minimum: 100, maximum: 2000, unit: "ms", advanced: false, fixed: null },
  ] as Producer["fields"];

  async function openSettings() {
    producers = [producer({ artifact: "lumiverb.video.transcript", title: "Transcripts", media: ["video"], fields })];
    renderSection();
    fireEvent.click(await screen.findByText("Settings · version 1"));
  }

  it("lets an admin change what the producer reads, and says why the rest can't change", async () => {
    await openSettings();
    expect(screen.getByText(/Chosen in Settings → AI/)).toBeTruthy();
    expect(screen.queryByLabelText(/^Model/)).toBeNull();  // no input for it
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "800" } });
    expect(screen.getByText(/Saving asks about the 9 clips already made/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save the settings of Transcripts" }));
    await waitFor(() => expect(sent.some((r) => r.method === "PUT")).toBe(true));
    const put = sent.find((r) => r.method === "PUT")!;
    expect(put.url).toContain("/producers/lumiverb.video.transcript/settings");
    expect(put.body).toEqual({ settings: { vad_min_silence_ms: 800 } });
  });

  it.each([["New files only", false], ["Also remake them", true]])(
    "asks what becomes of what the old settings made: %s (ADR-018 decision 6)", async (button, redo) => {
      answers = [err(409, "redo_on_change", "12 clips of transcripts were made with the old settings.",
                     { clips: 12, question: "remake" })];
      await openSettings();
      fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "800" } });
      fireEvent.click(screen.getByRole("button", { name: "Save the settings of Transcripts" }));
      expect(await screen.findByText(/made with the old settings/)).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: button }));
      await waitFor(() => expect(sent.filter((r) => r.method === "PUT")).toHaveLength(2));
      expect(sent.filter((r) => r.method === "PUT")[1].body).toEqual({ settings: { vad_min_silence_ms: 800 }, redo });
    });

  it("offers a quality as a notch, Good, Better or Best, not an encoder's number (ADR-018 decision 6)", async () => {
    const quality = { key: "quality", label: "Quality", kind: "notch", value: "better", default: "better",
                      minimum: null, maximum: null, unit: "", advanced: false, fixed: null, remakes: true,
                      choices: ["good", "better", "best"] } as NonNullable<Producer["fields"]>[number];
    producers = [producer({ artifact: "lumiverb.video.analysis_proxy", title: "Analysis proxies", media: ["video"], fields: [quality] })];
    renderSection();
    fireEvent.click(await screen.findByText("Settings · version 1"));
    const select = screen.getByLabelText("Quality") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual(["Good", "Better", "Best"]);
    expect(select.value).toBe("better");
    fireEvent.change(select, { target: { value: "best" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the settings of Analysis proxies" }));
    await waitFor(() => expect(sent.some((r) => r.method === "PUT")).toBe(true));
    expect(sent.find((r) => r.method === "PUT")!.body).toEqual({ settings: { quality: "best" } });
  });

  it("says new files get a new preview quality, the rest staying as they are, where nothing is remade yet", async () => {
    const quality = { key: "quality", label: "Quality", kind: "notch", value: "better", default: "better",
                      minimum: null, maximum: null, unit: "", advanced: false, fixed: null, remakes: true,
                      choices: ["good", "better", "best"] } as NonNullable<Producer["fields"]>[number];
    producers = [producer({ artifact: "lumiverb.preview", title: "Proxies and thumbnails", media: ["image", "video"],
                            redoable: false, scheduled: false, fields: [quality] })];
    renderSection();
    fireEvent.click(await screen.findByText("Settings · version 1"));
    fireEvent.change(screen.getByLabelText("Quality"), { target: { value: "best" } });
    expect(screen.getByText("New files get these settings; the 9 clips already made stay as they are.")).toBeTruthy();
    expect(screen.queryByText(/Saving asks about/)).toBeNull();
  });

  it("sends a default as null, so the account goes back to following it", async () => {
    await openSettings();
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "700" } });
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "500" } });
    expect((screen.getByRole("button", { name: "Save the settings of Transcripts" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("says why the server refused a value", async () => {
    // The browser's own bounds stop most; the server's word is final.
    answers = [err(422, "bad_setting", "Shortest silence skipped is from 100 to 2000 ms")];
    await openSettings();
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "700" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the settings of Transcripts" }));
    expect(await screen.findByText(/is from 100 to 2000 ms/)).toBeTruthy();
  });

  it("goes back to a setting's default when its field is emptied, as the CLI's KEY= does", async () => {
    producers = [producer({ artifact: "lumiverb.video.transcript", title: "Transcripts", media: ["video"],
                            fields: [{ ...fields![1], value: 800 }] })];
    renderSection();
    fireEvent.click(await screen.findByText("Settings · version 1"));
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the settings of Transcripts" }));
    await waitFor(() => expect(sent.some((r) => r.method === "PUT")).toBe(true));
    expect(sent.find((r) => r.method === "PUT")!.body).toEqual({ settings: { vad_min_silence_ms: null } });
  });

  it("an empty field that's already the default is no change", async () => {
    await openSettings();
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "" } });
    expect((screen.getByRole("button", { name: "Save the settings of Transcripts" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("cancels the question, sending nothing more", async () => {
    answers = [err(409, "redo_on_change", "12 clips of transcripts were made with the old settings.",
                   { clips: 12, question: "remake" })];
    await openSettings();
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "800" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the settings of Transcripts" }));
    expect(await screen.findByText(/made with the old settings/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText(/made with the old settings/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Also remake them" })).toBeNull();
    expect(sent.filter((r) => r.method === "PUT")).toHaveLength(1);
  });

  it("a change after the question takes the question away: its answer was for other values", async () => {
    answers = [err(409, "redo_on_change", "12 clips of transcripts were made with the old settings.",
                   { clips: 12, question: "remake" })];
    await openSettings();
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "800" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the settings of Transcripts" }));
    expect(await screen.findByText(/made with the old settings/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "900" } });
    expect(screen.queryByRole("button", { name: "Also remake them" })).toBeNull();
  });

  it("after saving shows what the server saved, so 1.50 isn't a change from 1.5", async () => {
    const temperature = { key: "temperature", label: "Temperature", kind: "float", value: 0.2, default: 0.2,
                          minimum: 0, maximum: 2, unit: "", advanced: false, fixed: null } as NonNullable<Producer["fields"]>[number];
    producers = [producer({ fields: [temperature] })];
    answers = [json(producer({ fields: [{ ...temperature, value: 1.5 }] }))];
    renderSection();
    fireEvent.click(await screen.findByText("Settings · version 1"));
    fireEvent.change(screen.getByLabelText(/Temperature/), { target: { value: "1.50" } });
    producers = [producer({ fields: [{ ...temperature, value: 1.5 }] })];  // what the refetch reads
    fireEvent.click(screen.getByRole("button", { name: "Save the settings of Descriptions and tags" }));
    await waitFor(() => expect((screen.getByLabelText(/Temperature/) as HTMLInputElement).value).toBe("1.5"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save the settings of Descriptions and tags" })
      .textContent).toBe("Save"));
    expect((screen.getByRole("button", { name: "Save the settings of Descriptions and tags" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByText(/Saving asks about the 9 clips already made/)).toBeNull();
  });

  it("says why settings can't change in words, not only on hover (phones can't hover)", async () => {
    const why = "Not read by this producer yet: changing it would change nothing it makes.";
    producers = [producer({ artifact: "lumiverb.image.faces", title: "Faces", fields: [
      { key: "det_size", label: "Detection size", kind: "int", value: 640, default: 640, minimum: null, maximum: null,
        unit: "px", advanced: false, fixed: why },
      { key: "min_confidence", label: "Least confidence", kind: "float", value: 0.5, default: 0.5, minimum: null,
        maximum: null, unit: "", advanced: false, fixed: why },
    ] })];
    renderSection();
    fireEvent.click(await screen.findByText("Settings · version 1"));
    expect(screen.getAllByText(why)).toHaveLength(1);  // said once for all of them
  });

  describe("face grouping", () => {
    const grouping = [
      { key: "min_confidence", label: "Least confidence", kind: "float", value: 0.5, default: 0.5, minimum: 0.3,
        maximum: 0.99, unit: "", advanced: false, fixed: null, remakes: true },
      { key: "merge_close_clusters", label: "Merge close groups", kind: "bool", value: true, default: true,
        minimum: null, maximum: null, unit: "", advanced: false, fixed: null, remakes: false },
      { key: "merge_distance", label: "Merge distance (cosine)", kind: "float", value: 0.45, default: 0.45,
        minimum: 0.05, maximum: 0.6, unit: "", advanced: true, fixed: null, remakes: false },
    ] as Producer["fields"];

    it("a yes-or-no setting is a checkbox, and turning it off sends false", async () => {
      producers = [producer({ artifact: "lumiverb.image.faces", title: "Faces", fields: grouping })];
      renderSection();
      fireEvent.click(await screen.findByText("Settings · version 1"));
      const box = screen.getByLabelText("Merge close groups") as HTMLInputElement;
      expect(box.type).toBe("checkbox");
      expect(box.checked).toBe(true);
      fireEvent.click(box);
      expect(box.checked).toBe(false);
      fireEvent.click(screen.getByRole("button", { name: "Save the settings of Faces" }));
      await waitFor(() => expect(sent.some((r) => r.method === "PUT")).toBe(true));
      expect(sent.find((r) => r.method === "PUT")!.body).toEqual({ settings: { merge_close_clusters: false } });
    });

    it("says a grouping change regroups, and not that anything is made again", async () => {
      producers = [producer({ artifact: "lumiverb.image.faces", title: "Faces", fields: grouping })];
      renderSection();
      fireEvent.click(await screen.findByText("Settings · version 1"));
      fireEvent.click(screen.getByLabelText("Merge close groups"));
      expect(screen.getByText("Face groups are worked out again; no face is found again.")).toBeTruthy();
      expect(screen.queryByText(/Saving asks about the 9 clips already made/)).toBeNull();
      fireEvent.change(screen.getByLabelText(/Least confidence/), { target: { value: "0.6" } });
      expect(screen.getByText(/Saving asks about the 9 clips already made/)).toBeTruthy();
    });

    it("says nothing is redone when nothing is done yet, and the face line is only for faces", async () => {
      producers = [producer({ artifact: "lumiverb.location.guess", title: "Location guesses", fields: grouping,
                              counts: { applicable: 0, current: 0, stale: 0, missing: 0, failing: 0 } })];
      renderSection();
      fireEvent.click(await screen.findByText("Settings · version 1"));
      fireEvent.change(screen.getByLabelText(/Least confidence/), { target: { value: "0.6" } });
      expect(screen.queryByText(/Saving asks about/)).toBeNull();
      fireEvent.click(screen.getByLabelText("Merge close groups"));
      expect(screen.queryByText(/Face groups/)).toBeNull();
    });

    it("says which plug-in a producer belongs to, and that it makes nothing while that's off", async () => {
      producers = [producer({ artifact: "place", title: "Place names", fields: [],
                              plugin: { plugin_id: "geonames", title: "Place names (GeoNames)", on: false } })];
      renderSection();
      expect((await row("Place names")).textContent)
        .toContain("Part of the Place names (GeoNames) plug-in: off, so nothing is made");
    });

    it("names an installed plug-in whose title says it's one once", async () => {
      producers = [producer({ artifact: "example", title: "Example facts", fields: [],
                              plugin: { plugin_id: "example", title: "Example plug-in", on: true } })];
      renderSection();
      expect((await row("Example facts")).textContent).toContain("Part of the Example plug-in.");
    });

    it("counts guesses being checked again apart from missing", async () => {
      producers = [producer({ artifact: "lumiverb.location.guess", title: "Location guesses", fields: [],
                              counts: { applicable: 10, current: 6, stale: 0, missing: 1, rechecking: 3, failing: 0 } })];
      renderSection();
      const shown = (await row("Location guesses")).textContent;
      expect(shown).toContain("6 current · 1 missing · 0 made another way · 3 rechecking");
      expect(shown).toContain("Only in the libraries where Infer location is on");
    });

    it("says a new inference window checks guesses again, not that they're made again", async () => {
      producers = [producer({ artifact: "lumiverb.location.guess", title: "Location guesses", fields: [
        { key: "inference_minutes", label: "Inference window", kind: "int", value: 360, default: 360, minimum: 5,
          maximum: 1440, unit: "min", advanced: false, fixed: null, remakes: false }] })];
      renderSection();
      fireEvent.click(await screen.findByText("Settings · version 1"));
      fireEvent.change(screen.getByLabelText(/Inference window/), { target: { value: "120" } });
      expect(screen.getByText("Guesses are checked again.")).toBeTruthy();
      expect(screen.queryByText(/Saving asks about/)).toBeNull();
    });

    it("shows editors yes or no as On or Off", async () => {
      role = "editor";
      producers = [producer({ artifact: "lumiverb.image.faces", title: "Faces",
                              fields: [{ ...grouping![1], value: false }] })];
      renderSection();
      fireEvent.click(await screen.findByText("Settings · version 1"));
      expect(screen.getByText("Off")).toBeTruthy();
    });
  });

  it("shows editors the settings, not a form", async () => {
    role = "editor";
    await openSettings();
    expect(screen.getByText("Shortest silence skipped")).toBeTruthy();
    expect(screen.getByText("500 ms")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save the settings of Transcripts" })).toBeNull();
  });
});


describe("Scene descriptions' Detail", () => {
  const detail = (value: string) => ({ key: "detail", label: "Detail", kind: "level" as const, value,
    default: "normal", minimum: null, maximum: null, unit: "", advanced: false, fixed: null, remakes: false,
    choices: ["low", "normal", "high"] });
  const sceneProducers = (value: string, below: number) => [
    producer({ artifact: "lumiverb.video.scenes", title: "Scenes", media: ["video"], part_of: "lumiverb.video.scene_descriptions", level: 1,
               fields: [detail(value)],
               counts: { applicable: 10, current: 10 - below, stale: 0, missing: 0, failing: 0, below } }),
    producer({ artifact: "lumiverb.video.scene_descriptions", title: "Scene descriptions", media: ["video"] }),
  ];

  it("shows one Detail control inside Scene descriptions, scenes as a part of it", async () => {
    producers = sceneProducers("normal", 0);
    renderSection();
    const li = await row("Scene descriptions");
    expect(within(li).getByRole("heading", { name: "Scenes" })).toBeTruthy();
    expect(within(li).getByRole("radio", { name: "Normal" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getAllByRole("radiogroup")).toHaveLength(1);
    expect(screen.queryByText(/below this/)).toBeNull();
  });

  it("saves the level picked, and the default as null", async () => {
    producers = sceneProducers("high", 0);
    renderSection();
    fireEvent.click(await screen.findByRole("radio", { name: "Normal" }));
    await waitFor(() => expect(sent.some((r) => r.method === "PUT")).toBe(true));
    expect(sent.find((r) => r.method === "PUT")).toMatchObject(
      { url: expect.stringContaining("/producers/lumiverb.video.scenes/settings"), body: { settings: { detail: null } } });
  });

  it("counts the videos below this and redoes them only when asked, naming all", async () => {
    producers = sceneProducers("high", 412);
    renderSection();
    expect(await screen.findByText("412 videos below this")).toBeTruthy();
    expect(sent.some((r) => r.url.includes("below/redo"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Redo 412 below this: Scenes" }));
    await waitFor(() => expect(sent.some((r) => r.url.includes("/producers/lumiverb.video.scenes/below/redo"))).toBe(true));
    expect(sent.find((r) => r.url.includes("below/redo"))!.body).toEqual({ all: true });
  });

  it("shows editors the level, with no way to change it or redo", async () => {
    role = "editor";
    producers = sceneProducers("high", 3);
    renderSection();
    expect(await screen.findByText("3 videos below this")).toBeTruthy();
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Redo/ })).toBeNull();
    expect(screen.getByText("High")).toBeTruthy();
  });
});

describe("Waiting for what it's made from", () => {
  // Scene descriptions apply only to videos whose scenes are found: with
  // Scenes paused, they count nothing, which isn't being caught up. The
  // server says how many wait for Scenes (waits: needs), and whether it's paused.
  const sceneRow = (scenesPaused: boolean, missing: number, described = 0) => {
    if (scenesPaused) paused.add("lumiverb.video.scenes");
    return sceneProducers(missing, described, scenesPaused);
  };
  const sceneProducers = (missing: number, described: number, scenesPaused: boolean) => [
    producer({ artifact: "lumiverb.video.scenes", title: "Scenes", media: ["video"], part_of: "lumiverb.video.scene_descriptions", paused: scenesPaused,
               counts: { applicable: 1500, current: 1500 - missing, stale: 0, missing, failing: 0 } }),
    producer({ artifact: "lumiverb.video.scene_descriptions", title: "Scene descriptions", media: ["video"], needs: ["lumiverb.video.analysis_proxy", "lumiverb.video.scenes"],
               counts: { applicable: described, current: described, stale: 0, missing: 0, failing: 0 },
               waits: missing ? [{ reason: "needs", clips: missing, artifact: "lumiverb.video.scenes", paused: scenesPaused }] : [] }),
    producer({ artifact: "lumiverb.video.analysis_proxy", title: "Analysis proxies", media: ["video"],
               counts: { applicable: 1500, current: 1500, stale: 0, missing: 0, failing: 0 } }),
  ];

  it("says the videos wait for Scenes, paused, not that there's nothing to make", async () => {
    producers = sceneRow(true, 1500);
    renderSection();
    const li = await row("Scene descriptions");
    expect(within(li).getByText("1,500 videos wait for Scenes, which is paused.")).toBeTruthy();
    expect(within(li).queryByText("Nothing to make yet.")).toBeNull();
    // Analysis proxies are all made: nothing waits for them.
    expect(within(li).queryByText(/wait for Analysis proxies/)).toBeNull();
  });

  it("says they wait for Scenes first while Scenes runs, beside what's made", async () => {
    producers = sceneRow(false, 400, 900);
    renderSection();
    const li = await row("Scene descriptions");
    expect(within(li).getByText("400 videos wait for Scenes first.")).toBeTruthy();
    expect(within(li).getByText("900")).toBeTruthy();  // its own counts, as ever
  });

  it("says nothing of Scenes once every video has them", async () => {
    producers = sceneRow(false, 0, 1500);
    renderSection();
    const li = await row("Scene descriptions");
    expect(within(li).queryByText(/wait for/)).toBeNull();
  });
});

describe("Why work waits (no silent waits)", () => {
  // Robert, Oct 12: analysis proxies stopped for hours and the row said nothing.
  // Each reason the server sends is said on the row, in one sentence.
  const proxies = (waits: Producer["waits"], over: Partial<Producer> = {}) => producer({
    artifact: "lumiverb.video.analysis_proxy", title: "Analysis proxies", media: ["video"], settings: {}, fields: [],
    counts: { applicable: 100, current: 60, stale: 0, missing: 40, failing: 0 }, waits, ...over });

  const lines = async (title: string) => {
    const list = within(await row(title)).queryByRole("list", { name: `Why ${title} waits` });
    return list ? within(list).getAllByRole("listitem").map((li) => li.textContent) : [];
  };

  it("says which library's drive the analysis proxies wait for, and since when", async () => {
    producers = [proxies([
      { reason: "storage", clips: 40, library_id: "lib_1", library_name: "Empty Besters", checked: true,
        away_since: "2026-10-12T18:20:00Z", seen_at: "2026-10-12T17:55:00Z" },
      { reason: "storage", clips: 3, library_id: "lib_2", library_name: "Footage", checked: false },
    ])];
    renderSection();
    const said = await lines("Analysis proxies");
    expect(said).toHaveLength(2);
    expect(said[0]).toMatch(/^40 videos in Empty Besters wait for its drive: it can't be reached since .+ \(last seen .+\)\.$/);
    expect(said[1]).toBe("3 videos in Footage wait for its drive: it hasn't been looked at since the scheduler started.");
  });

  it("says each other reason in a sentence of its own", async () => {
    producers = [proxies([
      { reason: "paused", clips: 40 },
      { reason: "scheduler", clips: 40, why: "not_running" },
      { reason: "scan", clips: 2, paused: true },
      { reason: "held", clips: 1 },
      { reason: "retry_later", clips: 5 },
      { reason: "given_up", clips: 1 },
    ])];
    renderSection();
    expect(await lines("Analysis proxies")).toEqual([
      "40 videos wait until it's resumed.",
      "40 videos wait for the scheduler, which isn't running.",
      "2 videos wait for a scan to see their file again, and its library's scans are paused.",
      "1 video is tried again within the hour: its last try couldn't finish.",
      "5 videos failed and wait to be tried again.",
      "1 video failed too many times and isn't tried again until someone asks.",
    ]);
  });

  it("says a new version waits for an admin's OK in the remakes' box, with Remake", async () => {
    producers = [proxies([{ reason: "new_version", clips: 7 }], {
      counts: { applicable: 100, current: 93, stale: 7, missing: 0, failing: 0 }, redo_stopped: true, new_version: true })];
    renderSection();
    const li = await row("Analysis proxies");
    expect(within(li).getByText(
      "A new version of Analysis proxies is ready: 7 videos made by an older one are made again once an admin resumes it.",
    )).toBeTruthy();
    expect(within(li).queryByText(/an admin chose to keep/)).toBeNull();  // not an admin's choice
    expect(within(li).getByRole("button", { name: "Remake Analysis proxies" })).toBeTruthy();
    expect(await lines("Analysis proxies")).toEqual([]);  // said once, in the redo's box
  });

  it("says why, not 'Nothing to make yet.', when nothing it applies to can be made yet", async () => {
    producers = [proxies([{ reason: "needs", clips: 9, artifact: "lumiverb.video.format", paused: false }], {
      counts: { applicable: 0, current: 0, stale: 0, missing: 0, failing: 0 } }),
      producer({ artifact: "lumiverb.video.format", title: "Video probes", media: ["video"], waits: [] })];
    renderSection();
    expect(await lines("Analysis proxies")).toEqual(["9 videos wait for Video probes first."]);
    expect(within(await row("Analysis proxies")).queryByText("Nothing to make yet.")).toBeNull();
  });
});

describe("Face detection's levels", () => {
  const level = (label: string, value: string, fallback: string) => ({ key: "detail", label, kind: "level" as const,
    value, default: fallback, minimum: null, maximum: null, unit: "", advanced: false, fixed: null, remakes: false,
    choices: ["off", "low", "normal", "high"] });
  const faceProducers = (photos: string, videos: string, photosBelow = 0) => [
    producer({ artifact: "lumiverb.image.faces", title: "Faces", media: ["image"], level: 2, fields: [level("Photos", photos, "normal")],
               counts: photos === "off" ? { applicable: 0, current: 0, stale: 0, missing: 0, failing: 0, below: 0 }
                 : { applicable: 10, current: 10 - photosBelow, stale: 0, missing: 0, failing: 0, below: photosBelow } }),
    producer({ artifact: "lumiverb.video.faces", title: "Faces in videos", media: ["video"], part_of: "lumiverb.image.faces", level: 1,
               fields: [level("Videos", videos, "low")],
               counts: videos === "off" ? { applicable: 0, current: 0, stale: 0, missing: 0, failing: 0, below: 0 }
                 : { applicable: 4, current: 4, stale: 0, missing: 0, failing: 0, below: 0 } }),
  ];

  it("shows Photos then Videos in the Faces row, each Off, Low, Normal or High", async () => {
    producers = faceProducers("normal", "low");
    renderSection();
    const li = await row("Faces");
    const groups = within(li).getAllByRole("radiogroup");
    expect(groups.map((g) => g.getAttribute("aria-labelledby"))).toEqual(["lumiverb.image.faces-level",
                                                                                   "lumiverb.video.faces-level"]);
    const photos = within(li).getByRole("radiogroup", { name: "Photos" });
    expect(within(photos).getAllByRole("radio").map((r) => r.textContent)).toEqual(["Off", "Low", "Normal", "High"]);
    expect(within(photos).getByRole("radio", { name: "Normal" }).getAttribute("aria-checked")).toBe("true");
    const videos = within(li).getByRole("radiogroup", { name: "Videos" });
    expect(within(videos).getByRole("radio", { name: "Low" }).getAttribute("aria-checked")).toBe("true");
  });

  it("saves Off for videos alone", async () => {
    producers = faceProducers("normal", "low");
    renderSection();
    const videos = within(await row("Faces")).getByRole("radiogroup", { name: "Videos" });
    fireEvent.click(within(videos).getByRole("radio", { name: "Off" }));
    await waitFor(() => expect(sent.some((r) => r.method === "PUT")).toBe(true));
    expect(sent.find((r) => r.method === "PUT")).toMatchObject(
      { url: expect.stringContaining("/producers/lumiverb.video.faces/settings"), body: { settings: { detail: "off" } } });
  });

  it("says Off plainly, not that there's nothing to make", async () => {
    producers = faceProducers("off", "off");
    renderSection();
    const li = await row("Faces");
    expect(within(li).getAllByText("Off: nothing new is found. What's found stays.")).toHaveLength(2);
    expect(within(li).queryByText("Nothing to make yet.")).toBeNull();
  });

  it("counts photos below this, as photos", async () => {
    producers = faceProducers("high", "low", 7);
    renderSection();
    expect(await screen.findByText("7 photos below this")).toBeTruthy();
  });
});

describe("duration", () => {
  it("says a span of time roughly, as people would", () => {
    expect([30, 90, 3600, 7500, 36000, 3 * 86400, 200000].map(duration)).toEqual(
      ["under a minute", "2 min", "1 h", "2 h 5 min", "10 h", "3 days", "2 days 7 h"]);
    expect(duration(40 * 86400)).toBe("over a month");
  });
});
