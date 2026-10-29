import { describe, expect, it } from "vitest";
import { cameFrom } from "./cameFrom";

describe("where a public page's visitor came from", () => {
  it("is the other site's host", () => {
    expect(cameFrom("https://www.facebook.com/some/post?id=1", "photos.example.com")).toBe("www.facebook.com");
    expect(cameFrom("http://lumiverb.io/", "photos.example.com")).toBe("lumiverb.io");
  });

  it("is nothing from this site, typed in, or not a web page", () => {
    expect(cameFrom("https://photos.example.com/projects/1", "photos.example.com")).toBeNull();
    expect(cameFrom("", "photos.example.com")).toBeNull();
    expect(cameFrom("android-app://com.slack/", "photos.example.com")).toBeNull();
    expect(cameFrom("not a url", "photos.example.com")).toBeNull();
  });
});
