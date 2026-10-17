import type { ProducerWait } from "../api/client";

/** Why a producer's due work waits: every reason GET /v1/producers sends, in the order a row shows them
 * (src/shared/waits.py, Robert, Oct 12: no silent waits). tests/test_no_silent_waits.py holds this list to
 * the server's, and WAIT_SENTENCES must say each one. */
export const WAIT_REASONS = [
  "paused",
  "scheduler",
  "ai_machines",
  "needs",
  "storage",
  "device",
  "new_version",
  "redo_stopped",
  "scan",
  "held",
  "retry_later",
  "given_up",
  "scan_failing",
] as const;

export type WaitReason = (typeof WAIT_REASONS)[number];

/** "videos", "photos" or "clips", for a count of a producer's media. */
export function mediaNoun(media: string[], count: number): string {
  const one = media.length === 1 && media[0] === "video" ? "video" : media.length === 1 ? "photo" : "clip";
  return count === 1 ? one : `${one}s`;
}

/** What a sentence needs besides the wait: the producer's row, other producers' titles, and times as the page
 * shows them. */
export interface WaitContext {
  title: string;
  media: string[];
  titleOf: (artifact: string) => string;
  when: (iso: string) => string;
}

const n = (value: number) => value.toLocaleString();

/** "1 photo" or "1,200 photos", and the words that agree with it. */
function some(w: ProducerWait, c: WaitContext) {
  const one = w.clips === 1;
  return {
    count: `${n(w.clips)} ${mediaNoun(c.media, w.clips)}`,
    wait: one ? "waits" : "wait",
    are: one ? "is" : "are",
    their: one ? "its" : "their",
    them: one ? "it" : "them",
  };
}

/** Each reason in one plain sentence, as the row says it. */
export const WAIT_SENTENCES: Record<WaitReason, (w: ProducerWait, c: WaitContext) => string> = {
  paused: (w, c) => {
    const s = some(w, c);
    return `${s.count} ${s.wait} until it's resumed.`;
  },
  scheduler: (w, c) => {
    const s = some(w, c);
    if (w.why === "starting") return `${s.count} ${s.wait} for the scheduler, which is starting.`;
    if (w.why === "cant_list") return `${s.count} ${s.wait}: the scheduler couldn't list ${s.them} (${w.message ?? "an error"}).`;
    return `${s.count} ${s.wait} for the scheduler, which isn't running.`;
  },
  // The server's sentence (it names Settings → AI, which the row links), after how many wait.
  ai_machines: (w, c) => {
    const s = some(w, c);
    const why = w.message ?? "Its AI machines can't be used. Settings → AI shows why.";
    return w.clips > 0 ? `${s.count} ${s.wait}. ${why}` : why;
  },
  needs: (w, c) => {
    const s = some(w, c);
    return `${s.count} ${s.wait} for ${c.titleOf(w.artifact ?? "")}${w.paused ? ", which is paused." : " first."}`;
  },
  storage: (w, c) => {
    const s = some(w, c);
    const where = w.library_name || "a library";
    const state = w.checked
      ? `it can't be reached${w.away_since ? ` since ${c.when(w.away_since)}` : ""}`
      : "it hasn't been looked at since the scheduler started";
    return `${s.count} in ${where} ${s.wait} for its drive: ${state}${w.seen_at ? ` (last seen ${c.when(w.seen_at)})` : ""}.`;
  },
  // A device reads the library: it makes these from its originals, and says when it last checked in.
  device: (w, c) => {
    const s = some(w, c);
    const who = w.device_name || "its device";
    const heard = w.checked_in_at ? `last checked in ${c.when(w.checked_in_at)}` : "not checked in yet";
    return `${s.count} in ${w.library_name || "a library"} ${s.wait} for ${who}, which reads it (${heard}).`;
  },
  new_version: (w, c) => {
    const s = some(w, c);
    return `A new version of ${c.title} is ready: ${s.count} made by an older one ${s.are} made again once an admin resumes it.`;
  },
  redo_stopped: (w) => {
    const one = w.clips === 1;
    return `Redo stopped: ${n(w.clips)} clip${one ? "" : "s"} ${one ? "stays as it is" : "stay as they are"} until it's resumed.`;
  },
  scan: (w, c) => {
    const s = some(w, c);
    return `${s.count} ${s.wait} for a scan to see ${s.their} file again${w.paused ? ", and its library's scans are paused." : "."}`;
  },
  held: (w, c) => {
    const s = some(w, c);
    return `${s.count} ${s.are} tried again within the hour: ${s.their} last try couldn't finish.`;
  },
  retry_later: (w, c) => {
    const s = some(w, c);
    return `${s.count} failed and ${s.wait} to be tried again.`;
  },
  given_up: (w, c) => {
    const s = some(w, c);
    return `${s.count} failed too many times and ${s.are}n't tried again until someone asks.`;
  },
  // A library's scans' (each library's row under Scans): no clips, a library.
  scan_failing: (w, c) => {
    const why = w.message ? ` (${w.message})` : "";
    const next = w.next_try ? `; next try at ${c.when(w.next_try)}` : "";
    return `Scans of ${w.library_name || "a library"} keep failing${why}${next}.`;
  },
};

/** The sentence for a wait; a reason this page doesn't know yet (a newer server) still says how many wait. */
export function waitSentence(w: ProducerWait, c: WaitContext): string {
  const say = WAIT_SENTENCES[w.reason as WaitReason];
  if (say) return say(w, c);
  const s = some(w, c);
  return `${s.count} ${s.wait}.`;
}

/** Reasons that stop its work until someone acts (shown amber); the rest only take a while. */
export function waitIsLoud(w: ProducerWait): boolean {
  if (w.reason === "needs" || w.reason === "scan") return !!w.paused;
  return ["paused", "scheduler", "ai_machines", "storage", "new_version", "given_up", "scan_failing"].includes(w.reason);
}
