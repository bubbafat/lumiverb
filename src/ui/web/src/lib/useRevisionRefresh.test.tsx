import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useRevisionRefresh } from "./useRevisionRefresh";

const INTERVAL = 30_000;

type Props = { revision: number | string | undefined; scope?: string };

function setup(initial: Props) {
  const refresh = vi.fn();
  const hook = renderHook(
    ({ revision, scope }: Props) =>
      useRevisionRefresh(revision, refresh, { scope, minIntervalMs: INTERVAL }),
    { initialProps: initial },
  );
  return { refresh, ...hook };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useRevisionRefresh", () => {
  it("takes the first revision it sees as the starting point, without refreshing", () => {
    const { refresh, rerender } = setup({ revision: undefined });
    rerender({ revision: 7 });
    rerender({ revision: 7 });
    vi.advanceTimersByTime(INTERVAL * 2);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("refreshes right away when the revision changes", () => {
    const { refresh, rerender } = setup({ revision: 7 });
    rerender({ revision: 8 });
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("ignores a revision that goes missing (a failed or pending poll)", () => {
    const { refresh, rerender } = setup({ revision: 7 });
    rerender({ revision: undefined });
    rerender({ revision: 7 });
    expect(refresh).not.toHaveBeenCalled();
  });

  it("refreshes once now and once at the end of the interval during a burst, not once per change", () => {
    const { refresh, rerender } = setup({ revision: 1 });
    // A long ingest: the poll sees a new revision every 10 seconds.
    rerender({ revision: 2 });
    expect(refresh).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(10_000);
    rerender({ revision: 3 });
    vi.advanceTimersByTime(10_000);
    rerender({ revision: 4 });
    expect(refresh).toHaveBeenCalledTimes(1);
    // The interval since the first refresh ends: one refresh covers 3 and 4.
    vi.advanceTimersByTime(10_000);
    expect(refresh).toHaveBeenCalledTimes(2);
    // Nothing changed since: no more refreshes.
    vi.advanceTimersByTime(INTERVAL * 3);
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("refreshes at most once per interval while the revision keeps changing", () => {
    const { refresh, rerender } = setup({ revision: 0 });
    // Two minutes of a new revision every 10 seconds: 12 changes.
    for (let r = 1; r <= 12; r++) {
      rerender({ revision: r });
      vi.advanceTimersByTime(10_000);
    }
    vi.advanceTimersByTime(INTERVAL);
    // One right away, then one per 30 seconds: 0s, 30s, 60s, 90s, and the last at 120s.
    expect(refresh).toHaveBeenCalledTimes(5);
  });

  it("refreshes right away again once a quiet interval has passed", () => {
    const { refresh, rerender } = setup({ revision: 1 });
    rerender({ revision: 2 });
    vi.advanceTimersByTime(INTERVAL + 1);
    rerender({ revision: 3 });
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("starts over when the scope changes: no refresh for the new library's first revision", () => {
    const { refresh, rerender } = setup({ revision: 5, scope: "lib_a" });
    rerender({ revision: 40, scope: "lib_b" });
    expect(refresh).not.toHaveBeenCalled();
    rerender({ revision: 41, scope: "lib_b" });
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("drops a pending refresh when the scope changes", () => {
    const { refresh, rerender } = setup({ revision: 1, scope: "lib_a" });
    rerender({ revision: 2, scope: "lib_a" });
    rerender({ revision: 3, scope: "lib_a" }); // pending, due in 30s
    rerender({ revision: 90, scope: "lib_b" });
    vi.advanceTimersByTime(INTERVAL * 2);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("drops a pending refresh on unmount", () => {
    const { refresh, rerender, unmount } = setup({ revision: 1 });
    rerender({ revision: 2 });
    rerender({ revision: 3 });
    unmount();
    vi.advanceTimersByTime(INTERVAL * 2);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("calls the latest refresh function, not the one from the render that saw the change", () => {
    const first = vi.fn();
    const latest = vi.fn();
    const { rerender } = renderHook(
      ({ revision, fn }: { revision: number; fn: () => void }) =>
        useRevisionRefresh(revision, fn, { minIntervalMs: INTERVAL }),
      { initialProps: { revision: 1, fn: first } },
    );
    rerender({ revision: 2, fn: first });
    rerender({ revision: 3, fn: first }); // pending
    rerender({ revision: 3, fn: latest }); // e.g. the filters changed meanwhile
    vi.advanceTimersByTime(INTERVAL);
    expect(first).toHaveBeenCalledTimes(1);
    expect(latest).toHaveBeenCalledTimes(1);
  });

  it("works with a string revision (several libraries' revisions in one)", () => {
    const { refresh, rerender } = setup({ revision: "lib_a:1,lib_b:4" });
    rerender({ revision: "lib_a:1,lib_b:5" });
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});
