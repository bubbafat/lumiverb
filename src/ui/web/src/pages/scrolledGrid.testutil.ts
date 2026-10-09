/**
 * Helpers for tests of a scrolled clip grid. jsdom has no layout, so these
 * stand in for it: an 800 px tall scroller whose content has 120 px (the
 * page's header and filter bar) above the grid, and the grid's rows read
 * from the positions the virtualizer gives them.
 */
import { act } from "@testing-library/react";
import { vi } from "vitest";

/** What sits above the grid in the scroller. */
export const ABOVE_GRID = 120;

/** Give the scroller a viewport; call before the page renders. Undo with vi.restoreAllMocks(). */
export function stubLayout(scroller: HTMLElement) {
  Object.defineProperty(scroller, "offsetHeight", { value: 800 });
  Object.defineProperty(scroller, "offsetWidth", { value: 1200 });
  Object.defineProperty(scroller, "clientHeight", { value: 800 });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    // Only the grid's own box is ever asked for, besides the scroller's.
    const top = this === scroller ? 0 : ABOVE_GRID - scroller.scrollTop;
    return { top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: top, toJSON() {} } as DOMRect;
  });
}

/**
 * Newest first: `fresh` new clips on top of 60 older ones, a day for every
 * 20 (so the grid has a few date headers), some of them portrait.
 */
export function clipsWith<T extends object>(base: T, fresh: number) {
  return Array.from({ length: fresh + 60 }, (_, i) => {
    const n = fresh + 60 - i;
    return {
      ...base,
      asset_id: `ast_${n}`,
      rel_path: `day1/clip${n}.mov`,
      width: n % 3 === 0 ? 1080 : 1920,
      height: n % 3 === 0 ? 1920 : 1080,
      taken_at: `2026-04-${String(1 + Math.floor(n / 20)).padStart(2, "0")}T12:00:00Z`,
    };
  });
}

/** The grid's rows of clips that are rendered, top to bottom. */
function renderedRows() {
  return [...document.querySelectorAll<HTMLElement>("[style*='translateY']")]
    .map((el) => ({
      top: Number(/translateY\((-?[\d.]+)px\)/.exec(el.style.transform)![1]),
      height: parseFloat(el.style.height),
      names: [...el.querySelectorAll("img")].map((img) => img.alt),
    }))
    .filter((r) => r.names.length > 0)
    .sort((a, b) => a.top - b.top);
}

/** The first clip on screen and how far its row's top sits from the scroller's top. */
export function firstOnScreen(scroller: HTMLElement): { name: string; offset: number } {
  const row = renderedRows().find((r) => ABOVE_GRID + r.top + r.height > scroller.scrollTop)!;
  return { name: row.names[0], offset: ABOVE_GRID + row.top - scroller.scrollTop };
}

/** Where a clip's row sits from the scroller's top (rows reflow, so it may not lead its row). */
export function placeOf(scroller: HTMLElement, name: string): { name: string; offset: number } {
  const row = renderedRows().find((r) => r.names.includes(name));
  return { name, offset: row ? ABOVE_GRID + row.top - scroller.scrollTop : NaN };
}

/** The grid's height, as the virtualizer sizes it. */
export function gridHeight(): number {
  const row = document.querySelector<HTMLElement>("[style*='translateY']")!;
  return parseFloat(row.parentElement!.style.height);
}

/** A browser reports a change of scrollTop with a scroll event; jsdom doesn't. */
export function reportScroll(scroller: HTMLElement) {
  act(() => {
    scroller.dispatchEvent(new Event("scroll"));
  });
}

/** jsdom has no element scrollTo: jump there, as a smooth scroll ends up. Returns its calls. */
export function stubScrollTo(scroller: HTMLElement): ScrollToOptions[] {
  const calls: ScrollToOptions[] = [];
  scroller.scrollTo = ((opts: ScrollToOptions) => {
    calls.push(opts);
    scroller.scrollTop = opts.top ?? scroller.scrollTop;
    scroller.dispatchEvent(new Event("scroll"));
  }) as HTMLElement["scrollTo"];
  return calls;
}

export function scrollTo(scroller: HTMLElement, top: number) {
  act(() => {
    scroller.scrollTop = top;
  });
  reportScroll(scroller);
}
