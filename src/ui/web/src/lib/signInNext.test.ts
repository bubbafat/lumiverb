import { describe, expect, it } from "vitest";
import { signInNext } from "./signInNext";

const here = "http://localhost:6464";

describe("signInNext", () => {
  it("goes back to the page on this site that sent the visitor to sign in", () => {
    expect(signInNext("/browse?f=library%3Alib_1#top", here)).toBe("/browse?f=library%3Alib_1#top");
    expect(signInNext(`${here}/people/p_1`, here)).toBe("/people/p_1");
    expect(signInNext("/", here)).toBe("/");
  });

  it("goes home when there's nowhere to go back to", () => {
    expect(signInNext(null, here)).toBe("/");
    expect(signInNext("", here)).toBe("/");
    expect(signInNext("http://[", here)).toBe("/");
  });

  it("never leaves this site", () => {
    for (const next of ["https://example.com/x", "//example.com/x", "/\\example.com", "\\\\example.com",
                        "/.//example.com", "/./\\example.com", "javascript:alert(1)", "http://localhost:6465/x"]) {
      const dest = signInNext(next, here);
      expect(dest.startsWith("/"), next).toBe(true);
      expect(dest.startsWith("//") || dest.startsWith("/\\"), next).toBe(false);
      expect(new URL(dest, here).origin, next).toBe(here);
    }
  });
});
