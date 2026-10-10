import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  API_KEY_STORAGE_KEY,
  ApiError,
  deleteLibrary,
  dismissCluster,
  exportProject,
  getApiKey,
  setApiKey,
} from "./client";

/** A 401 refreshes the token and retries; only a failed refresh or a second 401 signs out. */

type Reply = { status: number; body?: unknown };

function json(status: number, body?: unknown): Response {
  if (status === 204) return new Response(null, { status });
  return new Response(JSON.stringify(body ?? {}), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Stub fetch: /v1/auth/refresh answers `refresh`, everything else takes the next of `replies`. */
function stubServer(replies: Reply[], refresh: Reply = { status: 200, body: { access_token: "new-token" } }) {
  const calls: { url: string; auth?: string }[] = [];
  const queue = [...replies];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const headers = (init?.headers ?? {}) as Record<string, string>;
      calls.push({ url, auth: headers.Authorization });
      if (url === "/v1/auth/refresh") return json(refresh.status, refresh.body);
      const next = queue.shift();
      if (!next) throw new Error(`unexpected fetch ${url}`);
      return json(next.status, next.body);
    }),
  );
  return calls;
}

const inProjects = {
  error: { code: "in_projects", message: "Clips are in projects", details: { projects: [{ name: "Reel" }] } },
};

beforeEach(() => {
  setApiKey("old-token");
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("apiFetch after a 401", () => {
  it("hands the retry's 409 decision to the caller and stays signed in", async () => {
    const calls = stubServer([{ status: 401 }, { status: 409, body: inProjects }]);

    const err = await deleteLibrary("lib_1").catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).code).toBe("in_projects");
    expect((err as ApiError).details).toEqual({ projects: [{ name: "Reel" }] });
    expect(getApiKey()).toBe("new-token");
    expect(calls.map((c) => c.url)).toEqual(["/v1/libraries/lib_1", "/v1/auth/refresh", "/v1/libraries/lib_1"]);
    expect(calls[2].auth).toBe("Bearer new-token");
  });

  it("returns the retry's result when it succeeds", async () => {
    stubServer([{ status: 401 }, { status: 200, body: { person_id: "p_1" } }]);
    await expect(dismissCluster("fc_3")).resolves.toEqual({ person_id: "p_1" });
    expect(getApiKey()).toBe("new-token");
  });

  it("signs out when the refresh fails", async () => {
    stubServer([{ status: 401 }], { status: 401 });
    const err = await deleteLibrary("lib_1").catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(401);
    expect(localStorage.getItem(API_KEY_STORAGE_KEY)).toBeNull();
  });

  it("doesn't try a refresh without a stored token", async () => {
    localStorage.clear();
    const calls = stubServer([{ status: 401 }]);
    const err = await deleteLibrary("lib_1").catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(401);
    expect(calls.map((c) => c.url)).toEqual(["/v1/libraries/lib_1"]);
  });

  it("signs out when the retry is 401 again", async () => {
    stubServer([{ status: 401 }, { status: 401 }]);
    const err = await deleteLibrary("lib_1").catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(401);
    expect(localStorage.getItem(API_KEY_STORAGE_KEY)).toBeNull();
  });

  it("retries with another tab's token when its own refresh fails", async () => {
    const calls: { url: string; auth?: string }[] = [];
    const queue = [json(401), json(200, { person_id: "p_1" })];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url, auth: ((init?.headers ?? {}) as Record<string, string>).Authorization });
        if (url === "/v1/auth/refresh") {
          setApiKey("other-tab-token"); // the other tab refreshed first
          return json(401);
        }
        return queue.shift()!;
      }),
    );

    await expect(dismissCluster("fc_3")).resolves.toEqual({ person_id: "p_1" });

    expect(getApiKey()).toBe("other-tab-token");
    expect(calls.map((c) => c.auth)).toEqual(["Bearer old-token", "Bearer old-token", "Bearer other-tab-token"]);
  });
});

describe("dismissCluster", () => {
  it("POSTs and returns the person to undo with", async () => {
    const calls = stubServer([{ status: 200, body: { person_id: "p_9" } }]);
    await expect(dismissCluster("fc_2")).resolves.toEqual({ person_id: "p_9" });
    expect(calls[0].url).toBe("/v1/faces/clusters/fc_2/dismiss");
  });

  it("throws the server's error instead of returning it as a result", async () => {
    stubServer([{ status: 409, body: { error: { code: "cluster_changed", message: "This group changed. Reload the groups.", details: {} } } }]);
    const err = await dismissCluster("fc_7").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).code).toBe("cluster_changed");
    expect((err as ApiError).message).toBe("This group changed. Reload the groups.");
  });
});

describe("exportProject after a 401", () => {
  it("signs out when the retry is 401 again", async () => {
    stubServer([{ status: 401 }, { status: 401 }]);
    await expect(exportProject("prj_1", "fcp7")).rejects.toBeInstanceOf(ApiError);
    expect(localStorage.getItem(API_KEY_STORAGE_KEY)).toBeNull();
  });

  it("reports the retry's error and stays signed in", async () => {
    stubServer([{ status: 401 }, { status: 404, body: { error: { message: "Project not found" } } }]);
    const err = await exportProject("prj_1", "fcp7").catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(404);
    expect((err as ApiError).message).toBe("Project not found");
    expect(getApiKey()).toBe("new-token");
  });
});
