import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { SelectionToolbar } from "./SelectionToolbar";

afterEach(cleanup);

function toolbar() {
  render(
    <SelectionToolbar count={1} onClear={vi.fn()}>
      <button type="button">Remove from project</button>
    </SelectionToolbar>,
  );
  return screen.getByText("1 selected").closest(".fixed") as HTMLElement;
}

describe("SelectionToolbar", () => {
  it("sits above the phone's bottom bar instead of under it", () => {
    // Both are pinned to the bottom; at bottom-6 the bar covered the toolbar.
    const classes = toolbar().className.split(/\s+/);
    expect(classes).toContain("bottom-safe-offset-20");
    expect(classes).toContain("md:bottom-6");
    expect(classes).not.toContain("bottom-6");
  });

  it("wraps whole buttons onto a second line rather than hiding them", () => {
    // Scrolling sideways hid "Add to project" past the edge of a phone.
    const bar = toolbar();
    expect(bar.className.split(/\s+/)).toContain("w-max");
    expect(bar.className.split(/\s+/)).toContain("max-w-[calc(100vw-1rem)]");
    const row = bar.firstElementChild as HTMLElement;
    const classes = row.className.split(/\s+/);
    expect(classes).toEqual(expect.arrayContaining(["flex-wrap", "whitespace-nowrap"]));
    expect(classes).not.toContain("overflow-x-auto");
  });
});
