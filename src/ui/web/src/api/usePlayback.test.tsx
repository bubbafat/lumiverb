import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { usePlayback } from "./usePlayback";

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:preview", revokeObjectURL: () => {} }));
});

afterEach(() => {
  cleanup();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("usePlayback", () => {
  it("plays the signed full-length link", async () => {
    fetchMock.mockImplementation(async (url: string) =>
      url.includes("/playback")
        ? new Response(JSON.stringify({ url: "/v1/stream/abc.def", expires_at: "x", source: "analysis_proxy", max_seconds: null }), { status: 200 })
        : new Response("{}", { status: 404 }),
    );
    const { result } = renderHook(() => usePlayback("ast_1", { enabled: true }), { wrapper });
    await waitFor(() => expect(result.current.src).toBe("/v1/stream/abc.def"));
    expect(result.current.source).toBe("analysis_proxy");
    expect(result.current.maxSeconds).toBeNull();
    // The 10-second preview isn't downloaded as well.
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/preview"))).toBe(false);
  });

  it("passes the public library along", async () => {
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ url: "/v1/stream/p.q", expires_at: "x", source: "preview", max_seconds: 30 }), { status: 200 }),
    );
    const { result } = renderHook(() => usePlayback("ast_1", { enabled: true, isPublic: true, publicLibraryId: "lib_9" }), { wrapper });
    await waitFor(() => expect(result.current.src).toBe("/v1/stream/p.q"));
    expect(String(fetchMock.mock.calls[0][0])).toContain("/v1/assets/ast_1/playback?public_library_id=lib_9");
    expect(result.current.maxSeconds).toBe(30);
  });

  it("falls back to the preview when there's no playback link", async () => {
    fetchMock.mockImplementation(async (url: string) =>
      url.includes("/playback")
        ? new Response(JSON.stringify({ error: { code: "not_found", message: "no" } }), { status: 404 })
        : new Response(new Blob(["mp4"]), { status: 200 }),
    );
    const { result } = renderHook(() => usePlayback("ast_1", { enabled: true }), { wrapper });
    await waitFor(() => expect(result.current.src).toBe("blob:preview"));
    expect(result.current.source).toBe("preview");
  });

  it("keeps its link while the preview plays, and switches when the whole video is ready", async () => {
    const answers = [
      { url: "/v1/stream/short1", source: "preview" },
      { url: "/v1/stream/short2", source: "preview" },
      { url: "/v1/stream/short3", source: "preview" },
      { url: "/v1/stream/full", source: "analysis_proxy" },
    ];
    let n = 0;
    fetchMock.mockImplementation(async () => {
      const a = answers[Math.min(n++, answers.length - 1)];
      return new Response(JSON.stringify({ ...a, expires_at: "x", max_seconds: null }), { status: 200 });
    });
    const seen: (string | null)[] = [];
    const { result } = renderHook(
      () => {
        const r = usePlayback("ast_1", { enabled: true, pollMs: 20 });
        seen.push(r.src);
        return r;
      },
      { wrapper },
    );
    await waitFor(() => expect(result.current.src).toBe("/v1/stream/full"));
    // A fresh link for the same preview would restart the video: the first one stays.
    const handedOut = seen.filter((src, i) => src && src !== seen[i - 1]);
    expect(handedOut).toEqual(["/v1/stream/short1", "/v1/stream/full"]);
    expect(n).toBeGreaterThanOrEqual(4);
    // Once whole, it stops asking.
    const asked = n;
    await new Promise((r) => setTimeout(r, 100));
    expect(n).toBe(asked);
  });

  it("renews: a new link for the same video replaces a failed one", async () => {
    let n = 0;
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ url: `/v1/stream/link${++n}`, expires_at: "x", source: "analysis_proxy", max_seconds: null }), { status: 200 }),
    );
    const { result } = renderHook(() => usePlayback("ast_1", { enabled: true }), { wrapper });
    await waitFor(() => expect(result.current.src).toBe("/v1/stream/link1"));
    result.current.renew();
    await waitFor(() => expect(result.current.src).toBe("/v1/stream/link2"));
  });

  it("reports a renewal that fails, so the player can say so", async () => {
    let n = 0;
    fetchMock.mockImplementation(async () =>
      ++n === 1
        ? new Response(JSON.stringify({ url: "/v1/stream/one", expires_at: "x", source: "analysis_proxy", max_seconds: null }), { status: 200 })
        : new Response(JSON.stringify({ error: { code: "x", message: "gone" } }), { status: 404 }),
    );
    const { result } = renderHook(() => usePlayback("ast_1", { enabled: true }), { wrapper });
    await waitFor(() => expect(result.current.src).toBe("/v1/stream/one"));
    expect(result.current.failed).toBe(false);
    result.current.renew();
    await waitFor(() => expect(result.current.failed).toBe(true));
  });

  it("asks for a public project's clip by project", async () => {
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ url: "/v1/stream/p", expires_at: "x", source: "preview", max_seconds: 10 }), { status: 200 }),
    );
    const { result } = renderHook(() => usePlayback("ast_1", { enabled: true, isPublic: true, publicProjectId: "prj_7" }), { wrapper });
    await waitFor(() => expect(result.current.src).toBe("/v1/stream/p"));
    expect(String(fetchMock.mock.calls[0][0])).toContain("/v1/assets/ast_1/playback?public_project_id=prj_7");
  });

  it("does nothing for photos", () => {
    const { result } = renderHook(() => usePlayback("ast_1", { enabled: false }), { wrapper });
    expect(result.current.src).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
