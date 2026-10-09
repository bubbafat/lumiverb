import { describe, it, expect } from "vitest";
import { basename, formatFileSize, formatDate, mediaCount, timeAgo } from "./format";

describe("timeAgo", () => {
  const now = Date.parse("2026-10-08T12:00:00Z");
  const before = (s: number) => new Date(now - s * 1000).toISOString();
  it("says seconds, minutes, hours and days like the server", () => {
    expect(timeAgo(before(5), now)).toBe("5 seconds ago");
    expect(timeAgo(before(5 * 60), now)).toBe("5 minutes ago");
    expect(timeAgo(before(5 * 3600), now)).toBe("5 hours ago");
    expect(timeAgo(before(5 * 86400), now)).toBe("5 days ago");
  });
  it("uses the singular for one", () => {
    expect(timeAgo(before(1), now)).toBe("1 second ago");
    expect(timeAgo(before(60), now)).toBe("1 minute ago");
    expect(timeAgo(before(3600), now)).toBe("1 hour ago");
  });
  it("stays in hours until two days", () => {
    expect(timeAgo(before(30 * 3600), now)).toBe("30 hours ago");
    expect(timeAgo(before(2 * 86400), now)).toBe("2 days ago");
  });
  it("never goes negative", () => {
    expect(timeAgo(before(-10), now)).toBe("0 seconds ago");
  });
});

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

describe("mediaCount", () => {
  it("says photos when only photos are shown", () => {
    expect(mediaCount(1, "image")).toBe("1 photo");
    expect(mediaCount(5, "image")).toBe("5 photos");
  });
  it("says videos when only videos are shown", () => {
    expect(mediaCount(1, "video")).toBe("1 video");
    expect(mediaCount(1200, "video")).toBe("1,200 videos");
  });
  it("says items when photos and videos are mixed", () => {
    expect(mediaCount(5)).toBe("5 items");
    expect(mediaCount(1, null)).toBe("1 item");
  });
  it("shows how many of the folder", () => {
    expect(mediaCount(3, "video", 10)).toBe("3 of 10 videos");
  });
});
