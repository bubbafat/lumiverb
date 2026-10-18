import type { ReaderCheckIn } from "../api/types";
import { timeAgo } from "./format";

/** A reader not heard from for this long is said to be (it checks in every minute or so). */
export const QUIET_MS = 5 * 60_000;

/** What a page knows of a library's reader: GET /v1/libraries and GET /v1/producers/queue both say it. */
export interface ReaderOf {
  reader?: string;
  reader_name?: string | null;
  checked_in?: ReaderCheckIn | null;
}

/** "14 GB", "212 MB", "40 KB". */
function bytes(n: number): string {
  for (const [unit, size] of [["TB", 1024 ** 4], ["GB", 1024 ** 3], ["MB", 1024 ** 2], ["KB", 1024]] as const) {
    if (n >= size) return `${(n / size).toFixed(n / size < 10 ? 1 : 0)} ${unit}`;
  }
  return `${n} B`;
}

/** What a reader's latest scan of the library has left to send, "212 files (14 GB) to upload", or null when none. */
export function toUpload(seen: Pick<ReaderCheckIn, "waiting_files" | "waiting_bytes">): string | null {
  if (seen.waiting_files <= 0) return null;
  return `${seen.waiting_files.toLocaleString()} file${seen.waiting_files === 1 ? "" : "s"}`
    + `${seen.waiting_bytes > 0 ? ` (${bytes(seen.waiting_bytes)})` : ""} to upload`;
}

/** Whether a device reads it (else this server). */
export function readByDevice(lib: ReaderOf): boolean {
  return !!lib.reader && lib.reader !== "server";
}

/** Who reads a library and how it last said it was (ADR-018 decision 1), in one line, and whether that's worth a
 * look: a device not heard from lately, or one that can't reach the library's folder. "Read by this server";
 * "Read by Studio Mac · scanning · 212 files (14 GB) to upload · checked in 1 minute ago"; "Read by Studio Mac ·
 * not heard from in 3 days". A device's library is never said to be out of this server's reach. */
export function readerLine(lib: ReaderOf, now: number = Date.now()): { text: string; attention: boolean } {
  if (!readByDevice(lib)) return { text: "Read by this server", attention: false };
  const who = `Read by ${lib.reader_name || "a device"}`;
  const seen = lib.checked_in;
  if (!seen) return { text: `${who} · not checked in yet`, attention: true };
  if (now - new Date(seen.checked_in_at).getTime() > QUIET_MS) {
    return { text: `${who} · not heard from in ${timeAgo(seen.checked_in_at, now).replace(/ ago$/, "")}`, attention: true };
  }
  const parts = [who];
  if (seen.state === "scanning") parts.push("scanning");
  if (seen.state === "paused") parts.push("scans paused");
  if (seen.state === "away") parts.push("it can't reach the library's folder");
  const sending = toUpload(seen);
  if (sending) parts.push(sending);
  parts.push(`checked in ${timeAgo(seen.checked_in_at, now)}`);
  return { text: parts.join(" · "), attention: seen.state === "away" };
}
