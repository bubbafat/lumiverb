import { describe, expect, it } from "vitest";
import { takenLiterally } from "./understood";

describe("takenLiterally", () => {
  it("quotes the words a part was understood from, as they were typed", () => {
    expect(takenLiterally("photos of art in Anaheim", "art")).toBe('photos of "art" in Anaheim');
    expect(takenLiterally("Sue dancing at California Adventure", "california adventure")).toBe(
      'Sue dancing at "California Adventure"',
    );
  });

  it("finds words the server read without their accents", () => {
    expect(takenLiterally("José at the beach", "jose")).toBe('"José" at the beach');
  });

  it("leaves quoted text alone and quotes the words outside it", () => {
    expect(takenLiterally('"art deco" art', "art")).toBe('"art deco" "art"');
  });

  it("only a whole word: never inside another", () => {
    expect(takenLiterally("party art", "art")).toBe('party "art"');
  });

  it("is the search as it was when the words aren't there", () => {
    expect(takenLiterally("dancing", "art")).toBe("dancing");
    expect(takenLiterally("dancing", "")).toBe("dancing");
  });
});
