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

  it("says a plug-in uninstalled with what it made kept is so, and nothing of one installed", () => {
    render(
      <PluginFactsSection
        plugins={[
          { plugin_id: "facts", title: "Facts", credit: null, removed: true,
            facts: [{ key: "where", label: "Where", text: "Near the harbour", url: null }] },
          { plugin_id: "example", title: "Example plug-in", credit: null, removed: false,
            facts: [{ key: "caption", label: "Caption", text: "Seen by the example", url: null }] },
        ]}
      />,
    );
    expect(screen.getByRole("region", { name: "Facts" }).textContent).toContain("uninstalled, kept as it was");
    expect(screen.getByRole("region", { name: "Example plug-in" }).textContent).not.toContain("uninstalled");
  });

  it("says its empty line where it had nothing to say, and links the license beside its credit", () => {
    render(
      <PluginFactsSection
        plugins={[
          { plugin_id: "places", title: "Place names",
            credit: { text: "GeoNames", url: "https://www.geonames.org/",
                      license: { text: "CC BY 4.0", url: "https://creativecommons.org/licenses/by/4.0/" } },
            facts: [{ key: "near", label: "Near", text: "Not near a town", url: null, empty: true }] },
        ]}
      />,
    );
    const places = screen.getByRole("region", { name: "Place names" });
    const empty = within(places).getByText("Not near a town");
    expect(empty.closest("a")).toBeNull();
    expect(empty.className).toContain("text-gray-500");
    expect((within(places).getByRole("link", { name: "GeoNames" }) as HTMLAnchorElement).href).toBe("https://www.geonames.org/");
    expect((within(places).getByRole("link", { name: "CC BY 4.0" }) as HTMLAnchorElement).href)
      .toBe("https://creativecommons.org/licenses/by/4.0/");
    expect(within(places).getByText(/^From/).textContent).toBe("From GeoNames · CC BY 4.0");
  });
});
