import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { PluginFactsSection } from "./PluginFactsSection";

afterEach(cleanup);

describe("A clip's facts from its plug-ins", () => {
  it("shows each plug-in's facts under its title, a link where it has one, and its credit", () => {
    render(
      <PluginFactsSection
        plugins={[
          { plugin_id: "facts", title: "Facts", credit: { text: "Facts Inc.", url: "https://example.org/facts" },
            facts: [{ key: "where", label: "Where", text: "Near the harbour", url: "https://example.org/map" },
                    { key: "when", label: "When", text: "On a Sunday", url: null }] },
          { plugin_id: "example", title: "Example plug-in", credit: null,
            facts: [{ key: "caption", label: "Caption", text: "Seen by the example", url: null }] },
        ]}
      />,
    );
    const facts = screen.getByRole("region", { name: "Facts" });
    expect(within(facts).getByText("Where").nextSibling?.textContent).toBe("Near the harbour");
    expect((within(facts).getByRole("link", { name: "Near the harbour" }) as HTMLAnchorElement).href)
      .toBe("https://example.org/map");
    expect(within(facts).getByText("On a Sunday").closest("a")).toBeNull();
    expect((within(facts).getByRole("link", { name: "Facts Inc." }) as HTMLAnchorElement).rel).toBe("noopener noreferrer");
    const example = screen.getByRole("region", { name: "Example plug-in" });
    expect(within(example).getByText("Caption").nextSibling?.textContent).toBe("Seen by the example");
    expect(within(example).queryByText(/^From/)).toBeNull();
  });
});
