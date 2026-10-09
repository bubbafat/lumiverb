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
let queue: Record<string, unknown> = { live: false, at: null, running: {}, waiting: {}, pools: {}, gpu_hold: 0,
                                       paused: false, paused_by: null, paused_at: null, scans_paused: false,
                                       scans_paused_by: null, scans_paused_at: null };
const sent: { method: string; url: string; body: unknown }[] = [];

function producer(over: Partial<Producer>): Producer {
  return {
    artifact: "vision", producer: "vision", version: "1", title: "Descriptions and tags", media: ["image"],
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
    if (path === "/libraries") return json([{ library_id: "lib_1", name: "Footage", root_path: "/f", status: "active" }]);
    if (path === "/projects") return json({ items: [{ project_id: "col_1", name: "Wedding" }] });
    if (path === "/producers") return json({ producers });
    if (path === "/producers/queue") {  // as the server: the producers paused on their own, whatever the scope
      return json({ paused_producers: producers.filter((p) => p.paused).map((p) => ({ artifact: p.artifact, title: p.title })),
                    ...queue });
    }
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
    const m = path.match(/^\/producers\/([^/]+)\/redo\/(stop|resume)$/);
    if (m && method === "POST") {
      const next = answers.shift();
      if (next) return next;
      producers = producers.map((p) => (p.artifact === m[1] ? { ...p, redo_stopped: m[2] === "stop" } : p));
      return new Response(null, { status: 204 });
    }
    const all = path.match(/^\/producers\/(pause|resume)$/);
    if (all && method === "POST") {
      const next = answers.shift();
      if (next) return next;
      const paused = all[1] === "pause";
      queue = { ...queue, paused, paused_at: paused ? "2026-10-09T10:00:00Z" : null };
      if (!paused) {  // Resume all turns everything on: no paused part is kept
        queue = { ...queue, scans_paused: false, scans_paused_at: null };
        producers = producers.map((p) => ({ ...p, paused: false, paused_at: null }));
      }
      return new Response(null, { status: 204 });
    }
    const scans = path.match(/^\/producers\/scans\/(pause|resume)$/);
    if (scans && method === "POST") {
      const scansPaused = scans[1] === "pause";
      queue = { ...queue, scans_paused: scansPaused, scans_paused_at: scansPaused ? "2026-10-09T11:00:00Z" : null };
      return new Response(null, { status: 204 });
    }
    const one = path.match(/^\/producers\/([^/]+)\/(pause|resume)$/);
    if (one && method === "POST") {
      const next = answers.shift();
      if (next) return next;
      const paused = one[2] === "pause";
      producers = producers.map((p) => (p.artifact === one[1]
        ? { ...p, paused, paused_at: paused ? "2026-10-09T10:00:00Z" : null } : p));
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
  queue = { live: false, at: null, running: {}, waiting: {}, pools: {}, gpu_hold: 0, paused: false, paused_by: null,
            paused_at: null, scans_paused: false, scans_paused_by: null, scans_paused_at: null };
  sent.length = 0;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ProcessingSection />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

async function row(title: string) {
  return (await screen.findByRole("heading", { name: title })).closest("li") as HTMLElement;
}

describe("ProcessingSection", () => {
  it("lists each producer with its counts, its settings and why one isn't made again yet", async () => {
    producers = [
      producer({}),
      producer({ artifact: "clip", title: "Visual search (CLIP)", uniform: true }),
      producer({ artifact: "scenes", title: "Scenes", media: ["video"], redoable: false,
                 why_not: "Finding a video's scenes again isn't built yet." }),
    ];
    renderSection();
    const vision = await row("Descriptions and tags");
    expect(vision.textContent).toContain("6 current · 1 missing · 3 stale");
    expect(within(vision).getByText(/qwen3-vl:8b-instruct/)).toBeTruthy();
    expect((await row("Visual search (CLIP)")).textContent).toContain("one model for all");
    const scenes = await row("Scenes");
    expect(scenes.textContent).toContain("Not made again yet. Finding a video's scenes again isn't built yet.");
    expect(within(scenes).queryByRole("button", { name: /Stop|Resume/ })).toBeNull();
  });

  it("says why a producer's work waits (its AI job has no machine), linking to Settings → AI", async () => {
    // Robert, Oct 9: removing the last machine for a job never asks; this is where it shows.
    producers = [
      producer({ waiting: "No machine does descriptions & text: its work waits until an admin adds one in Settings → AI." }),
      producer({ artifact: "clip", title: "Visual search (CLIP)" }),
    ];
    renderSection();
    const vision = await row("Descriptions and tags");
    const why = within(vision).getByRole("status");
    expect(why.textContent).toContain("No machine does descriptions & text");
    expect(within(why).getByRole("link", { name: "Settings → AI" }).getAttribute("href")).toBe("/settings/ai");
    expect(within(await row("Visual search (CLIP)")).queryByRole("status")).toBeNull();
  });

  it("says the stale ones are being redone, with no Upgrade step", async () => {
    producers = [producer({})];
    renderSection();
    const vision = await row("Descriptions and tags");
    expect(vision.textContent).toContain("Redoing 3 clips, after anything missing.");
    expect(screen.queryByRole("button", { name: /Upgrade/ })).toBeNull();
    expect(screen.getByText(/Changing a model in Settings → AI, or a producer.s settings below, is what starts that/)).toBeTruthy();
  });

  it("says nothing about redoing when nothing is stale", async () => {
    producers = [producer({ counts: { applicable: 10, current: 9, stale: 0, missing: 1, failing: 0 } })];
    renderSection();
    const vision = await row("Descriptions and tags");
    expect(vision.textContent).not.toContain("Redoing");
    expect(within(vision).queryByRole("button", { name: /Stop/ })).toBeNull();
  });

  it("stops a redo and resumes it", async () => {
    producers = [producer({})];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Stop redoing Descriptions and tags" }));
    await waitFor(() => expect(sent.some((s) => s.url.endsWith("/producers/vision/redo/stop"))).toBe(true));
    expect(await screen.findByText("Redo stopped: 3 clips stay as they are until it's resumed.")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "Resume redoing Descriptions and tags" }));
    await waitFor(() => expect(sent.some((s) => s.url.endsWith("/producers/vision/redo/resume"))).toBe(true));
    expect(await screen.findByText("Redoing 3 clips, after anything missing.")).toBeTruthy();
  });

  it("says why when the server refuses", async () => {
    producers = [producer({})];
    answers = [err(409, "cant_redo", "Descriptions and tags isn't made again yet.")];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Stop redoing Descriptions and tags" }));
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

  it("shows everyone the counts and the redo; only admins stop or resume it", async () => {
    role = "viewer";
    producers = [producer({}), producer({ artifact: "ocr", title: "Text in images (OCR)", redo_stopped: true })];
    renderSection();
    expect(await screen.findByText("Only admins can pause processing or stop a redo.")).toBeTruthy();
    expect((await row("Descriptions and tags")).textContent).toContain("Redoing 3 clips");
    expect((await row("Text in images (OCR)")).textContent).toContain("Redo stopped");
    expect(screen.queryByRole("button", { name: /Stop|Resume/ })).toBeNull();
  });

  it("pauses all processing and resumes it (admins)", async () => {
    queue = { ...queue, live: true, at: new Date().toISOString() };
    producers = [producer({})];
    renderSection();
    expect(await screen.findByText("Running")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "Pause all processing" }));
    await waitFor(() => expect(sent.some((s) => s.method === "POST" && s.url.endsWith("/producers/pause"))).toBe(true));
    expect((await screen.findByText(/All processing is paused/)).textContent).toContain(
      "nothing starts, scans included, and upkeep changes nothing, until it's resumed. What's running finishes; " +
      "the website stays up.");
    expect(await screen.findByText("All paused")).toBeTruthy();
    // The kill switch is on: its parts aren't switched alone meanwhile.
    expect(screen.queryByRole("button", { name: "Pause scans" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Pause Descriptions and tags" })).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "Resume all processing" }));
    await waitFor(() => expect(sent.some((s) => s.method === "POST" && s.url.endsWith("/producers/resume"))).toBe(true));
    await waitFor(() => expect(screen.queryByText(/All processing is paused/)).toBeNull());
    expect(await screen.findByRole("button", { name: "Pause all processing" })).toBeTruthy();
  });

  it("says all processing is paused while the scheduler isn't running too", async () => {
    queue = { ...queue, live: false, at: "2026-10-09T01:00:00Z", paused: true, paused_at: "2026-10-09T00:30:00Z" };
    renderSection();
    expect(await screen.findByText(/All processing is paused/)).toBeTruthy();
    expect(screen.getByText(/The scheduler isn't running/)).toBeTruthy();
  });

  it("pauses one producer and resumes it (admins)", async () => {
    producers = [producer({}), producer({ artifact: "ocr", title: "Text in images (OCR)" })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Pause Descriptions and tags" }));
    await waitFor(() => expect(sent.some((s) => s.method === "POST" && s.url.endsWith("/producers/vision/pause"))).toBe(true));
    const vision = await row("Descriptions and tags");
    await waitFor(() => expect(vision.textContent).toContain("Paused: nothing more of it starts until it's resumed."));
    expect(vision.textContent).toContain("Redoes 3 clips once it's resumed, after anything missing.");
    expect((await row("Text in images (OCR)")).textContent).not.toContain("Paused");
    fireEvent.click(await screen.findByRole("button", { name: "Resume Descriptions and tags" }));
    await waitFor(() => expect(sent.some((s) => s.method === "POST" && s.url.endsWith("/producers/vision/resume"))).toBe(true));
    await waitFor(() => expect(vision.textContent).not.toContain("Paused"));
  });

  it("while all processing is paused, the redo waits for it and Now says so", async () => {
    queue = { ...queue, live: true, at: new Date().toISOString(), paused: true, paused_at: "2026-10-09T10:00:00Z" };
    producers = [producer({})];
    renderSection();
    expect((await row("Descriptions and tags")).textContent).toContain(
      "Redoes 3 clips once processing is resumed, after anything missing.");
    expect((await screen.findByLabelText("Now")).textContent).toContain("Now: nothing more starts while paused");
  });

  it("pauses scans alone and resumes them (admins)", async () => {
    queue = { ...queue, live: true, at: new Date().toISOString() };
    producers = [producer({}), producer({ artifact: "proxy", title: "Proxies and thumbnails", scheduled: false,
                                          redoable: false, why_not: "Made by scans." })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Pause scans" }));
    await waitFor(() => expect(sent.some((s) => s.method === "POST" && s.url.endsWith("/producers/scans/pause"))).toBe(true));
    expect((await screen.findByText(/Scans are paused/)).textContent).toContain(
      "no new or changed files are found, and no thumbnails or video previews made, until they're resumed.");
    expect(screen.queryByText(/All processing is paused/)).toBeNull();
    expect((await row("Proxies and thumbnails")).textContent).toContain("Paused with scans");
    expect((await row("Descriptions and tags")).textContent).not.toContain("Paused");
    fireEvent.click(await screen.findByRole("button", { name: "Resume scans" }));
    await waitFor(() => expect(sent.some((s) => s.method === "POST" && s.url.endsWith("/producers/scans/resume"))).toBe(true));
    await waitFor(() => expect(screen.queryByText(/Scans are paused/)).toBeNull());
  });

  it("offers no pause for what scans make: pausing scans stops it", async () => {
    producers = [producer({ artifact: "proxy", title: "Proxies and thumbnails", scheduled: false, redoable: false,
                            why_not: "Made by scans." })];
    renderSection();
    const proxies = await row("Proxies and thumbnails");
    expect(within(proxies).queryByRole("button", { name: /Pause|Resume/ })).toBeNull();
  });

  it("says why the server refused a pause", async () => {
    producers = [producer({})];
    answers = [err(403, "forbidden", "Admins only.")];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Pause Descriptions and tags" }));
    expect((await within(await row("Descriptions and tags")).findByRole("alert")).textContent).toContain("Admins only.");
  });

  it("shows a yellow Partly paused naming the parts; Pause all then Resume all turns them all on", async () => {
    queue = { ...queue, live: true, at: new Date().toISOString(), scans_paused: true };
    producers = [producer({}), producer({ artifact: "ocr", title: "Text in images (OCR)", paused: true })];
    renderSection();
    const partly = await screen.findByText("Partly paused");
    expect(partly.closest("[data-state]")?.getAttribute("data-state")).toBe("partly");
    expect((await screen.findByText(/^Paused on their own:/)).textContent).toBe(
      "Paused on their own: scans, Text in images (OCR). Pause all stops everything; Resume all then turns " +
      "everything back on, these included.");
    fireEvent.click(screen.getByRole("button", { name: "Pause all processing" }));
    fireEvent.click(await screen.findByRole("button", { name: "Resume all processing" }));
    expect(await screen.findByText("Running")).toBeTruthy();
    expect(screen.queryByText(/Scans are paused/)).toBeNull();
    await waitFor(async () => expect((await row("Text in images (OCR)")).textContent).not.toContain("Paused"));
  });

  it("the header says what's paused even when the counts can't load or are for another scope", async () => {
    queue = { ...queue, live: true, at: new Date().toISOString(),
              paused_producers: [{ artifact: "ocr", title: "Text in images (OCR)" }] };
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) =>
      new URL(url, "http://x").pathname === "/v1/producers" ? err(403, "forbidden", "Editors only.") : base(url, init));
    renderSection();
    expect(await screen.findByText("Partly paused")).toBeTruthy();
    expect((await screen.findByText(/^Paused on their own:/)).textContent).toContain("Text in images (OCR)");
  });

  it("shows everyone what's paused; only admins pause or resume", async () => {
    role = "viewer";
    queue = { ...queue, live: true, at: new Date().toISOString(), scans_paused: true,
              scans_paused_at: "2026-10-09T10:00:00Z" };
    producers = [producer({ paused: true, paused_at: "2026-10-09T10:00:00Z" })];
    renderSection();
    expect(await screen.findByText(/Scans are paused/)).toBeTruthy();
    expect(await screen.findByText("Partly paused")).toBeTruthy();
    expect((await row("Descriptions and tags")).textContent).toContain("Paused: nothing more of it starts");
    expect(screen.queryByRole("button", { name: /Pause|Resume/ })).toBeNull();
    expect(screen.getByText("Only admins can pause processing or stop a redo.")).toBeTruthy();
    // What Pause all and Resume all would do is for those who can press them.
    expect((await screen.findByText(/^Paused on their own:/)).textContent).toBe(
      "Paused on their own: scans, Descriptions and tags.");
  });

  it("says one clip right", async () => {
    producers = [producer({ counts: { applicable: 1, current: 0, stale: 1, missing: 0, failing: 0 } })];
    renderSection();
    expect((await row("Descriptions and tags")).textContent).toContain("Redoing 1 clip, after anything missing.");
  });

  it("shows why clips fail, and tries them again", async () => {
    producers = [producer({ counts: { applicable: 10, current: 6, stale: 0, missing: 1, failing: 3, given_up: 1 } })];
    failures = [
      { asset_id: "ast_1", artifact: "vision", title: "Descriptions and tags", rel_path: "Day 1/a.jpg",
        library_id: "lib_1", library_name: "Footage", media_type: "image", error: "the model said nothing",
        attempts: 10, failed_at: "2026-10-09T01:00:00Z", retry_at: null, given_up: true },
      { asset_id: "ast_2", artifact: "vision", title: "Descriptions and tags", rel_path: "Day 1/b.jpg",
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
      artifact: "vision", asset_ids: ["ast_1"] });
    fireEvent.click(within(vision).getByRole("button", { name: "Try all again" }));
    await waitFor(() => expect(sent.filter((s) => s.url.endsWith("/producers/failures/retry")).length).toBe(2));
    expect(sent.filter((s) => s.url.endsWith("/producers/failures/retry"))[1].body).toEqual({ artifact: "vision" });
  });

  it("lets viewers see failures but not try them again", async () => {
    role = "viewer";
    producers = [producer({ counts: { applicable: 10, current: 9, stale: 0, missing: 0, failing: 1, given_up: 0 } })];
    failures = [{ asset_id: "ast_1", artifact: "vision", title: "Descriptions and tags", rel_path: "a.jpg",
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
    await waitFor(() => expect(sent.some((s) => s.url.includes("/producers/failures?artifact=vision&library_id=lib_1"))).toBe(true));
  });

  it("shows what the scheduler is doing now and what's next", async () => {
    queue = { live: true, at: new Date().toISOString(), running: { render: 1, vision: 2, redo_transcript: 1 },
              waiting: { vision: 40, scan: 1 }, pools: {}, gpu_hold: 1 };
    renderSection();
    const now = await screen.findByLabelText("Now");
    expect(now.textContent).toContain("Now: 1 analysis copy, 2 descriptions, 1 redo of transcripts");
    expect(now.textContent).toContain("Next: 40 descriptions, 1 scan");
    expect(now.textContent).toContain("AI machines sharing it take 1 fewer request meanwhile");
  });

  it("says how long until everything is made, and each job running", async () => {
    queue = { live: true, at: new Date().toISOString(), running: { render: 1, vision: 1 }, waiting: {}, pools: {},
              gpu_hold: 0, eta: { producers: { vision: 7500, analysis_proxy: 90 }, pools: {}, caught_up: 7500,
              jobs: [{ kind: "render", artifact: "analysis_proxy", unit: "second", units: 1440, elapsed: 60, left: 250 },
                     { kind: "vision", artifact: "vision", unit: "clip", units: 1, elapsed: 4, left: null }] } };
    producers = [producer({}), producer({ artifact: "analysis_proxy", title: "Analysis proxies", media: ["video"],
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
              eta: { producers: { vision: 0 }, pools: {}, caught_up: 0, jobs: [] } };
    renderSection();
    expect((await screen.findByLabelText("Now")).textContent).toContain("Caught up");
    cleanup();
    queue = { ...(queue as object), eta: { producers: { vision: null }, pools: {}, caught_up: null, jobs: [] } };
    renderSection();
    expect((await screen.findByLabelText("Now")).textContent).toContain(
      "How long until everything is made isn't known yet: it's learned from the jobs as they finish.");
  });

  it("says Paused for paused work, and names it", async () => {
    queue = { live: true, at: new Date().toISOString(), running: {}, waiting: {}, pools: {}, gpu_hold: 0,
              eta: { producers: { vision: null }, pools: {}, caught_up: null, jobs: [],
                     not_counted: [{ artifact: "vision", title: "Descriptions and tags", why: "paused" }] } };
    renderSection();
    expect((await screen.findByLabelText("Now")).textContent).toContain(
      "Paused.");
    cleanup();
    queue = { ...(queue as object), eta: { producers: { vision: null, ocr: 1800 }, pools: { vision: 1800 },
              caught_up: 1800, jobs: [],
              not_counted: [{ artifact: "vision", title: "Descriptions and tags", why: "paused" }] } };
    renderSection();
    expect((await screen.findByLabelText("Now")).textContent).toContain(
      "not counting descriptions and tags (paused)");
  });

  it("names what the headline leaves out, and says a job is late or how short its video is", async () => {
    queue = { live: true, at: new Date().toISOString(), running: { render: 9 }, waiting: {}, pools: {}, gpu_hold: 0,
              eta: { producers: { analysis_proxy: 600 }, pools: { render: 600 }, caught_up: 600,
                     not_counted: [{ artifact: "transcript", title: "Transcripts", why: "no_machine" },
                                   { artifact: "ocr", title: "Text in images (OCR)", why: "not_known_yet" }],
                     jobs: [{ kind: "render", artifact: "analysis_proxy", unit: "second", units: 45, elapsed: 400,
                              left: 0, late: true },
                            ...Array.from({ length: 9 }, () => ({ kind: "render", artifact: "analysis_proxy",
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
              eta: { producers: { ocr: null, transcript: null }, pools: {}, caught_up: 0, jobs: [],
                     not_counted: [{ artifact: "transcript", title: "Transcripts", why: "no_machine" }] } };
    producers = [producer({ artifact: "ocr", title: "Text in images (OCR)" })];
    renderSection();
    expect((await screen.findByLabelText("Now")).textContent).toContain(
      "Caught up, not counting transcripts (no machine doing them now).");
    expect((await row("Text in images (OCR)")).textContent).not.toContain("not known yet");
    cleanup();
    queue = { ...(queue as object), eta: { producers: { ocr: null }, pools: {}, caught_up: null, jobs: [],
              not_counted: [{ artifact: "ocr", title: "Text in images (OCR)", why: "not_known_yet" }] } };
    renderSection();
    expect((await row("Text in images (OCR)")).textContent).toContain("time left not known yet");
  });

  it("asks the scheduler what it's doing once for the whole page, not once a row", async () => {
    queue = { live: true, at: new Date().toISOString(), running: {}, waiting: {}, pools: {}, gpu_hold: 0,
              eta: { producers: {}, pools: {}, caught_up: 0, not_counted: [], jobs: [] } };
    producers = [producer({}), producer({ artifact: "ocr", title: "Text in images (OCR)" }),
                 producer({ artifact: "clip", title: "Visual search (CLIP)" })];
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
    failures = [{ asset_id: "ast_1", artifact: "vision", title: "Descriptions and tags", rel_path: "a.jpg",
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
    producers = [producer({ artifact: "transcript", title: "Transcripts", media: ["video"], fields })];
    renderSection();
    fireEvent.click(await screen.findByText("Settings · version 1"));
  }

  it("lets an admin change what the producer reads, and says why the rest can't change", async () => {
    await openSettings();
    expect(screen.getByText(/Chosen in Settings → AI/)).toBeTruthy();
    expect(screen.queryByLabelText(/^Model/)).toBeNull();  // no input for it
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "800" } });
    expect(screen.getByText(/made again with the new ones/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save the settings of Transcripts" }));
    await waitFor(() => expect(sent.some((r) => r.method === "PUT")).toBe(true));
    const put = sent.find((r) => r.method === "PUT")!;
    expect(put.url).toContain("/producers/transcript/settings");
    expect(put.body).toEqual({ settings: { vad_min_silence_ms: 800 } });
  });

  it("asks before making again what the old settings made", async () => {
    answers = [err(409, "redo_on_change", "New settings make 12 clips of transcripts again.", { clips: 12 })];
    await openSettings();
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "800" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the settings of Transcripts" }));
    expect(await screen.findByText(/make 12 clips of transcripts again/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save and make them again" }));
    await waitFor(() => expect(sent.filter((r) => r.method === "PUT")).toHaveLength(2));
    expect(sent.filter((r) => r.method === "PUT")[1].body).toEqual({ settings: { vad_min_silence_ms: 800 }, redo: true });
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
    producers = [producer({ artifact: "transcript", title: "Transcripts", media: ["video"],
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
    answers = [err(409, "redo_on_change", "New settings make 12 clips of transcripts again.", { clips: 12 })];
    await openSettings();
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "800" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the settings of Transcripts" }));
    expect(await screen.findByText(/make 12 clips of transcripts again/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText(/make 12 clips of transcripts again/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Save and make them again" })).toBeNull();
    expect(sent.filter((r) => r.method === "PUT")).toHaveLength(1);
  });

  it("a change after the question takes the question away: its answer was for other values", async () => {
    answers = [err(409, "redo_on_change", "New settings make 12 clips of transcripts again.", { clips: 12 })];
    await openSettings();
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "800" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the settings of Transcripts" }));
    expect(await screen.findByText(/make 12 clips of transcripts again/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/Shortest silence skipped/), { target: { value: "900" } });
    expect(screen.queryByRole("button", { name: "Save and make them again" })).toBeNull();
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
    expect(screen.queryByText(/made again with the new ones/)).toBeNull();
  });

  it("says why settings can't change in words, not only on hover (phones can't hover)", async () => {
    const why = "Not read by this producer yet: changing it would change nothing it makes.";
    producers = [producer({ artifact: "faces", title: "Faces", fields: [
      { key: "det_size", label: "Detection size", kind: "int", value: 640, default: 640, minimum: null, maximum: null,
        unit: "px", advanced: false, fixed: why },
      { key: "min_confidence", label: "Least confidence", kind: "float", value: 0.5, default: 0.5, minimum: null,
        maximum: null, unit: "", advanced: false, fixed: why },
    ] })];
    renderSection();
    fireEvent.click(await screen.findByText("Settings · version 1"));
    expect(screen.getAllByText(why)).toHaveLength(1);  // said once for all of them
  });

  it("shows editors the settings, not a form", async () => {
    role = "editor";
    await openSettings();
    expect(screen.getByText("Shortest silence skipped")).toBeTruthy();
    expect(screen.getByText("500 ms")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save the settings of Transcripts" })).toBeNull();
  });
});


describe("duration", () => {
  it("says a span of time roughly, as people would", () => {
    expect([30, 90, 3600, 7500, 36000, 3 * 86400, 200000].map(duration)).toEqual(
      ["under a minute", "2 min", "1 h", "2 h 5 min", "10 h", "3 days", "2 days 7 h"]);
    expect(duration(40 * 86400)).toBe("over a month");
  });
});
