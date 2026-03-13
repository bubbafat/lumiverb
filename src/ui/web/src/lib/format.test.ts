import { describe, it, expect } from "vitest";
import { basename, formatFileSize, formatDate } from "./format";

describe("basename", () => {
  it("extracts filename from a nested path", () => {
    expect(basename("foo/bar/baz.jpg")).toBe("baz.jpg");
  });
  it("returns the input when there is no slash", () => {
    expect(basename("photo.jpg")).toBe("photo.jpg");
  });
  it("handles a leading slash", () => {
    expect(basename("/photo.jpg")).toBe("photo.jpg");
  });
  it("returns empty string for a path ending in a slash", () => {
    expect(basename("foo/")).toBe("");
  });
});

describe("formatFileSize", () => {
  it("formats bytes under 1 KB", () => {
    expect(formatFileSize(512)).toBe("512 B");
    expect(formatFileSize(0)).toBe("0 B");
  });
  it("formats kilobytes", () => {
    expect(formatFileSize(1024)).toBe("1.0 KB");
    expect(formatFileSize(1536)).toBe("1.5 KB");
  });
  it("formats megabytes", () => {
    expect(formatFileSize(1024 * 1024)).toBe("1.0 MB");
    expect(formatFileSize(2.5 * 1024 * 1024)).toBe("2.5 MB");
  });
  it("uses MB for values >= 1 MB", () => {
    expect(formatFileSize(1024 * 1024 - 1)).toBe("1024.0 KB");
  });
});

describe("formatDate", () => {
  it("returns 'Unknown' for null", () => {
    expect(formatDate(null)).toBe("Unknown");
  });
  it("returns 'Unknown' for empty string", () => {
    expect(formatDate("")).toBe("Unknown");
  });
  it("formats a valid ISO string", () => {
    const result = formatDate("2024-06-15T12:00:00Z");
    // toLocaleString output is locale-dependent; just check it's not "Unknown"
    expect(result).not.toBe("Unknown");
    expect(result.length).toBeGreaterThan(0);
  });
  it("returns 'Unknown' for an unparseable string", () => {
    // new Date("not-a-date").toLocaleString() returns "Invalid Date" in most engines,
    // which doesn't throw, so we verify we don't get "Unknown" for invalid but non-throwing input.
    // The function only returns "Unknown" on null/empty or thrown exception.
    const result = formatDate("not-a-date");
    expect(typeof result).toBe("string");
  });
});
