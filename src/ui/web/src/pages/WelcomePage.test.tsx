import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import WelcomePage from "./WelcomePage";

const fetchMock = vi.fn();
const STEPS = ["storage", "network", "find", "ai", "people", "library"];
let role = "admin";
// The install's admin too: an account admin who isn't has no say in where previews live or who can open it.
let serverAdmin = true;
let done: string[] = [];
let detail = "normal";
const sent: { method: string; path: string; body: unknown }[] = [];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

function producer(artifact: string, fields: unknown[]) {
  return { artifact, producer: artifact, version: "1", title: artifact, media: ["image"], uniform: false, settings: {},
           settings_hash: "", fields, counts: null, scheduled: true, redoable: true, why_not: null, redo_stopped: false,
           redo_stopped_by: null, redo_stopped_at: null, paused: false, paused_by: null, paused_at: null, waiting: null };
}

function levelField(value: string) {
  return { key: "detail", label: "Detail", kind: "level", value, default: value, minimum: null, maximum: null, unit: "",
           advanced: false, fixed: null, remakes: false, choices: ["off", "low", "normal", "high"] };
}

beforeEach(() => {
  role = "admin";
  serverAdmin = true;
  done = [];
  detail = "normal";
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("scrollTo", () => {});
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    sent.push({ method, path, body });
    // The caller's steps, as the server says (routers/tenant.py).
    const steps = serverAdmin ? STEPS : STEPS.slice(2);
    const progress = () => ({ steps, done, dismissed: false, finished: steps.every((s) => done.includes(s)) });
    if (path === "/me") return json({ email: "a@b.c", role, server_admin: role === "admin" && serverAdmin });
    if (path === "/tenant/setup" && method === "GET") return json(progress());
    if (path === "/tenant/setup") {
      done = body.done ?? done;
      return json(progress());
    }
    if (path === "/system/storage") {
      return json({ previews: "/d", data: "/d", previews_available: true, previews_free: 1, previews_total: 2,
                    data_free: 1, data_total: 2, same_disk: true, moving: null, changeable_here: true, libraries: [] });
    }
    if (path === "/system/backups") {
      return json({ folder: "/d/backups", default_folder: "/d/backups", changeable_here: true, running: null,
                    last_error: null, last_failed_at: null, next_at: new Date().toISOString(), backups: [] });
    }
    if (path === "/system/network") return json({ available: true, other_devices: false, addresses: [] });
    if (path === "/producers") {
      return json({ producers: [producer("faces", [levelField(detail)]), producer("video_faces", [levelField("low")]),
                                producer("location", [{ key: "infer_location", label: "Infer", kind: "bool", value: false,
                                                        default: false, minimum: null, maximum: null, unit: "",
                                                        advanced: false, fixed: null }])] });
    }
    if (path === "/producers/faces/settings") {
      detail = body.settings.detail;
      return json(producer("faces", [levelField(detail)]));
    }
    if (path === "/libraries" && method === "POST") {
      return json({ library_id: "lib_1", name: body.name, root_path: body.root_path, is_public: false, cover_asset_id: null });
    }
    return json({}, 404);
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  sent.length = 0;
});

function renderPage(path = "/welcome") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/welcome" element={<WelcomePage />} />
          <Route path="/" element={<p>Libraries page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const stepTitle = () => screen.getByRole("heading", { level: 2 }).textContent;

describe("WelcomePage", () => {
  it("starts with where previews live, says why and what skipping costs, and goes on", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { level: 2, name: "Where previews live" })).toBeTruthy();
    expect(screen.getByText(/Moving them later copies them all/)).toBeTruthy();
    expect(screen.getByText(/They stay on this computer's drive/)).toBeTruthy();
    expect(await screen.findByText("Backups")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Who can open it" })).toBeTruthy();
    expect(sent.find((s) => s.method === "PUT" && s.path === "/tenant/setup")?.body).toEqual({ done: ["storage"] });
  });

  it("picks up at the first step not done, and every step can be visited", async () => {
    done = ["storage", "network"];
    renderPage();
    expect(await screen.findByRole("heading", { level: 2, name: "What to find" })).toBeTruthy();
    const steps = screen.getByRole("list", { name: "Steps" });
    expect(within(steps).getByRole("button", { name: "✓ Where previews live" })).toBeTruthy();
    fireEvent.click(within(steps).getByRole("button", { name: "5. People" }));
    expect(stepTitle()).toBe("People");
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(stepTitle()).toBe("AI machines");
  });

  it("sets how faces are found", async () => {
    done = ["storage", "network"];
    renderPage();
    const photos = await screen.findByRole("combobox", { name: "Faces in photos" });
    expect((photos as HTMLSelectElement).value).toBe("normal");
    fireEvent.change(photos, { target: { value: "high" } });
    await vi.waitFor(() => expect(sent.some((s) => s.path === "/producers/faces/settings")).toBe(true));
    expect(sent.find((s) => s.path === "/producers/faces/settings")?.body).toEqual({ settings: { detail: "high" } });
  });

  it("adds the first library, then finishing goes to it", async () => {
    done = STEPS.slice(0, 5);
    renderPage();
    expect(await screen.findByRole("heading", { level: 2, name: "Your first library" })).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "Library name" }), { target: { value: "Family" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Library folder" }), { target: { value: "/Volumes/Media/Photos" } });
    fireEvent.click(screen.getByRole("button", { name: "Add library" }));
    expect((await screen.findByRole("status")).textContent).toContain("Family is added");
    fireEvent.click(screen.getByRole("button", { name: "Finish" }));
    expect(await screen.findByText("Libraries page")).toBeTruthy();
    expect(done).toEqual(STEPS);
  });

  it("can be left and finished later", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "I'll finish later" }));
    expect(await screen.findByText("Libraries page")).toBeTruthy();
    expect(sent.some((s) => s.method === "PUT")).toBe(false);
  });

  it("leaves where previews live and who can open it to the server admin", async () => {
    serverAdmin = false;
    renderPage();
    expect(await screen.findByRole("heading", { level: 2, name: "What to find" })).toBeTruthy();
    const steps = screen.getByRole("list", { name: "Steps" });
    expect(within(steps).getAllByRole("button").map((b) => b.textContent)).toEqual(
      ["1. What to find", "2. AI machines", "3. People", "4. Your first library"]);
    expect(sent.some((s) => s.path === "/system/storage" || s.path === "/system/network")).toBe(false);
  });

  it("is an admin's", async () => {
    role = "editor";
    renderPage();
    expect(await screen.findByText("Only an admin sets Lumiverb up.")).toBeTruthy();
  });
});
