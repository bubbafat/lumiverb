import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchMock = vi.fn();

beforeEach(() => {
  vi.resetModules(); // what's kept is the module's: each test starts with none
  vi.stubGlobal("fetch", fetchMock);
  localStorage.setItem("lumiverb_token", "tok");
  window.history.pushState({}, "", "/projects");
});

afterEach(() => {
  fetchMock.mockReset();
  vi.unstubAllGlobals();
  localStorage.clear();
});

const ok = () => new Response("[]", { status: 200 });

async function client() {
  const mod = await import("./client");
  mod.setApiKey("tok");
  return mod;
}

/** What was sent to the server's log, by each send. */
function sent(): { method: string; path: string; error: string; page: string }[][] {
  return fetchMock.mock.calls
    .filter(([url]) => url === "/v1/me/failed-requests")
    .map(([, init]) => JSON.parse(init.body).failed);
}

describe("failed requests", () => {
  it("a request with no answer is said once one gets through: its path without its query, the browser's words", async () => {
    const { authFetch } = await client();
    fetchMock.mockRejectedValueOnce(new TypeError("Load failed"));
    await expect(authFetch("/projects?q=my private words")).rejects.toThrow("Load failed");
    expect(sent()).toEqual([]);

    fetchMock.mockResolvedValue(ok());
    await authFetch("/me");
    await vi.waitFor(() => expect(sent()).toHaveLength(1));
    const [[failed]] = sent();
    expect(failed).toMatchObject({ method: "GET", path: "/v1/projects", error: "Load failed", page: "/projects" });
    const [, init] = fetchMock.mock.calls.find(([url]) => url === "/v1/me/failed-requests")!;
    expect(init.headers.Authorization).toBe("Bearer tok");

    await authFetch("/me");
    expect(sent()).toHaveLength(1); // said once
  });

  it("a request the app called off isn't one", async () => {
    const { authFetch } = await client();
    fetchMock.mockRejectedValueOnce(new DOMException("aborted", "AbortError"));
    await expect(authFetch("/projects")).rejects.toThrow();
    fetchMock.mockResolvedValue(ok());
    await authFetch("/me");
    await new Promise((r) => setTimeout(r, 0));
    expect(sent()).toEqual([]);
  });

  it("a send that can't be made is kept for the next, never noted itself; one the server refuses is dropped", async () => {
    const { authFetch } = await client();
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await expect(authFetch("/libraries", { method: "post" })).rejects.toThrow();
    // The next request gets through, but the send itself doesn't.
    fetchMock.mockImplementation(async (url: string) =>
      url === "/v1/me/failed-requests" ? Promise.reject(new TypeError("Load failed")) : ok());
    await authFetch("/me");
    await vi.waitFor(() => expect(sent()).toHaveLength(1));
    await new Promise((r) => setTimeout(r, 0));

    fetchMock.mockImplementation(async (url: string) =>
      url === "/v1/me/failed-requests" ? new Response("", { status: 429 }) : ok());
    await authFetch("/me");
    await vi.waitFor(() => expect(sent()).toHaveLength(2));
    expect(sent()[1]).toEqual([expect.objectContaining({ method: "POST", path: "/v1/libraries", error: "Failed to fetch" })]);
    await new Promise((r) => setTimeout(r, 0));

    await authFetch("/me");
    await new Promise((r) => setTimeout(r, 0));
    expect(sent()).toHaveLength(2); // the refused one isn't sent again
  });

  it("keeps the last twenty, each one plain line", async () => {
    const { noteFailed, sendFailed } = await import("./failedRequests");
    for (let i = 0; i < 25; i++) noteFailed("get", `/assets/ast_${i}`, new TypeError("line one\nline two"));
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    sendFailed("tok");
    const [[...kept]] = sent();
    expect(kept).toHaveLength(20);
    expect(kept[0].path).toBe("/v1/assets/ast_5");
    expect(kept[19].error).toBe("line one line two");
  });

  it("signed out, nothing is sent", async () => {
    const { noteFailed, sendFailed } = await import("./failedRequests");
    noteFailed("GET", "/projects", new TypeError("Load failed"));
    sendFailed("");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
