import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import AiSection, { aiHasProblem, ago } from "./AiSection";
import type { AiMachine, AiSettings } from "../../api/client";

const BRAIN = "http://172.18.0.6:11434/v1";
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
const sent: { method: string; url: string; body: unknown }[] = [];

function machine(over: Partial<AiMachine>): AiMachine {
  return {
    machine_id: `aim_${over.name}`, name: "Brain", api_url: BRAIN, has_key: false, jobs: ["vision"], at_once: 2,
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
    jobs: [job("vision", "Descriptions & text", model, false), job("transcripts", "Transcripts", whisper, true)],
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
    if (path === "/ai/jobs/vision" && method === "PUT") {
      if (made && body.model && body.model !== model && !body.redo) {
        return err(409, "redo_on_change",
                   `${body.model} makes ${made} clips again: descriptions and tags (${made}). That runs after anything missing; until it's done, results mix the old model and the new.`,
                   { job: "vision", model: body.model, clips: made,
                     artifacts: [{ artifact: "vision", title: "Descriptions and tags", clips: made }] });
      }
      if (body.model && !machines.some((x) => offers[x.api_url]?.includes(body.model))) {
        return err(409, "model_not_offered", `No machine doing descriptions & text offers ${body.model}.`,
                   { job: "vision", model: body.model, machines: machines.map((x) => ({ name: x.name, models: offers[x.api_url] ?? [], error: offers[x.api_url] ? "" : "Couldn't reach it." })) });
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
  sent.length = 0;
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
      machine({ name: "Brain 3080" }),
      machine({ name: "Mac Studio", api_url: STUDIO, at_once: 4,
                status: { online: false, error: `Couldn't reach ${STUDIO}: ConnectionError.`, models: [], checked_at: new Date().toISOString() } }),
      machine({ name: "Spare", enabled: false, status: null }),
    ];
    renderSection();
    const brain = (await screen.findByText("Brain 3080")).closest("li")!;
    expect(within(brain).getByText("Does: Descriptions & text · 2 at once")).toBeTruthy();
    expect(within(brain).getByText(/Online · checked just now/)).toBeTruthy();
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

  it("a viewer sees it all and changes nothing", async () => {
    role = "viewer";
    machines = [machine({ name: "Brain" })];
    renderSection();
    await screen.findByText("Brain");
    await screen.findByText("Only admins can change these.");
    expect(screen.queryByRole("button", { name: "Add machine" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit Brain" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Change the model/ })).toBeNull();
  });

  it("adds a machine once Connect has checked it", async () => {
    machines = [machine({ name: "Brain" })];
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

  it("marks a machine as sharing the GPU video work runs on", async () => {
    machines = [machine({ name: "Brain" })];
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
    machines = [builtIn(), machine({ name: "Brain" })];
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
    offers = { [BRAIN]: [QWEN] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Add machine" }));
    const form = screen.getByRole("form", { name: "Add a machine" });
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Brain 3080" } });
    fireEvent.change(within(form).getByLabelText("Endpoint URL"), { target: { value: BRAIN } });
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
    machines = [builtIn(), machine({ name: "Brain" })];
    renderSection();
    const row = (await screen.findByText("Built in")).closest("li")!;
    expect(within(row).getByText("Whisper on the worker's own computer")).toBeTruthy();
    expect(within(row).getByText("Does: Transcripts · 1 at once")).toBeTruthy();
    expect(within(row).queryByRole("button", { name: "Remove Built in" })).toBeNull();
    expect(screen.getByRole("button", { name: "Remove Brain" })).toBeTruthy();
    expect(screen.getByText(/Transcripts:/, { selector: "span" }).closest("p")!.textContent).toContain("small");
  });

  it("edits the built-in Whisper: no URL, key or Connect; only the jobs it can do", async () => {
    whisper = "small";
    machines = [builtIn(), machine({ name: "Brain" })];
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
    machines = [builtIn(), machine({ name: "Brain" })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Edit Built in" }));
    const form = screen.getByRole("form", { name: "Edit Built in" });
    fireEvent.click(within(form).getByRole("checkbox", { name: "Use this machine" }));
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Transcripts: paused")).toBeTruthy();
    expect(screen.queryByText(/No other machine does/)).toBeNull();
    expect(lastSent("PATCH", "/ai/machines/aim_self")!.url).not.toContain("leave_jobs");
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
    machines = [machine({ name: "Brain", has_key: true })];
    offers = { [BRAIN]: [QWEN] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Edit Brain" }));
    const form = screen.getByRole("form", { name: "Edit Brain" });
    expect((within(form).getByLabelText("API key") as HTMLInputElement).placeholder).toContain("Saved");
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "Brain 3080" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await screen.findByText("Brain 3080");
    const body = lastSent("PATCH", "/ai/machines/aim_Brain")!.body as Record<string, unknown>;
    expect(body.name).toBe("Brain 3080");
    expect("api_key" in body).toBe(false);
  });

  it("removing the last machine doing a job just removes it (never asks)", async () => {
    machines = [machine({ name: "Brain" })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Remove Brain" }));
    await waitFor(() => expect(screen.queryByText("Brain")).toBeNull());
    expect(screen.queryByRole("button", { name: "Remove anyway" })).toBeNull();
    expect(lastSent("DELETE", "/ai/machines/aim_Brain")!.url).not.toContain("leave_jobs");
  });

  it("turning off the last machine doing a job just saves (never asks)", async () => {
    machines = [machine({ name: "Brain" })];
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Edit Brain" }));
    const form = screen.getByRole("form", { name: "Edit Brain" });
    fireEvent.click(within(form).getByRole("checkbox", { name: "Use this machine" }));
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Turned off: gets no work.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save anyway" })).toBeNull();
  });

  it("changes a job's model to one its machines offer, and says why not when none does", async () => {
    machines = [machine({ name: "Brain", status: { online: true, error: "", models: [QWEN, "llava:13b"], checked_at: null } })];
    offers = { [BRAIN]: [QWEN] };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Change the model for Descriptions & text" }));
    const select = screen.getByLabelText("Model for Descriptions & text");
    fireEvent.change(select, { target: { value: "llava:13b" } });
    expect(screen.getByText(/What was made with qwen3-vl:8b is made again with llava:13b, after anything missing/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Brain: qwen3-vl:8b");

    offers = { [BRAIN]: [QWEN, "llava:13b"] };
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText(/llava:13b/, { selector: "p" });
    expect(lastSent("PUT", "/ai/jobs/vision")!.body).toEqual({ model: "llava:13b" });
    // What the old model made is being made again: Processing follows it.
    const note = await screen.findByText(/What descriptions & text made with qwen3-vl:8b is being made again/);
    expect(within(note).getByRole("link", { name: "Processing" }).getAttribute("href")).toBe("/settings/processing");
  });
});

describe("AiSection model change that redoes clips", () => {
  it("asks with the count before a new model makes clips again", async () => {
    machines = [machine({ name: "Brain", status: { online: true, error: "", models: [QWEN, "llava:13b"], checked_at: null } })];
    offers = { [BRAIN]: [QWEN, "llava:13b"] };
    made = 12;
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Change the model for Descriptions & text" }));
    fireEvent.change(screen.getByLabelText("Model for Descriptions & text"), { target: { value: "llava:13b" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("llava:13b makes 12 clips again");
    expect(alert.textContent).toContain("after anything missing");
    expect(model).toBe(QWEN);  // nothing changed yet
    fireEvent.click(within(alert).getByRole("button", { name: "Change it and redo 12 clips" }));
    await screen.findByText(/What descriptions & text made with qwen3-vl:8b is being made again/);
    expect(lastSent("PUT", "/ai/jobs/vision")!.body).toEqual({ model: "llava:13b", redo: true });
  });

  it("forgets the question when another model is picked", async () => {
    machines = [machine({ name: "Brain", status: { online: true, error: "", models: [QWEN, "llava:13b", "m3"], checked_at: null } })];
    offers = { [BRAIN]: [QWEN, "llava:13b", "m3"] };
    made = 1;
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Change the model for Descriptions & text" }));
    fireEvent.change(screen.getByLabelText("Model for Descriptions & text"), { target: { value: "llava:13b" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("button", { name: "Change it and redo 1 clip" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Model for Descriptions & text"), { target: { value: "m3" } });
    expect(screen.queryByRole("button", { name: /Change it and redo/ })).toBeNull();
  });

  it("turns a job off without asking", async () => {
    machines = [machine({ name: "Brain", status: { online: true, error: "", models: [QWEN], checked_at: null } })];
    offers = { [BRAIN]: [QWEN] };
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
});
