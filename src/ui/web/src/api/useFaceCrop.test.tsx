import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { API_KEY_STORAGE_KEY, getApiKey, setApiKey } from "./client";
import { useFaceCrop } from "./useFaceCrop";
import { useAuthenticatedImage } from "./useAuthenticatedImage";

/** Face crops and thumbnails refresh the token on a 401 instead of signing out. */

function stubServer(statuses: number[], refreshStatus = 200) {
  const calls: { url: string; auth?: string }[] = [];
  const queue = [...statuses];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const headers = (init?.headers ?? {}) as Record<string, string>;
      calls.push({ url, auth: headers.Authorization });
      if (url === "/v1/auth/refresh") {
        return refreshStatus === 200
          ? new Response(JSON.stringify({ access_token: "new-token" }), { status: 200 })
          : new Response("{}", { status: refreshStatus });
      }
      const status = queue.shift() ?? 500;
      return status === 200 ? new Response(new Blob(["img"]), { status }) : new Response("{}", { status });
    }),
  );
  return calls;
}

const realCreateObjectURL = URL.createObjectURL;
const realRevokeObjectURL = URL.revokeObjectURL;

beforeEach(() => {
  setApiKey("old-token");
  URL.createObjectURL = vi.fn(() => "blob:crop");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  vi.unstubAllGlobals();
  URL.createObjectURL = realCreateObjectURL;
  URL.revokeObjectURL = realRevokeObjectURL;
  localStorage.clear();
});

describe("useFaceCrop", () => {
  it("refreshes on a 401 and shows the crop", async () => {
    const calls = stubServer([401, 200]);
    const { result } = renderHook(() => useFaceCrop("face_1"));
    await waitFor(() => expect(result.current.url).toBe("blob:crop"));
    expect(result.current.isLoading).toBe(false);
    expect(calls.map((c) => c.url)).toEqual(["/v1/faces/face_1/crop", "/v1/auth/refresh", "/v1/faces/face_1/crop"]);
    expect(calls[2].auth).toBe("Bearer new-token");
    expect(getApiKey()).toBe("new-token");
  });

  it("stays null without a crop", async () => {
    stubServer([404]);
    const { result } = renderHook(() => useFaceCrop("face_1"));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.url).toBeNull();
    expect(getApiKey()).toBe("old-token");
  });

  it("signs out when the refresh fails", async () => {
    stubServer([401], 401);
    const { result } = renderHook(() => useFaceCrop("face_1"));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.url).toBeNull();
    expect(localStorage.getItem(API_KEY_STORAGE_KEY)).toBeNull();
  });

  it("fetches nothing without a face", () => {
    const calls = stubServer([]);
    const { result } = renderHook(() => useFaceCrop(""));
    expect(result.current).toEqual({ url: null, isLoading: false });
    expect(calls).toEqual([]);
  });
});

describe("useAuthenticatedImage", () => {
  it("refreshes on a 401 instead of signing out", async () => {
    const calls = stubServer([401, 200]);
    const { result } = renderHook(() => useAuthenticatedImage("ast_1"));
    await waitFor(() => expect(result.current.url).toBe("blob:crop"));
    expect(calls[2].url).toBe("/v1/assets/ast_1/thumbnail");
    expect(getApiKey()).toBe("new-token");
  });
});
