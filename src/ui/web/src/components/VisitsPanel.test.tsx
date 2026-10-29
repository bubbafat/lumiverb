/**
 * A place's visits (ADR-025): its totals, a bar a day with its day said on hover or focus, the same day by day as a
 * table, its clips most opened, where visitors came from, and bots apart.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PlaceVisits } from "../api/client";
import { VisitsPanel, daySaid, everyDay } from "./VisitsPanel";

vi.mock("../api/useAuthenticatedImage", () => ({
  useAuthenticatedImage: () => ({ url: "blob:thumb", isLoading: false, error: null }),
}));

afterEach(() => cleanup());

const none = { visitors: 0, times: 0 };
const counts = (view: number, views: number, opened = 0, played = 0) => ({
  view: { visitors: view, times: views }, open: { visitors: opened, times: opened },
  play: { visitors: played, times: played }, export: none, demo: none,
});

function report(over: Partial<PlaceVisits> = {}): PlaceVisits {
  const today = new Date();
  const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const since = new Date(today);
  since.setDate(today.getDate() - 2);
  return {
    since: iso(since),
    days: [{ day: iso(since), ...counts(2, 3, 4) }, { day: iso(today), ...counts(1, 1, 0, 1) }],
    totals: counts(3, 4, 4, 1),
    clips: [{ asset_id: "ast_1", opened: 4, played: 0 }, { asset_id: "ast_2", opened: 0, played: 1 }],
    sources: [{ site: "facebook.com", visitors: 2 }],
    bots: 5,
    ...over,
  };
}

function show(load: (days: number) => Promise<PlaceVisits>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <VisitsPanel queryKey={["visits-test"]} load={load} />
    </QueryClientProvider>,
  );
}

describe("a place's visits", () => {
  it("says its totals, its clips, where visitors came from and bots", async () => {
    show(async () => report());
    expect((await screen.findByText("Visitors")).previousSibling?.textContent).toBe("3");
    expect(screen.getByText("Page views").previousSibling?.textContent).toBe("4");
    expect(screen.getByText("Clips opened").previousSibling?.textContent).toBe("4");
    expect(screen.getByText("Videos played").previousSibling?.textContent).toBe("1");
    expect(screen.queryByText("Exports")).toBeNull(); // a project's only
    const clips = screen.getByRole("region", { name: "Clips most opened" });
    expect(within(clips).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["4 opened", "1 played"]);
    expect(screen.getByRole("region", { name: "Where visitors came from" }).textContent).toContain("facebook.com · 2 visitors");
    expect(screen.getByText(/5 requests from search engines' crawlers and other bots/)).toBeTruthy();
  });

  it("has a bar a day, the day said on focus, and the same day by day as a table", async () => {
    show(async () => report());
    const chart = await screen.findByRole("figure", { name: "Visitors a day" });
    const bars = chart.querySelectorAll("g[tabindex='0']");
    expect(bars).toHaveLength(3); // three days, one of them without visits
    fireEvent.focus(bars[0]);
    expect(within(chart).getByRole("tooltip").textContent).toContain("2 visitors · 3 views · 4 opened");
    expect(bars[1].getAttribute("aria-label")).toContain("0 visitors");
    const rows = screen.getAllByRole("row");
    expect(rows).toHaveLength(3); // the head and the two days with visits, newest first
    expect(rows[1].textContent).toContain("1 visitor · 1 view · 1 played");
  });

  it("covers another period when asked", async () => {
    const load = vi.fn(async () => report());
    show(load);
    await screen.findByText("Page views");
    fireEvent.click(screen.getByRole("button", { name: "7 days" }));
    await vi.waitFor(() => expect(load).toHaveBeenCalledWith(7));
  });

  it("says when there are none", async () => {
    show(async () => report({ days: [], totals: counts(0, 0), clips: [], sources: [], bots: 0 }));
    expect(await screen.findByText("No visits in the last 30 days.")).toBeTruthy();
  });
});

describe("its days", () => {
  it("are every day from the first to today", () => {
    const days = everyDay({ ...report(), since: "2026-10-27", days: [{ day: "2026-10-28", ...counts(1, 1) }] },
                          new Date(2026, 9, 29, 9));
    expect(days.map((d) => d.day)).toEqual(["2026-10-27", "2026-10-28", "2026-10-29"]);
    expect(days.map((d) => d.view.visitors)).toEqual([0, 1, 0]);
  });

  it("say what was counted, visitors always", () => {
    expect(daySaid(counts(0, 0))).toBe("0 visitors");
    expect(daySaid({ ...counts(1, 2, 3, 1), export: { visitors: 1, times: 1 } }))
      .toBe("1 visitor · 2 views · 3 opened · 1 played · 1 export");
  });
});
