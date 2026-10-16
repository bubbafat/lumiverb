import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ApiKeysSection from "./ApiKeysSection";

const fetchMock = vi.fn();
const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();
const KEY = "lv_01M53_new-key";

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
    if (url.endsWith("/v1/me")) return json({ email: "a@b.c", role: "admin" });
    if (url.endsWith("/v1/keys") && (init?.method ?? "GET") === "GET") {
      return json({
        keys: [
          { key_id: "k1", label: "laptop", role: "editor", created_at: minutesAgo(3 * 24 * 60), last_used_at: minutesAgo(5) },
        ],
      });
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
