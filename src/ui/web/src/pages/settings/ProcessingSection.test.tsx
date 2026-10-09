import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ProcessingSection from "./ProcessingSection";
import type { Producer } from "../../api/client";

const fetchMock = vi.fn();
let role = "admin";
let producers: Producer[] = [];
// What the next POST upgrade answers, in order (then 200).
let answers: Response[] = [];
const sent: { method: string; url: string; body: unknown }[] = [];

function producer(over: Partial<Producer>): Producer {
  return {
    artifact: "vision", producer: "vision", version: "1", title: "Descriptions and tags", media: ["image"],
    uniform: false, settings: { model: "qwen3-vl:8b-instruct", temperature: 0.2 }, settings_hash: "h",
    counts: { applicable: 10, current: 6, stale: 3, missing: 1, failing: 0 }, upgradable: true, why_not: null,
    edited: 0, upgrades: [], ...over,
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
    const m = path.match(/^\/producers\/([^/]+)\/upgrade$/);
    if (m && method === "POST") {
      const next = answers.shift();
      if (next) return next;
      return json({ upgrade_id: "upg_1", artifact: m[1], scope: { kind: "all", id: null, name: null },
                    edits: body.edits ?? "keep", upgrading: 3, skipped_edited: body.edits === "skip" ? 1 : 0,
                    edits_to_replace: body.edits === "replace" ? 1 : 0 });
    }
    if (m && method === "DELETE") return new Response(null, { status: 204 });
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

const posted = () => sent.filter((s) => s.method === "POST").map((s) => s.body);

async function row(title: string) {
  return (await screen.findByRole("heading", { name: title })).closest("li") as HTMLElement;
}

describe("ProcessingSection", () => {
  it("lists each producer with its counts, its settings and why one can't be upgraded", async () => {
    producers = [
      producer({}),
      producer({ artifact: "clip", title: "Visual search (CLIP)", uniform: true }),
      producer({ artifact: "scenes", title: "Scenes", media: ["video"], upgradable: false,
                 why_not: "Finding a video's scenes again isn't built yet." }),
    ];
    renderSection();
    const vision = await row("Descriptions and tags");
    expect(vision.textContent).toContain("6 current · 1 missing · 3 stale");
    expect(within(vision).getByText("qwen3-vl:8b-instruct")).toBeTruthy();
    expect((await row("Visual search (CLIP)")).textContent).toContain("one model for all");
    const scenes = await row("Scenes");
    expect(scenes.textContent).toContain("Can't be upgraded yet. Finding a video's scenes again isn't built yet.");
    expect(within(scenes).queryByRole("button", { name: /Upgrade/ })).toBeNull();
  });

  it("upgrades the stale ones once the admin says yes", async () => {
    producers = [producer({})];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Upgrade 3 stale: Descriptions and tags" }));
    const form = screen.getByRole("form", { name: "Upgrade Descriptions and tags" });
    expect(form.textContent).toContain("Make 3 clips' descriptions and tags again?");
    fireEvent.click(within(form).getByRole("button", { name: "Upgrade" }));
    expect(await screen.findByText("Upgrading 3 clips.")).toBeTruthy();
    expect(posted()).toEqual([{}]);
  });

  it("counts and upgrades one library", async () => {
    producers = [producer({})];
    renderSection();
    await screen.findByRole("option", { name: "Footage" });
    fireEvent.change(screen.getByLabelText("Counts for"), { target: { value: "library:lib_1" } });
    await waitFor(() => expect(sent.some((s) => s.url.includes("/producers?library_id=lib_1"))).toBe(true));
    fireEvent.click(await screen.findByRole("button", { name: "Upgrade 3 stale: Descriptions and tags" }));
    const form = screen.getByRole("form", { name: "Upgrade Descriptions and tags" });
    expect(form.textContent).toContain("again in Footage?");
    fireEvent.click(within(form).getByRole("button", { name: "Upgrade" }));
    await screen.findByText("Upgrading 3 clips.");
    expect(posted()).toEqual([{ library_id: "lib_1" }]);
  });

  it("asks what to do with clips that have edits", async () => {
    producers = [producer({ edited: 1 })];
    answers = [err(409, "edited_clips", "1 of the 3 clips have your edits.",
                   { stale: 3, edited: 1, choices: ["keep", "replace", "skip"] })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Upgrade 3 stale: Descriptions and tags" }));
    fireEvent.click(screen.getByRole("button", { name: "Upgrade" }));
    expect(await screen.findByText("1 of the 3 clips have your edits.")).toBeTruthy();
    const keep = screen.getByRole("radio", { name: /Keep my edits/ }) as HTMLInputElement;
    expect(keep.checked).toBe(true);  // the default
    fireEvent.click(screen.getByRole("radio", { name: /Skip edited clips/ }));
    fireEvent.click(screen.getByRole("button", { name: "Upgrade" }));
    expect(await screen.findByText("Upgrading 3 clips. Skipped 1 clip with your edits.")).toBeTruthy();
    expect(posted()).toEqual([{}, { edits: "skip" }]);
  });

  it("keeps each answer when the server asks another question", async () => {
    producers = [producer({ edited: 1 })];
    answers = [
      err(409, "edited_clips", "1 of the 3 clips have your edits.", { stale: 3, edited: 1 }),
      err(409, "redo_everything", "All 3 clips will be made again.", { stale: 3 }),
    ];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Upgrade 3 stale: Descriptions and tags" }));
    fireEvent.click(screen.getByRole("button", { name: "Upgrade" }));
    fireEvent.click(await screen.findByRole("radio", { name: /Replace my edits/ }));
    fireEvent.click(screen.getByRole("button", { name: "Upgrade" }));
    fireEvent.click(await screen.findByRole("button", { name: "Redo all 3 clips" }));
    await screen.findByText(/Your edits on 1 clip go to history as each is made again\./);
    expect(posted()).toEqual([{}, { edits: "replace" }, { edits: "replace", confirm: true }]);
  });

  it("confirms a producer upgraded all at once with a button naming the count, whatever the counts are for", async () => {
    producers = [producer({ artifact: "clip", title: "Visual search (CLIP)", uniform: true })];
    answers = [err(409, "redo_everything", "All 1,200 clips' visual search (clip) will be made again.",
                   { artifact: "clip", stale: 1200 })];
    renderSection();
    await screen.findByRole("option", { name: "Footage" });
    fireEvent.change(screen.getByLabelText("Counts for"), { target: { value: "library:lib_1" } });
    // Narrowed counts, but CLIP is upgraded everywhere: the button says so.
    fireEvent.click(await screen.findByRole("button", { name: "Upgrade everywhere: Visual search (CLIP)" }));
    expect(screen.getByRole("form").textContent).toContain("upgraded everywhere at once");
    fireEvent.click(screen.getByRole("button", { name: "Upgrade" }));
    fireEvent.click(await screen.findByRole("button", { name: "Re-embed all 1,200 clips" }));
    await screen.findByText("Upgrading 3 clips.");
    expect(posted()).toEqual([{}, { confirm: true }]);  // never narrowed
  });

  it("says why when the server refuses", async () => {
    producers = [producer({})];
    answers = [err(409, "nothing_stale", "Nothing was made with older descriptions and tags settings.")];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Upgrade 3 stale: Descriptions and tags" }));
    fireEvent.click(screen.getByRole("button", { name: "Upgrade" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Nothing was made with older");
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(screen.queryByRole("form")).toBeNull();
  });

  it("shows an upgrade under way and stops it", async () => {
    producers = [producer({ upgrades: [{ upgrade_id: "upg_7", scope: { kind: "library", id: "lib_1", name: "Footage" },
                                         edits: "keep", approved_by: "u", approved_at: "2026-10-08T22:00:00Z",
                                         total: 400, remaining: 120 }] })];
    renderSection();
    const vision = await row("Descriptions and tags");
    expect(vision.textContent).toContain("Upgrading in Footage: 280 of 400 done");
    fireEvent.click(within(vision).getByRole("button", { name: "Stop upgrading Descriptions and tags in Footage" }));
    await waitFor(() => expect(sent.some((s) => s.method === "DELETE")).toBe(true));
    expect(sent.find((s) => s.method === "DELETE")!.url).toContain("/producers/vision/upgrade?upgrade_id=upg_7");
  });

  it("shows everyone the counts; only admins upgrade", async () => {
    role = "viewer";
    producers = [producer({ upgrades: [{ upgrade_id: "upg_7", scope: { kind: "all", id: null, name: null },
                                         edits: "keep", approved_by: "u", approved_at: "2026-10-08T22:00:00Z",
                                         total: 4, remaining: 4 }] })];
    renderSection();
    expect(await screen.findByText("Only admins can upgrade.")).toBeTruthy();
    expect((await row("Descriptions and tags")).textContent).toContain("3 stale");
    expect(screen.queryByRole("button", { name: /Upgrade/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Stop/ })).toBeNull();
  });
});
