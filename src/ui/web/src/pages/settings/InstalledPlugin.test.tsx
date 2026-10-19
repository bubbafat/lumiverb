import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import PluginsSection from "./PluginsSection";
import type { PluginRunning, PluginSetting } from "../../api/types";

const fetchMock = vi.fn();
let serverAdmin = true;
let running: PluginRunning = { state: "ready", memory_mb: 21, restarts: 0 };
let chosen: Record<string, unknown> = {};
let tokenSet = false;
const sent: { method: string; path: string; body: unknown }[] = [];

function setting(over: Partial<PluginSetting> & Pick<PluginSetting, "key" | "type" | "label">): PluginSetting {
  return { help: "", default: null, value: null, set: false, choices: [], scope: "account", advanced: false,
           remakes: false, editable: true, ...over };
}

function hello() {
  const value = (key: string, fallback: unknown) => (key in chosen ? chosen[key] : fallback);
  const settings: PluginSetting[] = [
    setting({ key: "greeting", type: "text", label: "Greeting", help: "What it says.", default: "Hello",
              value: value("greeting", "Hello") as string, set: "greeting" in chosen, max_length: 40 }),
    setting({ key: "times", type: "int", label: "Times", help: "How many times it says it.", default: 1,
              value: value("times", 1) as number, minimum: 1, maximum: 5, unit: "times", remakes: true }),
    setting({ key: "loud", type: "bool", label: "Loud", help: "Whether it shouts.", default: false,
              value: value("loud", false) as boolean }),
    setting({ key: "language", type: "choice", label: "Language", help: "Which language.", default: "en",
              value: value("language", "en") as string,
              choices: [{ value: "en", label: "English" }, { value: "fr", label: "French" }] }),
    ...(serverAdmin ? [setting({ key: "token", type: "secret", label: "Token", help: "A token it never uses.",
                                 scope: "server", advanced: true, set: tokenSet })] : []),
  ];
  return {
    plugin_id: "hello", title: "Hello", description: "A plug-in for tests.", state: "on", acknowledgment: null,
    acknowledged: null, data: null, kind: "installed", version: "2", author: "Lumiverb's tests",
    homepage: "https://example.org/hello", on_by_default: true, follows_default: true,
    reads: ["each clip's location (a person's, else its GPS, else an applied guess)"],
    answers: ["answers place searches (Set location's Place)"],
    downloads: [{ url: "https://download.example.org/words.zip", title: "Words", about_mb: 12 }], internet: false,
    running, settings, unchecked: null as string | null,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    sent.push({ method, path, body });
    if (path.endsWith("/me")) return json({ user_id: "u1", email: "a@example.com", role: "admin", server_admin: serverAdmin });
    if (path === "/plugins") return json({ plugins: [hello()] });
    if (path === "/plugins/hello/settings" && method === "PUT") {
      const settings = (body as { settings: Record<string, unknown> }).settings;
      if (settings.times === 9) {
        return json({ error: { code: "invalid_settings", message: "times: Times is from 1 to 5",
                               details: { errors: { times: "Times is from 1 to 5" } } } }, 422);
      }
      if (settings.times === 4 && (body as { redo?: boolean }).redo === undefined) {
        return json({ error: { code: "redo_on_change", message: "12 clips of hello were made with the old settings.",
                               details: { clips: 12, question: "remake" } } }, 409);
      }
      chosen = { ...chosen, ...settings };
      return json({ ...hello(), unchecked: "Hello isn't running: its own check didn't run" });
    }
    if (path === "/plugins/hello/server-settings" && method === "PUT") {
      tokenSet = (body as { settings: { token: string | null } }).settings.token !== null;
      return json(hello());
    }
    return json({}, 404);
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  serverAdmin = true;
  running = { state: "ready", memory_mb: 21, restarts: 0 };
  chosen = {};
  tokenSet = false;
  sent.length = 0;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PluginsSection />
    </QueryClientProvider>,
  );
}

const puts = () => sent.filter((s) => s.method === "PUT");
const lastPut = () => puts()[puts().length - 1];

describe("An installed plug-in in Settings › Plug-ins", () => {
  it("says who made it, that it runs, its default, and what it reads and reaches", async () => {
    renderSection();
    expect(await screen.findByText("Running")).toBeTruthy();
    expect(screen.getByText(/Version 2/).textContent).toContain("by Lumiverb's tests");
    expect((screen.getByRole("link", { name: "About it" }) as HTMLAnchorElement).href).toBe("https://example.org/hello");
    expect(screen.getByText("On by default, for every account that hasn't turned it on or off.")).toBeTruthy();
    for (const line of ["Reads each clip's location (a person's, else its GPS, else an applied guess)",
                        "Answers place searches (Set location's Place)",
                        "Downloads Words (about 12 MB) from download.example.org", "Doesn't use the internet"]) {
      expect(screen.getByText(line)).toBeTruthy();
    }
  });

  it.each([
    [{ state: "stopped", why: "It used 300 MB of memory, more than its 256 MB" }, "Stopped: It used 300 MB of memory, more than its 256 MB"],
    [{ state: "restarting", why: "It stopped (exit code 3)" }, "Stopped: It stopped (exit code 3). Starting it again."],
    [{ state: "failing", why: "Its list is broken" }, "Not working: Its list is broken"],
    [{ state: "not_running", why: "The plug-in runner isn't running" }, "The plug-in runner isn't running"],
    [{ state: "preparing", detail: "Reading the list", done: 3, total: 10 }, "Getting ready: Reading the list (3 of 10)"],
    [{ state: "ready", detail: "Fetching a newer list", done: 120, total: 610 }, "Fetching a newer list (120 of 610)"],
    [{ state: "off", why: "It's off in every account: it starts when an account turns it on" },
     "Not running: It's off in every account: it starts when an account turns it on"],
  ] as [PluginRunning, string][])("says what its process is doing: %o", async (state, said) => {
    running = { restarts: 0, ...state };
    renderSection();
    expect(await screen.findByText(said)).toBeTruthy();
  });

  it("builds its form from its schema: each setting its control, unit and help", async () => {
    renderSection();
    const form = await screen.findByRole("form", { name: "Hello: its settings" });
    expect((within(form).getByLabelText("Greeting") as HTMLInputElement).value).toBe("Hello");
    const times = within(form).getByLabelText(/Times/) as HTMLInputElement;
    expect([times.type, times.min, times.max, times.step, times.value]).toEqual(["number", "1", "5", "1", "1"]);
    expect(within(form).getByText("How many times it says it. Changing it asks what becomes of what it made.")).toBeTruthy();
    expect((within(form).getByLabelText("Loud") as HTMLInputElement).type).toBe("checkbox");
    const language = within(form).getByLabelText("Language") as HTMLSelectElement;
    expect(Array.from(language.options).map((o) => o.text)).toEqual(["English", "French"]);
    const server = screen.getByRole("form", { name: "Hello: for the whole server" });
    const token = within(server).getByLabelText("Token") as HTMLInputElement;
    expect([token.type, token.placeholder, token.value]).toEqual(["password", "Not set", ""]);
  });

  it("saves only what changed, and shows each error beside its field", async () => {
    renderSection();
    const form = await screen.findByRole("form", { name: "Hello: its settings" });
    const save = within(form).getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(within(form).getByLabelText(/Times/), { target: { value: "9" } });
    fireEvent.click(save);
    expect(await within(form).findByText("Times is from 1 to 5")).toBeTruthy();
    fireEvent.change(within(form).getByLabelText(/Times/), { target: { value: "3" } });
    fireEvent.change(within(form).getByLabelText("Language"), { target: { value: "fr" } });
    fireEvent.click(save);
    expect(await within(form).findByText("Saved. Hello isn't running: its own check didn't run.")).toBeTruthy();
    expect(puts().map((p) => [p.path, p.body])).toEqual([
      ["/plugins/hello/settings", { settings: { times: 9 } }],
      ["/plugins/hello/settings", { settings: { times: 3, language: "fr" } }],
    ]);
    expect(within(form).queryByText("Times is from 1 to 5")).toBeNull();
  });

  it("asks what becomes of what it made when a setting that remakes changes, and sends the answer", async () => {
    renderSection();
    const form = await screen.findByRole("form", { name: "Hello: its settings" });
    fireEvent.change(within(form).getByLabelText(/Times/), { target: { value: "4" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    const asked = await within(form).findByRole("alert");
    expect(asked.textContent).toContain("12 clips of hello were made with the old settings.");
    fireEvent.click(within(asked).getByRole("button", { name: "New ones only" }));
    expect(await within(form).findByText(/^Saved\./)).toBeTruthy();
    expect(puts().map((p) => p.body)).toEqual([{ settings: { times: 4 } }, { settings: { times: 4 }, redo: false }]);
    expect(within(form).queryByRole("alert")).toBeNull();
  });

  it("sets a secret for the server, never shows it, and clears it", async () => {
    renderSection();
    const server = await screen.findByRole("form", { name: "Hello: for the whole server" });
    fireEvent.change(within(server).getByLabelText("Token"), { target: { value: "s3cret" } });
    fireEvent.click(within(server).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(lastPut()).toEqual({ method: "PUT", path: "/plugins/hello/server-settings",
                                                       body: { settings: { token: "s3cret" } } }));
    const token = await within(server).findByPlaceholderText("Set: type to replace it") as HTMLInputElement;
    expect(token.value).toBe("");
    fireEvent.click(within(server).getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(lastPut()?.body).toEqual({ settings: { token: null } }));
  });

  it("shows an account admin who isn't a server admin the account's settings only", async () => {
    serverAdmin = false;
    renderSection();
    expect(await screen.findByRole("form", { name: "Hello: its settings" })).toBeTruthy();
    expect(screen.queryByRole("form", { name: "Hello: for the whole server" })).toBeNull();
  });
});
