import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import AiSection, { ago } from "./AiSection";

const URL = "http://vision.local:11434/v1";
const fetchMock = vi.fn();
let role = "admin";
let saved: { api_url: string; has_key: boolean; model: string; status: unknown } = {
  api_url: "",
  has_key: false,
  model: "",
  status: null,
};
let offered: string[] = ["llava:13b", "qwen3-vl:8b"];
let reachable = true;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    if (url.endsWith("/v1/me")) return json({ email: "a@b.c", role });
    if (url.endsWith("/v1/tenant/vision/connect")) {
      if (!reachable) {
        return json({ error: { code: "vision_unreachable", message: `Couldn't reach ${body.api_url}: ConnectionError.` } }, 502);
      }
      return json({ models: offered });
    }
    if (url.endsWith("/v1/tenant/vision") && init?.method === "PUT") {
      if (body.api_url && !offered.includes(body.model)) {
        return json({ error: { code: "vision_model_unavailable", message: `${body.api_url} doesn't offer ${body.model}.`, details: { models: offered } } }, 409);
      }
      saved = {
        api_url: body.api_url,
        has_key: body.api_key === undefined ? saved.has_key : !!body.api_key,
        model: body.model,
        status: null, // the worker checks the new settings before using them
      };
      return json(saved);
    }
    if (url.endsWith("/v1/tenant/vision")) return json(saved);
    return json({}, 404);
  });
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  role = "admin";
  saved = { api_url: "", has_key: false, model: "", status: null };
  offered = ["llava:13b", "qwen3-vl:8b"];
  reachable = true;
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AiSection />
    </QueryClientProvider>,
  );
}

function calls(suffix: string, method?: string) {
  return fetchMock.mock.calls
    .filter(([u, init]) => String(u).endsWith(suffix) && (!method || (init as RequestInit | undefined)?.method === method))
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
}

describe("AiSection", () => {
  it("says vision AI is off until a model is chosen", async () => {
    renderSection();
    expect(await screen.findByText(/Vision AI is off/)).toBeTruthy();
  });

  it("connects, lists the endpoint's models, and saves the one picked", async () => {
    renderSection();
    fireEvent.change(await screen.findByLabelText("Endpoint URL"), { target: { value: URL } });
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "sk-1" } });
    const save = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true); // nothing to pick from yet

    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    const select = (await screen.findByLabelText("Model")) as HTMLSelectElement;
    expect(screen.getByText("Connected: 2 models")).toBeTruthy();
    expect(Array.from(select.options).map((o) => o.value)).toEqual(["", "llava:13b", "qwen3-vl:8b"]);
    expect(calls("/tenant/vision/connect")).toEqual([{ api_url: URL, api_key: "sk-1" }]);
    expect(save.disabled).toBe(true); // a model must be picked

    fireEvent.change(select, { target: { value: "qwen3-vl:8b" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByText(/Saved. The worker hasn't checked it yet/)).toBeTruthy());
    expect(calls("/tenant/vision", "PUT")).toEqual([{ api_url: URL, api_key: "sk-1", model: "qwen3-vl:8b" }]);
    // The key typed is saved now, and never shown again.
    expect((screen.getByLabelText("API key") as HTMLInputElement).value).toBe("");
  });

  it("clears a key once it's saved, even when nothing else changed", async () => {
    saved = { api_url: URL, has_key: true, model: "llava:13b", status: null };
    renderSection();
    fireEvent.change(await screen.findByLabelText("API key"), { target: { value: "sk-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    fireEvent.change(await screen.findByLabelText("Model"), { target: { value: "llava:13b" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("/tenant/vision", "PUT")).toHaveLength(1));
    await waitFor(() => expect((screen.getByLabelText("API key") as HTMLInputElement).value).toBe(""));
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("says when the worker found it working", async () => {
    saved = {
      api_url: URL,
      has_key: false,
      model: "qwen3-vl:8b",
      status: { ok: true, error: "", model: "qwen3-vl:8b", api_url: URL, checked_at: new Date().toISOString() },
    };
    renderSection();
    expect(await screen.findByText(/Working: qwen3-vl:8b answered just now/)).toBeTruthy();
  });

  it("says plainly why it couldn't connect, and offers no models", async () => {
    reachable = false;
    renderSection();
    fireEvent.change(await screen.findByLabelText("Endpoint URL"), { target: { value: URL } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByText(/Couldn't reach http:\/\/vision.local/)).toBeTruthy();
    expect(screen.queryByLabelText("Model")).toBeNull();
  });

  it("highlights the worker's report that it can't use the model", async () => {
    saved = {
      api_url: URL,
      has_key: false,
      model: "qwen3-vl:8b",
      status: { ok: false, error: `${URL} no longer offers qwen3-vl:8b.`, model: "qwen3-vl:8b", api_url: URL, checked_at: new Date().toISOString() },
    };
    renderSection();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Vision AI is paused");
    expect(alert.textContent).toContain("no longer offers qwen3-vl:8b");
    expect(alert.textContent).toContain("no clip is marked as failed");
  });

  it("keeps the saved key unless one is typed or it's removed", async () => {
    saved = { api_url: URL, has_key: true, model: "llava:13b", status: null };
    renderSection();
    expect(((await screen.findByLabelText("API key")) as HTMLInputElement).placeholder).toMatch(/Saved/);
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    fireEvent.change(await screen.findByLabelText("Model"), { target: { value: "qwen3-vl:8b" } });
    expect(screen.getByText(/Changing the model marks existing descriptions/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls("/tenant/vision", "PUT")).toHaveLength(1));
    expect(calls("/tenant/vision/connect")[0]).toEqual({ api_url: URL }); // the saved key, server-side
    expect(calls("/tenant/vision", "PUT")[0]).toEqual({ api_url: URL, model: "qwen3-vl:8b" });
  });

  it("shows what the endpoint offers when the model went away before saving", async () => {
    renderSection();
    fireEvent.change(await screen.findByLabelText("Endpoint URL"), { target: { value: URL } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    fireEvent.change(await screen.findByLabelText("Model"), { target: { value: "qwen3-vl:8b" } });
    offered = ["llava:13b"];
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/doesn't offer qwen3-vl:8b/)).toBeTruthy();
    const select = screen.getByLabelText("Model") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toEqual(["", "llava:13b"]);
    expect(select.value).toBe("");
  });

  it("turns vision AI off", async () => {
    saved = { api_url: URL, has_key: true, model: "llava:13b", status: null };
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Turn off vision AI" }));
    await waitFor(() => expect(screen.getByText(/Vision AI is off/)).toBeTruthy());
    expect(calls("/tenant/vision", "PUT")).toEqual([{ api_url: "", model: "" }]);
  });

  it("shows others the settings without letting them change them", async () => {
    role = "editor";
    saved = { api_url: URL, has_key: true, model: "llava:13b", status: null };
    renderSection();
    expect(await screen.findByText("Only admins can change this.")).toBeTruthy();
    expect(screen.getByText("Model: llava:13b")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull();
  });
});

describe("ago", () => {
  it("says how long ago, plainly", () => {
    const now = Date.parse("2026-10-08T12:00:00Z");
    expect(ago("2026-10-08T11:59:30Z", now)).toBe("just now");
    expect(ago("2026-10-08T11:59:00Z", now)).toBe("1 minute ago");
    expect(ago("2026-10-08T09:00:00Z", now)).toBe("3 hours ago");
  });
});
