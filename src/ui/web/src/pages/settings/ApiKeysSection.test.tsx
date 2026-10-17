import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ApiKeysSection from "./ApiKeysSection";

const fetchMock = vi.fn();
const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();
const KEY = "lv_01M53_new-key";
const daysFromNow = (d: number) => new Date(Date.now() + d * 24 * 3600_000).toISOString();
let role = "admin";
let idleDays: number | null = null;
let endsAt: string | null = null;
const patches: unknown[] = [];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

/** The browser's copy command, copying or not. */
function copyCommand(works: boolean) {
  const exec = vi.fn(() => works);
  Object.defineProperty(document, "execCommand", { value: exec, configurable: true });
  return exec;
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/v1/me")) return json({ email: "a@b.c", role });
    if (url.endsWith("/v1/keys") && (init?.method ?? "GET") === "GET") {
      return json({
        keys: [
          { key_id: "k1", label: "laptop", role: "editor", created_at: minutesAgo(3 * 24 * 60), last_used_at: minutesAgo(5),
            ends_at: endsAt },
        ],
      });
    }
    if (url.endsWith("/v1/tenant/settings") && (init?.method ?? "GET") === "GET") return json({ key_idle_days: idleDays });
    if (url.endsWith("/v1/tenant/settings")) {
      const body = JSON.parse(String(init?.body));
      patches.push(body);
      // The server's rule (routers/tenant.py): turning them on ends the old script's key, so it asks first.
      if (body.key_idle_days !== null && !body.confirm_end_keys) {
        return json({ error: { code: "keys_would_end", message: "…",
                               details: { key_idle_days: body.key_idle_days,
                                          keys: [{ key_id: "k9", label: "old script", role: "editor", last_used_at: null }] } } },
                    409);
      }
      idleDays = body.key_idle_days;
      return json({ key_idle_days: idleDays });
    }
    if (url.endsWith("/v1/keys")) {
      return json({ key_id: "k2", label: "cli", role: "admin", last_used_at: null, created_at: minutesAgo(0),
                    plaintext: KEY });
    }
    return new Response("{}", { status: 404 });
  });
  // A page over plain http on the network (a Mac's install by its name): no Clipboard API.
  Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
});

afterEach(() => {
  role = "admin";
  idleDays = null;
  endsAt = null;
  patches.length = 0;
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ApiKeysSection />
    </QueryClientProvider>,
  );
}

async function createKey() {
  renderSection();
  fireEvent.change(screen.getByPlaceholderText("e.g. CI pipeline"), { target: { value: "cli" } });
  fireEvent.click(screen.getByRole("button", { name: "Create key" }));
  expect(await screen.findByText(KEY)).toBeTruthy();
}

describe("ApiKeysSection", () => {
  it("spells out when a key was made and last used", async () => {
    renderSection();
    expect(await screen.findByText("3 days ago")).toBeTruthy();
    expect(screen.getByText("5 minutes ago")).toBeTruthy();
  });

  it("says when a key ends unless it's used", async () => {
    idleDays = 90;
    endsAt = daysFromNow(89);
    renderSection();
    expect(await screen.findByText(`Ends ${new Date(endsAt).toLocaleDateString()} unless used`)).toBeTruthy();
  });

  it("lets an admin end unused keys, asking first when some would end at once", async () => {
    renderSection();
    const on = await screen.findByRole("checkbox");
    expect((on as HTMLInputElement).checked).toBe(false);
    fireEvent.click(on);
    expect((screen.getByLabelText("Days unused") as HTMLInputElement).value).toBe("90");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const asked = await screen.findByRole("alertdialog", { name: "End them now?" });
    expect(asked.textContent).toContain("1 key hasn't been used for longer than 90 days");
    expect(asked.textContent).toContain("old script");
    expect(idleDays).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save and end them" }));
    expect(await screen.findByText("Saved")).toBeTruthy();
    expect(patches).toEqual([{ key_idle_days: 90 }, { key_idle_days: 90, confirm_end_keys: true }]);
    expect(idleDays).toBe(90);
  });

  it("refuses days it can't use", async () => {
    renderSection();
    fireEvent.click(await screen.findByRole("checkbox"));
    fireEvent.change(screen.getByLabelText("Days unused"), { target: { value: "0" } });
    expect(screen.getByRole("alert").textContent).toContain("from 1 to 3,650");
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("tells someone who isn't an admin what it is", async () => {
    role = "editor";
    idleDays = 30;
    renderSection();
    expect(await screen.findByText("A key ends once nobody has used it for 30 days.")).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("copies a new key with one click, no dialog, where the page can't use the Clipboard API", async () => {
    const prompt = vi.spyOn(window, "prompt");
    const exec = copyCommand(true);
    await createKey();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(await screen.findByRole("button", { name: "Copied" })).toBeTruthy();
    expect(exec).toHaveBeenCalledWith("copy");
    expect(prompt).not.toHaveBeenCalled();
  });

  it("selects the key to copy by hand, and says so, when the browser won't copy at all", async () => {
    const prompt = vi.spyOn(window, "prompt");
    copyCommand(false);
    await createKey();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    expect(await screen.findByText(/the key is selected, so press ⌘C/)).toBeTruthy();
    expect(window.getSelection()?.toString()).toBe(KEY);
    expect(prompt).not.toHaveBeenCalled();
  });
});
