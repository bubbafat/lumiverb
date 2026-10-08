import { afterEach, describe, expect, it, vi } from "vitest";
import { listProjects, setProjectStatus } from "./client";

/** Project lifecycle: archived projects leave the default list. */

function stubFetch(body: unknown) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("listProjects", () => {
  it("lists active projects by default", async () => {
    const calls = stubFetch({ items: [] });
    await listProjects();
    expect(calls[0].url).toBe("/v1/projects");
  });

  it("asks for archived or all projects", async () => {
    const calls = stubFetch({ items: [] });
    await listProjects("archived");
    await listProjects("all");
    expect(calls.map((c) => c.url)).toEqual([
      "/v1/projects?status=archived",
      "/v1/projects?status=all",
    ]);
  });
});

describe("setProjectStatus", () => {
  it("PATCHes the status", async () => {
    const calls = stubFetch({ project_id: "prj_1", status: "archived" });
    await setProjectStatus("prj_1", "archived");
    expect(calls[0].url).toBe("/v1/projects/prj_1");
    expect(calls[0].init?.method).toBe("PATCH");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ status: "archived" });
  });
});
