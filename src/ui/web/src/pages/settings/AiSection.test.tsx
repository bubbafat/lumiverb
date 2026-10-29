import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import AiSection, { aiHasProblem, ago } from "./AiSection";
import type { AiMachine, AiSettings } from "../../api/client";

const GAMING_PC = "http://172.18.0.6:11434/v1";
const STUDIO = "http://10.10.10.1:11434/v1";
const QWEN = "qwen3-vl:8b";
const SPEACHES = "http://10.10.10.2:8000/v1";
// What the built-in Whisper offers (faster-whisper's models).
const WHISPERS = ["large-v3", "medium", "small"];
const fetchMock = vi.fn();
let role = "admin";
let model = QWEN;
// The transcripts job's model; off unless a test says.
let whisper = "";
let machines: AiMachine[] = [];
// What each URL offers when asked; missing: unreachable.
let offers: Record<string, string[]> = {};
// Clips the old model made: a new one asks first (PUT answers 409 redo_on_change).
let made = 0;
// Jobs beyond descriptions and transcripts a test lists (CLIP's, fixed).
let more: (ReturnType<typeof job> & { fixed?: string | null })[] = [];
const sent: { method: string; url: string; body: unknown }[] = [];

function machine(over: Partial<AiMachine>): AiMachine {
  return {
    machine_id: `aim_${over.name?.replace(/ /g, "_")}`, name: "Gaming PC", api_url: GAMING_PC, has_key: false, jobs: ["vision"], at_once: 2,
    enabled: true, built_in: false,
    status: { online: true, error: "", models: [QWEN], checked_at: new Date().toISOString() }, ...over,
  };
}

function builtIn(over: Partial<AiMachine> = {}): AiMachine {
  return machine({ machine_id: "aim_self", name: "Built in", api_url: "", jobs: ["transcripts"], at_once: 1,
                   built_in: true, status: { online: true, error: "", models: WHISPERS, checked_at: new Date().toISOString() },
                   ...over });
}

function job(name: string, label: string, chosen: string, builtInCan: boolean) {
  const doing = machines.filter((m) => m.enabled && m.jobs.includes(name));
  const offers = (m: AiMachine) => (m.built_in ? WHISPERS : m.status?.models ?? []);
  return { job: name, label, model: chosen, machines: doing.length,
           offering: doing.filter((m) => m.status?.online && offers(m).includes(chosen)).length,
           choices: [...new Set(doing.flatMap(offers))].sort(), built_in: builtInCan };
}

function settings(): AiSettings {
  return {
    machines,
    jobs: [job("vision", "Descriptions & text", model, false), job("transcripts", "Transcripts", whisper, true), ...more],
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

function err(status: number, code: string, message: string, details: unknown = {}) {
  return json({ error: { code, message, details } }, status);
}

let assisted = false; // the account lets searches ask its AI machine (Settings › AI › Search)

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    sent.push({ method, url, body });
    const path = new URL(url, "http://x").pathname.replace(/^\/v1/, "");
    if (path === "/me") return json({ email: "a@b.c", role });
    if (path === "/tenant/settings") {
      if (method === "PATCH") assisted = body.assisted_search;
      return json({ video_preview_max_seconds: null, public_video_preview_max_seconds: 10, assisted_search: assisted });
    }
    if (path === "/ai" && method === "GET") return json(settings());
    if (path === "/ai/connect") {
      const models = offers[body.api_url.replace(/\/+$/, "")];
      return models ? json({ models }) : err(502, "machine_unreachable", `Couldn't reach ${body.api_url}: ConnectionError.`);
    }
    if (path === "/ai/machines" && method === "POST") {
      const models = offers[body.api_url] ?? [];
      machines = [...machines, machine({ ...body, has_key: !!body.api_key, status: { online: true, error: "", models, checked_at: null } })];
      return json(settings(), 201);
    }
    const m = path.match(/^\/ai\/machines\/([^/]+)$/);
    if (m) {
      const target = machines.find((x) => x.machine_id === m[1])!;
      const after = method === "DELETE" ? null : { ...target, ...body };
      if (target.built_in && (body?.api_url !== undefined || body?.api_key !== undefined)) {
        return err(409, "built_in_machine", "The built-in machine is the worker's own computer: it has no URL or key.");
      }
      machines = after ? machines.map((x) => (x === target ? { ...after, has_key: body.api_key === undefined ? x.has_key : !!body.api_key } : x))
                       : machines.filter((x) => x !== target);
      return json(settings());
    }
    const tried = path.match(/^\/ai\/machines\/([^/]+)\/try$/);
    if (tried) {
      return json({ machine: "Gaming PC", model: body.model || model,
                    sent: "a picture Lumiverb draws (a red house on a hill, and a sign reading OPEN 9 TO 5), and a video's three scenes",
                    tries: [
                      { titles: ["Scene descriptions", "Descriptions and tags"], answer: '{"description": "A red house."}',
                        result: { description: "A red house.", tags: ["house"] }, problem: "", finish: "stop",
                        tokens: { prompt: 1200, answer: 64 }, limit: 500, seconds: 2.4 },
                      { titles: ["Text in images (OCR)"], answer: "", result: null, finish: "length", tokens: null,
                        problem: "It ran out of room: it used all 500 tokens of its answer before finishing (a model that thinks first can spend them all thinking).",
                        limit: 500, seconds: 9.1 },
                    ] });
    }
    if (path === "/ai/jobs/vision" && method === "PUT") {
      if (made && body.model && body.model !== model && body.redo === undefined) {
        return err(409, "redo_on_change",
                   `${made} clips were made with the old model: descriptions and tags (${made}). New files only with ${body.model}, and they stay as they are until you remake them; or remake them too, after everything else.`,
                   { job: "vision", model: body.model, clips: made, question: "remake",
                     artifacts: [{ artifact: "lumiverb.image.description", title: "Descriptions and tags", clips: made }] });
      }
      if (body.model && !machines.some((x) => offers[x.api_url ?? ""]?.includes(body.model))) {
        return err(409, "model_not_offered", `No machine doing descriptions & text offers ${body.model}.`,
                   { job: "vision", model: body.model, machines: machines.map((x) => ({ name: x.name, models: offers[x.api_url ?? ""] ?? [], error: offers[x.api_url ?? ""] ? "" : "Couldn't reach it." })) });
      }
      model = body.model;
      return json(settings());
    }
    return json({}, 404);
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  role = "admin";
  model = QWEN;
  whisper = "";
  machines = [];
  offers = {};
  made = 0;
  more = [];
  sent.length = 0;
  assisted = false;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AiSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const lastSent = (method: string, part: string) => [...sent].reverse().find((s) => s.method === method && s.url.includes(part));

describe("AiSection", () => {
  it("lists each machine with its jobs, how many at once and how it's doing", async () => {
    machines = [
      machine({ name: "Gaming PC 3080" }),
      machine({ name: "Mac Studio", api_url: STUDIO, at_once: 4,
                status: { online: false, error: `Couldn't reach ${STUDIO}: ConnectionError.`, models: [], checked_at: new Date().toISOString() } }),
      machine({ name: "Spare", enabled: false, status: null }),
    ];
    renderSection();
    const gamingPc = (await screen.findByText("Gaming PC 3080")).closest("li")!;
    expect(within(gamingPc).getByText("Does: Descriptions & text · 2 at once")).toBeTruthy();
    expect(within(gamingPc).getByText(/Online · checked just now/)).toBeTruthy();
    const studio = screen.getByText("Mac Studio").closest("li")!;
    expect(within(studio).getByRole("alert").textContent).toContain("Offline: Couldn't reach");
    expect(within(studio).getByText(/4 at once/)).toBeTruthy();
    expect(within(screen.getByText("Spare").closest("li")!).getByText("Turned off: gets no work.")).toBeTruthy();
    expect(screen.getByText(/offered by 1 of 2 machines/)).toBeTruthy();
  });

  it("says plainly when a job can't run, and when it's off", async () => {
    machines = [machine({ status: { online: false, error: "no answer", models: [], checked_at: null } })];
    renderSection();
    expect((await screen.findByText("Descriptions & text: paused")).closest("[role=alert]")).toBeTruthy();
    cleanup();
    model = "";
    renderSection();
    expect(await screen.findByText(/Descriptions & text are off: no model is chosen/)).toBeTruthy();
  });

  it("points to the README's Ollama how-to where descriptions need a machine", async () => {
    model = "";
    whisper = "small";
    renderSection();
    const off = (await screen.findByText(/Descriptions & text are off/)).closest("[role=status]") as HTMLElement;
    const guide = within(off).getByRole("link", { name: "How to set up Ollama" });
    expect(guide.getAttribute("href")).toBe("https://github.com/bubbafat/lumiverb#ai-descriptions-with-ollama");
    expect(guide.getAttribute("target")).toBe("_blank");
    expect(screen.getByRole("link", { name: "how to set it up" }).getAttribute("href")).toBe(guide.getAttribute("href"));
    cleanup();
    // A model, but no machine doing it: paused, and the how-to again.
    model = QWEN;
    renderSection();
    const paused = (await screen.findByText("Descriptions & text: paused")).closest("[role=alert]") as HTMLElement;
    expect(within(paused).getByRole("link", { name: "How to set up Ollama" })).toBeTruthy();
    cleanup();
    // Transcripts without a machine: Ollama doesn't transcribe, so no how-to there.
    whisper = "small";
    model = QWEN;
    machines = [machine({})];
    renderSection();
    const transcripts = (await screen.findByText("Transcripts: paused")).closest("[role=alert]") as HTMLElement;
    expect(within(transcripts).queryByRole("link")).toBeNull();
  });

  it("a viewer sees it all and changes nothing", async () => {
    role = "viewer";
    // An address is an account admin's: the API sends anyone else null, and says it as the machine's name.
    machines = [machine({ name: "Gaming PC", api_url: null }),
                machine({ name: "Mac Studio", api_url: null,
                          status: { online: false, error: "Mac Studio didn't answer (ConnectionError)", models: [],
                                    checked_at: new Date().toISOString() } })];
    renderSection();
    const gamingPc = (await screen.findByText("Gaming PC")).closest("li")!;
    expect(within(gamingPc).getByText("Does: Descriptions & text · 2 at once")).toBeTruthy();
    expect(within(gamingPc).getByText(/Online · checked just now/)).toBeTruthy();
    expect(gamingPc.querySelector(".font-mono"), "no empty line where its address would be").toBeNull();
    const studio = screen.getByText("Mac Studio").closest("li")!;
    expect(within(studio).getByRole("alert").textContent).toContain("Mac Studio didn't answer (ConnectionError)");
    await screen.findByText("Only admins can change these.");
    expect(screen.queryByRole("button", { name: "Add machine" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit Gaming PC" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Change the model/ })).toBeNull();
  });

  it("adds a machine once Connect has checked it", async () => {
    machines = [machine({ name: "Gaming PC" })];
    offers = { [STUDIO]: [QWEN, "llava:13b"] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Add machine" }));
    const form = screen.getByRole("form", { name: "Add a machine" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Mac Studio" } });
    fireEvent.change(within(form).getByLabelText("Endpoint URL"), { target: { value: STUDIO } });
    fireEvent.change(within(form).getByLabelText("Requests at once"), { target: { value: "4" } });
    const add = within(form).getByRole("button", { name: "Add" }) as HTMLButtonElement;
    expect(add.disabled).toBe(true); // not checked yet
    fireEvent.click(within(form).getByRole("button", { name: "Connect" }));
    expect((await within(form).findByText(/Connected: offers/)).textContent).toContain(QWEN);
    await waitFor(() => expect(add.disabled).toBe(false));
    fireEvent.click(add);
    await waitFor(() => expect(screen.queryByRole("form", { name: "Add a machine" })).toBeNull());
    expect(lastSent("POST", "/ai/machines")!.body).toEqual({ name: "Mac Studio", api_url: STUDIO, api_key: "", jobs: ["vision"], at_once: 4, enabled: true,
                                                            shares_gpu: false });
    expect(await screen.findByText("Mac Studio")).toBeTruthy();
  });

  it("offers where a machine is: each fills in its URL and name, and says what to know; a custom URL stays", async () => {
    // The demo server, 10-21: each provider had a catch of its own (OpenAI's URL, Anthropic's models list, Gemini's
    // names and thinking models).
    const anthropic = "https://api.anthropic.com/v1";
    offers = { [anthropic]: ["claude-haiku-4-5-20251001", "claude-opus-4-6"] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Add machine" }));
    const form = screen.getByRole("form", { name: "Add a machine" });
    const where = within(form).getByLabelText("Where") as HTMLSelectElement;
    expect(where.value).toBe("custom");
    expect((within(form).getByLabelText("Endpoint URL") as HTMLInputElement).value).toBe("");
    expect([...where.options].map((o) => o.text)).toEqual(["Custom URL", "Ollama on this computer",
      "Ollama on another computer", "OpenAI", "Anthropic (Claude)", "Google Gemini"]);

    fireEvent.change(where, { target: { value: "anthropic" } });
    expect((within(form).getByLabelText("Endpoint URL") as HTMLInputElement).value).toBe(anthropic);
    expect((within(form).getByLabelText("Name") as HTMLInputElement).value).toBe("Claude");
    const about = within(form).getByRole("list", { name: "About Anthropic (Claude)" });
    expect(about.textContent).toContain("Needs an API key: console.anthropic.com › API keys.");
    expect(about.textContent).toContain("Works for descriptions & text: claude-haiku-4-5-20251001");

    // Another choice replaces a name it filled in, never one typed; the URL stays editable.
    fireEvent.change(where, { target: { value: "ollama" } });
    expect((within(form).getByLabelText("Name") as HTMLInputElement).value).toBe("Ollama");
    expect(within(form).getByRole("list", { name: "About Ollama on this computer" }).textContent).toContain(
      "Needs no key.");
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Den PC" } });
    fireEvent.change(where, { target: { value: "anthropic" } });
    expect((within(form).getByLabelText("Name") as HTMLInputElement).value).toBe("Den PC");
    fireEvent.change(within(form).getByLabelText("API key"), { target: { value: "sk-ant-x" } });
    fireEvent.click(within(form).getByRole("button", { name: "Connect" }));
    expect((await within(form).findByText(/Connected: offers/)).textContent).toContain("claude-haiku-4-5-20251001");
    expect(within(form).queryByText(/doesn't list/)).toBeNull();
  });

  it("an edited machine has no Where: its URL is its own", async () => {
    machines = [machine({ name: "Gaming PC" })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Edit Gaming PC" }));
    expect(within(screen.getByRole("form", { name: "Edit Gaming PC" })).queryByLabelText("Where")).toBeNull();
  });

  it("tests a machine: what each request came back with, its room used, and why one wouldn't do", async () => {
    machines = [machine({ name: "Gaming PC" })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Test Gaming PC" }));
    const results = await screen.findByLabelText("Test of Gaming PC");
    expect(lastSent("POST", "/try")!.body).toEqual({ model: "" }); // the job's model
    expect(results.textContent).toContain(`Sent a picture Lumiverb draws`);
    expect(within(results).getByText("Scene descriptions, Descriptions and tags")).toBeTruthy();
    expect(within(results).getByRole("status").textContent).toBe("Answered as the work needs it · 2.4 s · 64 of 500 tokens");
    expect(within(results).getByRole("alert").textContent).toContain("It ran out of room");
    expect(within(results).getAllByText("Its answer")).toHaveLength(1); // only one that answered something
  });

  it("can't test a machine before descriptions & text have a model", async () => {
    model = "";
    machines = [machine({ name: "Gaming PC" })];
    renderSection();
    const test = (await screen.findByRole("button", { name: "Test Gaming PC" })) as HTMLButtonElement;
    expect(test.disabled).toBe(true);
    expect(test.title).toBe("Choose a model for descriptions & text first");
  });

  it("marks a machine as sharing the GPU video work runs on", async () => {
    machines = [machine({ name: "Gaming PC" })];
    offers = { [STUDIO]: [QWEN] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Add machine" }));
    const form = screen.getByRole("form", { name: "Add a machine" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "GPU box" } });
    fireEvent.change(within(form).getByLabelText("Endpoint URL"), { target: { value: STUDIO } });
    fireEvent.click(within(form).getByLabelText(/Shares the GPU with video work here/));
    fireEvent.click(within(form).getByRole("button", { name: "Connect" }));
    await within(form).findByText(/Connected: offers/);
    const add = within(form).getByRole("button", { name: "Add" }) as HTMLButtonElement;
    await waitFor(() => expect(add.disabled).toBe(false));
    fireEvent.click(add);
    await waitFor(() => expect(screen.queryByRole("form", { name: "Add a machine" })).toBeNull());
    expect((lastSent("POST", "/ai/machines")!.body as { shares_gpu: boolean }).shares_gpu).toBe(true);
  });

  it("marks a machine reached over plain HTTP across the network, and says how an AI worker switches to HTTPS", async () => {
    const worker = "http://10.10.10.3:6366/v1";
    const pinned = Array(32).fill("7F").join(":");
    machines = [
      machine({ name: "Gaming PC" }),
      machine({ name: "Ollama here", api_url: "http://localhost:11434/v1" }),
      machine({ name: "Old worker", api_url: worker, jobs: ["clip", "faces"] }),
      machine({ name: "New worker", api_url: "https://10.10.10.4:6366/v1", jobs: ["clip", "faces"], fingerprint: pinned }),
      builtIn(),
    ];
    renderSection();
    const row = async (name: string) => (await screen.findByText(name)).closest("li")!;
    const ollama = await row("Gaming PC");
    expect(within(ollama).getByText(/Unencrypted \(http:\/\/\)/)).toBeTruthy();
    expect(ollama.textContent).not.toContain("--https"); // Ollama can't serve HTTPS itself
    expect((await row("Ollama here")).textContent).not.toContain("Unencrypted"); // it crosses no network
    const old = await row("Old worker");
    expect(old.textContent).toContain("Unencrypted");
    expect(old.textContent).toContain("start it with --https");
    expect(within(old).getByRole("link", { name: "How to" }).getAttribute("href")).toBe(
      "https://github.com/bubbafat/lumiverb#a-second-machine-for-search-and-faces");
    const pinnedRow = await row("New worker");
    expect(pinnedRow.textContent).toContain(`Its certificate: ${pinned}`);
    expect(pinnedRow.textContent).not.toContain("Unencrypted");
    expect((await row("Built in")).textContent).not.toContain("Unencrypted");
  });

  it("asks an https machine for its certificate's fingerprint, and Connects and adds by it", async () => {
    const worker = "https://10.10.10.3:6366/v1";
    const pinned = Array(32).fill("7F").join(":");
    offers = { [worker]: ["ViT-B-32", "buffalo_l"] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Add machine" }));
    const form = screen.getByRole("form", { name: "Add a machine" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Worker" } });
    fireEvent.change(within(form).getByLabelText("Endpoint URL"), { target: { value: "http://10.10.10.3:6366/v1" } });
    expect(within(form).queryByLabelText("Certificate fingerprint")).toBeNull(); // http: nothing to compare
    fireEvent.change(within(form).getByLabelText("Endpoint URL"), { target: { value: worker } });
    fireEvent.change(within(form).getByLabelText("Certificate fingerprint"), { target: { value: pinned } });
    fireEvent.click(within(form).getByRole("button", { name: "Connect" }));
    await within(form).findByText(/Connected: offers/);
    expect(lastSent("POST", "/ai/connect")!.body).toEqual({ api_url: worker, api_key: "", fingerprint: pinned });
    const add = within(form).getByRole("button", { name: "Add" }) as HTMLButtonElement;
    await waitFor(() => expect(add.disabled).toBe(false));
    fireEvent.click(add);
    await waitFor(() => expect(screen.queryByRole("form", { name: "Add a machine" })).toBeNull());
    expect((lastSent("POST", "/ai/machines")!.body as { fingerprint: string }).fingerprint).toBe(pinned);
  });

  it("switches an AI worker to https: its saved key goes along, and a new certificate is asked again", async () => {
    const before = "http://10.10.10.3:6366/v1";
    const after = "https://10.10.10.3:6366/v1";
    const pinned = Array(32).fill("7F").join(":");
    machines = [machine({ name: "Worker", api_url: before, jobs: ["clip", "faces"], has_key: true })];
    offers = { [after]: ["ViT-B-32", "buffalo_l"] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Edit Worker" }));
    const form = screen.getByRole("form", { name: "Edit Worker" });
    fireEvent.change(within(form).getByLabelText("Endpoint URL"), { target: { value: after } });
    expect((within(form).getByLabelText("API key") as HTMLInputElement).placeholder).toContain("Saved");
    fireEvent.change(within(form).getByLabelText("Certificate fingerprint"), { target: { value: pinned } });
    const save = within(form).getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true); // asked again first
    fireEvent.click(within(form).getByRole("button", { name: "Connect" }));
    await within(form).findByText(/Connected: offers/);
    // Its saved key, by the machine: never typed again.
    expect(lastSent("POST", "/ai/connect")!.body).toEqual({ api_url: after, machine_id: "aim_Worker", fingerprint: pinned });
    await waitFor(() => expect(save.disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(screen.queryByRole("form", { name: "Edit Worker" })).toBeNull());
    const body = lastSent("PATCH", "/ai/machines/aim_Worker")!.body as Record<string, unknown>;
    expect(body.api_url).toBe(after);
    expect(body.fingerprint).toBe(pinned);
    expect("api_key" in body).toBe(false);
  });

  it("says why Connect couldn't, and won't give a machine a job whose model it lacks", async () => {
    offers = { [STUDIO]: ["llava:13b"] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Add machine" }));
    const form = screen.getByRole("form", { name: "Add a machine" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "X" } });
    fireEvent.change(within(form).getByLabelText("Endpoint URL"), { target: { value: "http://nowhere:1/v1" } });
    fireEvent.click(within(form).getByRole("button", { name: "Connect" }));
    expect((await within(form).findByRole("alert")).textContent).toContain("Couldn't reach");

    fireEvent.change(within(form).getByLabelText("Endpoint URL"), { target: { value: STUDIO } });
    fireEvent.click(within(form).getByRole("button", { name: "Connect" }));
    await within(form).findByText(/Connected: offers llava:13b/);
    const vision = within(form).getByRole("checkbox", { name: /Descriptions & text/ }) as HTMLInputElement;
    expect(vision.checked).toBe(false); // it doesn't offer the model, so it isn't given the job
    const add = within(form).getByRole("button", { name: "Add" }) as HTMLButtonElement;
    expect(add.disabled).toBe(false); // it can join without the job
    fireEvent.click(vision);
    expect(within(form).getByRole("alert").textContent).toContain(`It doesn't offer ${QWEN}`);
    expect(add.disabled).toBe(true);
  });

  it("Connect gives a new machine the jobs whose model it offers", async () => {
    whisper = "small";
    machines = [builtIn(), machine({ name: "Gaming PC" })];
    offers = { [SPEACHES]: ["small", "speaches-ai/Kokoro-82M-v1.0-ONNX"] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Add machine" }));
    const form = screen.getByRole("form", { name: "Add a machine" });
    const vision = within(form).getByRole("checkbox", { name: /Descriptions & text/ }) as HTMLInputElement;
    const transcripts = within(form).getByRole("checkbox", { name: /Transcripts/ }) as HTMLInputElement;
    expect([vision.checked, transcripts.checked]).toEqual([false, false]); // until it's checked
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Speaches" } });
    fireEvent.change(within(form).getByLabelText("Endpoint URL"), { target: { value: SPEACHES } });
    fireEvent.click(within(form).getByRole("button", { name: "Connect" }));
    await within(form).findByText(/Connected: offers small/);
    expect([vision.checked, transcripts.checked]).toEqual([false, true]);
    fireEvent.click(within(form).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(screen.queryByRole("form", { name: "Add a machine" })).toBeNull());
    expect((lastSent("POST", "/ai/machines")!.body as { jobs: string[] }).jobs).toEqual(["transcripts"]);
  });

  it("the first machine for a job with no model yet is given it, so its models can be picked", async () => {
    model = "";
    whisper = "small";
    machines = [builtIn()];
    offers = { [GAMING_PC]: [QWEN] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Add machine" }));
    const form = screen.getByRole("form", { name: "Add a machine" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Gaming PC 3080" } });
    fireEvent.change(within(form).getByLabelText("Endpoint URL"), { target: { value: GAMING_PC } });
    fireEvent.click(within(form).getByRole("button", { name: "Connect" }));
    await within(form).findByText(/Connected: offers/);
    const vision = within(form).getByRole("checkbox", { name: /Descriptions & text/ }) as HTMLInputElement;
    const transcripts = within(form).getByRole("checkbox", { name: /Transcripts/ }) as HTMLInputElement;
    // Descriptions have no model and no machine; transcripts' small isn't offered there.
    expect([vision.checked, transcripts.checked]).toEqual([true, false]);
    fireEvent.click(within(form).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(screen.queryByRole("form", { name: "Add a machine" })).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Change the model for Descriptions & text" }));
    const options = [...(screen.getByLabelText("Model for Descriptions & text") as HTMLSelectElement).options].map((o) => o.value);
    expect(options).toEqual(["", QWEN]);
  });

  it("a Whisper server isn't given descriptions just because they have no model yet", async () => {
    model = "";
    whisper = "small";
    machines = [builtIn()];
    offers = { [SPEACHES]: ["small"] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Add machine" }));
    const form = screen.getByRole("form", { name: "Add a machine" });
    fireEvent.change(within(form).getByLabelText("Endpoint URL"), { target: { value: SPEACHES } });
    fireEvent.click(within(form).getByRole("button", { name: "Connect" }));
    await within(form).findByText(/Connected: offers small/);
    const vision = within(form).getByRole("checkbox", { name: /Descriptions & text/ }) as HTMLInputElement;
    const transcripts = within(form).getByRole("checkbox", { name: /Transcripts/ }) as HTMLInputElement;
    expect([vision.checked, transcripts.checked]).toEqual([false, true]);
  });

  it("shows the built-in Whisper as this computer's, and it can't be removed", async () => {
    whisper = "small";
    machines = [builtIn(), machine({ name: "Gaming PC" })];
    renderSection();
    const row = (await screen.findByText("Built in")).closest("li")!;
    expect(within(row).getByText("Whisper on the worker's own computer")).toBeTruthy();
    expect(within(row).getByText("Does: Transcripts · 1 at once")).toBeTruthy();
    expect(within(row).queryByRole("button", { name: "Remove Built in" })).toBeNull();
    expect(screen.getByRole("button", { name: "Remove Gaming PC" })).toBeTruthy();
    expect(screen.getByText(/Transcripts:/, { selector: "span" }).closest("p")!.textContent).toContain("small");
  });

  it("edits the built-in Whisper: no URL, key or Connect; only the jobs it can do", async () => {
    whisper = "small";
    machines = [builtIn(), machine({ name: "Gaming PC" })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Edit Built in" }));
    const form = screen.getByRole("form", { name: "Edit Built in" });
    expect(within(form).queryByLabelText("Endpoint URL")).toBeNull();
    expect(within(form).queryByLabelText("API key")).toBeNull();
    expect(within(form).queryByRole("button", { name: "Connect" })).toBeNull();
    expect(within(form).getAllByRole("checkbox").map((c) => c.closest("label")!.textContent)).toEqual([
      expect.stringContaining("Transcripts"), "Use this machine"]);
    fireEvent.change(within(form).getByLabelText("Requests at once"), { target: { value: "2" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("form", { name: "Edit Built in" })).toBeNull());
    expect(lastSent("PATCH", "/ai/machines/aim_self")!.body).toEqual({ name: "Built in", jobs: ["transcripts"], at_once: 2, enabled: true });
    expect(await screen.findByText("Does: Transcripts · 2 at once")).toBeTruthy();
  });

  it("turning off the built-in Whisper when nothing else transcribes just does it (never asks)", async () => {
    whisper = "small";
    machines = [builtIn(), machine({ name: "Gaming PC" })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Edit Built in" }));
    const form = screen.getByRole("form", { name: "Edit Built in" });
    fireEvent.click(within(form).getByRole("checkbox", { name: "Use this machine" }));
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Transcripts: paused")).toBeTruthy();
    expect(screen.queryByText(/No other machine does/)).toBeNull();
    expect(lastSent("PATCH", "/ai/machines/aim_self")!.url).not.toContain("leave_jobs");
  });

  it("shows a fixed model with why, and no way to change it", async () => {
    machines = [builtIn({ jobs: ["transcripts", "clip"] })];
    more = [{ ...job("clip", "Visual search (CLIP)", "ViT-B-32", true),
              fixed: "Another model makes every photo's vectors again; not offered yet." }];
    renderSection();
    expect(await screen.findByText("Another model makes every photo's vectors again; not offered yet.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Change the model for Visual search (CLIP)" })).toBeNull();
    expect(screen.getByRole("button", { name: "Change the model for Transcripts" })).toBeTruthy();
  });

  it("picks the transcripts model from what the machines doing them offer", async () => {
    whisper = "small";
    machines = [builtIn(), machine({ name: "Speaches", api_url: SPEACHES, jobs: ["transcripts"],
                                     status: { online: true, error: "", models: ["small", "deepdml/faster-whisper-large-v3-turbo-ct2"], checked_at: null } })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Change the model for Transcripts" }));
    const options = [...(screen.getByLabelText("Model for Transcripts") as HTMLSelectElement).options].map((o) => o.value);
    expect(options).toEqual(["", "deepdml/faster-whisper-large-v3-turbo-ct2", "large-v3", "medium", "small"]);
  });

  it("edits a machine: a rename needs no Connect, and the saved key stays unless changed", async () => {
    machines = [machine({ name: "Gaming PC", has_key: true })];
    offers = { [GAMING_PC]: [QWEN] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Edit Gaming PC" }));
    const form = screen.getByRole("form", { name: "Edit Gaming PC" });
    expect((within(form).getByLabelText("API key") as HTMLInputElement).placeholder).toContain("Saved");
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Gaming PC 3080" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await screen.findByText("Gaming PC 3080");
    const body = lastSent("PATCH", "/ai/machines/aim_Gaming_PC")!.body as Record<string, unknown>;
    expect(body.name).toBe("Gaming PC 3080");
    expect("api_key" in body).toBe(false);
  });

  it("asks for a saved key again when this server can't read it (saved on another)", async () => {
    machines = [machine({ name: "Gaming PC", has_key: true, key_readable: false })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Edit Gaming PC" }));
    const form = screen.getByRole("form", { name: "Edit Gaming PC" });
    expect(form.textContent).toContain("Its saved key can't be read on this server");
  });

  it("removing the last machine doing a job just removes it (never asks)", async () => {
    machines = [machine({ name: "Gaming PC" })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Remove Gaming PC" }));
    await waitFor(() => expect(screen.queryByText("Gaming PC")).toBeNull());
    expect(screen.queryByRole("button", { name: "Remove anyway" })).toBeNull();
    expect(lastSent("DELETE", "/ai/machines/aim_Gaming_PC")!.url).not.toContain("leave_jobs");
  });

  it("turning off the last machine doing a job just saves (never asks)", async () => {
    machines = [machine({ name: "Gaming PC" })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Edit Gaming PC" }));
    const form = screen.getByRole("form", { name: "Edit Gaming PC" });
    fireEvent.click(within(form).getByRole("checkbox", { name: "Use this machine" }));
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Turned off: gets no work.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save anyway" })).toBeNull();
  });

  it("changes a job's model to one its machines offer, and says why not when none does", async () => {
    machines = [machine({ name: "Gaming PC", status: { online: true, error: "", models: [QWEN, "llava:13b"], checked_at: null } })];
    offers = { [GAMING_PC]: [QWEN] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Change the model for Descriptions & text" }));
    const select = screen.getByLabelText("Model for Descriptions & text");
    fireEvent.change(select, { target: { value: "llava:13b" } });
    expect(screen.getByText(/Saving asks about what qwen3-vl:8b made: llava:13b for new files only, or remake that too/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Gaming PC: qwen3-vl:8b");

    offers = { [GAMING_PC]: [QWEN, "llava:13b"] };
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText(/llava:13b/, { selector: "p" });
    expect(lastSent("PUT", "/ai/jobs/vision")!.body).toEqual({ model: "llava:13b" });
    // What the old model made is being remade: Processing follows it.
    const note = await screen.findByText(/What descriptions & text made with qwen3-vl:8b is being remade/);
    expect(within(note).getByRole("link", { name: "Processing" }).getAttribute("href")).toBe("/settings/processing");
  });
});

describe("AiSection model change that redoes clips", () => {
  it("asks with the count whether to remake what the old model made too", async () => {
    machines = [machine({ name: "Gaming PC", status: { online: true, error: "", models: [QWEN, "llava:13b"], checked_at: null } })];
    offers = { [GAMING_PC]: [QWEN, "llava:13b"] };
    made = 12;
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Change the model for Descriptions & text" }));
    fireEvent.change(screen.getByLabelText("Model for Descriptions & text"), { target: { value: "llava:13b" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("12 clips were made with the old model");
    expect(model).toBe(QWEN);  // nothing changed yet
    fireEvent.click(within(alert).getByRole("button", { name: "Also remake 12 clips" }));
    await screen.findByText(/What descriptions & text made with qwen3-vl:8b is being remade/);
    expect(lastSent("PUT", "/ai/jobs/vision")!.body).toEqual({ model: "llava:13b", redo: true });
  });

  it("new files only keeps what the old model made, and says Processing remakes it when asked", async () => {
    machines = [machine({ name: "Gaming PC", status: { online: true, error: "", models: [QWEN, "llava:13b"], checked_at: null } })];
    offers = { [GAMING_PC]: [QWEN, "llava:13b"] };
    made = 12;
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Change the model for Descriptions & text" }));
    fireEvent.change(screen.getByLabelText("Model for Descriptions & text"), { target: { value: "llava:13b" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    fireEvent.click(within(await screen.findByRole("alert")).getByRole("button", { name: "New files only" }));
    const note = await screen.findByText(/made with qwen3-vl:8b stays as it is: the new model is for new files/);
    expect(within(note).getByRole("link", { name: "Processing" })).toBeTruthy();
    expect(lastSent("PUT", "/ai/jobs/vision")!.body).toEqual({ model: "llava:13b", redo: false });
  });

  it("forgets the question when another model is picked", async () => {
    machines = [machine({ name: "Gaming PC", status: { online: true, error: "", models: [QWEN, "llava:13b", "m3"], checked_at: null } })];
    offers = { [GAMING_PC]: [QWEN, "llava:13b", "m3"] };
    made = 1;
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Change the model for Descriptions & text" }));
    fireEvent.change(screen.getByLabelText("Model for Descriptions & text"), { target: { value: "llava:13b" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("button", { name: "Also remake 1 clip" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Model for Descriptions & text"), { target: { value: "m3" } });
    expect(screen.queryByRole("button", { name: /Also remake/ })).toBeNull();
  });

  it("turns a job off without asking", async () => {
    machines = [machine({ name: "Gaming PC", status: { online: true, error: "", models: [QWEN], checked_at: null } })];
    offers = { [GAMING_PC]: [QWEN] };
    made = 12;
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Change the model for Descriptions & text" }));
    fireEvent.click(screen.getByRole("button", { name: "Turn off descriptions & text" }));
    await waitFor(() => expect(model).toBe(""));
    expect(lastSent("PUT", "/ai/jobs/vision")!.body).toEqual({ model: "" });
  });
});

describe("aiHasProblem", () => {
  const ok = { machines: [machine({})], jobs: [{ job: "vision", label: "Descriptions & text", model: QWEN, machines: 1, offering: 1, choices: [QWEN], built_in: false }] };
  it("flags a job that can't run, or an offline machine doing one", () => {
    expect(aiHasProblem(ok)).toBe(false);
    expect(aiHasProblem({ ...ok, jobs: [{ ...ok.jobs[0], machines: 0, offering: 0 }], machines: [] })).toBe(true);
    const offline = machine({ name: "B", status: { online: false, error: "x", models: [], checked_at: null } });
    expect(aiHasProblem({ ...ok, machines: [machine({}), offline] })).toBe(true);
    expect(aiHasProblem({ ...ok, machines: [machine({}), { ...offline, enabled: false }] })).toBe(false);
    expect(aiHasProblem(undefined)).toBe(false);
  });
});

describe("ago", () => {
  it("says roughly when", () => {
    const now = Date.parse("2026-10-08T12:00:00Z");
    expect(ago("2026-10-08T11:59:30Z", now)).toBe("just now");
    expect(ago("2026-10-08T11:57:00Z", now)).toBe("3 minutes ago");
    expect(ago("2026-10-08T10:00:00Z", now)).toBe("2 hours ago");
    expect(ago(null, now)).toBe("");
  });

  it("an admin lets searches use the machine; anyone else sees whether they may", async () => {
    renderSection();
    const box = await screen.findByRole("checkbox", { name: /AI-assisted search/ });
    await waitFor(() => expect((box as HTMLInputElement).disabled).toBe(false));
    expect((box as HTMLInputElement).checked).toBe(false);
    fireEvent.click(box);
    await waitFor(() => expect(lastSent("PATCH", "/tenant/settings")?.body).toEqual({ assisted_search: true }));
    await waitFor(() => expect((box as HTMLInputElement).checked).toBe(true));
    cleanup();
    role = "viewer";
    renderSection();
    const seen = await screen.findByRole("checkbox", { name: /AI-assisted search/ });
    await waitFor(() => expect((seen as HTMLInputElement).checked).toBe(true));
    expect((seen as HTMLInputElement).disabled).toBe(true);
  });
});
