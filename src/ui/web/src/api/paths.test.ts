import { afterEach, describe, expect, it, vi } from "vitest";
import { getLibrary, getPlayback, deleteLibraryFilter, setLibraryRole } from "./client";
import { apiPath, segment } from "./paths";

/** An id from a link or the data is one path segment, whatever it holds: react-router decodes route
 * params, so a link's id reaches the client as typed. */

const CRAFTED = "../../admin/accounts?x#";

function stubFetch() {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(url);
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

/** The path a browser asks for, after it resolves the URL ("..", "?" and "#" taking effect). */
function resolved(url: string): { segments: string[]; search: string; hash: string } {
  const u = new URL(url, "https://lumiverb.test");
  return { segments: u.pathname.split("/").slice(1).map(decodeURIComponent), search: u.search, hash: u.hash };
}

describe("apiPath", () => {
  it("keeps a crafted id one segment", () => {
    const path = apiPath`/libraries/${CRAFTED}/people`;
    expect(path).toBe("/libraries/..%2F..%2Fadmin%2Faccounts%3Fx%23/people");
    expect(resolved(`/v1${path}`)).toEqual({
      segments: ["v1", "libraries", CRAFTED, "people"],
      search: "",
      hash: "",
    });
  });

  it("encodes every value, numbers too, and nothing of the template's own", () => {
    expect(apiPath`/a/${"b c"}/d/${7}`).toBe("/a/b%20c/d/7");
    expect(apiPath`/plain`).toBe("/plain");
  });

  it("refuses what can't be a segment: a browser reads . and .. as steps along the path", () => {
    for (const bad of ["..", ".", ""]) {
      expect(() => segment(bad)).toThrow(/Not an id/);
      expect(() => apiPath`/libraries/${bad}`).toThrow(/Not an id/);
    }
    expect(segment("...")).toBe("...");
  });
});

describe("the client's requests", () => {
  it("send a crafted id as one segment, never another API path", async () => {
    const calls = stubFetch();
    await getLibrary(CRAFTED);
    await setLibraryRole(CRAFTED, "usr_1/../../x", "viewer");
    await deleteLibraryFilter(CRAFTED, "#frag");
    await getPlayback(CRAFTED, CRAFTED);
    expect(calls).toEqual([
      "/v1/libraries/..%2F..%2Fadmin%2Faccounts%3Fx%23",
      "/v1/libraries/..%2F..%2Fadmin%2Faccounts%3Fx%23/people/usr_1%2F..%2F..%2Fx",
      "/v1/libraries/..%2F..%2Fadmin%2Faccounts%3Fx%23/filters/%23frag",
      "/v1/assets/..%2F..%2Fadmin%2Faccounts%3Fx%23/playback?public_library_id=..%2F..%2Fadmin%2Faccounts%3Fx%23",
    ]);
    for (const url of calls) {
      const { segments, hash } = resolved(url);
      expect(segments.slice(0, 2)).toEqual(["v1", url.includes("/assets/") ? "assets" : "libraries"]);
      expect(segments[2]).toBe(CRAFTED);
      expect(hash).toBe("");
    }
  });

  it("refuse an id that can't be a segment without asking the server", async () => {
    const calls = stubFetch();
    await expect(getLibrary("..")).rejects.toThrow(/Not an id/);
    expect(calls).toEqual([]);
  });
});
