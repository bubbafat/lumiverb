import { describe, it, expect } from "vitest";
import { parseSearchQuery } from "./parseSearchQuery";

describe("parseSearchQuery", () => {
  it("extracts has:faces filter", () => {
    const result = parseSearchQuery("has:faces");
    expect(result.text).toBe("");
    expect(result.filters).toEqual({ has_faces: "true" });
  });

  it("extracts has:faces with surrounding text", () => {
    const result = parseSearchQuery("sunset has:faces beach");
    expect(result.text).toBe("sunset beach");
    expect(result.filters.has_faces).toBe("true");
  });

  it("is case insensitive", () => {
    const result = parseSearchQuery("HAS:FACES");
    expect(result.filters.has_faces).toBe("true");
  });

  it("combines has:faces with other filters", () => {
    const result = parseSearchQuery("has:faces is:favorite");
    expect(result.filters.has_faces).toBe("true");
    expect(result.filters.favorite).toBe("true");
  });

  it('extracts person:"name" filter', () => {
    const result = parseSearchQuery('person:"Susan"');
    expect(result.text).toBe("");
    expect(result.filters).toEqual({ person: "Susan" });
  });

  it('extracts person:"name" with surrounding text', () => {
    const result = parseSearchQuery('sunset person:"Susan" beach');
    expect(result.text).toBe("sunset beach");
    expect(result.filters.person).toBe("Susan");
  });

  it('combines person:"name" with other filters', () => {
    const result = parseSearchQuery('person:"Susan" is:favorite has:faces');
    expect(result.filters.person).toBe("Susan");
    expect(result.filters.favorite).toBe("true");
    expect(result.filters.has_faces).toBe("true");
  });
});
