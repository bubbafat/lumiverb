import { describe, expect, it } from "vitest";
import { readerLine, readByDevice } from "./readers";

const NOW = Date.parse("2026-10-17T18:00:00Z");
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();

function seen(over: Partial<{ state: "scanning" | "idle" | "paused" | "away"; waiting_files: number; waiting_bytes: number;
                              checked_in_at: string }> = {}) {
  return { client: "studio.local", version: "0.1.68", state: "idle" as const, waiting_files: 0, waiting_bytes: 0,
           last_full_scan_at: null, checked_in_at: ago(1), ...over };
}

describe("Who reads a library", () => {
  it("says this server plainly", () => {
    expect(readerLine({ reader: "server" }, NOW)).toEqual({ text: "Read by this server", attention: false });
    expect(readerLine({}, NOW).text).toBe("Read by this server");
    expect(readByDevice({ reader: "server" })).toBe(false);
  });

  it("names the device, what it's doing, what it has to upload, and when it checked in", () => {
    const lib = { reader: "dev_1", reader_name: "Studio Mac",
                  checked_in: seen({ state: "scanning", waiting_files: 212, waiting_bytes: 14 * 1024 ** 3 }) };
    expect(readerLine(lib, NOW)).toEqual({
      text: "Read by Studio Mac · scanning · 212 files (14 GB) to upload · checked in 1 minute ago", attention: false });
    expect(readerLine({ ...lib, checked_in: seen() }, NOW).text).toBe("Read by Studio Mac · checked in 1 minute ago");
  });

  it("asks for a look when it isn't heard from, hasn't checked in, or can't reach the folder", () => {
    const studio = { reader: "dev_1", reader_name: "Studio Mac" };
    expect(readerLine({ ...studio, checked_in: seen({ checked_in_at: ago(3 * 24 * 60) }) }, NOW)).toEqual({
      text: "Read by Studio Mac · not heard from in 3 days", attention: true });
    expect(readerLine({ ...studio, checked_in: null }, NOW)).toEqual({
      text: "Read by Studio Mac · not checked in yet", attention: true });
    const away = readerLine({ ...studio, checked_in: seen({ state: "away" }) }, NOW);
    expect(away.attention).toBe(true);
    expect(away.text).toContain("it can't reach the library's folder");
    expect(away.text).not.toMatch(/Can't be reached/);
  });
});
