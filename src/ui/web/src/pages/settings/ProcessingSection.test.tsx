import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ProcessingSection from "./ProcessingSection";
import type { Producer } from "../../api/client";

const fetchMock = vi.fn();
let role = "admin";
let producers: Producer[] = [];
// What the next stop or resume answers (then 204).
let answers: Response[] = [];
const sent: { method: string; url: string; body: unknown }[] = [];

function producer(over: Partial<Producer>): Producer {
  return {
    artifact: "vision", producer: "vision", version: "1", title: "Descriptions and tags", media: ["image"],
    uniform: false, settings: { model: "qwen3-vl:8b-instruct", temperature: 0.2 }, settings_hash: "h",
    counts: { applicable: 10, current: 6, stale: 3, missing: 1, failing: 0 }, redoable: true, why_not: null,
    paused: false, paused_by: null, paused_at: null, ...over,
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
    const m = path.match(/^\/producers\/([^/]+)\/redo\/(stop|resume)$/);
    if (m && method === "POST") {
      const next = answers.shift();
      if (next) return next;
      producers = producers.map((p) => (p.artifact === m[1] ? { ...p, paused: m[2] === "stop" } : p));
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
  sent.length = 0;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ProcessingSection />
    </QueryClientProvider>,
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
    expect(within(vision).getByText("qwen3-vl:8b-instruct")).toBeTruthy();
    expect((await row("Visual search (CLIP)")).textContent).toContain("one model for all");
    const scenes = await row("Scenes");
    expect(scenes.textContent).toContain("Not made again yet. Finding a video's scenes again isn't built yet.");
    expect(within(scenes).queryByRole("button", { name: /Stop|Resume/ })).toBeNull();
  });

  it("says the stale ones are being redone, with no Upgrade step", async () => {
    producers = [producer({})];
    renderSection();
    const vision = await row("Descriptions and tags");
    expect(vision.textContent).toContain("Redoing 3 clips, after anything missing.");
    expect(screen.queryByRole("button", { name: /Upgrade/ })).toBeNull();
    expect(screen.getByText(/Changing a model in Settings → AI is what starts that/)).toBeTruthy();
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
    producers = [producer({}), producer({ artifact: "ocr", title: "Text in images (OCR)", paused: true })];
    renderSection();
    expect(await screen.findByText("Only admins can stop or resume a redo.")).toBeTruthy();
    expect((await row("Descriptions and tags")).textContent).toContain("Redoing 3 clips");
    expect((await row("Text in images (OCR)")).textContent).toContain("Redo stopped");
    expect(screen.queryByRole("button", { name: /Stop|Resume/ })).toBeNull();
  });

  it("says one clip right", async () => {
    producers = [producer({ counts: { applicable: 1, current: 0, stale: 1, missing: 0, failing: 0 } })];
    renderSection();
    expect((await row("Descriptions and tags")).textContent).toContain("Redoing 1 clip, after anything missing.");
  });
});
