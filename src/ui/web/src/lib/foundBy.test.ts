import { describe, expect, it } from "vitest";
import { foundBy, foundByLine } from "./foundBy";
import type { SearchContext } from "../api/types";

function ctx(matched_in: string | null, extra: Partial<SearchContext> = {}): SearchContext {
  return { score: 1, hit_type: "asset", snippet: null, start_ms: null, end_ms: null, matched_in, ...extra };
}

describe("what found a clip", () => {
  it("flags all but its description, with the words that matched", () => {
    expect(foundBy(ctx("description", { snippet: "A diner sign" }), false))
      .toEqual({ label: "Description", detail: "A diner sign", startAt: null, flagged: false });
    expect(foundBy(ctx("text", { snippet: "Dundee Diner Kitchen" }), false))
      .toEqual({ label: "Text in photo", detail: "Dundee Diner Kitchen", startAt: null, flagged: true });
    expect(foundBy(ctx("text"), true)?.label).toBe("Text in video");
    expect(foundBy(ctx("note", { snippet: "Lunch in Dundee" }), false)?.detail).toBe("Lunch in Dundee");
    expect(foundBy(ctx("path", { snippet: "its description" }), false))
      .toEqual({ label: "Folder or file name", detail: null, startAt: null, flagged: true });
    expect(foundBy(ctx("fact", { snippet: "Dundee, Oregon", from_plugin: "Place names" }), false)?.label)
      .toBe("Place names");
  });

  it("says where in a video, and opens there", () => {
    expect(foundBy(ctx("scene", { hit_type: "scene", start_ms: 42_000 }), true))
      .toMatchObject({ label: "Scene at 0:42", startAt: 42 });
    expect(foundBy(ctx("transcript", { hit_type: "transcript", start_ms: 83_000 }), true))
      .toMatchObject({ label: "Said at 1:23", startAt: 83 });
    expect(foundBy(ctx("transcript"), true)?.label).toBe("Said in it");
  });

  it("says where it was or who's in it for a search understood", () => {
    expect(foundBy(ctx("place", { snippet: "Dundee" }), false)).toMatchObject({ label: "Near Dundee", detail: null });
    expect(foundBy(ctx("person", { snippet: "Susan Horvick" }), false)?.label).toBe("Recognised: Susan Horvick");
  });

  it("says nothing when the search doesn't", () => {
    expect(foundBy(null, false)).toBeNull();
    expect(foundBy(ctx(null), false)).toBeNull();
    expect(foundBy(ctx("something new"), false)).toBeNull();
  });

  it("is one line with its words", () => {
    expect(foundByLine(foundBy(ctx("text", { snippet: "Dundee Diner" }), false)!)).toBe("Text in photo: Dundee Diner");
    expect(foundByLine(foundBy(ctx("place", { snippet: "Dundee" }), false)!)).toBe("Near Dundee");
  });
});
