import { describe, expect, it } from "vitest";
import { WAIT_REASONS, WAIT_SENTENCES, mediaNoun, waitIsLoud, waitSentence, type WaitContext } from "./waits";
import type { ProducerWait } from "../api/client";

// What each reason's sentence needs, as the server sends it (GET /v1/producers waits).
const DETAILS: Record<string, Partial<ProducerWait>> = {
  scheduler: { why: "not_running" },
  ai_machines: { job: "vision", why: "offline", message: "No machine doing descriptions & text is online: its work waits. Settings → AI shows why." },
  needs: { artifact: "scenes", paused: false },
  storage: { library_id: "lib_1", library_name: "Empty Besters", checked: true, away_since: "2026-10-12T18:20:00Z",
             seen_at: "2026-10-12T17:55:00Z" },
  scan: { paused: false },
};

const say: WaitContext = {
  title: "Analysis proxies",
  media: ["video"],
  titleOf: (artifact) => ({ scenes: "Scenes" })[artifact] ?? artifact,
  when: (iso) => `<${iso}>`,
};

describe("Why work waits", () => {
  it("has a sentence for every reason the server sends, and none it doesn't", () => {
    // tests/test_no_silent_waits.py holds WAIT_REASONS to src/shared/waits.py.
    expect(Object.keys(WAIT_SENTENCES).sort()).toEqual([...WAIT_REASONS].sort());
  });

  it.each([...WAIT_REASONS])("says %s plainly, for one clip and for many", (reason) => {
    for (const clips of [1, 1500]) {
      const text = waitSentence({ reason, clips, ...DETAILS[reason] }, say);
      expect(text.length).toBeGreaterThan(10);
      expect(text).not.toMatch(/undefined|NaN|null|\[object/);
      expect(text.trim().endsWith(".")).toBe(true);
    }
    // The count is said (a stopped redo counts clips, not videos).
    expect(waitSentence({ reason, clips: 1500, ...DETAILS[reason] }, say)).toContain("1,500");
  });

  it("agrees with the count", () => {
    expect(waitSentence({ reason: "scan", clips: 1 }, say)).toBe("1 video waits for a scan to see its file again.");
    expect(waitSentence({ reason: "scan", clips: 12, paused: true }, say)).toBe(
      "12 videos wait for a scan to see their file again, and Scans is paused.");
    expect(waitSentence({ reason: "held", clips: 1 }, say)).toBe(
      "1 video is tried again within the hour: its last try couldn't finish.");
    expect(waitSentence({ reason: "given_up", clips: 2 }, say)).toBe(
      "2 videos failed too many times and aren't tried again until someone asks.");
  });

  it("says which library's drive, and since when it can't be reached", () => {
    expect(waitSentence({ reason: "storage", clips: 40, ...DETAILS.storage }, say)).toBe(
      "40 videos in Empty Besters wait for its drive: it can't be reached since <2026-10-12T18:20:00Z>"
      + " (last seen <2026-10-12T17:55:00Z>).");
    expect(waitSentence({ reason: "storage", clips: 40, library_name: "Empty Besters", checked: false }, say)).toBe(
      "40 videos in Empty Besters wait for its drive: it hasn't been looked at since the scheduler started.");
  });

  it("says a new version is ready and waits for an admin, apart from an admin's stop", () => {
    expect(waitSentence({ reason: "new_version", clips: 40 }, say)).toBe(
      "A new version of Analysis proxies is ready: 40 videos made by an older one are made again once an admin"
      + " resumes it.");
    expect(waitSentence({ reason: "redo_stopped", clips: 3 }, say)).toBe(
      "Redo stopped: 3 clips stay as they are until it's resumed.");
  });

  it("says the scheduler's state", () => {
    expect(waitSentence({ reason: "scheduler", clips: 5, why: "not_running" }, say)).toBe(
      "5 videos wait for the scheduler, which isn't running.");
    expect(waitSentence({ reason: "scheduler", clips: 5, why: "starting" }, say)).toBe(
      "5 videos wait for the scheduler, which is starting.");
    expect(waitSentence({ reason: "scheduler", clips: 5, why: "cant_list", message: "column x does not exist" }, say))
      .toBe("5 videos wait: the scheduler couldn't list them (column x does not exist).");
  });

  it("says an AI job's wait even with nothing due yet, and how many when there are", () => {
    const off = { reason: "ai_machines", job: "vision", why: "off",
                  message: "Descriptions & text are off: no model is chosen in Settings → AI." };
    expect(waitSentence({ ...off, clips: 0 }, say)).toBe(off.message);
    expect(waitSentence({ ...off, clips: 3 }, say)).toBe(`3 videos wait. ${off.message}`);
  });

  it("still says how many wait for a reason this page doesn't know yet", () => {
    expect(waitSentence({ reason: "something_new", clips: 3 }, say)).toBe("3 videos wait.");
  });

  it("is amber when someone must act, grey when it only takes a while", () => {
    expect(waitIsLoud({ reason: "storage", clips: 1 })).toBe(true);
    expect(waitIsLoud({ reason: "held", clips: 1 })).toBe(false);
    expect(waitIsLoud({ reason: "needs", clips: 1, paused: false })).toBe(false);
    expect(waitIsLoud({ reason: "needs", clips: 1, paused: true })).toBe(true);
    expect(waitIsLoud({ reason: "scan", clips: 1, paused: true })).toBe(true);
  });

  it("names media as people would", () => {
    expect([mediaNoun(["video"], 1), mediaNoun(["image"], 2), mediaNoun(["image", "video"], 3)]).toEqual(
      ["video", "photos", "clips"]);
  });
});
